import pytest

from backend.config.loader import load_settings
from backend.evaluation import metrics


@pytest.fixture(scope="module")
def settings():
    return load_settings()


def test_cost_calculation_matches_formula(settings):
    premium = settings.tier("premium")  # $3 in / $15 out per 1M
    cost = metrics.calculate_cost(premium, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(premium.input_cost_per_1m + premium.output_cost_per_1m)
    cost = metrics.calculate_cost(premium, 850, 240)
    expected = (850 / 1e6) * premium.input_cost_per_1m + (240 / 1e6) * premium.output_cost_per_1m
    assert cost == pytest.approx(expected)


def test_cheap_tier_is_cheapest(settings):
    ordered = settings.tiers_by_cost()
    assert [t.key for t in ordered] == ["cheap", "medium", "premium"]


def test_percentiles():
    vals = list(range(1, 101))
    assert metrics.percentile(vals, 50) == 50 or metrics.percentile(vals, 50) == 51
    assert metrics.percentile(vals, 95) >= 94
    assert metrics.percentile([], 95) == 0.0


def _record(strategy, tier, cost, latency, quality, complexity="SIMPLE",
            model="m"):
    return {"strategy": strategy, "tier": tier, "cost_usd": cost, "latency_ms": latency,
            "quality_score": quality, "expected_complexity": complexity, "model": model,
            "routing": {"complexity": complexity}}


def test_comparison_savings_and_retention():
    records = [
        _record("none", "premium", 0.10, 4000, 5),
        _record("none", "premium", 0.10, 4200, 5),
        _record("intelligent", "cheap", 0.01, 800, 4),
        _record("intelligent", "medium", 0.02, 1500, 5),
        _record("rule", "cheap", 0.02, 900, 4),
        _record("rule", "medium", 0.03, 1600, 4),
    ]
    comp = metrics.comparison(records)
    by = {s["strategy"]: s for s in comp["strategies"]}
    assert by["none"]["total_cost_usd"] == pytest.approx(0.20)
    assert by["intelligent"]["cost_savings_pct"] == pytest.approx(85.0)
    assert by["intelligent"]["quality_retention_pct"] == pytest.approx(90.0)
    assert by["none"]["cost_savings_pct"] == pytest.approx(0.0)


def test_model_utilization_percentages():
    records = [_record("intelligent", t, 0.01, 100, 5) for t in
               ["cheap"] * 7 + ["medium"] * 2 + ["premium"]]
    summ = metrics.strategy_summary(records, "intelligent")
    assert summ["model_utilization"]["cheap"]["pct"] == 70.0
    assert summ["model_utilization"]["premium"]["pct"] == 10.0


def test_quality_breakdowns():
    records = [
        _record("none", "premium", 0.1, 100, 5, "HARD"),
        _record("intelligent", "cheap", 0.01, 100, 4, "HARD"),
        _record("intelligent", "cheap", 0.01, 100, None, "SIMPLE"),
    ]
    q = metrics.quality_breakdowns(records)
    assert q["by_complexity"]["HARD"]["none"] == 5
    assert q["by_complexity"]["HARD"]["intelligent"] == 4
    assert "SIMPLE" not in q["by_complexity"]  # unevaluated records excluded
    assert q["score_distribution"]["5"] == 1


def test_tier_providers_configured(settings):
    assert settings.tier("cheap").provider == "groq"
    assert settings.tier("medium").provider == "groq"
    assert settings.tier("premium").provider == "openai"
    assert settings.router.provider in ("groq", "openai")
    assert settings.router.input_cost_per_1m > 0  # router overhead is priced


def test_every_tier_is_priced_for_real(settings):
    """No tier may rely on a free endpoint costed at a reference price.

    Groq bills real money at published rates, so every number in the cost
    comparison is actual spend and needs no asterisk in the dashboard.
    """
    for key in ("cheap", "medium", "premium"):
        t = settings.tier(key)
        assert t.input_cost_per_1m > 0 and t.output_cost_per_1m > 0
        assert not t.name.endswith(":free")
        assert not t.pricing_note, "real-priced tiers need no pricing caveat"


def test_tiers_are_ordered_cheap_to_premium(settings):
    costs = [settings.tier(k).blended_cost for k in ("cheap", "medium", "premium")]
    assert costs == sorted(costs), "tier ranks must track real cost order"


def test_reasoning_models_pin_low_reasoning_effort(settings):
    """gpt-oss models truncate their visible answer without this - see models.yaml."""
    for key in ("cheap", "medium"):
        t = settings.tier(key)
        if "gpt-oss" in t.name:
            assert t.request_params.get("reasoning_effort") == "low"


def test_cost_savings_is_per_request_not_total():
    """Regression: an extra baseline record must not inflate the saving.

    Two baseline records at $0.010 and one routed record at $0.005 is a 50%
    per-request saving. Comparing totals would report 75%.
    """
    from backend.evaluation.metrics import comparison
    rec = lambda strat, cost: {"strategy": strat, "cost_usd": cost, "latency_ms": 1,
                               "quality_score": 5, "tier": "premium", "model": "m"}
    recs = [rec("none", 0.010), rec("none", 0.010), rec("intelligent", 0.005)]
    by = {s["strategy"]: s for s in comparison(recs)["strategies"]}
    assert by["intelligent"]["cost_savings_pct"] == 50.0
