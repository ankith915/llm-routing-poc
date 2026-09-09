"""End-to-end optimizer runs with a fake provider (no network).

These are the invariants the whole product rests on: cost is attributed to the
model that actually answered, the router's own cost is counted, the ledger's
bars sum to the saving, and every record carries the trace that explains it.
"""
import asyncio

import pytest

from backend import pipeline, storage
from backend.evaluation.metrics import comparison
from backend.llm.client import ClientPool, LLMError
from backend.optimizer.envelope import RequestEnvelope

from conftest import FakeClient

Q = "Why is payment-api experiencing high latency?"
TQ = {"id": "Q-15", "complexity": "MEDIUM", "task_type": "diagnosis", "query": Q,
      "expected_answer": "DB connection saturation.", "criteria": "blames the DB"}


def run(coro):
    return asyncio.run(coro)


def go(query, strategy, settings, client, **kw):
    return run(pipeline.run_query(query, strategy, settings, client.as_pool(), **kw))


# --------------------------------------------------------------- strategies

def test_every_strategy_produces_a_comparable_record(store, settings):
    client = FakeClient()

    async def all_four():
        pool = client.as_pool()
        out = []
        for s in ("none", "rule", "intelligent", "optimized"):
            out.append(await pipeline.run_query(Q, s, settings, pool, evaluate=True, test_query=TQ))
        return out

    none, rule, intel, opt = run(all_four())

    assert none["tier"] == "premium", "baseline must always use the premium model"
    assert rule["tier"] == "medium", "'why' is MEDIUM under the keyword router"
    assert intel["classification"]["rung"] == "llm_router"
    assert opt["classification"]["rung"] in ("rules", "classifier", "hint")

    # cost: identical prompts, so premium must cost more than the routed tiers
    assert none["answer_cost_usd"] > rule["answer_cost_usd"]
    assert none["cost_usd"] > opt["cost_usd"]
    # the LLM router's own cost is counted, never hidden
    assert intel["router_cost_usd"] > 0
    assert intel["cost_usd"] > intel["answer_cost_usd"]
    # the free rung costs nothing
    assert opt["router_cost_usd"] == 0

    records = run(storage.all_records())
    assert len(records) == 4
    assert len({r["request_id"] for r in records}) == 4
    assert all(r["quality_score"] is not None for r in records)
    assert all(r["trace"] for r in records)

    by = {s["strategy"]: s for s in comparison(records)["strategies"]}
    assert by["optimized"]["cost_savings_pct"] > 0
    assert by["none"]["cost_savings_pct"] == 0.0


def test_trace_covers_every_stage(store, settings):
    r = go(Q, "optimized", settings, FakeClient(), evaluate=True, test_query=TQ)
    stages = [s["stage"] for s in r["trace"]]
    assert stages[:3] == ["policy", "budget", "exact_cache"]
    for expected in ("classify", "semantic_cache", "route", "context", "plan", "execute",
                     "quality", "ledger"):
        assert expected in stages, expected
    ledger_step = next(s for s in r["trace"] if s["stage"] == "ledger")
    assert ledger_step["detail"]["attribution"]
    route_step = next(s for s in r["trace"] if s["stage"] == "route")
    assert route_step["detail"]["candidates"], "the trace must list what was considered"
    assert route_step["detail"]["reason"]


def test_ledger_bars_sum_to_the_saving(store, settings):
    for strategy in ("none", "rule", "optimized"):
        r = go(Q, strategy, settings, FakeClient(), evaluate=True, test_query=TQ)
        led = r["ledger"]
        total = sum(a["usd"] for a in led["attribution"])
        assert total == pytest.approx(led["savings_usd"], abs=1e-9), strategy
        assert led["baseline_cost_usd"] - led["actual_cost_usd"] == pytest.approx(led["savings_usd"], abs=1e-9)


def test_router_overhead_appears_as_a_negative_bar(store, settings):
    r = go(Q, "intelligent", settings, FakeClient(), evaluate=True, test_query=TQ)
    bars = {a["layer"]: a["usd"] for a in r["ledger"]["attribution"]}
    assert bars["overhead"] < 0
    assert abs(bars["overhead"]) == pytest.approx(r["router_cost_usd"] + r["embedding_cost_usd"], abs=1e-9)


