"""The only cost calculator.

Every dollar figure in the system - answer cost, router overhead, embedding
cost, batch-discounted cost, baseline estimates - goes through `price()`.
Nothing else multiplies tokens by a rate.

Components (all optional, all itemised in the returned breakdown):
  input           uncached prompt tokens x input rate
  cached_input    provider-reported cached prompt tokens x cached rate
                  (only when the registry has a cached rate; otherwise they are
                  billed as ordinary input, which is what the provider does)
  output          completion tokens x output rate (reasoning tokens are part of
                  completion tokens on every provider we bill; they are reported
                  separately for visibility, never double-counted)
  batch_discount  multiplier applied to the whole call when the execution mode
                  is batch and the provider publishes a discount
"""
from dataclasses import dataclass, field

from backend.optimizer.registry import ModelSpec, Registry, get_registry


@dataclass
class CostBreakdown:
    model_id: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    input_usd: float
    cached_input_usd: float
    output_usd: float
    subtotal_usd: float
    discount_multiplier: float
    total_usd: float
    price_registry_version: str
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (round(v, 10) if isinstance(v, float) else v) for k, v in vars(self).items()}


def price(model: ModelSpec, input_tokens: int, output_tokens: int, *,
          cached_input_tokens: int = 0, reasoning_tokens: int = 0,
          execution_mode: str = "interactive", registry: Registry | None = None) -> CostBreakdown:
    registry = registry or get_registry()
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cached = max(0, min(int(cached_input_tokens), input_tokens))
    notes = []
    if cached and model.cached_input_cost_per_1m is None:
        notes.append("provider reported cached tokens but the registry has no cached rate; billed as input")
        cached = 0
    uncached = input_tokens - cached
    input_usd = uncached / 1e6 * model.input_cost_per_1m
    cached_usd = cached / 1e6 * (model.cached_input_cost_per_1m or 0.0)
    output_usd = output_tokens / 1e6 * model.output_cost_per_1m
    subtotal = input_usd + cached_usd + output_usd
    mult = 1.0
    if execution_mode in ("batch", "offline"):
        disc = registry.provider(model.provider).batch_discount
        if disc:
            mult = 1.0 - float(disc)
            notes.append(f"{model.provider} batch discount {int(disc * 100)}% applied (modeled; executed synchronously)")
        else:
            notes.append(f"{model.provider} publishes no batch discount; billed at standard rate")
    return CostBreakdown(
        model_id=model.id, input_tokens=input_tokens, cached_input_tokens=cached,
        output_tokens=output_tokens, reasoning_tokens=max(0, int(reasoning_tokens)),
        input_usd=input_usd, cached_input_usd=cached_usd, output_usd=output_usd,
        subtotal_usd=subtotal, discount_multiplier=mult, total_usd=subtotal * mult,
        price_registry_version=registry.version, notes=notes)


def price_usd(model: ModelSpec, input_tokens: int, output_tokens: int, **kw) -> float:
    return price(model, input_tokens, output_tokens, **kw).total_usd


def embedding_usd(tokens: int, embedding_id: str | None = None,
                  registry: Registry | None = None) -> float:
    registry = registry or get_registry()
    spec = registry.embedding(embedding_id)
    if spec is None:
        return 0.0
    return max(0, int(tokens)) / 1e6 * spec.input_cost_per_1m


def by_model_id(model_id: str, input_tokens: int, output_tokens: int, **kw) -> CostBreakdown:
    registry = kw.get("registry") or get_registry()
    return price(registry.get(model_id), input_tokens, output_tokens, **kw)
