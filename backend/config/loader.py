"""Settings: the model registry (config/models.yaml) plus environment.

`Settings.tiers` keeps the cheap/medium/premium view the routing strategies
and the experiment use (one default model per tier). The full registry, with
every candidate model and its capabilities, lives in
backend.optimizer.registry and is built from the same file.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TIER_ORDER = ["cheap", "medium", "premium"]
TIER_RANK = {"cheap": 1, "medium": 2, "premium": 3}
PROVIDERS = ("groq", "openai", "openrouter")
DEFAULT_PROVIDER = "groq"


@dataclass(frozen=True)
class ModelTier:
    key: str                      # cheap | medium | premium
    name: str                     # model id at its provider
    provider: str                 # groq | openai | openrouter
    input_cost_per_1m: float
    output_cost_per_1m: float
    max_latency_ms: int
    max_context_tokens: int
    pricing_note: str = ""
    params: tuple = ()            # extra request fields, as sorted (key, value) pairs
    cached_input_cost_per_1m: float | None = None

    @property
    def request_params(self) -> dict:
        return dict(self.params)

    @property
    def rank(self) -> int:
        return TIER_RANK[self.key]

    @property
    def blended_cost(self) -> float:
        """Rough single number for 'cheapest first' ordering (3:1 in/out mix)."""
        return 0.75 * self.input_cost_per_1m + 0.25 * self.output_cost_per_1m


@dataclass(frozen=True)
class AuxModel:
    """Router / evaluator model config."""
    name: str
    provider: str
    input_cost_per_1m: float = 0.0
    output_cost_per_1m: float = 0.0
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    tiers: dict                   # key -> ModelTier (the tier-default models)
    router: AuxModel
    evaluator: AuxModel
    answer_params: dict
    api_keys: dict                # provider -> key ('' if unset)
    base_urls: dict               # provider -> base url
    experiment_concurrency: int
    demo_query_count: int
    raw: dict = field(default_factory=dict, compare=False, repr=False)
    price_registry_version: str = "unversioned"

    def tier(self, key: str) -> ModelTier:
        return self.tiers[key]

    def tiers_by_cost(self) -> list:
        return sorted(self.tiers.values(), key=lambda t: t.blended_cost)


def _price_of(raw: dict, model_id: str) -> tuple:
    for m in raw.get("models", []):
        if m["id"] == model_id:
            return float(m["input_cost_per_1m"]), float(m["output_cost_per_1m"])
    return 0.0, 0.0


def _aux(raw: dict, section: dict, env_model_var: str) -> AuxModel:
    name = os.getenv(env_model_var, section.get("model", "gpt-4o-mini"))
    inp, out = _price_of(raw, name)
    return AuxModel(
        name=name,
        provider=section.get("provider", DEFAULT_PROVIDER),
        input_cost_per_1m=float(section.get("input_cost_per_1m_tokens", inp)),
        output_cost_per_1m=float(section.get("output_cost_per_1m_tokens", out)),
        params={k: section[k] for k in ("temperature", "max_tokens") if k in section},
    )


def load_raw(config_path: Path | None = None) -> dict:
    path = config_path or (CONFIG_DIR / "models.yaml")
    return yaml.safe_load(path.read_text())


def load_settings(config_path: Path | None = None) -> Settings:
    raw = load_raw(config_path)
    tiers = {}
    for m in raw["models"]:
        if not m.get("tier_default"):
            continue
        provider = m.get("provider", DEFAULT_PROVIDER)
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider '{provider}' for model '{m['id']}'")
        key = m["tier"]
        if key in tiers:
            raise ValueError(f"two tier_default models for tier '{key}'")
        tiers[key] = ModelTier(
            key=key,
            name=m["id"],
            provider=provider,
            input_cost_per_1m=float(m["input_cost_per_1m"]),
            output_cost_per_1m=float(m["output_cost_per_1m"]),
            max_latency_ms=int(m["max_latency_ms"]),
            max_context_tokens=int(m["max_context_tokens"]),
            pricing_note=m.get("pricing_note", ""),
            params=tuple(sorted((m.get("params") or {}).items())),
            cached_input_cost_per_1m=m.get("cached_input_cost_per_1m"),
        )
    missing = [t for t in TIER_ORDER if t not in tiers]
    if missing:
        raise ValueError(f"models.yaml needs a tier_default model for: {missing}")
    providers = raw.get("providers", {})

    def base_url(p):
        cfg = providers.get(p, {})
        return os.getenv(cfg.get("base_url_env", f"{p.upper()}_BASE_URL"),
                         cfg.get("default_base_url", ""))

    def api_key(p):
        cfg = providers.get(p, {})
        return os.getenv(cfg.get("api_key_env", f"{p.upper()}_API_KEY"), "")

    return Settings(
        tiers=tiers,
        router=_aux(raw, raw.get("router", {}), "ROUTER_MODEL"),
        evaluator=_aux(raw, raw.get("evaluator", {}), "EVALUATOR_MODEL"),
        answer_params=raw.get("answer", {}),
        api_keys={p: api_key(p) for p in PROVIDERS},
        base_urls={p: base_url(p) for p in PROVIDERS},
        experiment_concurrency=int(os.getenv("EXPERIMENT_CONCURRENCY", "2")),
        demo_query_count=int(os.getenv(
            "DEMO_QUERY_COUNT",
            raw.get("experiment", {}).get("demo_query_count", 18))),
        raw=raw,
        price_registry_version=str(raw.get("price_registry_version", "unversioned")),
    )
