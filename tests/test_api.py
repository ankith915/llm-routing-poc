"""API surface: routes, auth, tenant isolation, the OpenAI-compatible endpoint."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import storage
from backend.api import main as api_main
from backend.llm.client import LLMClient  # noqa: F401
from conftest import FakeClient


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The app's lifespan builds a real ClientPool from .env, which would spend
    real provider credit during tests. Replace the constructor itself so no test
    can reach a provider however it starts the app."""
    fake = FakeClient()

    def fake_pool(settings, chaos=None, on_result=None):
        return fake.as_pool()

    monkeypatch.setattr(api_main.ClientPool, "from_settings", staticmethod(fake_pool))
    monkeypatch.setattr(api_main.LLMClient, "__init__", _forbidden)
    return fake


def _forbidden(*a, **kw):
    raise AssertionError("a test tried to construct a real LLM client - no network in tests")


@pytest.fixture
def client(store, no_network, monkeypatch):
    with TestClient(api_main.app) as c:
        c.fake = no_network
        yield c


def seed(client, n=3, strategy="optimized"):
    qs = ["What is the current CPU usage of payment-api?",
          "Why is payment-api experiencing high latency?",
          "Analyze the payment-api incident and identify the most likely root cause.",
          "Classify this alert into exactly one category from [database, network, application, "
          "capacity, security] and reply with the category only: \"db-primary-01 (db-01) ERROR: "
          "Max connections reached (200/200)\""]
    out = []
    for q in qs[:n]:
        r = client.post("/api/query", json={"query": q, "strategy": strategy, "evaluate": True})
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


# --------------------------------------------------------------------- basics

def test_config_declares_versions_and_open_auth(client):
    c = client.get("/api/config").json()
    assert c["price_registry_version"] and c["policy_version"]
    assert c["auth"]["enabled"] is False
    assert "OPEN" in c["auth"]["note"], "an unsecured instance must say so plainly"
    assert c["semantic_cache"]["separable_with_subject_guards"] is True
    assert set(c["strategies"]) >= {"none", "rule", "intelligent", "optimized"}


def test_query_returns_a_full_explainable_record(client):
    r = seed(client, 1)[0]
    for key in ("request_id", "trace", "ledger", "routing", "classification", "context", "plan",
                "quality", "baseline_cost_usd", "savings_pct", "policy_version",
                "price_registry_version"):
        assert key in r, key
    assert r["ledger"]["attribution"]
    assert r["routing"]["candidates"]


def test_query_rejects_bad_input(client):
    assert client.post("/api/query", json={"query": "  "}).status_code == 400
    assert client.post("/api/query", json={"query": "hi", "strategy": "magic"}).status_code == 400


def test_request_list_and_trace_detail(client):
    recs = seed(client, 2)
    lst = client.get("/api/requests").json()
    assert lst["count"] == 2
    assert "trace" not in lst["requests"][0], "the list view must stay small"
    detail = client.get(f"/api/requests/{recs[0]['request_id']}").json()
    assert detail["trace"] and detail["answer"]
    assert client.get("/api/requests/REQ-9999").status_code == 404


# ------------------------------------------------------------------ analytics

def test_analytics_endpoints_are_consistent_with_each_other(client):
    seed(client, 3)
    ov = client.get("/api/analytics/overview").json()
    wf = client.get("/api/analytics/waterfall").json()
    assert ov["requests"] == 3
    assert wf["reconciles"] is True, f"waterfall must reconcile, residual {wf['residual_usd']}"
    assert wf["baseline_usd"] == pytest.approx(ov["baseline_spend_usd"], abs=1e-6)
    assert wf["final_usd"] == pytest.approx(ov["spend_usd"], abs=1e-6)
    assert sum(s["usd"] for s in wf["steps"]) == pytest.approx(wf["savings_usd"], abs=1e-6)


