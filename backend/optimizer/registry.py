"""Model registry: every candidate model with prices, capabilities and priors.

Built from config/models.yaml. The registry is the single source for
  - what a model costs (consumed only through optimizer.pricing),
  - what it can do (tools, structured output, reasoning controls, context),
  - how good it is expected to be per difficulty band, and *why we think so*
    (measured from graded runs in this repo, or an explicitly assumed prior).
"""
from dataclasses import dataclass, field

from backend.config.loader import TIER_RANK, load_raw

DIFFICULTIES = ("SIMPLE", "MEDIUM", "HARD")


@dataclass(frozen=True)
class QualityPrior:
    score: float
    source: str            # measured | assumed | learned
    n: int = 0

    @property
    def measured(self) -> bool:
        return self.source in ("measured", "learned") and self.n > 0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    provider: str
    tier: str
    tier_default: bool
    role: str
    input_cost_per_1m: float
    output_cost_per_1m: float
    cached_input_cost_per_1m: float | None
    max_context_tokens: int
    max_latency_ms: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_source: str
    capabilities: dict
    reasoning_default: str | None
    reasoning_tokens_measured: dict
    params: dict
    quality_priors: dict            # difficulty -> QualityPrior
    enabled: bool

    @property
    def rank(self) -> int:
        return TIER_RANK[self.tier]

    @property
    def blended_cost(self) -> float:
        return 0.75 * self.input_cost_per_1m + 0.25 * self.output_cost_per_1m

    def supports(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))

    def prior(self, difficulty: str) -> QualityPrior:
        return self.quality_priors.get(difficulty) or self.quality_priors.get("MEDIUM") \
            or QualityPrior(score=3.0, source="assumed")

    def public(self) -> dict:
        return {
            "id": self.id, "label": self.label, "provider": self.provider, "tier": self.tier,
            "tier_default": self.tier_default, "role": self.role, "enabled": self.enabled,
            "input_cost_per_1m": self.input_cost_per_1m,
            "output_cost_per_1m": self.output_cost_per_1m,
            "cached_input_cost_per_1m": self.cached_input_cost_per_1m,
            "max_context_tokens": self.max_context_tokens,
            "max_latency_ms": self.max_latency_ms,
            "latency": {"p50_ms": self.latency_p50_ms, "p95_ms": self.latency_p95_ms,
                        "source": self.latency_source},
            "capabilities": self.capabilities,
            "reasoning_default": self.reasoning_default,
            "reasoning_tokens_measured": self.reasoning_tokens_measured,
            "quality_priors": {d: {"score": p.score, "source": p.source, "n": p.n}
                               for d, p in self.quality_priors.items()},
        }


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    prompt_cache: bool
    batch_discount: float | None
    note: str = ""


@dataclass(frozen=True)
class EmbeddingSpec:
    id: str
    provider: str
    input_cost_per_1m: float
    dimensions: int


@dataclass
class Registry:
    version: str
    effective_date: str
    models: dict = field(default_factory=dict)       # id -> ModelSpec
    providers: dict = field(default_factory=dict)    # id -> ProviderSpec
    embeddings: dict = field(default_factory=dict)   # id -> EmbeddingSpec

    def get(self, model_id: str) -> ModelSpec:
        try:
            return self.models[model_id]
        except KeyError:
            raise KeyError(f"model '{model_id}' is not in the registry (version {self.version})")

    def candidates(self, include_disabled: bool = False, roles=("workhorse", "balanced", "frontier")) -> list:
        """Answer candidates, cheapest first."""
        return sorted((m for m in self.models.values()
                       if (m.enabled or include_disabled) and m.role in roles),
                      key=lambda m: (m.blended_cost, m.id))

    def tier_default(self, tier: str) -> ModelSpec:
        return next(m for m in self.models.values() if m.tier == tier and m.tier_default)

    def provider(self, provider_id: str) -> ProviderSpec:
        return self.providers.get(provider_id) or ProviderSpec(provider_id, provider_id, False, None)

    def embedding(self, embedding_id: str | None = None) -> EmbeddingSpec | None:
        if embedding_id:
            return self.embeddings.get(embedding_id)
        return next(iter(self.embeddings.values()), None)

    def public(self) -> dict:
        return {
            "price_registry_version": self.version,
            "effective_date": self.effective_date,
            "providers": {k: vars(v) for k, v in self.providers.items()},
            "models": [m.public() for m in sorted(self.models.values(), key=lambda m: m.blended_cost)],
            "embeddings": {k: vars(v) for k, v in self.embeddings.items()},
        }


def _priors(raw: dict) -> dict:
    out = {}
    for d in DIFFICULTIES:
        p = (raw or {}).get(d)
        if p:
            out[d] = QualityPrior(score=float(p["score"]), source=str(p.get("source", "assumed")),
                                  n=int(p.get("n", 0)))
    return out


def build_registry(raw: dict | None = None) -> Registry:
    raw = raw or load_raw()
    reg = Registry(version=str(raw.get("price_registry_version", "unversioned")),
                   effective_date=str(raw.get("effective_date", "")))
    for pid, p in (raw.get("providers") or {}).items():
        reg.providers[pid] = ProviderSpec(
            id=pid, label=p.get("label", pid), prompt_cache=bool(p.get("prompt_cache")),
            batch_discount=p.get("batch_discount"), note=p.get("rate_limit_note", ""))
    for m in raw["models"]:
        lp = m.get("latency_profile") or {}
        reg.models[m["id"]] = ModelSpec(
            id=m["id"], label=m.get("label", m["id"]), provider=m["provider"], tier=m["tier"],
            tier_default=bool(m.get("tier_default")), role=m.get("role", "workhorse"),
            input_cost_per_1m=float(m["input_cost_per_1m"]),
            output_cost_per_1m=float(m["output_cost_per_1m"]),
            cached_input_cost_per_1m=m.get("cached_input_cost_per_1m"),
            max_context_tokens=int(m["max_context_tokens"]),
            max_latency_ms=int(m["max_latency_ms"]),
            latency_p50_ms=lp.get("p50_ms"), latency_p95_ms=lp.get("p95_ms"),
            latency_source=str(lp.get("source", "unmeasured")),
            capabilities=dict(m.get("capabilities") or {}),
            reasoning_default=m.get("reasoning_default"),
            reasoning_tokens_measured=dict(m.get("reasoning_tokens_measured") or {}),
            params=dict(m.get("params") or {}),
            quality_priors=_priors(m.get("quality_priors")),
            enabled=bool(m.get("enabled", True)),
        )
    for e in raw.get("embeddings") or []:
        reg.embeddings[e["id"]] = EmbeddingSpec(
            id=e["id"], provider=e["provider"], input_cost_per_1m=float(e["input_cost_per_1m"]),
            dimensions=int(e.get("dimensions", 0)))
    return reg


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
