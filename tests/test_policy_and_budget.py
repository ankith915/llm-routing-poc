"""Policy precedence, feature flags, request envelope, budget enforcement, id uniqueness."""
import asyncio

import pytest

from backend import storage
from backend.optimizer import budget
from backend.optimizer.envelope import RequestEnvelope
from backend.optimizer.policy import get_policies, load_policies



@pytest.fixture
def policies():
    return load_policies()


# ---------------------------------------------------------------- policy

def test_defaults_apply_to_unknown_tenant(policies):
    p = policies.resolve("nobody", "nothing")
    assert p["quality_target"] == 4.0
    assert p.source("quality_target") == "defaults"


def test_application_overrides_tenant_overrides_defaults(policies):
    p = policies.resolve("acme", "finance-copilot")
    assert p["sensitivity_class"] == "confidential"
    assert p.source("sensitivity_class") == "application"
    assert p["quality_target"] == 4.3
    t = policies.resolve("globex", "kb-search")
    assert t["sensitivity_class"] == "confidential" and t.source("sensitivity_class") == "tenant"
    assert t["cache"]["semantic"]["threshold"] == 0.95
    assert t.source("cache.semantic.threshold") == "application"
    assert t["cache"]["exact"]["enabled"] is True          # untouched sibling survives deep merge


def test_request_overrides_win(policies):
    p = policies.resolve("acme", "ops-assistant", {"quality_target": 4.8, "sla_class": "batch"})
    assert p["quality_target"] == 4.8 and p.source("quality_target") == "request"
    assert p["sla_class"] == "batch"


def test_unknown_policy_keys_are_reported_not_applied(policies):
    p = policies.resolve("acme", "ops-assistant", {"qualty_target": 1.0})
    assert "request:qualty_target" in p.ignored_keys
    assert p["quality_target"] == 4.0


def test_sensitivity_restricts_providers(policies):
    assert policies.resolve("acme", "ops-assistant").providers_for_sensitivity() == ["groq", "openai"]
    assert policies.resolve("acme", "finance-copilot").providers_for_sensitivity() == ["openai"]
    assert policies.resolve("acme", "ops-assistant", {"sensitivity_class": "restricted"}).providers_for_sensitivity() == []


def test_invalid_sla_or_sensitivity_rejected(policies):
    with pytest.raises(ValueError):
        policies.resolve("acme", "ops-assistant", {"sla_class": "yesterday"})
    with pytest.raises(ValueError):
        policies.resolve("acme", "ops-assistant", {"sensitivity_class": "secretish"})


def test_flags_precedence(policies, monkeypatch):
    assert policies.flags()[0]["ENABLE_EXACT_CACHE"] is True
    monkeypatch.setenv("FLAG_ENABLE_EXACT_CACHE", "0")
    flags, prov = policies.flags()
    assert flags["ENABLE_EXACT_CACHE"] is False and prov["ENABLE_EXACT_CACHE"] == "environment"
    policies.set_flag("ENABLE_EXACT_CACHE", True)
    flags, prov = policies.flags()
    assert flags["ENABLE_EXACT_CACHE"] is True and prov["ENABLE_EXACT_CACHE"] == "runtime override"
    policies.set_flag("ENABLE_EXACT_CACHE", None)
    assert policies.flags()[0]["ENABLE_EXACT_CACHE"] is False
    with pytest.raises(KeyError):
        policies.set_flag("ENABLE_TIME_TRAVEL", True)


def test_runtime_override_layer(policies):
    policies.set_override("acme", "ops-assistant", {"quality_target": 4.6})
    assert policies.resolve("acme", "ops-assistant")["quality_target"] == 4.6
    assert policies.resolve("acme", "ops-assistant").source("quality_target") == "runtime"
    policies.set_override("acme", "ops-assistant", None)
    assert policies.resolve("acme", "ops-assistant")["quality_target"] == 4.0


def test_catalogue_lists_tenants_and_flags(policies):
    c = policies.catalogue()
    ids = {t["id"] for t in c["tenants"]}
    assert {"acme", "globex"} <= ids
    assert "ENABLE_MODEL_ROUTING" in c["flags"]


# -------------------------------------------------------------- envelope

def test_envelope_takes_last_user_message_from_messages():
    e = RequestEnvelope(query="", messages=[{"role": "system", "content": "s"},
                                            {"role": "user", "content": "first"},
                                            {"role": "assistant", "content": "a"},
                                            {"role": "user", "content": " second "}])
    assert e.query == "second"


def test_envelope_rejects_empty_and_bad_values():
    with pytest.raises(ValueError):
        RequestEnvelope(query="   ")
    with pytest.raises(ValueError):
        RequestEnvelope(query="q", mode="dream")
    with pytest.raises(ValueError):
        RequestEnvelope(query="q", strategy="magic")
    RequestEnvelope(query="q", strategy="fixed:gpt-4.1")   # allowed


def test_envelope_from_openai_body():
    body = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "Is auth-svc healthy?"}],
            "optimizer": {"sla_class": "batch", "quality_target": 4.5, "feature_id": "f1"},
            "response_format": {"type": "json_schema", "json_schema": {"name": "x"}}}
    e = RequestEnvelope.from_openai(body, "globex", "kb-search")
    assert e.query == "Is auth-svc healthy?" and e.tenant_id == "globex"
    assert e.policy_overrides() == {"sla_class": "batch", "quality_target": 4.5, "feature_id": "f1"}
    assert e.output_schema == {"name": "x"}
    assert e.metadata["requested_model"] == "gpt-4.1"


# ---------------------------------------------------------------- budget

