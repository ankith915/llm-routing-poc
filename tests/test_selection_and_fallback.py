"""Tests for demo-run query selection and the answer fallback chain.

Both behaviours exist to keep a client demo short and clean: a stratified
subset so a quick run still covers every complexity band, and a fallback so a
single flaky provider call never drops a cell from the comparison (or prints
a stack trace on screen).
"""
import asyncio

import pytest

from backend import pipeline, telemetry
from backend.config.loader import load_settings
from backend.router import rule_router
from backend.llm.client import LLMError, LLMResponse

from test_pipeline_offline import FakeClient, store  # noqa: F401  (fixture import)


# --------------------------------------------------------------------------
# stratified selection
# --------------------------------------------------------------------------

def test_select_queries_preserves_complexity_ratio():
    """18 of 36 (14S/12M/10H) must come out 7/6/5 - not the first 18 in order."""
    picked = telemetry.select_queries(18)
    assert len(picked) == 18
    counts = {}
    for q in picked:
        counts[q["complexity"]] = counts.get(q["complexity"], 0) + 1
    assert counts == {"SIMPLE": 7, "MEDIUM": 6, "HARD": 5}


def test_select_queries_never_returns_only_simple():
    """A naive queries[:limit] would return 14 SIMPLE + 4 MEDIUM and zero HARD."""
    picked = telemetry.select_queries(18)
    assert {q["complexity"] for q in picked} == {"SIMPLE", "MEDIUM", "HARD"}


@pytest.mark.parametrize("limit", [3, 5, 9, 15, 18, 20, 30])
def test_select_queries_covers_all_bands_and_size(limit):
    picked = telemetry.select_queries(limit)
    assert len(picked) == limit
    assert {q["complexity"] for q in picked} == {"SIMPLE", "MEDIUM", "HARD"}


def test_select_queries_none_or_oversized_returns_all():
    total = len(telemetry.load_test_queries())
    assert len(telemetry.select_queries(None)) == total
    assert len(telemetry.select_queries(999)) == total


def test_select_queries_is_deterministic():
    assert telemetry.select_queries(18) == telemetry.select_queries(18)


def test_select_queries_returns_ids_in_dataset_order():
    ids = [q["id"] for q in telemetry.select_queries(18)]
    assert ids == sorted(ids)


# --------------------------------------------------------------------------
# fallback chain ordering
# --------------------------------------------------------------------------

def test_fallback_chain_escalates_then_descends():
    s = load_settings()
    chain = lambda k: [t.key for t in pipeline.fallback_chain(s, s.tier(k))]
    assert chain("cheap") == ["cheap", "medium", "premium"]
    assert chain("medium") == ["medium", "premium", "cheap"]
    assert chain("premium") == ["premium", "medium", "cheap"]


# --------------------------------------------------------------------------
# fallback behaviour in run_query
# --------------------------------------------------------------------------

class FlakyClient(FakeClient):
    """Fails for a named set of models, succeeds for everything else."""

    def __init__(self, failing_models):
        super().__init__()
        self.failing = set(failing_models)

    async def chat(self, model, messages, **kw):
        if model in self.failing:
            self.calls.append(model)
            raise LLMError(f"simulated outage for {model}")
        return await super().chat(model, messages, **kw)


class BlankAnswerClient(FakeClient):
    """Returns a blank answer for named models - the gpt-oss truncation mode."""

    def __init__(self, blank_models):
        super().__init__()
        self.blank = set(blank_models)

    async def chat(self, model, messages, **kw):
        resp = await super().chat(model, messages, **kw)
        if model in self.blank and "routing controller" not in messages[-1]["content"]:
            return LLMResponse("   ", model, "fake", resp.input_tokens,
                               resp.output_tokens, resp.latency_ms)
        return resp


ANSWERABLE = "Why is payment-api experiencing high latency?"


def routed_tier(settings, query):
    """The tier the rule router actually picks - don't hard-code the assumption."""
    return settings.tier(rule_router.route(query)["selected_tier"])


def test_run_query_falls_back_when_tier_fails(store):
    s = load_settings()
    routed = routed_tier(s, ANSWERABLE)
    client = FlakyClient([routed.name])
    rec = asyncio.run(pipeline.run_query(ANSWERABLE, "rule", s, client.as_pool()))

    assert rec["answer"], "fallback must still produce an answer"
    assert rec["fallback_used"] is True
    assert rec["routing"]["selected_tier"] == routed.key, "routing decision is unchanged"
    assert rec["routed_tier"] == routed.key
    assert rec["tier"] != routed.key, "cost must be attributed to the tier that answered"
    assert routed.name in client.calls


def test_run_query_falls_back_on_blank_answer(store):
    """An empty completion is a failure, not a valid answer to grade."""
    s = load_settings()
    routed = routed_tier(s, ANSWERABLE)
    client = BlankAnswerClient([routed.name])
    rec = asyncio.run(pipeline.run_query(ANSWERABLE, "rule", s, client.as_pool()))

    assert rec["answer"].strip(), "must not persist a blank answer"
    assert rec["fallback_used"] is True
    assert rec["tier"] != routed.key


def test_run_query_no_fallback_flag_on_success(store):
    s = load_settings()
    rec = asyncio.run(pipeline.run_query(ANSWERABLE, "rule", s, FakeClient().as_pool()))
    assert rec["fallback_used"] is False
    assert rec["tier"] == rec["routing"]["selected_tier"]


def test_run_query_raises_when_every_tier_fails(store):
    """Total failure still raises - the caller decides how to report it."""
    s = load_settings()
    client = FlakyClient([t.name for t in s.tiers.values()])
    with pytest.raises(LLMError):
        asyncio.run(pipeline.run_query(ANSWERABLE, "rule", s, client.as_pool()))


def test_unconfigured_provider_is_skipped_not_counted_as_fallback(store):
    """A tier whose provider has no key is skipped silently.

    It must not be recorded as a fallback: nothing failed, the tier simply
    was not available to call.
    """
    s = load_settings()
    client = FakeClient()
    # Only the premium tier's provider has a key configured.
    premium = s.tier("premium")
    pool = pipeline.ClientPool({premium.provider: client})

    rec = asyncio.run(pipeline.run_query(ANSWERABLE, "rule", s, pool))
    assert rec["tier"] == "premium", "should skip to the only configured provider"
    assert rec["fallback_used"] is False, "a skip is not a fallback"


@pytest.mark.parametrize("limit", [1, 2])
def test_select_queries_below_band_count_does_not_crash(limit):
    """Regression: limits smaller than the number of bands used to raise
    ValueError (max() of an empty sequence in the claw-back loop)."""
    picked = telemetry.select_queries(limit)
    assert len(picked) == limit
