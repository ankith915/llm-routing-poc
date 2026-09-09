"""Exact/semantic cache guards, tenant isolation, provider health and chaos."""
import asyncio
import time

import pytest

from backend.optimizer import cache, embeddings, health
from backend.optimizer.policy import load_policies


V = {"prompt_version": "v3", "knowledge_version": "k1", "policy_version": "p1"}
ENTRY = {"request_id": "REQ-1", "answer": "87%", "model": "m", "cost_usd": 0.002, "latency_ms": 900}


def run(coro):
    return asyncio.run(coro)


def test_exact_cache_hit_miss_ttl_and_versions(store):
    async def go():
        miss = await cache.exact_lookup("acme", "ops", "What is X?", V, 3600)
        assert miss.hit is None
        await cache.exact_store("acme", "ops", "What is X?", V, ENTRY)
        hit = await cache.exact_lookup("acme", "ops", "  what is x ", V, 3600)   # normalised
        assert hit.hit and hit.hit.kind == "exact" and hit.hit.entry["answer"] == "87%"
        assert any("versions match" in g for g in hit.hit.guards)
        other = await cache.exact_lookup("acme", "ops", "What is X?", {**V, "knowledge_version": "k2"}, 3600)
        assert other.hit is None, "a new knowledge version must never serve the old answer"
        expired = await cache.exact_lookup("acme", "ops", "What is X?", V, ttl_s=0, now=time.time() + 5)
        assert expired.hit is None and "expired" in expired.rejected[0]["reason"]
        assert (await cache.exact_lookup("acme", "ops", "What is X?", V, 3600)).hit is None, "evicted"
    run(go())


def test_exact_cache_is_tenant_and_app_isolated(store):
    async def go():
        await cache.exact_store("acme", "ops", "Q", V, ENTRY)
        assert (await cache.exact_lookup("globex", "ops", "Q", V, 3600)).hit is None
        assert (await cache.exact_lookup("acme", "other", "Q", V, 3600)).hit is None
        assert (await cache.exact_lookup("acme", "ops", "Q", V, 3600)).hit is not None
        assert await cache.clear("globex") == 0
        assert await cache.clear("acme", "ops") == 1
    run(go())


def test_semantic_cache_accepts_only_when_every_guard_passes(store):
    emb = embeddings.LocalEmbedder()

    async def go():
        await cache.semantic_store("acme", "ops", "What is the current CPU usage of payment-api?",
                                   "metric_lookup", V, emb, {**ENTRY, "quality_ok": True})
        # paraphrase, same task type -> hit with explanation
        r = await cache.semantic_lookup("acme", "ops", "What's the CPU usage of payment-api right now?",
                                        "metric_lookup", V, emb, threshold=0.5, ttl_s=3600)
        assert r.hit and r.hit.kind == "semantic" and r.hit.similarity >= 0.5
        assert any("task type" in g for g in r.hit.guards) and r.embedder == "local-lexical"
        # same words, different task type -> rejected with the reason
        r = await cache.semantic_lookup("acme", "ops", "What's the CPU usage of payment-api right now?",
                                        "diagnosis", V, emb, threshold=0.5, ttl_s=3600)
        assert r.hit is None and "task type differs" in r.rejected[0]["reason"]
        # below threshold
        r = await cache.semantic_lookup("acme", "ops", "Summarize ticket TCK-5521", "metric_lookup", V, emb, 0.5, 3600)
        assert r.hit is None and "threshold" in r.rejected[0]["reason"]
        # version change
        r = await cache.semantic_lookup("acme", "ops", "What's the CPU usage of payment-api right now?",
                                        "metric_lookup", {**V, "prompt_version": "v4"}, emb, 0.5, 3600)
        assert r.hit is None and "version differs" in r.rejected[0]["reason"]
        # identical query is exact-cache territory
        r = await cache.semantic_lookup("acme", "ops", "What is the current CPU usage of payment-api?",
                                        "metric_lookup", V, emb, 0.5, 3600)
        assert r.hit is None and "identical" in r.rejected[0]["reason"]
        # other tenant sees nothing
        r = await cache.semantic_lookup("globex", "ops", "What's the CPU usage of payment-api right now?",
                                        "metric_lookup", V, emb, 0.5, 3600)
        assert r.hit is None and r.checked == 0
    run(go())


def test_semantic_cache_rejects_entries_that_failed_quality(store):
    emb = embeddings.LocalEmbedder()

    async def go():
        await cache.semantic_store("acme", "ops", "Is auth-svc healthy?", "health_check", V, emb,
                                   {**ENTRY, "quality_ok": False})
        r = await cache.semantic_lookup("acme", "ops", "Is the auth-svc healthy right now?", "health_check",
                                        V, emb, 0.3, 3600)
        assert r.hit is None and "quality gate" in r.rejected[0]["reason"]
    run(go())


def test_local_embedder_similarity_properties():
    a = embeddings.local_vector("What is the current CPU usage of payment-api?")
    b = embeddings.local_vector("What's the CPU usage of payment-api right now?")
    c = embeddings.local_vector("Summarize ticket TCK-5521 for the incident commander")
    assert embeddings.cosine(a, a) == pytest.approx(1.0)
    assert embeddings.cosine(a, b) > embeddings.cosine(a, c)
    assert embeddings.cosine(a, [0.0]) == 0.0


def test_choose_embedder_falls_back_to_local():
    class Pool:
        def has(self, p): return False
    assert isinstance(embeddings.choose(Pool(), "auto"), embeddings.LocalEmbedder)
    with pytest.raises(RuntimeError):
        embeddings.choose(Pool(), "openai")


# ------------------------------------------------------------------ health

def test_circuit_breaker_opens_and_half_opens():
    h = health.HealthRegistry(failure_threshold=2, open_seconds=10)
    h.record("groq", True, 100)
    assert not h.breaker_open("groq")
    h.record("groq", False, error="boom")
    h.record("groq", False, error="boom")
    assert h.breaker_open("groq") and h.get("groq").error_rate == pytest.approx(2 / 3)
    ph = h.get("groq")
    assert ph.is_open(10, now=ph.opened_at + 11) is False        # half-open after the window
    assert ph.consecutive_failures == 0
    h.record("groq", True, 120)
    assert not h.breaker_open("groq") and h.get("groq").p50_latency_ms in (100, 120)


def test_chaos_injection_modes():
    h = health.HealthRegistry()
    h.inject("openai", "fail")
    assert h.chaos_for("openai") == "fail" and h.public()["chaos"] == {"openai": "fail"}
    with pytest.raises(ValueError):
        h.inject("openai", "explode")
    h.inject("openai", None)
    assert h.chaos_for("openai") is None
