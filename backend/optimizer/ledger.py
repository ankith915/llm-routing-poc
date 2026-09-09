"""Cost ledger and savings attribution.

The ledger is the record of what a request actually cost and what it would
have cost on the baseline (premium model, uncompressed context, no cache).
Attribution splits (baseline - actual) into bars, applied in a fixed order so
that the bars always sum to the total saving:

  1. cache          the whole baseline cost avoided (exact or semantic hit)
  2. context        premium input rate x tokens removed by the context stage
  3. routing        premium cost on the sent prompt minus the selected model's
                    cost on the same prompt (standard rate)
  4. reasoning      estimated tokens avoided by lowering the reasoning level
                    (registry measurement; labelled estimated)
  5. execution_mode batch discount actually applied to the served call
  -  overhead       router, embedding and judge-in-the-loop costs (negative)
  -  escalation     cost of attempts that failed the gate (negative)
  -  fallback       cost of attempts that failed at the provider (negative)

`baseline_source` is `measured` when a baseline record for the same query
exists in the store, `estimated` otherwise.
"""
from dataclasses import dataclass, field

from backend.optimizer import pricing
from backend.optimizer.registry import Registry


@dataclass
class Attribution:
    layer: str
    label: str
    usd: float
    basis: str            # measured | estimated | modeled
    detail: str = ""

    def public(self) -> dict:
        return {"layer": self.layer, "label": self.label, "usd": round(self.usd, 8),
                "basis": self.basis, "detail": self.detail}


LAYER_LABELS = {
    "exact_cache": "Exact cache", "semantic_cache": "Semantic cache", "context": "Context optimisation",
    "routing": "Model routing", "provider_cache": "Provider prompt cache",
    "reasoning": "Reasoning optimisation", "execution_mode": "Execution mode",
    "overhead": "Optimizer overhead", "escalation": "Quality escalations", "fallback": "Provider fallbacks",
}


@dataclass
class Ledger:
    baseline_cost_usd: float
    baseline_source: str
    actual_cost_usd: float
    attribution: list = field(default_factory=list)

    @property
    def savings_usd(self) -> float:
        return self.baseline_cost_usd - self.actual_cost_usd

    @property
    def savings_pct(self) -> float:
        return round(100 * self.savings_usd / self.baseline_cost_usd, 1) if self.baseline_cost_usd else 0.0

    def public(self) -> dict:
        return {"baseline_cost_usd": round(self.baseline_cost_usd, 8), "baseline_source": self.baseline_source,
                "actual_cost_usd": round(self.actual_cost_usd, 8), "savings_usd": round(self.savings_usd, 8),
                "savings_pct": self.savings_pct, "attribution": [a.public() for a in self.attribution]}


def baseline_cost(registry: Registry, baseline_input_tokens: int, output_tokens: int,
                  calibration: dict | None = None) -> tuple:
    """What this request would have cost on the frontier model.

    Always an estimate of the same shape - the premium price applied to the
    request's own uncompressed input tokens and the output length that was
    actually produced - so per-request comparisons are not swamped by the
    run-to-run variance in how long an answer happens to be.

    Substituting a real baseline run's dollar cost instead would look more
    honest and measure worse: two runs of the same question on the same model
    differ mainly in output length, so a single request could show a negative
    saving purely from that. Real baseline runs are used differently: they
    calibrate the estimate. `calibration` carries the ratio of true to estimated
    baseline cost observed on requests where both exist, with its sample size.

    Returns (usd, basis) where basis is "estimated" or "calibrated".
    """
    premium = registry.tier_default("premium")
    est = pricing.price(premium, baseline_input_tokens, output_tokens, registry=registry).total_usd
    if calibration and calibration.get("n", 0) >= 3 and calibration.get("factor"):
        return est * float(calibration["factor"]), "calibrated"
    return est, "estimated"


