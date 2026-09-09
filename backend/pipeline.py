"""Integration layer between the API/experiment and the optimizer control plane.

Holds the process-wide runtime (registry, policies, classifier, health), builds
an EngineContext per call, allocates request ids, and implements shadow mode.

`run_query` keeps its original signature so the experiment runner and the older
tests keep working; it now returns the richer optimizer record.
"""
import copy
import statistics
import time

from backend import storage, telemetry
from backend.config.loader import Settings
from backend.llm.client import ClientPool, LLMError
from backend.optimizer import budget, classifier as classifier_mod, embeddings, engine, pricing
from backend.optimizer.envelope import RequestEnvelope
from backend.optimizer.health import get_health
from backend.optimizer.policy import get_policies
from backend.optimizer.registry import get_registry

_LEARNED_TTL_S = 5.0
_learned_cache: dict = {"at": 0.0, "n": 0, "quality": {}, "output": {},
                        "baseline_calibration": {"factor": 1.0, "n": 0}}


def learned_stats(records: list) -> dict:
    """Per-model measured quality and output length, from graded live records.

    These override the registry's priors in the router's estimates once there
    are enough samples, and the trace says which was used.
    """
    q, o = {}, {}
    ratios = []
    premium = get_registry().tier_default("premium")
    for r in records:
        if r.get("mode") == "shadow" or r.get("rejected"):
            continue
        model, diff = r.get("model"), r.get("difficulty") or r.get("expected_complexity")
        score = r.get("quality_score")
        if model and diff and score is not None and not r.get("cache_hit"):
            q.setdefault(model, {}).setdefault(diff, []).append(float(score))
        task = r.get("task_type")
        if model and task and r.get("output_tokens"):
            o.setdefault(model, {}).setdefault(task, []).append(int(r["output_tokens"]))
        # Calibration: on a real frontier-model run, how does its true cost
        # compare with the estimate the ledger would have made for it? The mean
        # ratio corrects the estimate's bias without importing its variance.
        if (r.get("strategy") == "none" and r.get("model") == premium.id
                and (r.get("compression") or "off") == "off" and r.get("cost_usd")
                and r.get("baseline_input_tokens") and r.get("output_tokens")):
            est = pricing.price(premium, int(r["baseline_input_tokens"]), int(r["output_tokens"]),
                                registry=get_registry()).total_usd
            if est > 0:
                ratios.append(float(r["cost_usd"]) / est)
    return {
        "quality": {m: {d: {"mean": round(statistics.fmean(v), 3), "n": len(v)} for d, v in per.items()}
                    for m, per in q.items()},
        "output": {m: {t: {"mean": round(statistics.fmean(v)), "n": len(v)} for t, v in per.items()}
                   for m, per in o.items()},
        "baseline_calibration": _calibration(ratios),
    }


def _calibration(ratios: list) -> dict:
    """Mean ratio of a real frontier run's cost to what the ledger would have
    estimated for it, with the spread so nobody has to take it on trust."""
    if not ratios:
        return {"factor": 1.0, "n": 0, "spread": None,
                "note": "no frontier-model runs recorded yet; baselines are uncalibrated estimates"}
    factor = statistics.fmean(ratios)
    spread = round(statistics.pstdev(ratios), 4) if len(ratios) > 1 else 0.0
    return {"factor": round(factor, 5), "n": len(ratios), "spread": spread,
            "note": f"calibrated on {len(ratios)} real frontier-model run(s); the estimate is "
                    f"multiplied by {factor:.3f} (standard deviation {spread})"}


async def learned(records: list | None = None, force: bool = False) -> dict:
    now = time.time()
    if not force and records is None and now - _learned_cache["at"] < _LEARNED_TTL_S:
        return _learned_cache
    recs = records if records is not None else await storage.all_records()
    stats = learned_stats(recs)
    _learned_cache.update(at=now, n=len(recs), **stats)
    return _learned_cache