def test_baseline_is_estimated_then_calibrated_by_real_frontier_runs(store, settings):
    """A per-request baseline is always an estimate of the same shape, so a
    single request cannot show a negative saving purely because that answer
    happened to run long. Real frontier runs calibrate the estimate instead of
    replacing it."""
    client = FakeClient()

    async def go():
        pool = client.as_pool()
        first = await pipeline.run_query(Q, "optimized", settings, pool, evaluate=True, test_query=TQ)
        # no frontier runs on record yet
        assert first["baseline_source"] == "estimated"
        for _ in range(3):
            await pipeline.run_query(Q, "none", settings, pool, evaluate=True, test_query=TQ)
        stats = await pipeline.learned(await storage.all_records(), force=True)
        cal = stats["baseline_calibration"]
        assert cal["n"] >= 3 and cal["factor"] > 0
        assert "calibrated on" in cal["note"]
        ctx = await pipeline.context_for(settings, pool, await storage.all_records())
        later = await pipeline.run_query("Is auth-svc currently healthy?", "optimized", settings,
                                         pool, ctx=ctx)
        return later, cal

    later, cal = run(go())
    assert later["baseline_source"] == "calibrated"
    assert later["baseline_cost_usd"] > 0


def test_calibration_is_ignored_below_three_samples(store, settings):
    from backend.optimizer import ledger
    from backend.optimizer.registry import get_registry
    reg = get_registry()
    usd, basis = ledger.baseline_cost(reg, 1000, 200, {"factor": 2.0, "n": 2})
    assert basis == "estimated"
    usd2, basis2 = ledger.baseline_cost(reg, 1000, 200, {"factor": 2.0, "n": 5})
    assert basis2 == "calibrated" and usd2 == pytest.approx(usd * 2.0)


# ------------------------------------------------------------------ caching

def test_exact_cache_serves_the_second_identical_request(store, settings):
    client = FakeClient()

    async def twice():
        pool = client.as_pool()
        a = await pipeline.run_query(Q, "optimized", settings, pool, test_query=TQ)
        b = await pipeline.run_query("  why is PAYMENT-API experiencing high latency ", "optimized",
                                     settings, pool, test_query=TQ)
        return a, b

    first, second = run(twice())
    assert first["cache_hit"] is None and second["cache_hit"] is True
    assert second["cache_kind"] == "exact"
    assert second["answer"] == first["answer"]
    assert second["cost_usd"] < first["cost_usd"]
    assert second["savings_pct"] > 90
    hit = next(s for s in second["trace"] if s["stage"] == "exact_cache")
    assert hit["status"] == "hit" and hit["detail"]["hit"]["guards"]
    assert len(client.answer_calls) == 1, "a cache hit must not call a model"


def test_semantic_cache_serves_a_paraphrase_and_explains_itself(store, settings):
    client = FakeClient()

    async def two():
        pool = client.as_pool()
        await pipeline.run_query("What is the current CPU usage of payment-api?", "optimized",
                                 settings, pool)
        return await pipeline.run_query("What's the CPU utilization on payment-api right now?",
                                        "optimized", settings, pool)

    second = run(two())
    assert second["cache_hit"] is True and second["cache_kind"] == "semantic"
    step = next(s for s in second["trace"] if s["stage"] == "semantic_cache")
    assert step["detail"]["hit"]["similarity"] >= 0.5
    assert any("task type" in g for g in step["detail"]["hit"]["guards"])


def test_cache_is_disabled_for_the_baseline_strategy(store, settings):
    client = FakeClient()

    async def twice():
        pool = client.as_pool()
        await pipeline.run_query(Q, "none", settings, pool, test_query=TQ)
        return await pipeline.run_query(Q, "none", settings, pool, test_query=TQ)

    second = run(twice())
    assert second["cache_hit"] is None
    assert len(client.answer_calls) == 2, "the baseline must pay for every request"


# ----------------------------------------------------------------- fallback

def test_fallback_attributes_cost_to_the_model_that_answered(store, settings):
    from backend.optimizer.registry import get_registry
    cheap = get_registry().tier_default("cheap")
    client = FakeClient(failing_models=[cheap.id])
    r = go("What is the current CPU usage of payment-api?", "optimized", settings, client)
    assert r["fallback_used"] is True
    assert r["routed_model"] == cheap.id, "the routing decision itself is unchanged"
    assert r["model"] != cheap.id, "cost must follow the model that actually answered"
    assert r["fallback_cost_usd"] == 0.0, "a failed call returns no usage to bill"
    assert any(a["error"] for a in r["attempts"])
    step = next(s for s in r["trace"] if s["stage"] == "execute")
    assert step["detail"]["fallback_used"] is True


