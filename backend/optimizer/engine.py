"""The optimizer control plane: one request, every stage, one explanation.

`execute(envelope, ctx)` runs the staged pipeline and returns a record that is
both the API response and the ledger row. Every stage appends a TraceStep, so
"why did this request cost this much" is answered by reading the trace top to
bottom.

Stage order and why it is this order:

  policy -> budget -> exact cache -> classify -> semantic cache -> route
         -> context -> plan -> execute -> quality gate -> (escalate) -> ledger

* Caches are consulted on the *canonical, uncompressed* request, so a hit never
  depends on which compression policy happened to be active when the entry was
  written.
* Classification runs before the semantic cache because the semantic guard
  compares task types - similarity alone is not answer equivalence.
* Routing runs before context optimisation because compression is tier-aware:
  measured on this corpus, folding context costs the cheap tier ~1.1 quality
  points and the frontier tier nothing. The router therefore sees, and the
  context-window constraint uses, the *uncompressed* token count.

Strategy profiles decide which stages are live, so the evaluation workbench can
compare like with like:

  none         baseline: premium model, full context, no cache, no compression
  rule         deterministic keyword router only
  intelligent  LLM router only
  optimized    the whole control plane
  fixed:<id>   pin one model (used by the model-economics page)
"""
import datetime
import time
from dataclasses import dataclass, field

from backend import storage, telemetry
from backend.evaluation import evaluator
from backend.llm.client import ClientPool, LLMError
from backend.optimizer import (budget, cache, context as context_stage, embeddings, ledger as ledger_mod,
                               llm_router, planner, pricing, quality, router as router_stage, tasks)
from backend.optimizer.classifier import Classification
from backend.optimizer.envelope import RequestEnvelope
from backend.router import rule_router

ENGINE_VERSION = "optimizer-1"

# stage flags per strategy: (cache, context_opt, routing, reasoning, batch, gate)
PROFILES = {
    "none":        dict(cache=False, context=False, routing=False, reasoning=False, batch=False, gate=False),
    "rule":        dict(cache=False, context=False, routing=True, reasoning=False, batch=False, gate=False),
    "intelligent": dict(cache=False, context=False, routing=True, reasoning=False, batch=False, gate=False),
    "optimized":   dict(cache=True, context=True, routing=True, reasoning=True, batch=True, gate=True),
}


@dataclass
class TraceStep:
    stage: str
    status: str                     # ok | hit | miss | skipped | rejected | escalated | failed
    summary: str
    detail: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    tokens: int | None = None

    def public(self) -> dict:
        return {"stage": self.stage, "status": self.status, "summary": self.summary,
                "detail": self.detail, "cost_usd": round(self.cost_usd, 8),
                "latency_ms": self.latency_ms, "tokens": self.tokens}


@dataclass
class EngineContext:
    """Everything the engine needs that is not the request itself."""
    settings: object
    pool: ClientPool
    registry: object
    policies: object
    classifier: object
    health: object
    embedder: object = None
    learned_quality: dict = field(default_factory=dict)
    learned_output: dict = field(default_factory=dict)
    baseline_calibration: dict = field(default_factory=dict)  # {factor, n, spread}


class Attempt:
    __slots__ = ("model", "response", "cost_standard", "cost_actual", "error", "injected", "reason",
                 "cost_list_price")

    def __init__(self, model, response=None, cost_standard=0.0, cost_actual=0.0,
                 error=None, injected=False, reason=""):
        self.model, self.response = model, response
        self.cost_standard, self.cost_actual = cost_standard, cost_actual
        self.cost_list_price = cost_standard
        self.error, self.injected, self.reason = error, injected, reason

    def public(self):
        return {"model": self.model.id, "provider": self.model.provider,
                "ok": self.response is not None, "cost_usd": round(self.cost_actual, 8),
                "latency_ms": getattr(self.response, "latency_ms", None),
                "error": self.error, "injected": self.injected, "reason": self.reason}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


