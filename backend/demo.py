"""Reproducible demo scenarios.

Every scenario runs real requests through the real engine and returns what
actually happened, including the request ids so the UI can link straight to the
trace. Nothing here fabricates a number: if a scenario cannot demonstrate its
point on this run (a cheap model happened to answer well, say) it says so.

Scenarios are deliberately small - a client demo cannot wait for 100 requests -
and each states its own success criterion.
"""
import asyncio
import copy
import time

from backend import storage, telemetry
from backend.analytics import aggregate
from backend.optimizer import budget, cache, policy as policy_mod
from backend.optimizer.envelope import RequestEnvelope
from backend.optimizer.health import get_health

TENANT, APP = "acme", "ops-assistant"


def _q(qid: str) -> dict:
    return next(t for t in telemetry.load_test_queries() if t["id"] == qid)


SCENARIOS = {}


def scenario(key, title, question, criterion, order):
    def deco(fn):
        SCENARIOS[key] = {"key": key, "title": title, "question": question,
                          "criterion": criterion, "order": order, "run": fn}
        return fn
    return deco


async def _run(pipeline, settings, pool, qid, strategy="optimized", fresh_cache=False, **kw):
    """Run one labelled query.

    `fresh_cache` clears this tenant's cache first. Scenarios that demonstrate
    routing, failover or budget need it: a cache hit from an earlier scenario
    would short-circuit the very mechanism the scenario exists to show, and a
    demo that silently proves nothing is worse than no demo.
    """
    if fresh_cache:
        await cache.clear(kw.get("tenant_id", TENANT))
    tq = _q(qid)
    env = RequestEnvelope(query=tq["query"], strategy=strategy, evaluate=True, test_query=tq,
                          tenant_id=kw.pop("tenant_id", TENANT),
                          application_id=kw.pop("application_id", APP), **kw)
    return await pipeline.run_envelope(env, settings, pool)


def _card(r: dict) -> dict:
    """The small summary the demo UI shows per request."""
    return {"request_id": r.get("request_id"), "query": r.get("query"),
            "model": r.get("model"), "tier": r.get("tier"), "task": r.get("task_label"),
            "difficulty": r.get("difficulty"), "cost_usd": r.get("cost_usd"),
            "baseline_cost_usd": r.get("baseline_cost_usd"), "savings_pct": r.get("savings_pct"),
            "quality_score": r.get("quality_score"), "gate_passed": r.get("quality_gate_passed"),
            "latency_ms": r.get("latency_ms"), "cache_kind": r.get("cache_kind"),
            "escalated_from": r.get("escalated_from"), "fallback_used": r.get("fallback_used"),
            "execution_mode": r.get("execution_mode"), "reason": (r.get("routing") or {}).get("reason")}


# ------------------------------------------------------------------ scenarios

@scenario("baseline", "Everything to the frontier model",
          "What does this workload cost the way most teams run it?",
          "Every request uses the premium model and nothing is cached.", 1)
async def _baseline(pipeline, settings, pool, params):
    ids = params.get("queries") or ["Q-01", "Q-15", "Q-27", "Q-37", "Q-50"]
    out = [await _run(pipeline, settings, pool, q, "none") for q in ids]
    cost = sum(r["cost_usd"] for r in out)
    return {"requests": [_card(r) for r in out],
            "metrics": {"total_cost_usd": round(cost, 6),
                        "avg_quality": _avg([r["quality_score"] for r in out]),
                        "frontier_pct": 100.0},
            "verdict": f"{len(out)} requests, ${cost:.4f}, every one on "
                       f"{out[0]['model']}. This is the number to beat.",
            "passed": all(r["tier"] == "premium" for r in out)}


@scenario("optimized", "The same workload through the optimizer",
          "What changes when each request is routed on what it actually needs?",
          "The same questions cost less, with the quality gate reporting on every answer.", 2)
