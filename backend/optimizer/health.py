"""Provider health, circuit breaker and fault injection.

Health is a rolling window of recent outcomes per provider. The breaker opens
after `failure_threshold` consecutive failures and stays open for
`open_seconds`; while open the router deprioritises the provider (it is still
usable as a last-resort fallback so availability wins over cost).

Chaos: `inject(provider, mode)` makes the client raise for that provider so
fallback and breaker paths can be demonstrated on real code, not a mock-up.
The ledger marks such failures `injected: true`.
"""
import time
from collections import deque
from dataclasses import dataclass, field

CHAOS_MODES = ("fail", "timeout", "rate_limit", "slow")


@dataclass
class ProviderHealth:
    provider: str
    window: deque = field(default_factory=lambda: deque(maxlen=50))
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_error: str | None = None
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))

    def record(self, ok: bool, latency_ms: int | None = None, error: str | None = None,
               threshold: int = 3) -> None:
        self.window.append(ok)
        if ok:
            self.consecutive_failures = 0
            if latency_ms is not None:
                self.latencies.append(latency_ms)
        else:
            self.consecutive_failures += 1
            self.last_error = error
            if self.consecutive_failures >= threshold and self.opened_at is None:
                self.opened_at = time.time()

    def is_open(self, open_seconds: float, now: float | None = None) -> bool:
        if self.opened_at is None:
            return False
        if (now or time.time()) - self.opened_at > open_seconds:
            self.opened_at = None          # half-open: let the next call probe
            self.consecutive_failures = 0
            return False
        return True

    @property
    def error_rate(self) -> float:
        return 0.0 if not self.window else 1 - sum(self.window) / len(self.window)

    @property
    def p50_latency_ms(self) -> int | None:
        if not self.latencies:
            return None
        s = sorted(self.latencies)
        return s[len(s) // 2]


class HealthRegistry:
    def __init__(self, failure_threshold: int = 3, open_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.providers: dict = {}
        self.chaos: dict = {}            # provider -> mode

    def get(self, provider: str) -> ProviderHealth:
        return self.providers.setdefault(provider, ProviderHealth(provider))

    def record(self, provider: str, ok: bool, latency_ms: int | None = None, error: str | None = None):
        self.get(provider).record(ok, latency_ms, error, self.failure_threshold)

    def breaker_open(self, provider: str) -> bool:
        return self.get(provider).is_open(self.open_seconds)

    def reset(self, provider: str | None = None) -> None:
        if provider:
            self.providers.pop(provider, None)
        else:
            self.providers.clear()

    # ------------------------------------------------------------- chaos
    def inject(self, provider: str, mode: str | None) -> None:
        if mode is None:
            self.chaos.pop(provider, None)
        elif mode not in CHAOS_MODES:
            raise ValueError(f"chaos mode must be one of {CHAOS_MODES}")
        else:
            self.chaos[provider] = mode

    def chaos_for(self, provider: str) -> str | None:
        return self.chaos.get(provider)

    def public(self) -> dict:
        return {"providers": {p: {
            "error_rate": round(h.error_rate, 3), "consecutive_failures": h.consecutive_failures,
            "breaker_open": h.is_open(self.open_seconds), "last_error": h.last_error,
            "p50_latency_ms": h.p50_latency_ms, "samples": len(h.window),
            "chaos": self.chaos.get(p)} for p, h in self.providers.items()},
            "chaos": dict(self.chaos), "failure_threshold": self.failure_threshold,
            "open_seconds": self.open_seconds}


_registry: HealthRegistry | None = None


def get_health() -> HealthRegistry:
    global _registry
    if _registry is None:
        _registry = HealthRegistry()
    return _registry


def reset_health() -> None:
    global _registry
    _registry = None
