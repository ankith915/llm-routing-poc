"""Demo scenarios must actually demonstrate their point.

Each of these guards a specific way a scenario can silently stop proving
anything: a cache hit short-circuiting the mechanism, an escalation path with
nothing to escalate to, a fault injection that never reaches the client.
"""
import asyncio

import pytest

from backend import demo, pipeline, storage
from backend.llm.client import ClientPool
from backend.optimizer.health import get_health

from conftest import FakeClient


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rig(store, settings):
    fake = FakeClient()
    health = get_health()
    pool = ClientPool({p: fake for p in ("groq", "openai")})
    # Wire the chaos hook the way the app's lifespan does, so an injected fault
    # actually reaches the client rather than being silently ignored.
    fake.chaos = health.chaos_for
    orig = fake.chat

    async def chat(model, messages, **kw):
        mode = health.chaos_for(_provider_of(model))
        if mode in ("fail", "timeout", "rate_limit"):
            from backend.llm.client import InjectedFault
            raise InjectedFault(_provider_of(model), mode)
        return await orig(model, messages, **kw)

    fake.chat = chat
    return settings, pool, fake


def _provider_of(model_id):
    return "groq" if model_id.startswith("openai/gpt-oss") else "openai"


def test_baseline_uses_only_the_frontier_model(rig):
    settings, pool, _ = rig
    r = run(demo.run_scenario("baseline", pipeline, settings, pool))
    assert r["passed"]
    assert all(x["tier"] == "premium" for x in r["requests"])
    assert r["metrics"]["frontier_pct"] == 100.0


def test_optimized_beats_the_baseline_and_uses_more_than_one_model(rig):
    settings, pool, _ = rig
    run(demo.run_scenario("baseline", pipeline, settings, pool))
    r = run(demo.run_scenario("optimized", pipeline, settings, pool))
    assert r["passed"]
    assert r["metrics"]["total_cost_usd"] < r["metrics"]["baseline_cost_usd"]
    assert len(r["metrics"]["model_mix"]) >= 2, "a routing demo that uses one model proves nothing"


def test_cache_scenario_shows_a_hit_and_refuses_a_near_miss(rig):
    settings, pool, fake = rig
    r = run(demo.run_scenario("cache", pipeline, settings, pool))
    m = r["metrics"]
    assert m["exact_hit"] is True, "the identical request must be served from cache"
    assert m["guarded_wrong_answer"] is True, (
        "the same question about a different service must NOT be served from cache")
    assert r["passed"]


def test_escalation_scenario_actually_escalates(rig):
    settings, pool, _ = rig
    r = run(demo.run_scenario("escalation", pipeline, settings, pool))
    assert r["metrics"]["escalated"] is True, (
        "the escalation demo must exercise the escalation path, not report that nothing happened")
    assert r["metrics"]["from"] != r["metrics"]["to"]
    assert r["metrics"]["extra_cost_usd"] > 0, "the rejected attempt must still be billed"


def test_failover_scenario_survives_an_injected_provider_outage(rig):
    settings, pool, _ = rig
    r = run(demo.run_scenario("failover", pipeline, settings, pool))
    m = r["metrics"]
    assert m["answered_by_provider"] != m["injected_provider"], (
        "the request must be answered by a different provider than the one made to fail")
    assert m["fallback_used"] is True
    assert any(a.get("injected") for a in m["attempts"]), "the injected fault must reach the client"
    assert get_health().chaos_for("groq") is None, "the scenario must clean up after itself"


