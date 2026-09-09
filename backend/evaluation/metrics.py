"""Cost calculation and aggregate metrics.

`calculate_cost` stays as the tier-level helper the older tests use; it now
delegates to optimizer.pricing so there is still exactly one place where tokens
become dollars.
"""
import statistics

from backend.config.loader import ModelTier
from backend.optimizer.registry import get_registry

STRATEGIES = ["none", "rule", "intelligent", "optimized"]
STRATEGY_LABELS = {"none": "No routing (baseline)", "rule": "Rule-based routing",
                   "intelligent": "Intelligent routing", "optimized": "Full optimizer",
                   "shadow": "Shadow mode"}


def calculate_cost(tier: ModelTier, input_tokens: int, output_tokens: int,
                   cached_input_tokens: int = 0, execution_mode: str = "interactive") -> float:
    from backend.optimizer import pricing
    reg = get_registry()
    try:
        spec = reg.get(tier.name)
    except KeyError:                       # a tier configured outside the registry
        return (input_tokens / 1e6) * tier.input_cost_per_1m + \
               (output_tokens / 1e6) * tier.output_cost_per_1m
    return pricing.price(spec, input_tokens, output_tokens, cached_input_tokens=cached_input_tokens,
                         execution_mode=execution_mode, registry=reg).total_usd


def percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(pct / 100 * (len(vs) - 1))))
    return float(vs[idx])


def _mean(values):
    return statistics.fmean(values) if values else 0.0


def served(records: list) -> list:
    """Records that actually produced an answer (not rejected, not shadow)."""
    return [r for r in records if not r.get("rejected") and r.get("mode") != "shadow"]


def strategy_summary(records: list, strategy: str) -> dict:
    rs = [r for r in records if r["strategy"] == strategy]
    costs = [r["cost_usd"] for r in rs]
    lats = [r["latency_ms"] for r in rs]
    quals = [r["quality_score"] for r in rs if r.get("quality_score") is not None]
    gated = [r for r in rs if r.get("quality_gate_passed") is not None]
    util, tasks_seen = {}, {}
    for r in rs:
        key = r.get("model") or r.get("tier") or "unknown"
        util[key] = util.get(key, 0) + 1
        if r.get("task_type"):
            tasks_seen[r["task_type"]] = tasks_seen.get(r["task_type"], 0) + 1
    tier_util = {}
    for r in rs:
        t = "cache" if r.get("cache_hit") else (r.get("tier") or "unknown")
        tier_util[t] = tier_util.get(t, 0) + 1
    successes = [r for r in rs if r.get("quality_gate_passed") is not False]
    return {
        "strategy": strategy,
        "label": STRATEGY_LABELS.get(strategy, strategy),
        "requests": len(rs),
        "total_cost_usd": round(sum(costs), 6),
        "avg_cost_usd": round(_mean(costs), 8),
        "total_baseline_cost_usd": round(sum(r.get("baseline_cost_usd") or 0 for r in rs), 6),
        "avg_quality": round(_mean(quals), 3) if quals else None,
        "evaluated": len(quals),
        "quality_pass_rate": (round(100 * sum(1 for r in gated if r["quality_gate_passed"]) / len(gated), 1)
                              if gated else None),
        "cost_per_successful_task_usd": (round(sum(costs) / len(successes), 8) if successes else None),
        "successful_tasks": len(successes),
        "avg_latency_ms": round(_mean(lats)),
        "p50_latency_ms": round(percentile(lats, 50)),
        "p95_latency_ms": round(percentile(lats, 95)),
        "cache_hit_rate": (round(100 * sum(1 for r in rs if r.get("cache_hit")) / len(rs), 1) if rs else 0.0),
        "escalation_rate": (round(100 * sum(1 for r in rs if r.get("escalation_count")) / len(rs), 1) if rs else 0.0),
        "fallback_rate": (round(100 * sum(1 for r in rs if r.get("fallback_used")) / len(rs), 1) if rs else 0.0),
        "frontier_pct": (round(100 * sum(1 for r in rs if r.get("tier") == "premium") / len(rs), 1) if rs else 0.0),
        "input_tokens": sum(r.get("input_tokens") or 0 for r in rs),
        "output_tokens": sum(r.get("output_tokens") or 0 for r in rs),
        "model_utilization": {
            k: {"count": v, "pct": round(100 * v / len(rs), 1)} for k, v in sorted(util.items())
        } if rs else {},
        "tier_utilization": {
            k: {"count": v, "pct": round(100 * v / len(rs), 1)} for k, v in sorted(tier_util.items())
        } if rs else {},
        "task_mix": tasks_seen,
    }


