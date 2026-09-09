"""Registry and the single cost calculator."""
import pytest

from backend.optimizer import pricing
from backend.optimizer.registry import build_registry, get_registry


@pytest.fixture(scope="module")
def reg():
    return get_registry()


def test_registry_is_versioned_and_dated(reg):
    assert reg.version and reg.version != "unversioned"
    assert reg.effective_date.startswith("2026-")


def test_every_tier_has_exactly_one_default(reg):
    for tier in ("cheap", "medium", "premium"):
        defaults = [m for m in reg.models.values() if m.tier == tier and m.tier_default]
        assert len(defaults) == 1, tier


def test_candidates_are_cheapest_first_and_exclude_auxiliary(reg):
    c = reg.candidates()
    costs = [m.blended_cost for m in c]
    assert costs == sorted(costs)
    assert all(m.role != "auxiliary" for m in c)
    assert all(m.enabled for m in c)


def test_measured_priors_carry_sample_size(reg):
    premium = reg.tier_default("premium")
    for d in ("SIMPLE", "MEDIUM", "HARD"):
        p = premium.prior(d)
        assert p.source == "measured" and p.n > 0


def test_assumed_priors_are_flagged(reg):
    cheap = reg.tier_default("cheap")
    assert cheap.prior("SIMPLE").measured
    assert not cheap.prior("HARD").measured


def test_price_matches_formula(reg):
    m = reg.get("gpt-4.1")
    b = pricing.price(m, 1_000_000, 1_000_000)
    assert b.total_usd == pytest.approx(m.input_cost_per_1m + m.output_cost_per_1m)
    b = pricing.price(m, 850, 240)
    assert b.total_usd == pytest.approx(850 / 1e6 * 2.0 + 240 / 1e6 * 8.0)
    assert b.price_registry_version == reg.version


def test_cached_input_is_billed_at_cached_rate(reg):
    m = reg.get("gpt-4.1")
    b = pricing.price(m, 2000, 100, cached_input_tokens=1500)
    assert b.cached_input_tokens == 1500
    assert b.input_usd == pytest.approx(500 / 1e6 * 2.0)
    assert b.cached_input_usd == pytest.approx(1500 / 1e6 * 0.5)


def test_cached_tokens_without_cached_rate_bill_as_input(reg):
    m = reg.get("openai/gpt-oss-20b")
    b = pricing.price(m, 2000, 100, cached_input_tokens=1500)
    assert b.cached_input_tokens == 0
    assert b.input_usd == pytest.approx(2000 / 1e6 * m.input_cost_per_1m)
    assert any("no cached rate" in n for n in b.notes)


def test_cached_tokens_never_exceed_input(reg):
    m = reg.get("gpt-4.1")
    b = pricing.price(m, 100, 10, cached_input_tokens=999)
    assert b.cached_input_tokens == 100 and b.input_usd == 0


def test_batch_discount_only_for_providers_that_publish_one(reg):
    openai = reg.get("gpt-4.1")
    groq = reg.get("openai/gpt-oss-20b")
    b = pricing.price(openai, 1000, 1000, execution_mode="batch")
    assert b.discount_multiplier == pytest.approx(0.5)
    assert b.total_usd == pytest.approx(b.subtotal_usd * 0.5)
    g = pricing.price(groq, 1000, 1000, execution_mode="batch")
    assert g.discount_multiplier == 1.0
    assert any("no batch discount" in n for n in g.notes)


def test_reasoning_tokens_are_reported_not_double_billed(reg):
    m = reg.get("openai/gpt-oss-120b")
    a = pricing.price(m, 1000, 300)
    b = pricing.price(m, 1000, 300, reasoning_tokens=200)
    assert a.total_usd == b.total_usd
    assert b.reasoning_tokens == 200


def test_embedding_cost(reg):
    assert pricing.embedding_usd(1_000_000) == pytest.approx(0.02)
    assert pricing.embedding_usd(0) == 0.0