async def _call(ctx: EngineContext, model, messages, params, max_tokens, mode) -> Attempt:
    """One provider call, priced both at standard and at the execution mode's rate."""
    try:
        resp = await ctx.pool.get(model.provider).chat(
            model=model.id, messages=messages,
            temperature=ctx.settings.answer_params.get("temperature", 0.2),
            max_tokens=max_tokens, extra_params=params, retries=1)
    except LLMError as e:
        return Attempt(model, error=str(e)[:200], injected=getattr(e, "injected", False))
    if not resp.content.strip():
        return Attempt(model, error=f"{model.id} returned an empty answer")
    std = pricing.price(model, resp.input_tokens, resp.output_tokens,
                        cached_input_tokens=resp.cached_input_tokens,
                        reasoning_tokens=resp.reasoning_tokens, registry=ctx.registry)
    lst = pricing.price(model, resp.input_tokens, resp.output_tokens,
                        reasoning_tokens=resp.reasoning_tokens, registry=ctx.registry)
    act = pricing.price(model, resp.input_tokens, resp.output_tokens,
                        cached_input_tokens=resp.cached_input_tokens,
                        reasoning_tokens=resp.reasoning_tokens, execution_mode=mode,
                        registry=ctx.registry)
    a = Attempt(model, resp, std.total_usd, act.total_usd)
    a.cost_list_price = lst.total_usd
    return a


