"""Load model tier configuration from models.yaml plus environment settings.

Model names, providers and prices live in config/models.yaml so they can be
swapped without touching application code. Two providers are supported:
  groq       - Groq's OpenAI-compatible API (cheap/medium tiers)
  openai     - the OpenAI API directly (premium tier, router, judge)
  openrouter - still supported, but no longer used by the shipped config
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
    pricing_note: str = ""        # e.g. free endpoint costed at paid-variant price
    params: tuple = ()            # extra request fields, as sorted (key, value) pairs

    @property
    def request_params(self) -> dict:
        """Provider request fields to merge into the chat payload."""
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
    tiers: dict                   # key -> ModelTier
    router: AuxModel
    evaluator: AuxModel
    answer_params: dict
    api_keys: dict                # provider -> key ('' if unset)
    base_urls: dict               # provider -> base url
    experiment_concurrency: int
    demo_query_count: int         # queries used by a "quick" demo run

    def tier(self, key: str) -> ModelTier:
        return self.tiers[key]

    def tiers_by_cost(self) -> list:
        return sorted(self.tiers.values(), key=lambda t: t.blended_cost)


def _aux(raw: dict, env_model_var: str) -> AuxModel:
    return AuxModel(
        name=os.getenv(env_model_var, raw.get("model", "gpt-4o-mini")),
        provider=raw.get("provider", DEFAULT_PROVIDER),
        input_cost_per_1m=float(raw.get("input_cost_per_1m_tokens", 0)),
        output_cost_per_1m=float(raw.get("output_cost_per_1m_tokens", 0)),
        params={k: raw[k] for k in ("temperature", "max_tokens") if k in raw},
    )


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or (CONFIG_DIR / "models.yaml")
    raw = yaml.safe_load(path.read_text())
    tiers = {}
    for key, m in raw["models"].items():
        provider = m.get("provider", DEFAULT_PROVIDER)
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider '{provider}' for tier '{key}'")
        tiers[key] = ModelTier(
            key=key,
            name=m["name"],
            provider=provider,
            input_cost_per_1m=float(m["input_cost_per_1m_tokens"]),
            output_cost_per_1m=float(m["output_cost_per_1m_tokens"]),
            max_latency_ms=int(m["max_latency_ms"]),
            max_context_tokens=int(m["max_context_tokens"]),
            pricing_note=m.get("pricing_note", ""),
            params=tuple(sorted((m.get("params") or {}).items())),
        )
    return Settings(
        tiers=tiers,
        router=_aux(raw.get("router", {}), "ROUTER_MODEL"),
        evaluator=_aux(raw.get("evaluator", {}), "EVALUATOR_MODEL"),
        answer_params=raw.get("answer", {}),
        api_keys={
            "groq": os.getenv("GROQ_API_KEY", ""),
            "openai": os.getenv("OPENAI_API_KEY", ""),
            "openrouter": os.getenv("OPENROUTER_API_KEY", ""),
        },
        base_urls={
            "groq": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            "openai": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            "openrouter": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        },
        experiment_concurrency=int(os.getenv("EXPERIMENT_CONCURRENCY", "2")),
        demo_query_count=int(os.getenv(
            "DEMO_QUERY_COUNT",
            raw.get("experiment", {}).get("demo_query_count", 18))),
    )