def baseline_records(records: list) -> list:
    """Records answered with compression off - the routing-only view.

    Every headline routing number is computed on these so the routing effect is
    never blended with the compression effect. Records written before
    compression existed carry no key; they are baseline.
    """
    return [r for r in records if (r.get("compression") or "off") == "off"]


def comparison(records: list) -> dict:
    records = served(records)
    present = [s for s in STRATEGIES if any(r["strategy"] == s for r in records)] or STRATEGIES[:3]
    summaries = {s: strategy_summary(records, s) for s in present}
    # Savings compare *average* cost per request, not totals: a strategy with a
    # few extra (or missing) records must not look better or worse for it.
    base = summaries.get("none") or {}
    baseline = base.get("avg_cost_usd") or 0.0
    base_q = base.get("avg_quality")
    for summ in summaries.values():
        if baseline > 0 and summ["requests"] > 0:
            summ["cost_savings_pct"] = round(100 * (baseline - summ["avg_cost_usd"]) / baseline, 1)
        else:
            summ["cost_savings_pct"] = None
        if base_q and summ["avg_quality"]:
            summ["quality_retention_pct"] = round(100 * summ["avg_quality"] / base_q, 1)
        else:
            summ["quality_retention_pct"] = None
        bq = base.get("cost_per_successful_task_usd")
        if bq and summ.get("cost_per_successful_task_usd"):
            summ["cost_per_task_savings_pct"] = round(
                100 * (bq - summ["cost_per_successful_task_usd"]) / bq, 1)
        else:
            summ["cost_per_task_savings_pct"] = None
    return {"strategies": [summaries[s] for s in present]}


def compression_breakdown(records: list) -> dict:
    """Per-mode comparison plus, for every non-off mode, its delta against off."""
    by_mode = {}
    for r in served(records):
        by_mode.setdefault(r.get("compression") or "off", []).append(r)
    out = {"modes": {m: comparison(rs) for m, rs in by_mode.items()}, "delta": {}}
    if "off" not in by_mode:
        return out
    base = {s["strategy"]: s for s in out["modes"]["off"]["strategies"]}
    for mode, rs in by_mode.items():
        if mode == "off":
            continue
        cur = {s["strategy"]: s for s in out["modes"][mode]["strategies"]}
        out["delta"][mode] = {}
        for strat in STRATEGIES:
            b, c = base.get(strat), cur.get(strat)
            if not b or not c or not b["requests"] or not c["requests"]:
                continue
            mine = [r for r in rs if r["strategy"] == strat]
            tb = sum(r.get("context_tokens_before") or 0 for r in mine)
            ta = sum(r.get("context_tokens_after") or 0 for r in mine)
            out["delta"][mode][strat] = {
                "cost_pct": (round(100 * (c["avg_cost_usd"] - b["avg_cost_usd"]) / b["avg_cost_usd"], 1)
                             if b["avg_cost_usd"] else None),
                "quality_delta": (round(c["avg_quality"] - b["avg_quality"], 3)
                                  if c["avg_quality"] is not None and b["avg_quality"] is not None
                                  else None),
                "tokens_saved_pct": round(100 * (tb - ta) / tb, 1) if tb else 0.0,
            }
    return out


def quality_breakdowns(records: list) -> dict:
    by_complexity, by_model, by_task = {}, {}, {}
    for r in served(records):
        if r.get("quality_score") is None:
            continue
        cx = r.get("difficulty") or r.get("expected_complexity") or "UNKNOWN"
        by_complexity.setdefault(cx, {}).setdefault(r["strategy"], []).append(r["quality_score"])
        by_model.setdefault(r.get("model") or "unknown", []).append(r["quality_score"])
        if r.get("task_type"):
            by_task.setdefault(r["task_type"], []).append(r["quality_score"])
    return {
        "by_complexity": {cx: {s: round(_mean(v), 3) for s, v in per.items()}
                          for cx, per in by_complexity.items()},
        "by_model": {m: {"avg_quality": round(_mean(v), 3), "evaluated": len(v)}
                     for m, v in by_model.items()},
        "by_task": {t: {"avg_quality": round(_mean(v), 3), "evaluated": len(v)}
                    for t, v in by_task.items()},
        "score_distribution": _score_distribution(records),
    }


def _score_distribution(records: list) -> dict:
    dist = {str(i): 0 for i in range(1, 6)}
    for r in records:
        q = r.get("quality_score")
        if q is not None:
            dist[str(int(round(q)))] = dist.get(str(int(round(q))), 0) + 1
    return dist


def overview(records: list) -> dict:
    base = baseline_records(records)
    return {
        "total_requests": len(served(base)),
        "all_requests": len(records),
        "total_cost_usd": round(sum(r["cost_usd"] for r in served(base)), 6),
        "comparison": comparison(base),
        "quality": quality_breakdowns(base),
        "compression": compression_breakdown(records),
    }