async def execute(env: RequestEnvelope, ctx: EngineContext) -> dict:
    t0 = time.perf_counter()
    trace: list = []
    profile = PROFILES.get(env.strategy, PROFILES["optimized"])
    if env.strategy.startswith("fixed:"):
        profile = dict(PROFILES["optimized"], routing=True)
    add = lambda *a, **k: trace.append(TraceStep(*a, **k))

    # ------------------------------------------------------------- 1. policy
    policy = ctx.policies.resolve(env.tenant_id, env.application_id, env.policy_overrides())
    flag = lambda name: policy.flag(name)
    add("policy", "ok",
        f"{policy.get('label', env.application_id)} - quality target {policy.get('quality_target')}, "
        f"SLA {policy.get('sla_class')}, sensitivity {policy.get('sensitivity_class')}",
        {"policy_version": policy.version, "values": policy.values, "provenance": policy.provenance,
         "flags": policy.flags, "ignored_keys": policy.ignored_keys,
         "known_tenant": ctx.policies.known_tenant(env.tenant_id)})

    test_query = env.test_query or telemetry.find_test_query(env.query)
    task_hint = env.task_type_hint or (test_query or {}).get("task_type")

    baseline_sections = telemetry.build_sections(env.query) if env.context is None else None
    baseline_context = env.context if env.context is not None else telemetry.assemble(
        baseline_sections, telemetry.baseline_section_names(env.query))
    baseline_input_tokens = context_stage.count(
        telemetry.SYSTEM_PROMPT + baseline_context + env.query) + 12
    premium = ctx.registry.tier_default("premium")
    max_out = ctx.settings.answer_params.get("max_tokens", 900)
    # Two numbers, because they answer different questions.
    #
    # `floor_cost` is the cheapest this request could possibly be served for. If
    # even that exceeds the remaining budget, no routing decision can rescue it
    # and the request is rejected before a model is called.
    #
    # `worst_case` is the premium model on the full context: what is reserved
    # while the request is in flight, so concurrent traffic cannot collectively
    # overshoot a hard limit. It is released and reconciled against actual spend
    # when the request finishes.
    cheapest = min(ctx.registry.candidates(), key=lambda m: m.blended_cost, default=premium)
    floor_cost = pricing.price(cheapest, baseline_input_tokens, 64, registry=ctx.registry).total_usd
    worst_case = pricing.price(premium, baseline_input_tokens, max_out,
                               registry=ctx.registry).total_usd

    # ------------------------------------------------------------- 2. budget
    soft = float((policy.get("budget") or {}).get("soft_threshold", 0.8) or 0.8)
    bdec = await budget.check_and_reserve(ctx.policies, env.tenant_id, env.application_id,
                                          floor_cost, soft, reserve_usd=worst_case)
    add("budget", "ok" if bdec.allowed else "rejected",
        bdec.reason, {**bdec.public(), "cheapest_possible_usd": round(floor_cost, 8),
                      "reserved_while_in_flight_usd": round(worst_case, 6),
                      "remaining_usd": bdec.remaining_usd})
    if not bdec.allowed:
        return await _finish_rejected(env, ctx, policy, trace, t0,
                                      f"budget: {bdec.reason}", baseline_input_tokens)

    versions = cache.versions_of(policy)
    if env.compression is not None:
        # Compression is an experiment dimension: a run with the compressor on
        # must not be served answers produced with it off, or the A/B compares
        # a cached uncompressed answer with a fresh compressed one.
        versions["variant"] = f"compression={env.compression}"
    cache_cfg = policy.get("cache") or {}
    embedding_cost = router_cost = 0.0
    router_latency = 0

    # -------------------------------------------------------- 3. exact cache
    hit = None
    if profile["cache"] and flag("ENABLE_EXACT_CACHE") and (cache_cfg.get("exact") or {}).get("enabled", True):
        look = await cache.exact_lookup(env.tenant_id, env.application_id, env.query, versions,
                                        int((cache_cfg["exact"]).get("ttl_seconds", 3600)))
        add("exact_cache", "hit" if look.hit else "miss",
            f"identical request served from cache (age {look.hit.age_seconds:.0f}s)" if look.hit
            else look.note, look.public())
        hit = look.hit
    else:
        add("exact_cache", "skipped", "exact cache disabled for this strategy or by policy", {})

    classification = None
    if hit is None:
        # ------------------------------------------------------ 4. classify
        local = ctx.classifier.classify(env.query, hint=task_hint)
        classification = local
        threshold = float((policy.get("classifier") or {}).get("llm_router_confidence_threshold", 0.55))
        if env.strategy == "intelligent" or (profile["routing"] and env.strategy == "optimized"
                                             and local.confidence < threshold
                                             and flag("ENABLE_LLM_ROUTER_FALLBACK")
                                             and ctx.pool.has(ctx.settings.router.provider)):
            classification, router_cost, router_latency = await llm_router.classify(
                env.query, baseline_input_tokens, ctx.settings, ctx.pool, local, ctx.registry)
        add("classify", "ok",
            f"{tasks.get(classification.task_type).label} / {classification.difficulty} "
            f"via {classification.rung} (confidence {classification.confidence:.2f})",
            {**classification.public(),
             "local": local.public() if classification is not local else None,
             "threshold": threshold},
            cost_usd=router_cost, latency_ms=router_latency)

        # --------------------------------------------------- 5. semantic cache
        sem_cfg = cache_cfg.get("semantic") or {}
        if profile["cache"] and flag("ENABLE_SEMANTIC_CACHE") and sem_cfg.get("enabled", True):
            emb = ctx.embedder or embeddings.choose(ctx.pool, sem_cfg.get("embedder", "auto"))
            thr, basis = cache.threshold_for(emb, sem_cfg)
            look = await cache.semantic_lookup(
                env.tenant_id, env.application_id, env.query, classification.task_type, versions,
                emb, thr, int(sem_cfg.get("ttl_seconds", 1800)), threshold_basis=basis)
            embedding_cost += look.embedding_cost_usd
            add("semantic_cache", "hit" if look.hit else "miss",
                (f"equivalent request served (similarity {look.hit.similarity:.3f} >= {thr})" if look.hit
                 else f"{look.note}; {look.checked} candidate(s) checked"),
                {**look.public(), "semantic_embedder": emb.semantic, "threshold": thr,
                 "threshold_basis": basis},
                cost_usd=look.embedding_cost_usd, latency_ms=look.embedding_latency_ms)
            hit = look.hit
        else:
            add("semantic_cache", "skipped", "semantic cache disabled for this strategy or by policy", {})

    if hit is not None:
        return await _finish_cache_hit(env, ctx, policy, trace, t0, hit, classification,
                                       baseline_input_tokens, router_cost, embedding_cost, test_query)

    # ---------------------------------------------------------- 6. routing
    strategy = env.strategy
    if strategy == "rule":
        # keep the deterministic keyword router as the published Strategy B
        rr = rule_router.route(env.query)
        classification = Classification(task_type=classification.task_type, difficulty=rr["complexity"],
                                        confidence=1.0, task_confidence=1.0, difficulty_confidence=1.0,
                                        rung="rules", required_capabilities=classification.required_capabilities,
                                        note=rr["reason"])
    task = tasks.get(classification.task_type)
    provisional_budget = min(ctx.settings.answer_params.get("max_tokens", 900), task.output_budget_tokens)
    decision = router_stage.route(
        classification, policy, ctx.registry, ctx.health, strategy=strategy,
        input_tokens=baseline_input_tokens, output_budget=provisional_budget,
        budget_pressure=bdec.pressure, learned=ctx.learned_quality, learned_output=ctx.learned_output,
        execution_mode="interactive", routing_enabled=profile["routing"] and flag("ENABLE_MODEL_ROUTING"),
        remaining_budget_usd=bdec.remaining_usd)
    add("route", "rejected" if decision.selected is None else "ok",
        decision.reason, decision.public())
    if decision.selected is None:
        await budget.reconcile(ctx.policies, env.tenant_id, env.application_id, worst_case, 0.0)
        return await _finish_rejected(env, ctx, policy, trace, t0, decision.rejected_request,
                                      baseline_input_tokens, decision=decision)
    model = decision.selected

    # ------------------------------------------------------- 7. context
    cplan = context_stage.optimise(
        env.query, classification.task_type,
        enabled=profile["context"] and flag("ENABLE_CONTEXT_OPTIMIZATION"),
        policy_compression=policy.get("compression"), model_tier=model.tier,
        legacy_compression=env.compression, supplied_context=env.context)
    add("context", "ok",
        (f"{cplan.tokens_before} -> {cplan.tokens_after} context tokens "
         f"({cplan.saved_pct}% less)" if cplan.tokens_saved else
         f"{cplan.tokens_after} context tokens; nothing safe to remove"),
        cplan.public(), tokens=cplan.tokens_after)

    # ---------------------------------------------------------- 8. plan
    eplan = planner.plan(model, classification.task_type, classification.difficulty, policy, ctx.registry,
                         batch_enabled=profile["batch"] and flag("ENABLE_BATCH_OPTIMIZATION"),
                         reasoning_enabled=profile["reasoning"] and flag("ENABLE_REASONING_OPTIMIZATION"),
                         answer_max_tokens=ctx.settings.answer_params.get("max_tokens", 900))
    add("plan", "ok",
        f"{eplan.execution_mode} execution, reasoning {eplan.reasoning_level or 'n/a'}, "
        f"output budget {eplan.output_budget_tokens} tokens", eplan.public())

    # -------------------------------------------------- 9. execute + fallback
    messages = context_stage.messages_for(cplan.context, env.query)
    attempts: list = []
    chain = decision.fallback_chain if (profile["routing"] and flag("ENABLE_FALLBACK")
                                        and (policy.get("fallback") or {}).get("enabled", True)) else [model]
    max_attempts = int((policy.get("fallback") or {}).get("max_attempts", 3))
    served: Attempt | None = None
    for i, cand in enumerate(chain[:max_attempts]):
        if not ctx.pool.has(cand.provider):
            attempts.append(Attempt(cand, error=f"no API key configured for {cand.provider}",
                                    reason="skipped"))
            continue
        if i > 0 and ctx.health.breaker_open(cand.provider):
            attempts.append(Attempt(cand, error=f"circuit breaker open for {cand.provider}",
                                    reason="breaker"))
            continue
        params = eplan.request_params if cand.id == model.id else dict(cand.params)
        a = await _call(ctx, cand, messages, params, eplan.output_budget_tokens, eplan.execution_mode)
        attempts.append(a)
        if a.response is not None:
            served = a
            break
    if served is None:
        await budget.reconcile(ctx.policies, env.tenant_id, env.application_id, worst_case, 0.0)
        add("execute", "failed", "every eligible provider failed",
            {"attempts": [a.public() for a in attempts]})
        raise LLMError("all providers failed - " + "; ".join(
            f"{a.model.id}: {a.error}" for a in attempts if a.error))
    # A provider we never called because it has no key configured was skipped,
    # not failed: that is not a fallback.
    fallback_used = any(a.error and a.reason != "skipped" for a in attempts)
    fallback_cost = sum(a.cost_actual for a in attempts if a is not served)
    add("execute", "ok",
        f"{served.model.label} answered in {served.response.latency_ms}ms"
        + (f" after {len(attempts) - 1} failed attempt(s)" if fallback_used else ""),
        {"model": served.model.id, "provider": served.model.provider,
         "execution_mode": eplan.execution_mode, "reasoning_level": eplan.reasoning_level,
         "input_tokens": served.response.input_tokens, "output_tokens": served.response.output_tokens,
         "cached_input_tokens": served.response.cached_input_tokens,
         "reasoning_tokens": served.response.reasoning_tokens,
         "finish_reason": served.response.finish_reason,
         "fallback_used": fallback_used, "attempts": [a.public() for a in attempts]},
        cost_usd=served.cost_actual, latency_ms=served.response.latency_ms,
        tokens=served.response.input_tokens + served.response.output_tokens)

    # ------------------------------------------------- 10. quality gate
    gate, judge = await _gate(env, ctx, policy, profile, classification, cplan, served, test_query)
    add("quality", "ok" if gate.passed else "failed", gate.reason, gate.public())

    # -------------------------------------------------- 11. escalation
    escalation_cost = 0.0
    escalated_from = None
    esc_cfg = policy.get("escalation") or {}
    if (not gate.passed and profile["gate"] and flag("ENABLE_ESCALATION") and esc_cfg.get("enabled", True)
            and gate.escalate):
        stronger = [c.model for c in decision.candidates
                    if c.eligible and c.model.rank > served.model.rank and ctx.pool.has(c.model.provider)]
        stronger.sort(key=lambda m: (m.rank, m.blended_cost))
        if stronger:
            target = stronger[0]
            # An escalation is a retry of a request whose first answer was
            # rejected: the caller is already waiting, so it runs interactively
            # whatever the SLA said. That is a deliberate trade, not a flag.
            esc_plan = planner.plan(target, classification.task_type, classification.difficulty, policy,
                                    ctx.registry, batch_enabled=False,
                                    batch_disabled_reason="escalation retries run interactively "
                                                          "because the first attempt already failed",
                                    reasoning_enabled=profile["reasoning"] and flag("ENABLE_REASONING_OPTIMIZATION"),
                                    answer_max_tokens=ctx.settings.answer_params.get("max_tokens", 900))
            # Escalation re-sends the *uncompressed* context: the cheap tier's
            # failure may itself have been caused by a compressed prompt.
            esc_messages = context_stage.messages_for(cplan.baseline_context, env.query)
            a2 = await _call(ctx, target, esc_messages, esc_plan.request_params,
                             esc_plan.output_budget_tokens, "interactive")
            if a2.response is not None:
                escalation_cost = served.cost_actual + fallback_cost
                escalated_from = served.model.id
                gate2, judge2 = await _gate(env, ctx, policy, profile, classification, cplan, a2, test_query)
                add("escalate", "escalated",
                    f"{served.model.label} failed the gate; escalated to {target.label} "
                    f"({'passed' if gate2.passed else 'still below threshold'})",
                    {"from": served.model.id, "to": target.id, "trigger": gate.reason,
                     "result": gate2.public()},
                    cost_usd=a2.cost_actual, latency_ms=a2.response.latency_ms)
                served, gate, judge = a2, gate2, judge2
                cplan.context = cplan.baseline_context
                eplan = esc_plan
            else:
                add("escalate", "failed", f"escalation to {target.label} failed: {a2.error}",
                    {"from": served.model.id, "to": target.id, "error": a2.error})
        else:
            add("escalate", "skipped", "no stronger eligible model available to escalate to",
                {"trigger": gate.reason})

    # ------------------------------------------------------ 12. ledger
    overhead = router_cost + embedding_cost
    actual = served.cost_actual + fallback_cost + escalation_cost + overhead
    base_usd, base_src = ledger_mod.baseline_cost(
        ctx.registry, baseline_input_tokens, served.response.output_tokens, ctx.baseline_calibration)
    reasoning_avoided = 0
    if eplan.reasoning_reduced and served.model.reasoning_tokens_measured:
        m = served.model.reasoning_tokens_measured
        reasoning_avoided = max(0, int(m.get("default", 0)) - int(m.get(eplan.reasoning_level or "low", 0)))
    led = ledger_mod.attribute(
        ctx.registry, baseline_usd=base_usd, baseline_source=base_src, actual_usd=actual,
        cache_kind=None, baseline_input_tokens=baseline_input_tokens,
        sent_input_tokens=served.response.input_tokens, output_tokens=served.response.output_tokens,
        served_model_id=served.model.id, served_cost_standard=served.cost_standard,
        served_cost_actual=served.cost_actual, reasoning_tokens_avoided=reasoning_avoided,
        overhead_usd=overhead, escalation_usd=escalation_cost, fallback_usd=fallback_cost,
        cached_input_tokens=served.response.cached_input_tokens,
        served_cost_list_price=served.cost_list_price)
    add("ledger", "ok",
        f"${actual:.5f} actual vs ${base_usd:.5f} baseline ({led.savings_pct}% saved, {base_src})",
        led.public(), cost_usd=actual)

    await budget.reconcile(ctx.policies, env.tenant_id, env.application_id, worst_case, actual)

    record = _record(env, ctx, policy, classification, decision, cplan, eplan, served, gate, judge,
                     led, trace, t0, attempts, fallback_used, escalated_from, test_query,
                     router_cost, embedding_cost, escalation_cost, fallback_cost, baseline_input_tokens)

    # ------------------------------------------------- 13. cache write-back
    if profile["cache"] and gate.passed is not False and env.mode in ("live", "live_internal"):
        entry = {"request_id": record["request_id"], "answer": record["answer"],
                 "model": served.model.id, "cost_usd": actual, "latency_ms": record["latency_ms"],
                 "quality_ok": gate.passed, "quality_score": record["quality_score"],
                 "task_type": classification.task_type}
        if flag("ENABLE_EXACT_CACHE") and (cache_cfg.get("exact") or {}).get("enabled", True):
            await cache.exact_store(env.tenant_id, env.application_id, env.query, versions, entry)
        sem_cfg = cache_cfg.get("semantic") or {}
        if flag("ENABLE_SEMANTIC_CACHE") and sem_cfg.get("enabled", True):
            emb = ctx.embedder or embeddings.choose(ctx.pool, sem_cfg.get("embedder", "auto"))
            await cache.semantic_store(env.tenant_id, env.application_id, env.query,
                                       classification.task_type, versions, emb, entry)
    if env.mode in ("live", "live_internal"):
        await storage.append(record)
    return record


