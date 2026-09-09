"""End-to-end pipeline test with a fake LLM client (no network)."""
import asyncio
import json

import pytest

from backend import pipeline, storage
from backend.config.loader import load_settings
from backend.evaluation.metrics import comparison
from backend.llm.client import ClientPool, LLMResponse


class FakeClient:
    """Returns a canned routing decision, answer, or judge verdict by prompt shape."""

    def __init__(self):
        self.calls = []

    def as_pool(self):
        return ClientPool({"groq": self, "openai": self, "openrouter": self})

    async def chat(self, model, messages, temperature=0.2, max_tokens=900,
                   retries=2, extra_params=None):
        text = messages[-1]["content"]
        self.calls.append(model)
        if "routing controller" in text:
            content = json.dumps({
                "complexity": "MEDIUM", "reasoning_required": False,
                "context_requirement": "MEDIUM", "quality_requirement": "HIGH",
                "latency_requirement": "LOW", "recommended_tier": "MEDIUM",
                "reason": "needs light correlation"})
            return LLMResponse(content, model, "fake", 200, 60, 150)
        if "grading an IT-operations assistant" in text:
            return LLMResponse('{"score": 4, "rationale": "correct"}', model, "fake", 300, 30, 120)
        return LLMResponse("The DB pool is exhausted.", model, "fake", 1000, 200, 900)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)   # force the file backend
    monkeypatch.setattr(storage, "RESULTS_PATH", tmp_path / "results.json")
    monkeypatch.setattr(storage, "_records", [])
    monkeypatch.setattr(storage, "_mtime", None)
    return storage


def test_full_pipeline_all_strategies(store):
    settings = load_settings()
    pool = FakeClient().as_pool()
    tq = {"id": "Q-15", "complexity": "MEDIUM",
          "query": "Why is payment-api experiencing high latency?",
          "expected_answer": "DB connection saturation.", "criteria": "blames the DB"}

    async def run():
        out = []
        for strategy in ("none", "rule", "intelligent"):
            out.append(await pipeline.run_query(tq["query"], strategy, settings, pool,
                                                evaluate=True, test_query=tq))
        return out

    r_none, r_rule, r_intel = asyncio.run(run())

    # tier selection per strategy
    assert r_none["tier"] == "premium"
    assert r_rule["tier"] == "medium"          # "why" -> MEDIUM under rule router
    assert r_intel["tier"] == "medium"         # cheapest satisfying HIGH quality + MEDIUM cx

    # cost: premium answer must cost more than medium answer for identical tokens
    assert r_none["answer_cost_usd"] > r_rule["answer_cost_usd"]
    # intelligent carries router overhead on top of the answer cost
    assert r_intel["cost_usd"] > r_intel["answer_cost_usd"]
    assert r_intel["routing"]["router_cost_usd"] > 0
    assert r_intel["latency_ms"] == r_intel["answer_latency_ms"] + r_intel["routing"]["router_latency_ms"]

    # every record graded and persisted
    records = asyncio.run(store.all_records())
    assert len(records) == 3
    assert [r["request_id"] for r in records] == ["REQ-0001", "REQ-0002", "REQ-0003"]
    assert all(r["quality_score"] == 4 for r in records)

    comp = comparison(records)
    by = {s["strategy"]: s for s in comp["strategies"]}
    assert by["intelligent"]["cost_savings_pct"] > 0
    assert by["intelligent"]["quality_retention_pct"] == 100.0


def test_intelligent_falls_back_to_rules_on_router_failure(store):
    settings = load_settings()

    class BrokenRouterClient(FakeClient):
        async def chat(self, model, messages, **kw):
            if "routing controller" in messages[-1]["content"]:
                return LLMResponse("I cannot help with that.", model, "fake", 10, 5, 50)
            return await super().chat(model, messages, **kw)

    async def run():
        return await pipeline.run_query(
            "Analyze the payment-api incident and identify the most likely root cause.",
            "intelligent", settings, BrokenRouterClient().as_pool())

    r = asyncio.run(run())
    assert r["routing"]["fallback"] is True
    assert r["tier"] == "premium"  # rule router classifies as HARD


def test_pool_raises_clear_error_for_missing_provider():
    from backend.llm.client import LLMError
    pool = ClientPool({})
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        pool.get("openai")