def test_blank_completion_is_a_failure_not_an_answer(store, settings):
    from backend.optimizer.registry import get_registry
    cheap = get_registry().tier_default("cheap")
    client = FakeClient(blank_models=[cheap.id])
    r = go("What is the current CPU usage of payment-api?", "optimized", settings, client)
    assert r["answer"].strip()
    assert r["fallback_used"] is True and r["model"] != cheap.id


def test_every_provider_failing_raises(store, settings):
    from backend.optimizer.registry import get_registry
    all_models = [m.id for m in get_registry().candidates()]
    client = FakeClient(failing_models=all_models)
    with pytest.raises(LLMError, match="all providers failed"):
        go(Q, "optimized", settings, client, test_query=TQ)


def test_unconfigured_provider_is_skipped_not_counted_as_fallback(store, settings):
    """A tier whose provider has no key is skipped; nothing failed."""
    client = FakeClient()
    pool = ClientPool({"openai": client})          # only OpenAI models are callable
    r = run(pipeline.run_query("What is the current CPU usage of payment-api?", "optimized",
                               settings, pool))
    assert r["provider"] == "openai"
    assert r["fallback_used"] is False
    assert all(a["reason"] == "skipped" for a in r["attempts"] if a["error"])


def test_pool_raises_clear_error_for_missing_provider():
    pool = ClientPool({})
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        pool.get("openai")


# --------------------------------------------------------------- escalation

def test_failed_quality_gate_escalates_and_bills_both_attempts(store, settings):
    # the cheap tier answers badly (judge 2), the stronger model answers well (judge 5)
    client = FakeClient(judge_scores=[2, 5])
    r = go("What is the current CPU usage of payment-api?", "optimized", settings, client,
           evaluate=True,
           test_query={"id": "Q-01", "complexity": "SIMPLE", "task_type": "metric_lookup",
                       "query": "What is the current CPU usage of payment-api?",
                       "expected_answer": "87%", "criteria": "states 87%"})
    assert r["escalated_from"] is not None
    assert r["escalation_count"] == 1
    assert r["quality_score"] == 5
    assert r["escalation_cost_usd"] > 0, "the failed attempt is still paid for"
    assert r["cost_usd"] > r["answer_cost_usd"]
    step = next(s for s in r["trace"] if s["stage"] == "escalate")
    assert step["status"] == "escalated" and "failed the gate" in step["summary"]
    bars = {a["layer"] for a in r["ledger"]["attribution"]}
    assert "escalation" in bars


def test_escalation_resends_uncompressed_context(store, settings):
    client = FakeClient(judge_scores=[2, 5])
    go("What is the current CPU usage of payment-api?", "optimized", settings, client, evaluate=True,
       test_query={"id": "Q-01", "complexity": "SIMPLE", "task_type": "metric_lookup",
                   "query": "What is the current CPU usage of payment-api?",
                   "expected_answer": "87%", "criteria": "states 87%"})
    first, second = client.answer_calls[0], client.answer_calls[1]
    assert len(second["messages"][-1]["content"]) >= len(first["messages"][-1]["content"])


# ------------------------------------------------------------------ shadow

def test_shadow_mode_serves_the_incumbent_and_projects_the_saving(store, settings):
    client = FakeClient()
    env = RequestEnvelope(query=Q, strategy="optimized", mode="shadow", test_query=TQ)
    r = run(pipeline.run_envelope(env, settings, client.as_pool()))
    assert r["mode"] == "shadow"
    assert r["tier"] == "premium", "production traffic keeps using the incumbent model"
    sh = r["shadow"]
    assert sh["basis"] == "estimated"
    assert sh["projected_cost_usd"] < sh["incumbent_cost_usd"]
    assert sh["projected_saving_pct"] > 0
    assert "estimated" in sh["summary"]
    records = run(storage.all_records())
    assert len(records) == 1, "shadow must store exactly one record"
    assert len(client.answer_calls) == 1, "shadow must not call a second model"