def invalidate_learned() -> None:
    _learned_cache["at"] = 0.0


async def context_for(settings: Settings, pool: ClientPool, records: list | None = None) -> engine.EngineContext:
    stats = await learned(records)
    return engine.EngineContext(
        settings=settings, pool=pool, registry=get_registry(), policies=get_policies(),
        classifier=classifier_mod.get_classifier(), health=get_health(),
        embedder=None, learned_quality=stats["quality"], learned_output=stats["output"],
        baseline_calibration=stats["baseline_calibration"])


async def run_envelope(env: RequestEnvelope, settings: Settings, pool: ClientPool,
                       ctx: engine.EngineContext | None = None) -> dict:
    ctx = ctx or await context_for(settings, pool)
    env.request_id = env.request_id or await storage.next_request_id()
    if env.mode == "shadow":
        return await run_shadow(env, settings, pool, ctx)
    rec = await engine.execute(env, ctx)
    invalidate_learned()
    return rec


async def run_shadow(env: RequestEnvelope, settings: Settings, pool: ClientPool,
                     ctx: engine.EngineContext | None = None) -> dict:
    """Serve the request the way the application does today, and record what the
    optimizer *would* have done.

    Production traffic is untouched: the answer the caller receives comes from
    the incumbent (baseline) model. The optimizer's route is priced from the
    registry with the incumbent's own token counts, so the projected saving is
    an estimate and is labelled as one everywhere it appears.
    """
    ctx = ctx or await context_for(settings, pool)
    env.request_id = env.request_id or await storage.next_request_id()
    incumbent_env = copy.copy(env)
    incumbent_env.strategy = "none"
    incumbent_env.mode = "shadow"        # execute for real; run_shadow stores the merged record
    incumbent = await engine.execute(incumbent_env, ctx)
    if incumbent.get("rejected"):
        return incumbent

    plan_env = copy.copy(env)
    plan_env.strategy = "optimized"
    plan_env.mode = "shadow"
    plan_env.request_id = env.request_id
    projection = await plan_only(plan_env, ctx, incumbent)

    rec = dict(incumbent)
    rec.update({
        "mode": "shadow", "strategy": "shadow",
        "shadow": projection,
        "trace": incumbent["trace"] + [{"stage": "shadow", "status": "ok",
                                        "summary": projection["summary"], "detail": projection,
                                        "cost_usd": 0.0, "latency_ms": 0, "tokens": None}],
    })
    await storage.append(rec)
    invalidate_learned()
    return rec