async def _gate(env, ctx, policy, profile, classification, cplan, attempt, test_query):
    """Deterministic validators first; the judge only where it adds something."""
    answer = attempt.response.content
    validators = [quality.validate_non_empty(answer)]
    task = tasks.get(classification.task_type)
    spec = (test_query or {}).get("validator")
    det = quality.run_validator(spec, task.validator, answer, cplan.context)
    if det is not None:
        validators.append(det)
    judge = None
    # The judge costs money and needs ground truth; run it when we have an
    # expected answer, or when the caller asked for an evaluation.
    deterministic_pass = det is not None and det.passed and det.kind in ("label", "json_schema", "sql")
    if test_query and (env.evaluate or profile["gate"]) and not deterministic_pass \
            and ctx.pool.has(ctx.settings.evaluator.provider):
        judge = await evaluator.evaluate(env.query, test_query["expected_answer"], test_query["criteria"],
                                         answer, ctx.settings, ctx.pool)
    threshold = float(policy.get("quality_gate_threshold", 3.5))
    esc_enabled = bool((policy.get("escalation") or {}).get("enabled", True)) and profile["gate"]
    return quality.decide(validators, judge, threshold, esc_enabled), judge


def _record(env, ctx, policy, classification, decision, cplan, eplan, served, gate, judge, led,
            trace, t0, attempts, fallback_used, escalated_from, test_query, router_cost,
            embedding_cost, escalation_cost, fallback_cost, baseline_input_tokens) -> dict:
    resp = served.response
    total_latency = int((time.perf_counter() - t0) * 1000)
    return {
        # identity
        "request_id": env.request_id, "trace_id": env.trace_id, "timestamp": _now_iso(),
        "tenant_id": env.tenant_id, "application_id": env.application_id,
        "environment": policy.get("environment"), "feature_id": policy.get("feature_id"),
        "session_id": env.session_id, "user_id": env.user_id,
        "query_id": (test_query or {}).get("id"), "query": env.query, "mode": env.mode,
        # task
        "strategy": env.strategy, "task_type": classification.task_type,
        "task_label": tasks.get(classification.task_type).label,
        "difficulty": classification.difficulty, "expected_complexity": (test_query or {}).get("complexity"),
        "classification": classification.public(),
        "sensitivity_class": policy.get("sensitivity_class"), "sla_class": policy.get("sla_class"),
        # routing
        "routing": decision.public(),
        "tier": served.model.tier, "model": served.model.id, "model_used": resp.model_used,
        "provider": served.model.provider, "tier_provider": served.model.provider,
        "routed_tier": decision.selected.tier if decision.selected else None,
        "routed_model": decision.selected.id if decision.selected else None,
        "fallback_used": fallback_used, "fallback_count": sum(1 for a in attempts if a.error),
        "escalated_from": escalated_from, "escalation_count": 1 if escalated_from else 0,
        "attempts": [a.public() for a in attempts],
        # context and execution
        "context": cplan.public(), "plan": eplan.public(),
        "context_tokens_before": cplan.tokens_before, "context_tokens_after": cplan.tokens_after,
        "context_saved_pct": cplan.saved_pct, "compression": cplan.compression_mode,
        "compression_error": cplan.compression_error,
        "execution_mode": eplan.execution_mode, "reasoning_level": eplan.reasoning_level,
        "output_budget_tokens": eplan.output_budget_tokens,
        "unused_output_budget": max(0, eplan.output_budget_tokens - resp.output_tokens),
        # tokens
        "input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens,
        "cached_input_tokens": resp.cached_input_tokens, "reasoning_tokens": resp.reasoning_tokens,
        "baseline_input_tokens": baseline_input_tokens,
        # cost
        "answer_cost_usd": round(served.cost_actual, 8), "router_cost_usd": round(router_cost, 8),
        "embedding_cost_usd": round(embedding_cost, 8), "escalation_cost_usd": round(escalation_cost, 8),
        "fallback_cost_usd": round(fallback_cost, 8),
        "cost_usd": round(led.actual_cost_usd, 8), "baseline_cost_usd": round(led.baseline_cost_usd, 8),
        "baseline_source": led.baseline_source, "savings_usd": round(led.savings_usd, 8),
        "savings_pct": led.savings_pct, "ledger": led.public(),
        "cache_hit": None, "cache_kind": None,
        # quality
        "quality_score": gate.score, "quality_gate_passed": gate.passed,
        "quality_reason": gate.reason, "quality_rationale": (judge or {}).get("quality_rationale"),
        "judge_model": (judge or {}).get("judge_model"), "quality": gate.public(),
        # latency
        "latency_ms": total_latency, "answer_latency_ms": resp.latency_ms,
        "ttft_ms": resp.ttft_ms,
        # versions and trace
        "policy_version": policy.version, "router_version": classification.public()["router_version"],
        "price_registry_version": ctx.registry.version, "engine_version": ENGINE_VERSION,
        "answer": resp.content, "trace": [s.public() for s in trace],
    }