def test_negative_tokens_clamp_to_zero(reg):
    m = reg.get("gpt-4.1")
    assert pricing.price(m, -5, -5).total_usd == 0.0


def test_registry_rejects_unknown_model(reg):
    with pytest.raises(KeyError):
        reg.get("nope")


def test_build_registry_from_custom_raw():
    raw = {"price_registry_version": "t.1", "effective_date": "2026-01-01",
           "providers": {"p": {"batch_discount": 0.25}},
           "models": [{"id": "m", "provider": "p", "tier": "cheap", "tier_default": True,
                       "input_cost_per_1m": 1, "output_cost_per_1m": 2,
                       "max_context_tokens": 1000, "max_latency_ms": 100}]}
    r = build_registry(raw)
    assert r.get("m").prior("HARD").source == "assumed"
    assert pricing.price(r.get("m"), 1e6, 0, execution_mode="batch", registry=r).total_usd == pytest.approx(0.75)


# ------------------------------------------------------------- attribution

def test_attribution_bars_are_non_overlapping_and_sum_exactly(reg):
    """Routing, provider cache and execution mode decompose the serving cost in
    three steps. If they overlapped, the waterfall would double-count a saving
    and still appear to reconcile."""
    from backend.optimizer import ledger, pricing
    premium = reg.tier_default("premium")
    served = reg.get("gpt-4.1-mini")
    sent_in, out, cached = 2000, 300, 1500
    list_price = pricing.price(served, sent_in, out, registry=reg).total_usd
    standard = pricing.price(served, sent_in, out, cached_input_tokens=cached, registry=reg).total_usd
    actual = pricing.price(served, sent_in, out, cached_input_tokens=cached,
                           execution_mode="batch", registry=reg).total_usd
    base = pricing.price(premium, 2600, out, registry=reg).total_usd
    led = ledger.attribute(
        reg, baseline_usd=base, baseline_source="estimated", actual_usd=actual, cache_kind=None,
        baseline_input_tokens=2600, sent_input_tokens=sent_in, output_tokens=out,
        served_model_id=served.id, served_cost_standard=standard, served_cost_actual=actual,
        reasoning_tokens_avoided=0, overhead_usd=0.0, escalation_usd=0.0, fallback_usd=0.0,
        cached_input_tokens=cached, served_cost_list_price=list_price)
    bars = {a.layer: a.usd for a in led.attribution}
    assert sum(bars.values()) == pytest.approx(led.savings_usd, abs=1e-12)
    assert bars["provider_cache"] == pytest.approx(list_price - standard, abs=1e-12)
    assert bars["execution_mode"] == pytest.approx(standard - actual, abs=1e-12)
    premium_on_sent = pricing.price(premium, sent_in, out, registry=reg).total_usd
    assert bars["routing"] == pytest.approx(premium_on_sent - list_price, abs=1e-9)
    assert bars["context"] > 0
    # every bar carries a basis a reader can check
    assert all(a.basis in ("measured", "estimated", "modeled", "calibrated") for a in led.attribution)


def test_provider_cache_bar_absent_when_nothing_was_cached(reg):
    from backend.optimizer import ledger, pricing
    served = reg.tier_default("medium")
    c = pricing.price(served, 1000, 200, registry=reg).total_usd
    led = ledger.attribute(
        reg, baseline_usd=0.01, baseline_source="estimated", actual_usd=c, cache_kind=None,
        baseline_input_tokens=1000, sent_input_tokens=1000, output_tokens=200,
        served_model_id=served.id, served_cost_standard=c, served_cost_actual=c,
        reasoning_tokens_avoided=0, overhead_usd=0.0, escalation_usd=0.0, fallback_usd=0.0,
        cached_input_tokens=0, served_cost_list_price=c)
    assert "provider_cache" not in {a.layer for a in led.attribution}
    assert sum(a.usd for a in led.attribution) == pytest.approx(led.savings_usd, abs=1e-12)