def test_waterfall_steps_carry_their_basis(client):
    seed(client, 3)
    wf = client.get("/api/analytics/waterfall").json()
    assert wf["steps"], "expected at least one attribution bar"
    for s in wf["steps"]:
        assert s["basis"] and all(b in ("measured", "estimated", "modeled") for b in s["basis"])


def test_waste_and_recommendations_report_evidence(client):
    seed(client, 3, strategy="none")
    seed(client, 3, strategy="none")          # repeats: cache finding
    w = client.get("/api/analytics/waste").json()
    assert w["requests"] == 6
    keys = {f["key"] for f in w["findings"]}
    assert "repeated_identical_requests" in keys
    for f in w["findings"]:
        assert f["evidence"] and f["recommended_action"] and "basis" in f
    rec = client.get("/api/analytics/recommendations").json()
    for r in rec["recommendations"]:
        assert r["quality_evidence"] and "policy_patch" in r and "confidence" in r


def test_cache_analytics_reports_separation_measurement(client):
    seed(client, 2)
    c = client.get("/api/analytics/cache").json()
    sep = c["separation"]
    assert sep["separable_on_similarity_alone"] is False
    assert sep["separable_with_subject_guards"] is True
    assert c["exact_hit_rate"] is not None and "cost_avoided_usd" in c


def test_unit_economics_has_cost_per_successful_task(client):
    seed(client, 3)
    ue = client.get("/api/analytics/unit-economics").json()
    dims = {d["dimension"] for d in ue["dimensions"]}
    assert {"application_id", "task_type", "tenant_id"} <= dims
    rows = next(d for d in ue["dimensions"] if d["dimension"] == "task_type")["rows"]
    assert rows and rows[0]["cost_per_successful_task_usd"] is not None


# ------------------------------------------------------------------ registry

def test_models_endpoint_exposes_prices_capabilities_and_prior_sources(client):
    m = client.get("/api/models").json()
    assert m["price_registry_version"] and m["effective_date"]
    ids = {x["id"] for x in m["models"]}
    assert "gpt-4.1" in ids
    gpt = next(x for x in m["models"] if x["id"] == "gpt-4.1")
    assert gpt["quality_priors"]["HARD"]["source"] == "measured"
    assert gpt["capabilities"]["tools"] is True
    assert gpt["cached_input_cost_per_1m"] == 0.5


def test_tasks_endpoint_lists_the_taxonomy(client):
    t = client.get("/api/tasks").json()
    names = {x["name"] for x in t["tasks"]}
    assert {"sql_generation", "root_cause_analysis", "alert_classification"} <= names


# ------------------------------------------------------------ policy & budget

def test_policy_and_flag_updates_take_effect(client):
    p = client.get("/api/policies").json()
    assert any(t["id"] == "acme" for t in p["tenants"])
    r = client.put("/api/policies", json={"tenant_id": "acme", "application_id": "ops-assistant",
                                          "patch": {"quality_target": 4.9}})
    assert r.json()["effective"]["values"]["quality_target"] == 4.9
    client.put("/api/policies", json={"tenant_id": "acme", "application_id": "ops-assistant",
                                      "patch": None})
    f = client.put("/api/flags", json={"name": "ENABLE_SEMANTIC_CACHE", "value": False}).json()
    assert f["flags"]["ENABLE_SEMANTIC_CACHE"]["value"] is False
    assert f["flags"]["ENABLE_SEMANTIC_CACHE"]["source"] == "runtime override"
    assert client.put("/api/flags", json={"name": "NOPE", "value": True}).status_code == 400
    client.put("/api/flags", json={"name": "ENABLE_SEMANTIC_CACHE", "value": None})


def test_disabling_a_flag_changes_behaviour(client):
    client.put("/api/flags", json={"name": "ENABLE_EXACT_CACHE", "value": False})
    try:
        seed(client, 1)
        seed(client, 1)
        recs = client.get("/api/requests").json()["requests"]
        assert all(not r.get("cache_hit") for r in recs)
    finally:
        client.put("/api/flags", json={"name": "ENABLE_EXACT_CACHE", "value": None})