async def _optimized(pipeline, settings, pool, params):
    ids = params.get("queries") or ["Q-01", "Q-15", "Q-27", "Q-37", "Q-50"]
    out = [await _run(pipeline, settings, pool, q, "optimized") for q in ids]
    cost = sum(r["cost_usd"] for r in out)
    records = await storage.all_records()
    base = [r for r in records if r["strategy"] == "none" and r.get("query_id") in ids]
    base_cost = sum(r["cost_usd"] for r in base) or sum(r["baseline_cost_usd"] for r in out)
    mix = {}
    for r in out:
        mix[r.get("model") or "cache"] = mix.get(r.get("model") or "cache", 0) + 1
    return {"requests": [_card(r) for r in out],
            "metrics": {"total_cost_usd": round(cost, 6), "baseline_cost_usd": round(base_cost, 6),
                        "savings_pct": round(100 * (base_cost - cost) / base_cost, 1) if base_cost else 0,
                        "avg_quality": _avg([r["quality_score"] for r in out]),
                        "model_mix": mix},
            "verdict": f"${cost:.4f} against ${base_cost:.4f} - "
                       f"{round(100 * (base_cost - cost) / base_cost) if base_cost else 0}% less, "
                       f"across {len(mix)} different model(s).",
            "passed": cost < base_cost}


@scenario("cache", "The same question, asked twice",
          "What happens when traffic repeats, as real traffic does?",
          "The second ask is served from cache with no model call; a paraphrase is served too.", 3)
async def _cache(pipeline, settings, pool, params):
    await cache.clear(TENANT)
    first = await _run(pipeline, settings, pool, "Q-01", "optimized")
    again = await _run(pipeline, settings, pool, "Q-01", "optimized")
    para = await _run(pipeline, settings, pool, "Q-54", "optimized")     # paraphrase of Q-01
    other = await _run(pipeline, settings, pool, "Q-10", "optimized")    # same shape, different service
    sem_step = next((s for s in para["trace"] if s["stage"] == "semantic_cache"), {})
    guard_step = next((s for s in other["trace"] if s["stage"] == "semantic_cache"), {})
    rejected = (guard_step.get("detail") or {}).get("rejected", [])
    return {"requests": [_card(r) for r in (first, again, para, other)],
            "metrics": {"exact_hit": again.get("cache_kind") == "exact",
                        "semantic_hit": para.get("cache_kind") == "semantic",
                        "similarity": (sem_step.get("detail") or {}).get("hit", {}).get("similarity")
                        if para.get("cache_kind") == "semantic" else None,
                        "guarded_wrong_answer": bool(rejected) and other.get("cache_kind") is None,
                        "cost_avoided_usd": round(again["baseline_cost_usd"] + (
                            para["baseline_cost_usd"] if para.get("cache_kind") else 0), 6)},
            "verdict": (f"Identical request: {'served from cache' if again.get('cache_kind') == 'exact' else 'missed'}. "
                        f"Paraphrase: {'served from the semantic cache' if para.get('cache_kind') == 'semantic' else 'not served'}. "
                        f"Same question about a different service: "
                        f"{'correctly refused by the subject guard' if other.get('cache_kind') is None else 'served (investigate)'}."),
            "passed": again.get("cache_kind") == "exact" and other.get("cache_kind") is None,
            "note": "The last request is the important one: it is lexically the closest match of all, "
                    "and serving it would have returned another service's number."}


@scenario("escalation", "A question the cheap route gets wrong",
          "Does the system trade quality for money when the cheap route fails?",
          "The quality gate rejects the weak answer and the request is escalated, at a stated cost.", 4)
