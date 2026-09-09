"""Routing engine: the cheapest route that safely satisfies the constraints.

For every candidate model the engine records whether it is eligible and, if
not, exactly which constraint removed it; for eligible models it estimates
cost, quality and latency and states where each estimate came from. The
decision is a table plus a short list of factors - never a hidden rationale.

Selection rule (policy `frontier_preferred` false):
    minimise expected cost  s.t.  expected quality >= quality_target,
                                  expected latency <= latency_target,
                                  expected cost <= max_cost_per_request.
With `frontier_preferred` true the objective flips to maximise quality within
the cost cap - unless budget pressure is above the soft threshold, in which
case cost wins again. Quality is never relaxed for money unless the policy
sets `pressure_quality_relaxation` explicitly.
"""
from dataclasses import dataclass, field

from backend.config.loader import TIER_ORDER, TIER_RANK
from backend.optimizer import pricing, tasks
from backend.optimizer.registry import ModelSpec, Registry

STRATEGY_TIER = {"SIMPLE": "cheap", "MEDIUM": "medium", "HARD": "premium"}


@dataclass
class Candidate:
    model: ModelSpec
    eligible: bool = True
    exclusions: list = field(default_factory=list)
    expected_cost_usd: float = 0.0
    expected_quality: float = 0.0
    quality_source: str = ""
    expected_latency_ms: int = 0
    latency_source: str = ""
    meets_quality: bool = False
    meets_latency: bool = False
    meets_cost_cap: bool = False
    degraded: bool = False
    expected_output_tokens: int = 0

    def public(self) -> dict:
        return {"model": self.model.id, "label": self.model.label, "provider": self.model.provider,
                "tier": self.model.tier, "eligible": self.eligible, "exclusions": self.exclusions,
                "expected_cost_usd": round(self.expected_cost_usd, 7),
                "expected_quality": round(self.expected_quality, 2), "quality_source": self.quality_source,
                "expected_latency_ms": self.expected_latency_ms, "latency_source": self.latency_source,
                "meets_quality": self.meets_quality, "meets_latency": self.meets_latency,
                "meets_cost_cap": self.meets_cost_cap, "degraded": self.degraded,
                "expected_output_tokens": self.expected_output_tokens}


@dataclass
class RoutingDecision:
    selected: ModelSpec | None
    strategy: str
    candidates: list
    factors: list
    reason: str
    rejected_request: str | None = None      # set when policy forbids every route
    objective: str = "min_cost"
    quality_target: float = 0.0
    latency_target_ms: int = 0
    budget_pressure: float = 0.0
    fallback_chain: list = field(default_factory=list)

    def public(self) -> dict:
        return {"selected_model": self.selected.id if self.selected else None,
                "selected_tier": self.selected.tier if self.selected else None,
                "selected_provider": self.selected.provider if self.selected else None,
                "strategy": self.strategy, "objective": self.objective, "reason": self.reason,
                "rejected_request": self.rejected_request, "quality_target": self.quality_target,
                "latency_target_ms": self.latency_target_ms, "budget_pressure": round(self.budget_pressure, 3),
                "factors": self.factors, "candidates": [c.public() for c in self.candidates],
                "fallback_chain": [m.id for m in self.fallback_chain]}


def learned_quality(model_id: str, difficulty: str, learned: dict | None) -> tuple:
    """Measured quality from the ledger for (model, difficulty) when there are
    enough graded samples; None otherwise."""
    if not learned:
        return None, 0
    cell = learned.get(model_id, {}).get(difficulty)
    if cell and cell.get("n", 0) >= 3:
        return float(cell["mean"]), int(cell["n"])
    return None, 0


def estimate(model: ModelSpec, difficulty: str, task_type: str, input_tokens: int, output_budget: int,
             registry: Registry, learned: dict | None, execution_mode: str,
             learned_output: dict | None = None) -> tuple:
    q, n = learned_quality(model.id, difficulty, learned)
    if q is not None:
        quality, source = q, f"learned from {n} graded requests"
    else:
        p = model.prior(difficulty)
        quality = p.score
        source = (f"measured prior (n={p.n})" if p.measured else "assumed prior (unmeasured)")
    out_cell = (learned_output or {}).get(model.id, {}).get(task_type)
    if out_cell and out_cell.get("n", 0) >= 3:
        out_tokens = int(out_cell["mean"])
    else:
        out_tokens = max(20, int(output_budget * 0.6))
    cost = pricing.price(model, input_tokens, out_tokens, execution_mode=execution_mode,
                         registry=registry).total_usd
    if model.latency_p50_ms:
        lat, lsrc = int(model.latency_p50_ms), model.latency_source
    else:
        lat, lsrc = int(model.max_latency_ms), "registry ceiling (unmeasured)"
    return cost, quality, source, lat, lsrc, out_tokens