def test_budgets_endpoint_lists_scopes(client):
    b = client.get("/api/budgets").json()
    scopes = {(s["tenant_id"], s["application_id"]) for s in b["scopes"]}
    assert ("acme", "ops-assistant") in scopes
    row = next(s for s in b["scopes"] if s["application_id"] == "ops-assistant")
    assert {x["window"] for x in row["budgets"]} == {"daily", "monthly"}


# ---------------------------------------------------------- OpenAI-compatible

def test_openai_endpoint_serves_a_standard_completion_plus_optimizer_block(client):
    r = client.post("/v1/chat/completions", json={
        "model": "gpt-4.1",
        "messages": [{"role": "system", "content": "You are helpful."},
                     {"role": "user", "content": "Is auth-svc currently healthy?"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    opt = body["optimizer"]
    assert opt["requested_model"] == "gpt-4.1"
    assert opt["selected_model"] and opt["reason"]
    assert opt["trace_url"].endswith(opt["request_id"])
    assert r.headers["x-optimizer-request-id"] == opt["request_id"]
    # the trace it points at really exists
    assert client.get(opt["trace_url"]).status_code == 200


def test_openai_endpoint_honours_optimizer_hints(client):
    r = client.post("/v1/chat/completions", json={
        "model": "gpt-4.1", "messages": [{"role": "user", "content": "Summarize ticket TCK-5521"}],
        "optimizer": {"sla_class": "batch", "application_id": "ticket-enrichment"}})
    assert r.json()["optimizer"]["execution_mode"] in ("batch", "offline", "interactive")
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 400


def test_openai_models_endpoint(client):
    d = client.get("/v1/models").json()
    assert d["object"] == "list" and d["data"]


# ---------------------------------------------------------------- simulators

def test_simulator_is_labelled_modeled_and_arithmetic_checks_out(client):
    d = client.get("/api/simulate/defaults").json()
    s = client.post("/api/simulate", json=d).json()
    assert s["basis"] == "modeled" and "not a measurement" in s["note"]
    assert s["baseline"]["monthly_usd"] > s["optimized"]["monthly_usd"]
    assert s["savings"]["monthly_usd"] == pytest.approx(
        s["baseline"]["monthly_usd"] - s["optimized"]["monthly_usd"], abs=0.02)
    assert s["savings"]["annual_usd"] == pytest.approx(s["savings"]["monthly_usd"] * 12, abs=0.5)
    assert sum(l["usd"] for l in s["lever_contributions"]) > 0


def test_simulator_with_no_optimisation_saves_nothing(client):
    s = client.post("/api/simulate", json={
        "requests_per_month": 1000, "input_tokens": 1000, "output_tokens": 100,
        "cache_hit_rate": 0, "context_reduction_pct": 0, "batch_pct": 0,
        "mix": {"cheap": 0, "medium": 0, "premium": 100}}).json()
    assert s["savings"]["monthly_usd"] == pytest.approx(0.0, abs=0.01)


def test_build_vs_buy_refuses_to_recommend_self_hosting_at_low_volume(client):
    b = client.post("/api/simulate/build-vs-buy", json={"requests_per_day": 500}).json()
    assert b["verdict"] in ("api", "api_capacity")
    assert b["self_hosted"]["people_share_pct"] > 50, "people must dominate at POC volume"
    assert b["breakeven"]["requests_per_day"] > 500


# --------------------------------------------------------------- demo control

def test_scenario_catalogue_and_reset(client):
    s = client.get("/api/demo/scenarios").json()["scenarios"]
    keys = {x["key"] for x in s}
    assert {"baseline", "optimized", "cache", "escalation", "failover", "budget", "sla", "shadow"} <= keys
    assert all(x["question"] and x["criterion"] for x in s)
    assert client.post("/api/demo/reset").json()["budgets_reset"] is True


def test_chaos_injection_endpoint(client):
    h = client.post("/api/demo/chaos", json={"provider": "groq", "mode": "fail"}).json()
    assert h["chaos"]["groq"] == "fail"
    assert client.post("/api/demo/chaos", json={"provider": "groq", "mode": "x"}).status_code == 400
    client.post("/api/demo/chaos", json={"provider": "groq", "mode": None})


def test_classifier_endpoint_reports_honest_accuracy(client):
    c = client.get("/api/classifier").json()
    assert 0.8 <= c["accuracy"]["task_type_accuracy"] <= 1.0
    assert "Leave-one-out" in c["note"]
    r = client.post("/api/classifier/retrain").json()
    assert "learned_from_logged_decisions" in r


# ------------------------------------------------------------------ auth mode

def test_api_keys_isolate_tenants(store, no_network, monkeypatch):
    monkeypatch.setenv("OPTIMIZER_KEY_ACME", "sk-acme:acme:ops-assistant")
    monkeypatch.setenv("OPTIMIZER_KEY_GLOBEX", "sk-globex:globex:kb-search")
    with TestClient(api_main.app) as c:
        assert c.get("/api/config").status_code == 401
        acme = {"Authorization": "Bearer sk-acme"}
        globex = {"Authorization": "Bearer sk-globex"}
        cfg = c.get("/api/config", headers=acme).json()
        assert cfg["auth"]["enabled"] is True and cfg["principal"]["tenant_id"] == "acme"
        c.post("/api/query", json={"query": "Is auth-svc currently healthy?"}, headers=acme)
        assert c.get("/api/requests", headers=acme).json()["count"] == 1
        assert c.get("/api/requests", headers=globex).json()["count"] == 0, "tenant isolation"
        rid = c.get("/api/requests", headers=acme).json()["requests"][0]["request_id"]
        assert c.get(f"/api/requests/{rid}", headers=globex).status_code == 404
        # non-admin keys cannot change policy or run demo scenarios
        assert c.put("/api/flags", json={"name": "ENABLE_EXACT_CACHE", "value": False},
                     headers=acme).status_code == 403
        assert c.post("/api/demo/reset", headers=acme).status_code == 403
        # a key may not act for an application it does not own
        assert c.post("/api/query", json={"query": "hi there", "application_id": "finance-copilot"},
                      headers=acme).status_code == 403


def test_admin_key_can_administer(store, no_network, monkeypatch):
    monkeypatch.setenv("OPTIMIZER_ADMIN_KEY", "sk-admin")
    with TestClient(api_main.app) as c:
        h = {"Authorization": "Bearer sk-admin"}
        assert c.put("/api/flags", json={"name": "ENABLE_EXACT_CACHE", "value": None},
                     headers=h).status_code == 200
        assert c.get("/api/config", headers=h).json()["principal"]["admin"] is True


def test_legacy_records_are_excluded_from_cost_math_not_hidden(client, store):
    """Records written before the ledger existed have no baseline. Counting them
    would understate savings; dropping them silently would hide data."""
    import asyncio
    from backend import storage
    asyncio.run(storage.append({
        "request_id": "REQ-LEGACY", "strategy": "rule", "query": "old record",
        "model": "openai/gpt-oss-20b", "tier": "cheap", "cost_usd": 0.001,
        "latency_ms": 900, "quality_score": 4, "input_tokens": 100, "output_tokens": 20,
        "compression": "off", "timestamp": "2026-09-08T08:00:00+00:00"}))
    seed(client, 1)
    ov = client.get("/api/analytics/overview").json()
    assert ov["legacy_requests"] == 1
    assert ov["requests"] == 1, "the legacy record must not be counted in the cost math"
    wf = client.get("/api/analytics/waterfall").json()
    assert wf["reconciles"] is True
    log = client.get("/api/requests").json()
    assert any(r["request_id"] == "REQ-LEGACY" for r in log["requests"]), "still visible in the log"