async def _finish_cache_hit(env, ctx, policy, trace, t0, hit, classification, baseline_input_tokens,
                            router_cost, embedding_cost, test_query) -> dict:
    entry = hit.entry
    overhead = router_cost + embedding_cost
    base_usd, base_src = ledger_mod.baseline_cost(ctx.registry, baseline_input_tokens,
                                                  int(entry.get("output_tokens") or 250),
                                                  ctx.baseline_calibration)
    led = ledger_mod.attribute(
        ctx.registry, baseline_usd=base_usd, baseline_source=base_src, actual_usd=overhead,
        cache_kind=hit.kind, baseline_input_tokens=baseline_input_tokens,
        sent_input_tokens=0, output_tokens=0, served_model_id=None, served_cost_standard=0.0,
        served_cost_actual=0.0, reasoning_tokens_avoided=0, overhead_usd=overhead,
        escalation_usd=0.0, fallback_usd=0.0)
    latency = int((time.perf_counter() - t0) * 1000)
    trace.append(TraceStep("ledger", "ok",
                           f"${overhead:.5f} actual vs ${base_usd:.5f} baseline - the model call was avoided",
                           led.public(), cost_usd=overhead))
    task = classification.task_type if classification else entry.get("task_type", "diagnosis")
    record = {
        "request_id": env.request_id, "trace_id": env.trace_id, "timestamp": _now_iso(),
        "tenant_id": env.tenant_id, "application_id": env.application_id,
        "environment": policy.get("environment"), "feature_id": policy.get("feature_id"),
        "session_id": env.session_id, "user_id": env.user_id,
        "query_id": (test_query or {}).get("id"), "query": env.query, "mode": env.mode,
        "strategy": env.strategy, "task_type": task, "task_label": tasks.get(task).label,
        "difficulty": classification.difficulty if classification else None,
        "expected_complexity": (test_query or {}).get("complexity"),
        "classification": classification.public() if classification else None,
        "sensitivity_class": policy.get("sensitivity_class"), "sla_class": policy.get("sla_class"),
        "routing": {"selected_model": None, "reason": f"served from the {hit.kind} cache"},
        "tier": None, "model": f"{hit.kind}-cache", "model_used": entry.get("model"),
        "provider": "cache", "tier_provider": "cache", "routed_tier": None, "routed_model": None,
        "fallback_used": False, "fallback_count": 0, "escalated_from": None, "escalation_count": 0,
        "attempts": [],
        "context": {}, "plan": {}, "context_tokens_before": baseline_input_tokens,
        "context_tokens_after": 0, "context_saved_pct": 100.0,
        "compression": env.compression or "off",
        "compression_error": None, "execution_mode": "cache", "reasoning_level": None,
        "output_budget_tokens": 0, "unused_output_budget": 0,
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0,
        "baseline_input_tokens": baseline_input_tokens,
        "answer_cost_usd": 0.0, "router_cost_usd": round(router_cost, 8),
        "embedding_cost_usd": round(embedding_cost, 8), "escalation_cost_usd": 0.0,
        "fallback_cost_usd": 0.0, "cost_usd": round(overhead, 8),
        "baseline_cost_usd": round(base_usd, 8), "baseline_source": base_src,
        "savings_usd": round(led.savings_usd, 8), "savings_pct": led.savings_pct, "ledger": led.public(),
        "cache_hit": True, "cache_kind": hit.kind,
        "quality_score": entry.get("quality_score"), "quality_gate_passed": True,
        "quality_reason": f"served from the {hit.kind} cache; the stored answer passed its gate",
        "quality_rationale": None, "judge_model": None,
        "quality": {"passed": True, "validators": [], "judge": None,
                    "reason": f"cached answer from {entry.get('request_id')}"},
        "latency_ms": latency, "answer_latency_ms": 0, "ttft_ms": None,
        "policy_version": policy.version,
        "router_version": (classification.public()["router_version"] if classification else None),
        "price_registry_version": ctx.registry.version, "engine_version": ENGINE_VERSION,
        "answer": entry.get("answer", ""), "trace": [s.public() for s in trace],
    }
    await budget.reconcile(ctx.policies, env.tenant_id, env.application_id, 0.0, overhead)
    if env.mode in ("live", "live_internal"):
        await storage.append(record)
    return record