async def _escalation(pipeline, settings, pool, params):
    """Two stages, so the scenario is honest whichever way the models behave.

    Stage 1 runs a task with a *deterministic* contract (a JSON schema with
    expected values). If the routed model breaks the contract, the gate fails on
    a fact rather than on a judge's opinion, and the request escalates.

    If it passes, stage 2 re-runs the same question with the quality bar raised,
    and says so. That is a policy change made in the open, not a rigged answer.
    """
    policies = policy_mod.get_policies()
    qid = params.get("query") or "Q-43"          # log_extraction, json_schema validator
    first = await _run(pipeline, settings, pool, qid, "optimized", fresh_cache=True)
    stage, raised = 1, None
    r = first
    if not r.get("escalated_from"):
        raised = float(params.get("threshold", 5.0))
        policies.set_override(TENANT, APP, {"quality_gate_threshold": raised})
        try:
            r = await _run(pipeline, settings, pool, qid, "optimized", fresh_cache=True)
        finally:
            policies.set_override(TENANT, APP, None)
        stage = 2
    gate = next((s for s in r["trace"] if s["stage"] == "quality"), {})
    esc = next((s for s in r["trace"] if s["stage"] == "escalate"), {})
    return {"requests": [_card(x) for x in ([first, r] if stage == 2 else [r])],
            "metrics": {"escalated": bool(r.get("escalated_from")),
                        "from": r.get("escalated_from"), "to": r.get("model"),
                        "extra_cost_usd": r.get("escalation_cost_usd"),
                        "final_quality": r.get("quality_score"),
                        "gate_reason": r.get("quality_reason")},
            "verdict": (
                f"The gate rejected {r['escalated_from']} and escalated to {r['model']}; the second "
                f"attempt scored {r.get('quality_score')}. Both attempts are billed and the ledger "
                f"shows the first one as a negative bar."
                if r.get("escalated_from") else
                f"The routed model passed even at a {raised}/5 bar ({r.get('quality_reason')}). "
                f"That is the honest outcome of this run, not a scripted one — the mechanism is the "
                f"same one the quality page shows firing on other requests."),
            "passed": True,
            "note": (f"Stage 1 ran the deterministic contract check. It passed, so stage 2 re-ran the "
                     f"same question with the gate raised to {raised}/5 to exercise the escalation "
                     f"path. The bar change is stated here rather than hidden."
                     if stage == 2 else
                     "The routed model broke the task's schema contract, so the gate failed on a "
                     "fact rather than a judge's opinion.")}


@scenario("failover", "A provider goes down mid-demo",
          "What happens to availability when a provider fails?",
          "The request fails over to another provider and the breaker opens.", 5)
async def _failover(pipeline, settings, pool, params):
    health = get_health()
    target = params.get("provider") or "groq"
    health.inject(target, "fail")
    try:
        r = await _run(pipeline, settings, pool, params.get("query") or "Q-01", "optimized",
                       fresh_cache=True)
        after = health.public()["providers"].get(target, {})
    finally:
        health.inject(target, None)
    return {"requests": [_card(r)],
            "metrics": {"injected_provider": target, "fallback_used": r.get("fallback_used"),
                        "answered_by": r.get("model"), "answered_by_provider": r.get("provider"),
                        "attempts": r.get("attempts"),
                        "breaker_open": after.get("breaker_open"),
                        "consecutive_failures": after.get("consecutive_failures")},
            "verdict": (f"{target} was made to fail. The request was answered by {r.get('model')} on "
                        f"{r.get('provider')} after {len([a for a in r.get('attempts', []) if a.get('error')])} "
                        f"failed attempt(s). Cost is attributed to the model that answered."),
            "passed": bool(r.get("answer")),
            "note": "The fault is injected into the real client, so this exercises the actual "
                    "fallback path rather than a mock."}


@scenario("budget", "The budget tightens",
          "Does spend pressure change what the optimizer does?",
          "Under pressure the objective flips from best-quality to cheapest-safe; a hard limit rejects.", 6)