async def plan_only(env: RequestEnvelope, ctx: engine.EngineContext, incumbent: dict) -> dict:
    """Run the decision stages without calling an answer model, and price the
    route the optimizer would have taken using the incumbent's token counts."""
    from backend.optimizer import cache, context as context_stage, planner, router as router_stage, tasks

    policy = ctx.policies.resolve(env.tenant_id, env.application_id, env.policy_overrides())
    test_query = env.test_query or telemetry.find_test_query(env.query)
    local = ctx.classifier.classify(env.query, hint=env.task_type_hint or (test_query or {}).get("task_type"))
    versions = cache.versions_of(policy)
    cache_cfg = policy.get("cache") or {}
    sem_cfg = cache_cfg.get("semantic") or {}
    exact = await cache.exact_lookup(env.tenant_id, env.application_id, env.query, versions,
                                     int((cache_cfg.get("exact") or {}).get("ttl_seconds", 3600)))
    emb = ctx.embedder or embeddings.choose(ctx.pool, sem_cfg.get("embedder", "auto"))
    thr, basis = cache.threshold_for(emb, sem_cfg)
    sem = await cache.semantic_lookup(env.tenant_id, env.application_id, env.query, local.task_type,
                                      versions, emb, thr, int(sem_cfg.get("ttl_seconds", 1800)),
                                      threshold_basis=basis)
    would_cache = exact.hit or sem.hit
    baseline_input = int(incumbent.get("baseline_input_tokens") or incumbent.get("input_tokens") or 0)
    output_tokens = int(incumbent.get("output_tokens") or 0)
    task = tasks.get(local.task_type)
    dec = router_stage.route(local, policy, ctx.registry, ctx.health, strategy="optimized",
                             input_tokens=baseline_input,
                             output_budget=min(900, task.output_budget_tokens),
                             learned=ctx.learned_quality, learned_output=ctx.learned_output)
    actual = float(incumbent.get("cost_usd") or 0.0)
    if would_cache:
        projected, model_id, note = 0.0, would_cache.entry.get("model"), \
            f"served from the {would_cache.kind} cache"
    elif dec.selected is None:
        projected, model_id, note = actual, None, "policy would have rejected this request"
    else:
        cplan = context_stage.optimise(env.query, local.task_type, enabled=True,
                                       policy_compression=policy.get("compression"),
                                       model_tier=dec.selected.tier)
        eplan = planner.plan(dec.selected, local.task_type, local.difficulty, policy, ctx.registry)
        sent = max(0, baseline_input - cplan.tokens_saved)
        projected = pricing.price(dec.selected, sent, output_tokens,
                                  execution_mode=eplan.execution_mode, registry=ctx.registry).total_usd
        model_id = dec.selected.id
        note = (f"{dec.selected.label}, {cplan.tokens_saved} fewer context tokens, "
                f"{eplan.execution_mode} execution")
    saving = actual - projected
    q = None
    if dec.selected is not None:
        c = next((c for c in dec.candidates if c.model.id == dec.selected.id), None)
        q = c.expected_quality if c else None
    return {
        "basis": "estimated", "incumbent_model": incumbent.get("model"),
        "incumbent_cost_usd": round(actual, 8), "projected_model": model_id,
        "projected_cost_usd": round(projected, 8), "projected_saving_usd": round(saving, 8),
        "projected_saving_pct": round(100 * saving / actual, 1) if actual else 0.0,
        "projected_quality": q, "quality_basis": (c.quality_source if dec.selected and c else None),
        "would_cache": bool(would_cache), "cache_kind": would_cache.kind if would_cache else None,
        "classification": local.public(), "routing": dec.public(),
        "summary": (f"would have used {model_id or 'no model'} ({note}) for "
                    f"${projected:.5f} instead of ${actual:.5f} "
                    f"({round(100 * saving / actual, 1) if actual else 0}% less, estimated)"),
    }


# ------------------------------------------------------------------- legacy API

async def run_query(query: str, strategy: str, settings: Settings, pool: ClientPool,
                    evaluate: bool = False, test_query: dict | None = None,
                    compression: str = "off", tenant_id: str = "acme",
                    application_id: str = "ops-assistant", mode: str = "live",
                    ctx: engine.EngineContext | None = None, **kw) -> dict:
    env = RequestEnvelope(query=query, strategy=strategy, evaluate=evaluate, test_query=test_query,
                          compression=compression, tenant_id=tenant_id,
                          application_id=application_id, mode=mode, **kw)
    return await run_envelope(env, settings, pool, ctx)


def fallback_chain(settings: Settings, tier) -> list:
    """Tiers to try for one answer, best-first: the routed tier, then more
    capable tiers (a costlier answer beats a missing one), then cheaper ones."""
    others = [t for t in settings.tiers.values() if t.key != tier.key]
    higher = sorted((t for t in others if t.rank > tier.rank), key=lambda t: t.rank)
    lower = sorted((t for t in others if t.rank < tier.rank), key=lambda t: -t.rank)
    return [tier, *higher, *lower]


__all__ = ["run_query", "run_envelope", "run_shadow", "plan_only", "context_for", "learned",
           "learned_stats", "invalidate_learned", "fallback_chain", "ClientPool", "LLMError"]