async def _finish_rejected(env, ctx, policy, trace, t0, reason, baseline_input_tokens,
                           decision=None) -> dict:
    record = {
        "request_id": env.request_id, "trace_id": env.trace_id, "timestamp": _now_iso(),
        "tenant_id": env.tenant_id, "application_id": env.application_id,
        "environment": policy.get("environment"), "feature_id": policy.get("feature_id"),
        "query_id": None, "query": env.query, "mode": env.mode, "strategy": env.strategy,
        "task_label": None,
        "task_type": None, "difficulty": None, "classification": None,
        "sensitivity_class": policy.get("sensitivity_class"), "sla_class": policy.get("sla_class"),
        "routing": decision.public() if decision else {"reason": reason},
        "tier": None, "model": None, "provider": None, "rejected": True, "rejection_reason": reason,
        "fallback_used": False, "fallback_count": 0, "escalation_count": 0, "attempts": [],
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "reasoning_tokens": 0,
        "baseline_input_tokens": baseline_input_tokens,
        "cost_usd": 0.0, "baseline_cost_usd": 0.0, "baseline_source": "n/a", "savings_usd": 0.0,
        "savings_pct": 0.0, "ledger": {"attribution": []},
        "cache_hit": None, "cache_kind": None, "quality_score": None, "quality_gate_passed": None,
        "quality_reason": reason, "answer": "",
        "latency_ms": int((time.perf_counter() - t0) * 1000), "answer_latency_ms": 0,
        "policy_version": policy.version, "price_registry_version": ctx.registry.version,
        "engine_version": ENGINE_VERSION, "trace": [s.public() for s in trace],
    }
    if env.mode in ("live", "live_internal"):
        await storage.append(record)
    return record