def test_budget_reserve_reconcile_and_pressure(store, policies):
    async def run():
        st = await budget.status(policies, "acme", "ops-assistant")
        assert {s.scope for s in st} == {"tenant:acme", "app:acme/ops-assistant"}
        d = await budget.check_and_reserve(policies, "acme", "ops-assistant", 0.5)
        assert d.allowed and d.pressure == 0.0
        # reserved counts toward pressure until reconciled
        st = await budget.status(policies, "acme", "ops-assistant")
        app_daily = next(s for s in st if s.scope == "app:acme/ops-assistant" and s.window == "daily")
        assert app_daily.reserved_usd == pytest.approx(0.5)
        await budget.reconcile(policies, "acme", "ops-assistant", 0.5, 0.2)
        st = await budget.status(policies, "acme", "ops-assistant")
        app_daily = next(s for s in st if s.scope == "app:acme/ops-assistant" and s.window == "daily")
        assert app_daily.reserved_usd == pytest.approx(0.0)
        assert app_daily.spent_usd == pytest.approx(0.2)
        # app daily limit is 3.0 -> pressure 0.2/3
        d = await budget.check_and_reserve(policies, "acme", "ops-assistant", 0.0)
        assert d.pressure == pytest.approx(0.2 / 3.0)
    asyncio.run(run())


def test_hard_budget_rejects_only_when_even_the_cheapest_route_does_not_fit(store, policies):
    """A squeeze must re-route before it rejects. Rejecting on the premium
    model's worst case would turn every budget squeeze into an outage while a
    cheap route was still affordable."""
    async def run():
        await budget.book(policies, "acme", "ticket-enrichment", 0.99)   # app daily limit 1.0
        # the cheapest route still fits in the remaining $0.01: admit it, and
        # tell the router how little is left
        ok = await budget.check_and_reserve(policies, "acme", "ticket-enrichment",
                                            0.002, reserve_usd=0.5)
        assert ok.allowed and ok.pressure >= 0.8
        assert ok.remaining_usd == pytest.approx(0.01, abs=1e-6)
        assert "cost cap tightens" in ok.reason
        # the reservation is capped at what is actually left, so a large worst
        # case cannot lock out traffic that will be served cheaply
        st = await budget.status(policies, "acme", "ticket-enrichment")
        app_daily = next(s for s in st if s.scope == "app:acme/ticket-enrichment" and s.window == "daily")
        assert app_daily.reserved_usd <= 0.01 + 1e-9
        await budget.reconcile(policies, "acme", "ticket-enrichment", ok.hold_usd, 0.002)
        # now nothing fits at all
        await budget.book(policies, "acme", "ticket-enrichment", 0.01)
        d = await budget.check_and_reserve(policies, "acme", "ticket-enrichment", 0.002)
        assert not d.allowed
        assert "hard budget" in d.reason and "cheapest eligible route" in d.reason
        st = await budget.status(policies, "acme", "ticket-enrichment")
        assert all(s.reserved_usd == 0 for s in st), "a rejected request reserves nothing"
    asyncio.run(run())


def test_budget_pressure_tightens_the_router_cost_cap(store, policies):
    """The remaining budget becomes the request's cost cap, so the router picks
    a route that fits instead of the request being refused."""
    from backend.optimizer import router as router_stage
    from backend.optimizer.classifier import get_classifier
    from backend.optimizer.registry import get_registry
    reg = get_registry()
    pol = policies.resolve("acme", "ops-assistant")
    c = get_classifier().classify("Why is payment-api experiencing high latency?")
    loose = router_stage.route(c, pol, reg, None, input_tokens=1800, output_budget=300)
    tight = router_stage.route(c, pol, reg, None, input_tokens=1800, output_budget=300,
                               budget_pressure=0.95, remaining_budget_usd=0.0004)
    assert loose.selected is not None and tight.selected is not None
    assert tight.selected.blended_cost <= loose.selected.blended_cost
    assert any("remaining budget" in f for f in tight.factors)


def test_budget_scopes_are_isolated_between_tenants(store, policies):
    async def run():
        await budget.book(policies, "acme", "ops-assistant", 1.0)
        st = await budget.status(policies, "globex", "kb-search")
        assert all(s.spent_usd == 0 for s in st)
        n = await budget.reset("acme")
        assert n > 0
        st = await budget.status(policies, "acme", "ops-assistant")
        assert all(s.spent_usd == 0 for s in st)
    asyncio.run(run())


def test_no_budget_configured_means_no_pressure(store, policies):
    async def run():
        d = await budget.check_and_reserve(policies, "nobody", "nothing", 100.0)
        assert d.allowed and d.reason == "no budget configured"
    asyncio.run(run())


# ------------------------------------------------------------- storage ids

def test_request_ids_are_unique_under_concurrency(store):
    async def run():
        ids = await asyncio.gather(*(storage.next_request_id() for _ in range(50)))
        return ids
    ids = asyncio.run(run())
    assert len(set(ids)) == 50


def test_kv_roundtrip_and_prefix_scan(store):
    async def run():
        await storage.kv_set("cache:a:1", {"v": 1})
        await storage.kv_set("cache:a:2", {"v": 2})
        await storage.kv_set("other:x", {"v": 3})
        await storage.kv_update("cache:a:1", lambda v: {**v, "v": v["v"] + 10})
        assert (await storage.kv_get("cache:a:1"))["v"] == 11
        assert sorted(await storage.kv_keys("cache:")) == ["cache:a:1", "cache:a:2"]
        assert await storage.kv_clear("cache:") == 2
        assert await storage.kv_get("other:x") == {"v": 3}
        assert await storage.kv_get("missing") is None
    asyncio.run(run())
