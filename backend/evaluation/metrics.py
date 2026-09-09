"""Cost calculation and aggregate metrics."""
import statistics

from backend.config.loader import ModelTier

STRATEGIES = ["none", "rule", "intelligent"]
STRATEGY_LABELS = {"none": "No routing (baseline)", "rule": "Rule-based routing",
                   "intelligent": "Intelligent routing"}


def calculate_cost(tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
    input_cost = (input_tokens / 1_000_000) * tier.input_cost_per_1m
    output_cost = (output_tokens / 1_000_000) * tier.output_cost_per_1m
    return input_cost + output_cost


def percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(pct / 100 * (len(vs) - 1))))
    return float(vs[idx])


def _mean(values):
    return statistics.fmean(values) if values else 0.0


def strategy_summary(records: list, strategy: str) -> dict:
    rs = [r for r in records if r["strategy"] == strategy]
    costs = [r["cost_usd"] for r in rs]
    lats = [r["latency_ms"] for r in rs]
    quals = [r["quality_score"] for r in rs if r.get("quality_score") is not None]
    util = {}
    for r in rs:
        util[r["tier"]] = util.get(r["tier"], 0) + 1
    return {
        "strategy": strategy,
        "label": STRATEGY_LABELS.get(strategy, strategy),
        "requests": len(rs),
        "total_cost_usd": round(sum(costs), 6),
        "avg_cost_usd": round(_mean(costs), 8),
        "avg_quality": round(_mean(quals), 3) if quals else None,
        "evaluated": len(quals),
        "avg_latency_ms": round(_mean(lats)),
        "p50_latency_ms": round(percentile(lats, 50)),
        "p95_latency_ms": round(percentile(lats, 95)),
        "model_utilization": {
            k: {"count": v, "pct": round(100 * v / len(rs), 1)} for k, v in sorted(util.items())
        } if rs else {},
    }


def baseline_records(records: list) -> list:
    """Records answered with compression off - the routing-only view.

    Every headline number (savings, retention, utilisation) is computed on
    these so the routing effect is never blended with the compression effect.
    Records written before compression existed carry no key; they are baseline.
    """
    return [r for r in records if (r.get("compression") or "off") == "off"]


def comparison(records: list) -> dict:
    summaries = {s: strategy_summary(records, s) for s in STRATEGIES}
    # Savings compare *average* cost per request, not totals: a strategy with
    # a few extra (or missing) records must not look better or worse for it.
    baseline = summaries["none"]["avg_cost_usd"]
    base_q = summaries["none"]["avg_quality"]
    for s, summ in summaries.items():
        if baseline > 0 and summ["requests"] > 0:
            summ["cost_savings_pct"] = round(100 * (baseline - summ["avg_cost_usd"]) / baseline, 1)
        else:
            summ["cost_savings_pct"] = None
        if base_q and summ["avg_quality"]:
            summ["quality_retention_pct"] = round(100 * summ["avg_quality"] / base_q, 1)
        else:
            summ["quality_retention_pct"] = None
    return {"strategies": [summaries[s] for s in STRATEGIES]}


def compression_breakdown(records: list) -> dict:
    """Per-mode comparison plus, for every non-off mode, its delta against off.

    Deltas use *average* cost per request, so uneven counts between modes do
    not distort them, and context token savings come from the recorded
    before/after counts rather than an assumed ratio.
    """
    by_mode = {}
    for r in records:
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
            b, c = base[strat], cur[strat]
            if not b["requests"] or not c["requests"]:
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
    by_complexity, by_model = {}, {}
    for r in records:
        if r.get("quality_score") is None:
            continue
        cx = r.get("expected_complexity") or (r.get("routing") or {}).get("complexity") or "UNKNOWN"
        by_complexity.setdefault(cx, {}).setdefault(r["strategy"], []).append(r["quality_score"])
        by_model.setdefault(r["model"], []).append(r["quality_score"])
    return {
        "by_complexity": {
            cx: {s: round(_mean(v), 3) for s, v in per.items()}
            for cx, per in by_complexity.items()
        },
        "by_model": {
            m: {"avg_quality": round(_mean(v), 3), "evaluated": len(v)}
            for m, v in by_model.items()
        },
        "score_distribution": _score_distribution(records),
    }


def _score_distribution(records: list) -> dict:
    dist = {str(i): 0 for i in range(1, 6)}
    for r in records:
        q = r.get("quality_score")
        if q is not None:
            dist[str(int(q))] = dist.get(str(int(q)), 0) + 1
    return dist


def overview(records: list) -> dict:
    base = baseline_records(records)
    return {
        "total_requests": len(base),          # routing-only view, see baseline_records
        "all_requests": len(records),
        "total_cost_usd": round(sum(r["cost_usd"] for r in base), 6),
        "comparison": comparison(base),
        "quality": quality_breakdowns(base),
        "compression": compression_breakdown(records),
    }