def attribute(registry: Registry, *, baseline_usd: float, baseline_source: str, actual_usd: float,
              cache_kind: str | None, baseline_input_tokens: int, sent_input_tokens: int,
              output_tokens: int, served_model_id: str | None, served_cost_standard: float,
              served_cost_actual: float, reasoning_tokens_avoided: int, overhead_usd: float,
              escalation_usd: float, fallback_usd: float, cached_input_tokens: int = 0,
              served_cost_list_price: float | None = None) -> Ledger:
    """Split (baseline - actual) into non-overlapping bars.

    The serving cost is decomposed in three steps so no saving is counted twice:

        premium on the same prompt
          - served model at list price          -> model routing
          - provider prefix-cache discount      -> provider prompt cache
          - execution-mode discount             -> execution mode

    `served_cost_list_price` is what the chosen model would have cost with no
    cached tokens; `served_cost_standard` applies the provider's cached rate;
    `served_cost_actual` applies the execution mode on top.
    """
    premium = registry.tier_default("premium")
    led = Ledger(baseline_usd, baseline_source, actual_usd)
    bars = []
    if cache_kind:
        bars.append(Attribution(f"{cache_kind}_cache", LAYER_LABELS[f"{cache_kind}_cache"], baseline_usd,
                                baseline_source, "model call avoided entirely"))
    else:
        removed = max(0, baseline_input_tokens - sent_input_tokens)
        ctx_saving = removed / 1e6 * premium.input_cost_per_1m
        if removed:
            bars.append(Attribution("context", LAYER_LABELS["context"], ctx_saving, "measured",
                                    f"{removed} input tokens removed x premium input rate"))
        premium_on_sent = pricing.price(premium, sent_input_tokens, output_tokens, registry=registry).total_usd
        list_price = served_cost_standard if served_cost_list_price is None else served_cost_list_price
        # what the premium model would have cost for exactly this prompt and this answer length
        routing_saving = premium_on_sent - list_price
        if served_model_id and served_model_id != premium.id and abs(routing_saving) > 1e-12:
            bars.append(Attribution("routing", LAYER_LABELS["routing"], routing_saving, "measured",
                                    f"premium on the same prompt ${premium_on_sent:.6f} vs {served_model_id} "
                                    f"at list price ${list_price:.6f}"))
        cache_saving = list_price - served_cost_standard
        if cached_input_tokens and abs(cache_saving) > 1e-12:
            bars.append(Attribution("provider_cache", LAYER_LABELS["provider_cache"], cache_saving,
                                    "measured",
                                    f"{cached_input_tokens} prompt tokens served from the provider's "
                                    f"own prefix cache and billed at the cached rate"))
        if reasoning_tokens_avoided > 0 and served_model_id:
            served = registry.get(served_model_id)
            r_saving = reasoning_tokens_avoided / 1e6 * served.output_cost_per_1m
            bars.append(Attribution("reasoning", LAYER_LABELS["reasoning"], r_saving, "estimated",
                                    f"~{reasoning_tokens_avoided} hidden reasoning tokens avoided "
                                    f"(registry measurement {served.reasoning_tokens_measured.get('source', '')})"))
        mode_saving = served_cost_standard - served_cost_actual
        if abs(mode_saving) > 1e-12:
            bars.append(Attribution("execution_mode", LAYER_LABELS["execution_mode"], mode_saving, "modeled",
                                    "published batch discount applied; executed synchronously in this demo"))
    if overhead_usd > 1e-12:
        bars.append(Attribution("overhead", LAYER_LABELS["overhead"], -overhead_usd, "measured",
                                "router / embedding calls"))
    if escalation_usd > 1e-12:
        bars.append(Attribution("escalation", LAYER_LABELS["escalation"], -escalation_usd, "measured",
                                "attempts rejected by the quality gate"))
    if fallback_usd > 1e-12:
        bars.append(Attribution("fallback", LAYER_LABELS["fallback"], -fallback_usd, "measured",
                                "attempts that failed at the provider"))
    # Reasoning is an estimate layered on top of measured bars; keep the sum
    # exact by folding it into the routing bar's remainder.
    total_bars = sum(b.usd for b in bars)
    drift = led.savings_usd - total_bars
    if abs(drift) > 1e-9:
        target = next((b for b in bars if b.layer == "routing"), None) or \
                 next((b for b in bars if b.layer in ("exact_cache", "semantic_cache")), None)
        if target is not None:
            target.usd += drift
        elif bars:
            bars[0].usd += drift
        else:
            bars.append(Attribution("routing", LAYER_LABELS["routing"], drift, "measured", "residual"))
    led.attribution = bars
    return led
