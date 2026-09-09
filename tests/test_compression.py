"""Tests for context compression: the Headroom on/off A/B and the win we already have.

Two claims the demo makes, both pinned here so they can't drift:
  1. The pipeline's key=value context format already beats the naive JSON dump
     most teams ship (measured 26-35% fewer tokens).
  2. Compression is a first-class experiment dimension: every record says which
     mode produced it, headline numbers stay routing-only (compression off), and
     the A/B panel compares like with like.

Headroom itself is a heavy, local-only dependency, so these tests never call it.
The backend hook `compression._headroom_compress` is monkeypatched with a fake.
"""
import asyncio

import pytest

from backend import compression, experiment, pipeline, storage, telemetry
from backend.config.loader import load_settings
from backend.evaluation import metrics

from conftest import FakeClient  # noqa: F401


# --------------------------------------------------------------------------
# claim 1: the context format we already send beats a naive JSON dump
# --------------------------------------------------------------------------

def test_count_tokens_basics():
    assert compression.count_tokens("") == 0
    assert compression.count_tokens("cpu_percent=87") > 0
    assert compression.count_tokens("a " * 200) > compression.count_tokens("a " * 20)


def test_needs_logs_gate_matches_build_context():
    """naive_json_tokens must mirror exactly what build_context would include."""
    for tq in telemetry.load_test_queries():
        assert telemetry.needs_logs(tq["query"]) == (
            "## Log stream" in telemetry.build_context(tq["query"]))


def test_naive_json_dump_costs_more_than_pipeline_format():
    """The 'achieved win' on the slide: every demo query is >=20% cheaper than JSON."""
    for tq in telemetry.select_queries(18):
        ours = compression.count_tokens(telemetry.build_context(tq["query"]))
        naive = compression.naive_json_tokens(tq["query"])
        assert naive > ours, tq["id"]
        # The >=20% claim is about the telemetry line format (metrics, incidents,
        # logs). Prose sections (runbooks, tickets) gain less against JSON.
        telemetry_only = set(telemetry.baseline_section_names(tq["query"])) <= {"metrics", "incidents", "logs"}
        if telemetry_only:
            assert (naive - ours) / naive >= 0.20, f"{tq['id']}: only {100*(naive-ours)/naive:.1f}% saved"


# --------------------------------------------------------------------------
# compress_context
# --------------------------------------------------------------------------

CTX_Q = "Analyze the payment-api incident and identify the most likely root cause."


def fake_headroom(context: str, query: str):
    """Stand-in for Headroom: drops the first five lines, reports one transform."""
    lines = context.splitlines()
    return "\n".join(lines[5:]), ["fake:drop5"]


def test_mode_off_is_identity():
    ctx = telemetry.build_context(CTX_Q)
    out, st = compression.compress_context(ctx, CTX_Q, "off")
    assert out == ctx
    assert st["mode"] == "off"
    assert st["tokens_before"] == st["tokens_after"] > 0
    assert st["saved_pct"] == 0
    assert st["error"] is None


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        compression.compress_context("x", "q", "gzip")


def test_headroom_mode_uses_backend_and_reports_savings(monkeypatch):
    monkeypatch.setattr(compression, "_headroom_compress", fake_headroom)
    ctx = telemetry.build_context(CTX_Q)
    out, st = compression.compress_context(ctx, CTX_Q, "headroom")
    assert len(out) < len(ctx)
    assert st["mode"] == "headroom"
    assert st["tokens_after"] < st["tokens_before"]
    assert st["saved_pct"] > 0
    assert st["transforms"] == ["fake:drop5"]
    assert st["error"] is None


def test_headroom_failure_falls_back_to_original(monkeypatch):
    """A compressor crash must never lose the answer - pass the context through."""
    def boom(context, query):
        raise RuntimeError("boom")
    monkeypatch.setattr(compression, "_headroom_compress", boom)
    ctx = telemetry.build_context(CTX_Q)
    out, st = compression.compress_context(ctx, CTX_Q, "headroom")
    assert out == ctx
    assert st["tokens_after"] == st["tokens_before"]
    assert st["saved_pct"] == 0
    assert "boom" in st["error"]


def test_headroom_unavailable_is_reported_not_fatal(monkeypatch):
    """On a host without headroom-ai (Vercel), mode=headroom degrades to passthrough."""
    monkeypatch.setattr(compression, "_headroom_compress", None)
    ctx = telemetry.build_context(CTX_Q)
    out, st = compression.compress_context(ctx, CTX_Q, "headroom")
    assert out == ctx
    assert "not installed" in st["error"]


# --------------------------------------------------------------------------
# pipeline records the compression stats
# --------------------------------------------------------------------------

class CapturingClient(FakeClient):
    """Remembers the last answer prompt so tests can see what the model was sent."""

    def __init__(self):
        super().__init__()
        self.last_answer_prompt = None

    async def chat(self, model, messages, **kw):
        text = messages[-1]["content"]
        if "Context:" in text:
            self.last_answer_prompt = text
        return await super().chat(model, messages, **kw)