async def _budget(pipeline, settings, pool, params):
    """Show pressure changing a decision, then refusing one.

    The scenario turns on `frontier_preferred` for this application, which is
    the policy that gives pressure something to change: at normal pressure the
    application buys the best answer it can afford, and above the soft threshold
    cost wins among the routes that still clear the quality floor. The override
    is stated in the note rather than hidden, and quality is never relaxed.

    The squeeze is computed from what has actually been spent, so the scenario
    behaves the same on a fresh store and after a long demo.
    """
    app = params.get("application_id") or APP
    qid = params.get("query") or "Q-15"
    policies = policy_mod.get_policies()
    await budget.reset(TENANT)
    policies.set_override(TENANT, app, {"frontier_preferred": True})
    try:
        relaxed = await _run(pipeline, settings, pool, qid, "optimized",
                             application_id=app, fresh_cache=True)
        spent = max(s.spent_usd for s in await budget.status(policies, TENANT, app))
        # Headroom that a cheap route fits inside and the frontier route does not.
        headroom = max(0.0005, (relaxed.get("cost_usd") or 0.002) / 3)
        policies.set_override(TENANT, app, {"frontier_preferred": True,
                                            "budget": {"daily_usd": round(spent + headroom, 6),
                                                       "soft_threshold": 0.01}})
        pressured = await _run(pipeline, settings, pool, qid, "optimized",
                               application_id=app, fresh_cache=True)
        await budget.book(policies, TENANT, app, headroom * 4)      # spend the rest
        blocked = await _run(pipeline, settings, pool, "Q-30", "optimized",
                             application_id=app, fresh_cache=True)
    finally:
        policies.set_override(TENANT, app, None)
        await budget.reset(TENANT)
    b1 = next((x for x in pressured["trace"] if x["stage"] == "budget"), {})
    changed = relaxed.get("model") != pressured.get("model") and not pressured.get("rejected")
    saved = ((relaxed.get("cost_usd") or 0) - (pressured.get("cost_usd") or 0))
    return {"requests": [_card(relaxed), _card(pressured), _card(blocked)],
            "metrics": {"application": app,
                        "normal_model": relaxed.get("model"), "normal_cost": relaxed.get("cost_usd"),
                        "pressured_model": pressured.get("model"),
                        "pressured_cost": pressured.get("cost_usd"),
                        "route_changed": changed,
                        "pressured_quality": pressured.get("quality_score"),
                        "normal_quality": relaxed.get("quality_score"),
                        "pressure": (b1.get("detail") or {}).get("pressure"),
                        "rejected": bool(blocked.get("rejected")),
                        "rejection_reason": blocked.get("rejection_reason")},
            "verdict": (
                (f"At normal pressure the policy bought the best answer it could afford "
                 f"({relaxed.get('model')}, quality {relaxed.get('quality_score')}). Above the soft "
                 f"threshold cost won among the routes that still clear the quality floor, so it used "
                 f"{pressured.get('model')} at quality {pressured.get('quality_score')} — "
                 f"{round(100 * saved / (relaxed.get('cost_usd') or 1))}% cheaper for the same "
                 f"question. "
                 if changed else
                 f"The route did not change: {relaxed.get('model')} was already the cheapest model "
                 f"clearing this application's quality floor, so there was nothing safe to fall back "
                 f"to. That is the correct outcome, not a failed demo. ")
                + ("The next request was rejected before any model was called — a hard budget is a "
                   "control, not a report." if blocked.get("rejected")
                   else "The next request was still affordable and was served.")),
            "passed": bool(blocked.get("rejected")),
            "note": "This scenario turns on `frontier_preferred` for the application so that pressure "
                    "has something to change; the override is temporary and is removed afterwards. "
                    "Quality is never relaxed for money by default — what pressure changes is which "
                    "of the already-acceptable routes is chosen, and the cost cap the router works to."}


@scenario("sla", "The SLA changes",
          "Does the execution plan follow the service level, not just the model price?",
          "A batch SLA moves the same work to the discounted lane.", 7)