def test_budget_scenario_applies_pressure_and_then_rejects(rig):
    """What must hold on every workload: the remaining budget becomes the
    router's cost cap and is visible in the decision, a hard limit rejects
    before a model is called, and quality is never traded away for money.

    Whether the *route* changes is workload-dependent - if the optimizer is
    already on the cheapest model that clears the quality floor there is nothing
    safe to fall back to - so the scenario reports that honestly instead of
    manufacturing a difference, and this test does not demand one.
    """
    settings, pool, _ = rig
    r = run(demo.run_scenario("budget", pipeline, settings, pool))
    m = r["metrics"]
    assert m["rejected"] is True, "a hard budget must reject before any model is called"
    assert "cheapest eligible route" in (m["rejection_reason"] or ""), (
        "rejection must be because nothing fits, not because the worst case did not")
    assert m["pressure"] > 0
    assert m["route_changed"] or "nothing safe to fall back to" in r["verdict"], (
        "the scenario must either show the route changing or say why it did not")
    if m["pressured_quality"] is not None and m["normal_quality"] is not None:
        assert m["pressured_quality"] >= m["normal_quality"] - 0.5, (
            "budget pressure must not buy savings with quality")
    # the scenario's temporary overrides must not leak
    from backend.optimizer.policy import get_policies, load_policies
    eff = get_policies().resolve("acme", m["application"])
    assert eff["frontier_preferred"] == load_policies().resolve("acme", m["application"])["frontier_preferred"]
    assert eff["budget"]["daily_usd"] == load_policies().resolve("acme", m["application"])["budget"]["daily_usd"]


def test_budget_pressure_is_visible_in_the_routing_decision(rig):
    """The tightened cap has to appear in the trace, or nobody can explain the
    decision after the fact."""
    settings, pool, _ = rig
    from backend.optimizer import budget as budget_mod
    from backend.optimizer.policy import get_policies
    from backend.optimizer.envelope import RequestEnvelope
    pol = get_policies()
    pol.set_override("acme", "ops-assistant", {"budget": {"daily_usd": 0.004, "soft_threshold": 0.01}})
    try:
        run(budget_mod.book(pol, "acme", "ops-assistant", 0.0035))
        rec = run(pipeline.run_envelope(RequestEnvelope(
            query="Why is payment-api experiencing high latency?", strategy="optimized",
            tenant_id="acme", application_id="ops-assistant"), settings, pool))
    finally:
        pol.set_override("acme", "ops-assistant", None)
        run(budget_mod.reset("acme"))
    if rec.get("rejected"):
        return                      # nothing fit at all; that path is covered above
    factors = (rec.get("routing") or {}).get("factors", [])
    assert any("remaining budget" in f for f in factors), factors
    budget_step = next(s for s in rec["trace"] if s["stage"] == "budget")
    assert budget_step["detail"]["cheapest_possible_usd"] > 0
    assert budget_step["detail"]["reserved_while_in_flight_usd"] > 0


def test_sla_scenario_moves_the_same_work_to_the_batch_lane(rig):
    settings, pool, _ = rig
    r = run(demo.run_scenario("sla", pipeline, settings, pool))
    m = r["metrics"]
    assert m["interactive_mode"] == "interactive"
    assert m["batch_mode"] in ("batch", "offline"), (
        "the batch application must not quietly run interactively")


def test_shadow_scenario_does_not_reroute_production(rig):
    settings, pool, fake = rig
    r = run(demo.run_scenario("shadow", pipeline, settings, pool))
    assert all(x["tier"] == "premium" for x in r["requests"]), "shadow must serve the incumbent"
    assert r["metrics"]["basis"] == "estimated"
    assert r["metrics"]["projected_spend_usd"] < r["metrics"]["current_spend_usd"]
    records = run(storage.all_records())
    assert all(rec["mode"] == "shadow" for rec in records)
    assert len(records) == len(r["requests"]), "one stored record per shadow request"


def test_every_scenario_declares_what_it_should_show(rig):
    for s in demo.catalogue():
        assert s["question"] and s["criterion"] and s["title"]


def test_reset_clears_demo_state(rig):
    settings, pool, _ = rig
    run(demo.run_scenario("cache", pipeline, settings, pool))
    get_health().inject("groq", "fail")
    out = run(demo.reset(full=True))
    assert out["requests_cleared"] and out["chaos_cleared"]
    assert get_health().chaos_for("groq") is None
    assert run(storage.all_records()) == []