def test_run_query_default_compression_is_off(store):
    s = load_settings()
    rec = asyncio.run(pipeline.run_query(CTX_Q, "rule", s, FakeClient().as_pool()))
    assert rec["compression"] == "off"
    assert rec["context_tokens_before"] == rec["context_tokens_after"] > 0
    assert rec["context_saved_pct"] == 0


def test_run_query_headroom_sends_compressed_context(store, monkeypatch):
    monkeypatch.setattr(compression, "_headroom_compress", fake_headroom)
    s = load_settings()
    client = CapturingClient()
    original = telemetry.build_context(CTX_Q)
    dropped_line = original.splitlines()[1]          # a metrics row fake_headroom removes

    rec = asyncio.run(pipeline.run_query(CTX_Q, "rule", s, client.as_pool(),
                                         compression="headroom"))
    assert rec["compression"] == "headroom"
    assert rec["context_tokens_after"] < rec["context_tokens_before"]
    assert rec["context_saved_pct"] > 0
    assert dropped_line not in client.last_answer_prompt, "model must see the compressed context"


# --------------------------------------------------------------------------
# experiment: compression is a job dimension
# --------------------------------------------------------------------------

def test_start_default_is_off_only(store):
    s = load_settings()
    state = asyncio.run(experiment.start(s, limit=3, fresh=True))
    assert state["total"] == 3 * len(metrics.STRATEGIES)
    assert all(len(job) == 3 and job[2] == "off" for job in state["pending"])


def test_start_with_both_modes_doubles_total(store):
    s = load_settings()
    state = asyncio.run(experiment.start(s, limit=3, fresh=True,
                                         compressions=("off", "headroom")))
    assert state["total"] == 3 * len(metrics.STRATEGIES) * 2
    assert {job[2] for job in state["pending"]} == {"off", "headroom"}


def test_step_runs_mixed_modes_and_tags_records(store, monkeypatch):
    monkeypatch.setattr(compression, "_headroom_compress", fake_headroom)
    s = load_settings()
    pool = FakeClient().as_pool()

    async def run():
        await experiment.start(s, limit=1, fresh=True, compressions=("off", "headroom"))
        while (await experiment.step(s, pool, budget_s=60))["status"] != "done":
            pass
        return await storage.all_records()

    recs = asyncio.run(run())
    assert len(recs) == 2 * len(metrics.STRATEGIES)
    modes = {}
    for r in recs:
        modes[r["compression"]] = modes.get(r["compression"], 0) + 1
    assert modes == {"off": len(metrics.STRATEGIES), "headroom": len(metrics.STRATEGIES)}


def test_step_accepts_legacy_two_element_jobs(store):
    """State queued before this change (2-element jobs) must still drain cleanly."""
    s = load_settings()
    pool = FakeClient().as_pool()

    async def run():
        await storage.set_state(experiment.idle() | {
            "status": "running", "total": 1, "pending": [["Q-01", "rule"]]})
        await experiment.step(s, pool, budget_s=60)
        return await storage.all_records()

    recs = asyncio.run(run())
    assert len(recs) == 1
    assert recs[0]["compression"] == "off"


# --------------------------------------------------------------------------
# metrics: headline stays routing-only; A/B panel compares like with like
# --------------------------------------------------------------------------

def _rec(strategy, mode, cost, quality, before, after, tier="medium"):
    return {"strategy": strategy, "compression": mode, "cost_usd": cost,
            "quality_score": quality, "latency_ms": 1000, "tier": tier,
            "model": "m", "expected_complexity": "MEDIUM",
            "context_tokens_before": before, "context_tokens_after": after}


def _mixed():
    out = []
    for strat in metrics.STRATEGIES:
        out.append(_rec(strat, "off", 0.010, 5, 1000, 1000))
        out.append(_rec(strat, "headroom", 0.009, 4, 1000, 800))
    return out


def test_baseline_records_filters_headroom_and_keeps_legacy():
    recs = [_rec("rule", "off", 1, 5, 1, 1), _rec("rule", "headroom", 1, 5, 1, 1)]
    legacy = _rec("rule", "off", 1, 5, 1, 1)
    del legacy["compression"]                        # records from before this change
    recs.append(legacy)
    assert len(metrics.baseline_records(recs)) == 2


def test_overview_headline_uses_baseline_only():
    recs = _mixed()
    ov = metrics.overview(recs)
    assert ov["total_requests"] == len(metrics.STRATEGIES)   # only compression=off
    for s in ov["comparison"]["strategies"]:
        assert s["requests"] == 1
        assert s["avg_quality"] == 5                 # headroom's 4s must not leak in


def test_compression_breakdown_reports_modes_and_deltas():
    bd = metrics.compression_breakdown(_mixed())
    assert set(bd["modes"]) == {"off", "headroom"}
    for strat in metrics.STRATEGIES:
        d = bd["delta"]["headroom"][strat]
        assert d["cost_pct"] == -10.0
        assert d["quality_delta"] == -1.0
        assert d["tokens_saved_pct"] == 20.0


def test_compression_breakdown_with_only_off_has_no_delta():
    recs = [_rec(s, "off", 0.01, 5, 1000, 1000) for s in metrics.STRATEGIES]
    bd = metrics.compression_breakdown(recs)
    assert set(bd["modes"]) == {"off"}
    assert bd["delta"] == {}