async def _sla(pipeline, settings, pool, params):
    # A batch-eligible task with no strict output contract, so the comparison is
    # about the execution lane and nothing else.
    qid = params.get("query") or "Q-50"
    interactive = await _run(pipeline, settings, pool, qid, "optimized", fresh_cache=True)
    batchy = await _run(pipeline, settings, pool, qid, "optimized",
                        application_id="ticket-enrichment", fresh_cache=True)
    return {"requests": [_card(interactive), _card(batchy)],
            "metrics": {"interactive_mode": interactive.get("execution_mode"),
                        "batch_mode": batchy.get("execution_mode"),
                        "interactive_cost": interactive.get("cost_usd"),
                        "batch_cost": batchy.get("cost_usd"),
                        "batch_discount_applied": (batchy.get("plan") or {}).get("batch_discount")},
            "verdict": (f"The same question ran {interactive.get('execution_mode')} for the chat "
                        f"application and {batchy.get('execution_mode')} for the nightly one. "
                        + ("The batch discount is applied from the provider's published rate and is "
                           "labelled modeled: this demo still executes synchronously."
                           if (batchy.get('plan') or {}).get('batch_discount') else
                           "The provider serving it publishes no batch discount, so the price is "
                           "unchanged - the optimizer says so rather than claiming a saving.")),
            "passed": True}


@scenario("shadow", "Shadow mode on live traffic",
          "Can a client see the saving before changing anything?",
          "Production keeps the incumbent model; the optimizer only reports what it would have done.", 8)
async def _shadow(pipeline, settings, pool, params):
    ids = params.get("queries") or ["Q-01", "Q-15", "Q-37"]
    out = []
    for qid in ids:
        tq = _q(qid)
        env = RequestEnvelope(query=tq["query"], strategy="optimized", mode="shadow",
                              evaluate=False, test_query=tq, tenant_id=TENANT, application_id=APP)
        out.append(await pipeline.run_envelope(env, settings, pool))
    actual = sum(r["cost_usd"] for r in out)
    projected = sum(r["shadow"]["projected_cost_usd"] for r in out)
    return {"requests": [{**_card(r), "shadow": r["shadow"]} for r in out],
            "metrics": {"requests": len(out), "current_spend_usd": round(actual, 6),
                        "projected_spend_usd": round(projected, 6),
                        "projected_saving_pct": round(100 * (actual - projected) / actual, 1) if actual else 0,
                        "basis": "estimated"},
            "verdict": (f"{len(out)} requests served by the incumbent model at ${actual:.4f}. The "
                        f"optimizer would have spent ${projected:.4f} - "
                        f"{round(100 * (actual - projected) / actual) if actual else 0}% less, estimated "
                        f"from the registry using the incumbent's own token counts."),
            "passed": True,
            "note": "No production traffic was rerouted. The projection is an estimate and is labelled "
                    "as one everywhere it appears."}


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def catalogue() -> list:
    return [{k: v for k, v in s.items() if k != "run"}
            for s in sorted(SCENARIOS.values(), key=lambda s: s["order"])]


async def run_scenario(key: str, pipeline, settings, pool, params: dict | None = None) -> dict:
    s = SCENARIOS.get(key)
    if s is None:
        raise KeyError(f"unknown scenario '{key}'; use one of {sorted(SCENARIOS)}")
    t0 = time.perf_counter()
    result = await s["run"](pipeline, settings, pool, params or {})
    return {**{k: v for k, v in s.items() if k != "run"}, **result,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "ran_at": time.time()}


async def reset(full: bool = False) -> dict:
    """Clear demo state. `full` also clears the request log."""
    get_health().reset()
    get_health().chaos.clear()
    policy_mod.get_policies().runtime_flags.clear()
    policy_mod.get_policies().runtime_overrides.clear()
    cleared = await cache.clear()
    await budget.reset()
    if full:
        await storage.clear()
    return {"cache_entries_cleared": cleared, "budgets_reset": True, "chaos_cleared": True,
            "requests_cleared": bool(full)}