def fallback_chain(selected: ModelSpec, candidates: list) -> list:
    """Selected first, then more capable eligible models, then cheaper ones."""
    others = [c.model for c in candidates if c.eligible and c.model.id != selected.id]
    higher = sorted((m for m in others if m.rank > selected.rank), key=lambda m: (m.rank, m.blended_cost))
    lower = sorted((m for m in others if m.rank <= selected.rank), key=lambda m: (-m.rank, -m.blended_cost))
    return [selected, *higher, *lower]


def route(classification, policy, registry: Registry, health, *, strategy: str = "optimized",
          input_tokens: int, output_budget: int, budget_pressure: float = 0.0,
          learned: dict | None = None, learned_output: dict | None = None,
          execution_mode: str = "interactive", routing_enabled: bool = True,
          remaining_budget_usd: float | None = None) -> RoutingDecision:
    difficulty = classification.difficulty
    task = tasks.get(classification.task_type)
    factors = [f"task {task.label} / {difficulty} ({classification.rung}, "
               f"confidence {classification.confidence:.2f})"]
    q_target = float(policy.get("quality_target", 4.0))
    if task.high_stakes:
        q_target = max(q_target, 4.2)
        factors.append(f"high-stakes task: quality target raised to {q_target}")
    relax = float((policy.get("budget") or {}).get("pressure_quality_relaxation", 0.0) or 0.0)
    soft = float((policy.get("budget") or {}).get("soft_threshold", 0.8) or 0.8)
    if budget_pressure >= soft and relax > 0:
        q_target = max(1.0, q_target - relax)
        factors.append(f"budget pressure {budget_pressure:.0%}: quality target relaxed by {relax} (policy)")
    lat_target = int(policy.get("latency_target_ms", 6000))
    cost_cap = float(policy.get("max_cost_per_request_usd", 1.0))
    if remaining_budget_usd is not None and remaining_budget_usd < cost_cap:
        cost_cap = remaining_budget_usd
        factors.append(f"remaining budget ${remaining_budget_usd:.5f} is tighter than the policy cost "
                       f"cap; the cap for this request is the remaining budget")
    providers_ok = set(policy.providers_for_sensitivity())
    allowed_models = set(policy.get("allowed_models") or [])
    denied = set(policy.get("denied_models") or [])
    allow_unmeasured = bool(policy.get("allow_unmeasured_models", False))
    frontier_allowed = bool(policy.get("frontier_allowed", True))

    # --------------------------------------------- forced strategies
    forced = None
    if strategy == "none" or not routing_enabled:
        forced = registry.tier_default("premium")
        factors.append("baseline: every request goes to the premium model"
                       if strategy == "none" else "model routing flag off: premium model")
    elif strategy in ("rule", "intelligent"):
        forced = registry.tier_default(STRATEGY_TIER[difficulty])
        factors.append(f"{strategy} strategy maps {difficulty} to the {forced.tier} tier")
    elif strategy.startswith("fixed:"):
        forced = registry.get(strategy.split(":", 1)[1])
        factors.append(f"fixed model {forced.id}")

    cands = []
    pool = registry.candidates(include_disabled=forced is not None)
    if forced is not None and forced.id not in {m.id for m in pool}:
        pool.append(forced)
    for m in pool:
        c = Candidate(model=m)
        if not policy.providers_for_sensitivity():
            c.exclusions.append(f"sensitivity {policy.get('sensitivity_class')}: no external provider permitted")
        elif m.provider not in providers_ok:
            c.exclusions.append(f"provider {m.provider} not permitted for sensitivity {policy.get('sensitivity_class')}")
        if allowed_models and m.id not in allowed_models:
            c.exclusions.append("not in policy allowed_models")
        if m.id in denied:
            c.exclusions.append("in policy denied_models")
        if not frontier_allowed and m.tier == "premium":
            c.exclusions.append("frontier models not allowed by policy")
        for cap in classification.required_capabilities:
            if not m.supports(cap):
                c.exclusions.append(f"lacks capability {cap}")
        if m.max_context_tokens < input_tokens + output_budget:
            c.exclusions.append(f"context window {m.max_context_tokens} < {input_tokens + output_budget} tokens")
        if not m.enabled and forced is not m:
            c.exclusions.append("disabled in registry")
        c.expected_cost_usd, c.expected_quality, c.quality_source, c.expected_latency_ms, \
            c.latency_source, c.expected_output_tokens = estimate(
                m, difficulty, task.name, input_tokens, output_budget, registry, learned,
                execution_mode, learned_output)
        if (not allow_unmeasured and "assumed" in c.quality_source and forced is not m
                and not (learned_quality(m.id, difficulty, learned)[0] is not None)):
            c.exclusions.append(f"quality for {difficulty} is unmeasured (policy allow_unmeasured_models=false)")
        c.degraded = bool(health and health.breaker_open(m.provider))
        c.eligible = not c.exclusions
        c.meets_quality = c.expected_quality >= q_target
        c.meets_latency = c.expected_latency_ms <= lat_target
        c.meets_cost_cap = c.expected_cost_usd <= cost_cap
        cands.append(c)

    dec = RoutingDecision(None, strategy, cands, factors, "", quality_target=q_target,
                          latency_target_ms=lat_target, budget_pressure=budget_pressure)
    eligible = [c for c in cands if c.eligible]
    if forced is not None:
        fc = next(c for c in cands if c.model.id == forced.id)
        if not policy.providers_for_sensitivity():
            dec.rejected_request = "policy forbids every external provider for this sensitivity class"
            dec.reason = dec.rejected_request
            return dec
        dec.selected = forced
        dec.objective = "forced"
        dec.reason = factors[-1]
        if fc.degraded:
            factors.append(f"provider {forced.provider} circuit breaker is open; fallback chain will be used")
        dec.fallback_chain = fallback_chain(forced, cands if not eligible else cands)
        return dec

    if not eligible:
        dec.rejected_request = "no model satisfies the policy constraints: " + "; ".join(
            f"{c.model.id}: {', '.join(c.exclusions)}" for c in cands)
        dec.reason = dec.rejected_request
        return dec

    healthy = [c for c in eligible if not c.degraded] or eligible
    if len(healthy) < len(eligible):
        factors.append("providers with an open circuit breaker deprioritised: "
                       + ", ".join(c.model.provider for c in eligible if c.degraded))
    good = [c for c in healthy if c.meets_quality and c.meets_cost_cap]
    note = ""
    if not good:
        fits = [c for c in healthy if c.meets_cost_cap]
        if fits:
            best = max(fits, key=lambda c: c.expected_quality)
            note = (f"no eligible model reaches quality target {q_target}; best available "
                    f"({best.model.id} at {best.expected_quality:.2f}) chosen, quality gate will verify")
            good = [best]
        else:
            best = min(healthy, key=lambda c: c.expected_cost_usd)
            note = f"no eligible model fits the ${cost_cap} cost cap; cheapest chosen"
            good = [best]
    fast = [c for c in good if c.meets_latency]
    if not fast:
        factors.append(f"latency target {lat_target}ms unreachable for qualifying models; relaxed")
        fast = good
    prefer_frontier = bool(policy.get("frontier_preferred")) and budget_pressure < soft
    if prefer_frontier:
        chosen = max(fast, key=lambda c: (c.expected_quality, -c.expected_cost_usd))
        dec.objective = "max_quality_within_cap"
        factors.append("policy prefers the frontier model when it fits the cost cap")
    else:
        chosen = min(fast, key=lambda c: (c.expected_cost_usd, -c.expected_quality))
        dec.objective = "min_cost"
        if bool(policy.get("frontier_preferred")):
            factors.append(f"budget pressure {budget_pressure:.0%} >= {soft:.0%}: cost wins over frontier preference")
    if budget_pressure >= soft:
        factors.append(f"budget pressure {budget_pressure:.0%}: cheapest safe route enforced")
    factors.append(f"quality target {q_target} ({policy.source('quality_target')}); "
                   f"{chosen.model.id} expected {chosen.expected_quality:.2f} ({chosen.quality_source})")
    factors.append(f"expected cost ${chosen.expected_cost_usd:.5f} vs premium "
                   f"${next((c.expected_cost_usd for c in cands if c.model.tier == 'premium' and c.model.tier_default), 0):.5f}")
    pricier = [c for c in eligible if c.expected_cost_usd > chosen.expected_cost_usd and c.meets_quality]
    if pricier:
        factors.append(f"{len(pricier)} costlier model(s) also qualify; not needed")
    if note:
        factors.append(note)
    dec.selected = chosen.model
    dec.reason = (f"{chosen.model.label}: cheapest model whose expected quality "
                  f"({chosen.expected_quality:.2f}) meets the target ({q_target}) for a {difficulty} "
                  f"{task.label.lower()}" if dec.objective == "min_cost" else
                  f"{chosen.model.label}: highest expected quality within the cost cap (policy prefers frontier)")
    if note:
        dec.reason = note
    dec.fallback_chain = fallback_chain(chosen.model, cands)
    return dec


def tier_of(model: ModelSpec) -> str:
    return model.tier


__all__ = ["route", "RoutingDecision", "Candidate", "fallback_chain", "TIER_ORDER", "TIER_RANK"]
