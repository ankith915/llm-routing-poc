"""Budgets: soft/hard limits per scope and window, reservation, reconciliation.

Scopes: tenant, application (tenant/app). Windows: daily, monthly. Spend is
kept in the store's kv namespace so it survives a serverless instance and is
shared between them.

Pressure = spent / limit for the tightest scope. The router reads pressure and
prefers cheaper eligible routes above the soft threshold. A hard limit rejects
the request *before* execution, based on the estimated cost, and the ledger
records the rejection - a budget is a control, not a report.
"""
import datetime
from dataclasses import dataclass, field

from backend import storage

WINDOWS = ("daily", "monthly")


def _period(window: str, now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime("%Y-%m-%d") if window == "daily" else now.strftime("%Y-%m")


def _key(scope: str, window: str, period: str) -> str:
    return f"budget:{scope}:{window}:{period}"


@dataclass
class BudgetStatus:
    scope: str
    window: str
    period: str
    limit_usd: float | None
    spent_usd: float
    reserved_usd: float

    @property
    def used_usd(self) -> float:
        return self.spent_usd + self.reserved_usd

    @property
    def pressure(self) -> float:
        if not self.limit_usd:
            return 0.0
        return min(2.0, self.used_usd / self.limit_usd)

    @property
    def remaining_usd(self) -> float | None:
        return None if self.limit_usd is None else max(0.0, self.limit_usd - self.used_usd)

    def public(self) -> dict:
        return {"scope": self.scope, "window": self.window, "period": self.period,
                "limit_usd": self.limit_usd, "spent_usd": round(self.spent_usd, 6),
                "reserved_usd": round(self.reserved_usd, 6), "remaining_usd": self.remaining_usd,
                "pressure": round(self.pressure, 3)}


@dataclass
class BudgetDecision:
    allowed: bool
    pressure: float
    reason: str
    statuses: list = field(default_factory=list)
    tightest: BudgetStatus | None = None
    remaining_usd: float | None = None

    def public(self) -> dict:
        return {"allowed": self.allowed, "pressure": round(self.pressure, 3), "reason": self.reason,
                "remaining_usd": self.remaining_usd,
                "budgets": [s.public() for s in self.statuses]}


def scopes_for(tenant_id: str, application_id: str) -> list:
    return [f"tenant:{tenant_id}", f"app:{tenant_id}/{application_id}"]


def limits_for(policy_store, tenant_id: str, application_id: str) -> dict:
    """scope -> {window: limit}.

    Limits come from the *resolved* policy, not from the raw config, so a
    runtime override or a per-request layer changes the limit that is actually
    enforced. Reading the raw documents here would let the policy engine and the
    budget engine disagree about the same number.
    """
    app_budget = (policy_store.resolve(tenant_id, application_id).get("budget") or {})
    # The tenant scope is resolved without the application layer, so an
    # application's own limit cannot raise or lower its tenant's ceiling.
    tenant_budget = (policy_store.resolve(tenant_id, "__tenant__").get("budget") or {})
    return {f"tenant:{tenant_id}": {w: tenant_budget.get(f"{w}_usd") for w in WINDOWS},
            f"app:{tenant_id}/{application_id}": {w: app_budget.get(f"{w}_usd") for w in WINDOWS}}


async def status(policy_store, tenant_id: str, application_id: str, now=None) -> list:
    out = []
    for scope, lims in limits_for(policy_store, tenant_id, application_id).items():
        for w in WINDOWS:
            period = _period(w, now)
            v = await storage.kv_get(_key(scope, w, period)) or {}
            out.append(BudgetStatus(scope, w, period, lims.get(w),
                                    float(v.get("spent", 0.0)), float(v.get("reserved", 0.0))))
    return out


async def check_and_reserve(policy_store, tenant_id: str, application_id: str,
                            floor_usd: float, soft_threshold: float = 0.8,
                            now=None, reserve_usd: float | None = None) -> BudgetDecision:
    """Admit or reject a request, and reserve while it is in flight.

    `floor_usd` is the cheapest the request could possibly be served for. A hard
    limit rejects only when even that does not fit: rejecting on the premium
    model's worst case would turn every budget squeeze into an outage when a
    cheaper route was available. `reserve_usd` (the worst case) is what is held
    while the request runs, so concurrent traffic cannot collectively overshoot.
    """
    reserve_usd = floor_usd if reserve_usd is None else reserve_usd
    statuses = await status(policy_store, tenant_id, application_id, now)
    limited = [s for s in statuses if s.limit_usd]
    tightest = max(limited, key=lambda s: s.pressure) if limited else None
    pressure = tightest.pressure if tightest else 0.0
    remaining = min((s.remaining_usd for s in limited), default=None)
    for s in limited:
        if s.used_usd + floor_usd > s.limit_usd:
            return BudgetDecision(False, s.pressure,
                                  f"hard budget: {s.scope} {s.window} is at ${s.used_usd:.4f} of "
                                  f"${s.limit_usd:.2f}; even the cheapest eligible route "
                                  f"(${floor_usd:.5f}) does not fit", statuses, s,
                                  remaining_usd=s.remaining_usd)
    # Reserve no more than what is actually left, so a large worst case cannot
    # lock out traffic that will in fact be served cheaply.
    hold = reserve_usd if remaining is None else min(reserve_usd, max(floor_usd, remaining))
    for s in limited:
        await storage.kv_update(_key(s.scope, s.window, s.period),
                                lambda v: {**v, "reserved": float(v.get("reserved", 0.0)) + hold})
        s.reserved_usd += hold
    reason = ("no budget configured" if not limited else
              f"within budget; pressure {pressure:.0%}" +
              (f"; ${remaining:.4f} left, so the router's cost cap tightens to match"
               if remaining is not None and pressure >= soft_threshold else ""))
    d = BudgetDecision(True, pressure, reason, statuses, tightest, remaining_usd=remaining)
    d.hold_usd = hold
    return d


async def reconcile(policy_store, tenant_id: str, application_id: str,
                    estimated_usd: float, actual_usd: float, now=None) -> None:
    """Release the reservation and book the actual spend."""
    for s in await status(policy_store, tenant_id, application_id, now):
        if not s.limit_usd:
            continue
        await storage.kv_update(
            _key(s.scope, s.window, s.period),
            lambda v: {**v, "reserved": max(0.0, float(v.get("reserved", 0.0)) - estimated_usd),
                       "spent": float(v.get("spent", 0.0)) + actual_usd})


async def book(policy_store, tenant_id: str, application_id: str, actual_usd: float, now=None) -> None:
    """Book spend with no prior reservation (baseline / shadow / cache paths)."""
    await reconcile(policy_store, tenant_id, application_id, 0.0, actual_usd, now)


async def reset(tenant_id: str | None = None) -> int:
    n = 0
    for key in await storage.kv_keys("budget:"):
        if tenant_id is None or f":tenant:{tenant_id}:" in key or f":app:{tenant_id}/" in key:
            await storage.kv_delete(key)
            n += 1
    return n
