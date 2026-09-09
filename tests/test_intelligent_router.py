import pytest

from backend.config.loader import load_settings
from backend.router import intelligent_router as ir


@pytest.fixture(scope="module")
def settings():
    return load_settings()


def test_parse_decision_extracts_json():
    raw = 'Here you go:\n{"complexity": "hard", "recommended_tier": "premium", "reason": "x"}'
    d = ir.parse_decision(raw)
    assert d["complexity"] == "HARD"
    assert d["recommended_tier"] == "PREMIUM"


def test_parse_decision_rejects_garbage():
    with pytest.raises(ValueError):
        ir.parse_decision("no json here")


def test_simple_low_quality_selects_cheapest(settings):
    decision = {"complexity": "SIMPLE", "quality_requirement": "LOW",
                "reasoning_required": False, "recommended_tier": "CHEAP",
                "latency_requirement": "LOW"}
    tier, _ = ir.select_tier(decision, settings, context_tokens=1000)
    assert tier == "cheap"


def test_hard_selects_premium(settings):
    decision = {"complexity": "HARD", "quality_requirement": "CRITICAL",
                "reasoning_required": True, "recommended_tier": "PREMIUM",
                "latency_requirement": "LOW"}
    tier, _ = ir.select_tier(decision, settings, context_tokens=1000)
    assert tier == "premium"


def test_reasoning_bumps_off_cheap(settings):
    decision = {"complexity": "SIMPLE", "quality_requirement": "LOW",
                "reasoning_required": True, "recommended_tier": "CHEAP",
                "latency_requirement": "LOW"}
    tier, _ = ir.select_tier(decision, settings, context_tokens=1000)
    assert tier == "medium"  # cheapest tier that supports reasoning requirement


def test_huge_context_forces_largest_window(settings):
    decision = {"complexity": "SIMPLE", "quality_requirement": "LOW",
                "reasoning_required": False, "recommended_tier": "CHEAP",
                "latency_requirement": "LOW"}
    tier, _ = ir.select_tier(decision, settings, context_tokens=500_000)
    assert tier == "premium"  # only tier whose window fits 500k tokens
    # when no tier's window fits at all, the largest window is forced
    tier, note = ir.select_tier(decision, settings, context_tokens=2_000_000)
    assert tier == "premium"
    assert "context" in note


def test_latency_requirement_prefers_fast_tier(settings):
    # MEDIUM complexity requires rank>=2; latency HIGH (ceiling 3000ms) excludes
    # medium (5000) and premium (10000), so the constraint is relaxed -> medium.
    decision = {"complexity": "MEDIUM", "quality_requirement": "MEDIUM",
                "reasoning_required": False, "recommended_tier": "MEDIUM",
                "latency_requirement": "HIGH"}
    tier, note = ir.select_tier(decision, settings, context_tokens=1000)
    assert tier == "medium"
    assert "relaxed" in note
