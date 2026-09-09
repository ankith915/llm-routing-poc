"""Waste analysis and recommendations.

Every finding is derived from stored request records, states the evidence it
rests on, and separates what was *measured* from what is *projected*. A finding
with too few observations says so instead of extrapolating from one request.
"""
import statistics
from collections import defaultdict

from backend.analytics.aggregate import _avg_q, _f, live
from backend.optimizer import pricing, tasks

MIN_SAMPLE = 3          # below this, report the observation but not a projection
SEVERITY = {"high": 3, "medium": 2, "low": 1}


def _finding(key, title, severity, monthly_usd, avoidable_usd, evidence, action, detail=None,
             sample=0, projected=True):
    return {"key": key, "title": title, "severity": severity,
            "observed_cost_usd": round(monthly_usd, 6), "avoidable_cost_usd": round(avoidable_usd, 6),
            "evidence": evidence, "recommended_action": action, "detail": detail or {},
            "sample": sample, "basis": "projected" if projected else "measured",
            "confident": sample >= MIN_SAMPLE}


def analyse(records: list, registry, policies=None) -> dict:
    rs = live(records)
    findings = []
    if not rs:
        return {"findings": [], "requests": 0, "total_avoidable_usd": 0.0,
                "note": "No requests recorded yet - run a workload first."}
    total = sum(_f(r, "cost_usd") for r in rs)
    premium = registry.tier_default("premium")

    # ---------------------------------------- 1. frontier model on easy work
    easy_frontier = [r for r in rs if r.get("tier") == "premium"
                     and (r.get("difficulty") in ("SIMPLE", "MEDIUM"))
                     and not r.get("escalated_from") and r.get("strategy") != "none"]
    if easy_frontier:
        cost = sum(_f(r, "cost_usd") for r in easy_frontier)
        alt = registry.tier_default("medium")
        would = sum(pricing.price(alt, int(_f(r, "input_tokens", 0)), int(_f(r, "output_tokens", 0)),
                                  registry=registry).total_usd for r in easy_frontier)
        q_prem, q_alt = _avg_q(easy_frontier), None
        same_task = [x for x in rs if x.get("model") == alt.id
                     and x.get("difficulty") in ("SIMPLE", "MEDIUM")]
        q_alt = _avg_q(same_task)
        findings.append(_finding(
            "frontier_on_easy_work",
            f"{len(easy_frontier)} SIMPLE/MEDIUM request(s) used the frontier model",
            "high" if cost > 0.25 * total else "medium", cost, max(0.0, cost - would),
            f"{len(easy_frontier)} of {len(rs)} requests; frontier spend ${cost:.4f} of ${total:.4f}",
            f"Route SIMPLE/MEDIUM work of these task types to {alt.label}.",
            {"alternative_model": alt.id, "alternative_cost_usd": round(would, 6),
             "frontier_avg_quality": q_prem, "alternative_avg_quality": q_alt,
             "quality_note": ("measured on this workload" if q_alt is not None
                              else "no graded requests on the alternative yet - validate before migrating"),
             "task_types": _top(easy_frontier, "task_type")},
            sample=len(easy_frontier)))

    # ---------------------------------------------- 2. repeated identical requests
    by_query = defaultdict(list)
    for r in rs:
        by_query[(r.get("tenant_id"), r.get("application_id"), (r.get("query") or "").strip().lower())].append(r)
    dupes = {k: v for k, v in by_query.items() if len(v) > 1}
    paid_dupes = sum(max(0, len([x for x in v if not x.get("cache_hit")]) - 1) for v in dupes.values())
    if paid_dupes:
        wasted = sum(sum(_f(x, "cost_usd") for x in [y for y in v if not y.get("cache_hit")][1:])
                     for v in dupes.values())
        findings.append(_finding(
            "repeated_identical_requests",
            f"{paid_dupes} identical request(s) were answered by a model more than once",
            "high" if wasted > 0.1 * total else "medium", wasted, wasted,
            f"{len(dupes)} distinct queries repeated; {paid_dupes} paid repeats",
            "Enable the exact response cache for these applications, or raise its TTL.",
            {"examples": [{"query": k[2][:90], "times": len(v)}
                          for k, v in sorted(dupes.items(), key=lambda kv: -len(kv[1]))[:5]]},
            sample=paid_dupes))

    # -------------------------------------- 3. semantic near-misses left on the table
    near = []
    for r in rs:
        for s in (r.get("trace") or []):
            if s["stage"] == "semantic_cache" and s["status"] == "miss":
                for rej in (s["detail"] or {}).get("rejected", []):
                    if "similarity" in (rej.get("reason") or "") and (rej.get("similarity") or 0) > 0:
                        near.append((rej["similarity"], r))
    if near:
        best = sorted(near, key=lambda x: -x[0])[:5]
        recoverable = sum(_f(r, "cost_usd") for _, r in best)
        findings.append(_finding(
            "semantic_cache_threshold",
            f"{len(near)} request(s) had a near-miss against the semantic cache",
            "low", recoverable, 0.0,
            f"closest similarity {best[0][0]:.3f}; the threshold rejected them",
            "Review the threshold against the measured paraphrase/distractor separation before "
            "lowering it - a wrong cache hit costs more than a cache miss.",
            {"closest": [{"similarity": round(s, 3), "query": (r.get("query") or "")[:80]}
                         for s, r in best]},
            sample=len(near)))

    # ------------------------------------------------ 4. unused output budget
    budgeted = [r for r in rs if r.get("output_budget_tokens") and r.get("output_tokens") is not None]
    if budgeted:
        unused = sum(int(_f(r, "unused_output_budget", 0)) for r in budgeted)
        share = unused / max(1, sum(int(_f(r, "output_budget_tokens", 0)) for r in budgeted))
        if share > 0.4:
            findings.append(_finding(
                "unused_output_budget",
                f"{round(100 * share)}% of the reserved output budget went unused",
                "low", 0.0, 0.0,
                f"{unused} reserved-but-unused output tokens across {len(budgeted)} requests",
                "Lower max output tokens for these task types. This does not reduce spend directly "
                "(only generated tokens are billed) but it caps the tail risk on a runaway generation.",
                {"by_task": _top(budgeted, "task_type")}, sample=len(budgeted), projected=False))

    # ------------------------------------------ 5. oversized context for the task
    trimmed = [r for r in rs if _f(r, "baseline_input_tokens", 0) > _f(r, "input_tokens", 0) > 0]
    untrimmed = [r for r in rs if r.get("context") and not (r.get("context") or {}).get("tokens_saved")
                 and not r.get("cache_hit") and r.get("strategy") in ("none", "rule", "intelligent")]
    if untrimmed:
        extra = sum(max(0, int(_f(r, "baseline_input_tokens", 0)) - int(_f(r, "input_tokens", 0)))
                    for r in untrimmed)
        would_save = sum(_f(r, "input_tokens", 0) for r in untrimmed) * 0.0
        # price the sections the optimizer would have dropped, using each record's own model
        est = 0.0
        for r in untrimmed:
            task = tasks.get(r.get("task_type"))
            keep = set(task.context_sections)
            if not keep:
                continue
            est += _f(r, "cost_usd") * 0.0
        findings.append(_finding(
            "context_not_optimised",
            f"{len(untrimmed)} request(s) sent the full keyword-gated context",
            "medium", sum(_f(r, "cost_usd") for r in untrimmed), 0.0,
            f"{len(untrimmed)} requests ran a strategy with context optimisation disabled",
            "Run these workloads through the optimizer strategy to apply task-aware section "
            "selection; the savings waterfall will then attribute the difference.",
            {"extra_tokens_vs_optimised": extra}, sample=len(untrimmed)))
        _ = would_save, est

    # ----------------------------------------------------- 6. retries and fallbacks
    fb = [r for r in rs if r.get("fallback_used")]
    if fb:
        cost = sum(_f(r, "fallback_cost_usd") for r in fb)
        injected = sum(1 for r in fb for a in (r.get("attempts") or []) if a.get("injected"))
        findings.append(_finding(
            "provider_fallbacks",
            f"{len(fb)} request(s) needed a provider fallback",
            "medium" if len(fb) > 0.1 * len(rs) else "low", cost, cost,
            f"{len(fb)} of {len(rs)} requests failed over"
            + (f" ({injected} from injected faults)" if injected else ""),
            "Check provider health and rate limits. Failed attempts are billed only when the "
            "provider returned usage; the retry latency is paid either way.",
            {"providers": _top(fb, "provider"), "injected_faults": injected}, sample=len(fb)))

    # ----------------------------------------------------- 7. escalations
    esc = [r for r in rs if r.get("escalation_count")]
    if esc:
        cost = sum(_f(r, "escalation_cost_usd") for r in esc)
        findings.append(_finding(
            "quality_escalations",
            f"{len(esc)} request(s) were escalated after failing the quality gate",
            "medium", cost, 0.0,
            f"{len(esc)} of {len(rs)} requests paid for two attempts",
            "This is the safety net working, not pure waste. If one task type dominates, raise its "
            "quality floor so it routes higher first and skips the wasted cheap attempt.",
            {"by_task": _top(esc, "task_type"), "by_model": _top(esc, "escalated_from")},
            sample=len(esc), projected=False))

    # ---------------------------------------- 8. router overhead vs savings
    overhead = sum(_f(r, "router_cost_usd") + _f(r, "embedding_cost_usd") for r in rs)
    savings = sum(_f(r, "savings_usd") for r in rs)
    if overhead > 0 and savings > 0 and overhead > 0.15 * savings:
        findings.append(_finding(
            "router_overhead",
            f"Optimizer overhead is {round(100 * overhead / savings, 1)}% of the savings it produced",
            "high" if overhead > 0.4 * savings else "medium", overhead, overhead * 0.8,
            f"${overhead:.5f} of router and embedding calls against ${savings:.5f} of savings",
            "Distil the LLM router into the local classifier (it already trains on the router's own "
            "logged decisions) and raise the confidence threshold so fewer requests reach it.",
            {"llm_router_usd": round(sum(_f(r, "router_cost_usd") for r in rs), 6),
             "embedding_usd": round(sum(_f(r, "embedding_cost_usd") for r in rs), 6)},
            sample=len(rs), projected=False))

    # ---------------------------------------- 9. batch-eligible interactive traffic
    batchable = [r for r in rs if r.get("task_type") and tasks.get(r["task_type"]).batch_eligible
                 and r.get("execution_mode") == "interactive"]
    if batchable:
        disc = registry.provider(premium.provider).batch_discount or 0.5
        cost = sum(_f(r, "cost_usd") for r in batchable)
        findings.append(_finding(
            "batch_eligible_traffic",
            f"{len(batchable)} request(s) of batch-eligible task types ran interactively",
            "medium", cost, cost * disc,
            f"{round(100 * len(batchable) / len(rs))}% of requests are task types with no human waiting",
            f"Move these to the batch lane (published discount {int(disc * 100)}% on providers that "
            f"offer one) by setting sla_class: batch on the application.",
            {"task_types": _top(batchable, "task_type"), "discount": disc}, sample=len(batchable)))

    # ---------------------------------------- 10. no provider prefix-cache reuse
    stable = [r for r in rs if int(_f(r, "input_tokens", 0)) >= 1024 and not r.get("cache_hit")]
    cached_tok = sum(int(_f(r, "cached_input_tokens", 0)) for r in stable)
    if stable and cached_tok == 0:
        by_provider = _top(stable, "provider")
        supports = [p for p in by_provider if registry.provider(p).prompt_cache]
        if supports:
            findings.append(_finding(
                "no_provider_cache_reuse",
                f"{len(stable)} request(s) over 1k input tokens reported no provider cache reuse",
                "medium", sum(_f(r, "cost_usd") for r in stable), 0.0,
                f"0 cached input tokens reported across {len(stable)} eligible requests on {supports}",
                "Keep the system prompt and stable context identical and first in the prompt so the "
                "provider's automatic prefix cache can match; verify with cached_tokens in usage.",
                {"providers": by_provider, "prompt_cache_providers": supports}, sample=len(stable)))

    findings.sort(key=lambda f: (-SEVERITY.get(f["severity"], 0), -f["avoidable_cost_usd"]))
    return {"findings": findings, "requests": len(rs),
            "total_cost_usd": round(total, 6),
            "total_avoidable_usd": round(sum(f["avoidable_cost_usd"] for f in findings), 6),
            "note": "Avoidable cost is projected from the requests actually recorded. Findings marked "
                    "confident=false rest on fewer than %d observations." % MIN_SAMPLE}


def _top(rows, key, n=6):
    out = defaultdict(int)
    for r in rows:
        out[r.get(key) or "unknown"] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:n])


# ------------------------------------------------------------- recommendations

def recommend(records: list, registry, policies) -> dict:
    """Concrete, reviewable policy changes with the measured evidence behind them.

    Nothing is applied automatically: each recommendation carries the exact
    policy patch a human can approve.
    """
    rs = live(records)
    recs = []
    if len(rs) < MIN_SAMPLE:
        return {"recommendations": [], "requests": len(rs),
                "note": f"Need at least {MIN_SAMPLE} recorded requests before recommending changes."}

    # 1. per (application, task type) frontier usage that a cheaper model matched
    groups = defaultdict(list)
    for r in rs:
        if r.get("task_type") and r.get("application_id"):
            groups[(r["application_id"], r["task_type"])].append(r)
    for (app, task), rows in groups.items():
        frontier = [r for r in rows if r.get("tier") == "premium" and not r.get("escalated_from")]
        if len(frontier) < MIN_SAMPLE or len(frontier) / len(rows) < 0.5:
            continue
        alt = registry.tier_default("medium")
        alt_rows = [r for r in rs if r.get("model") == alt.id and r.get("task_type") == task]
        q_front, q_alt = _avg_q(frontier), _avg_q(alt_rows)
        cost = sum(_f(r, "cost_usd") for r in frontier)
        would = sum(pricing.price(alt, int(_f(r, "input_tokens", 0)), int(_f(r, "output_tokens", 0)),
                                  registry=registry).total_usd for r in frontier)
        saving = cost - would
        if saving <= 0:
            continue
        if q_alt is None:
            confidence, verdict = "low", (f"No graded {alt.label} requests for {task} yet. Run the "
                                          f"evaluation workbench on this task before migrating.")
        elif q_front is not None and q_alt >= q_front - 0.2:
            confidence, verdict = "high", (f"{alt.label} scored {q_alt} against {q_front} on this task "
                                           f"({len(alt_rows)} graded) - no measurable regression.")
        else:
            confidence, verdict = "low", (f"{alt.label} scored {q_alt} against {q_front} - a "
                                          f"{round((q_front or 0) - q_alt, 2)} point drop. Do not migrate.")
        recs.append({
            "key": f"route:{app}:{task}",
            "title": f"{round(100 * len(frontier) / len(rows))}% of {tasks.get(task).label.lower()} "
                     f"traffic in {app} uses the frontier model",
            "saving_usd": round(saving, 6),
            "saving_pct": round(100 * saving / cost, 1) if cost else 0.0,
            "quality_evidence": verdict, "confidence": confidence,
            "sample": len(frontier), "graded_alternative_sample": len(alt_rows),
            "action": f"Set a task-level quality target for {task} that {alt.label} satisfies, or add "
                      f"{alt.id} to allowed_models and lower quality_target.",
            "policy_patch": {"tenant": rs[0].get("tenant_id"), "application": app,
                             "patch": {"allowed_models": [alt.id, registry.tier_default("premium").id]}},
            "apply_safe": confidence == "high",
        })

    # 2. batch lane
    batchable = [r for r in rs if r.get("task_type") and tasks.get(r["task_type"]).batch_eligible
                 and r.get("execution_mode") == "interactive"]
    if len(batchable) >= MIN_SAMPLE:
        by_app = defaultdict(list)
        for r in batchable:
            by_app[r.get("application_id")].append(r)
        for app, rows in by_app.items():
            provs = {r.get("provider") for r in rows}
            disc = max([registry.provider(p).batch_discount or 0 for p in provs] or [0])
            cost = sum(_f(r, "cost_usd") for r in rows)
            recs.append({
                "key": f"batch:{app}",
                "title": f"{len(rows)} request(s) in {app} have no human waiting",
                "saving_usd": round(cost * disc, 6), "saving_pct": round(100 * disc, 1),
                "quality_evidence": "Batch execution does not change the model or the prompt, so "
                                    "quality is unaffected; only latency changes.",
                "confidence": "high" if disc else "low", "sample": len(rows),
                "graded_alternative_sample": 0,
                "action": f"Set sla_class: batch on {app}." + ("" if disc else
                          " No provider in use publishes a batch discount, so the saving would be zero "
                          "until the workload moves to one that does."),
                "policy_patch": {"tenant": rows[0].get("tenant_id"), "application": app,
                                 "patch": {"sla_class": "batch"}},
                "apply_safe": bool(disc),
            })

    # 3. distil the router
    llm_routed = [r for r in rs if ((r.get("classification") or {}).get("rung")) == "llm_router"]
    if len(llm_routed) >= MIN_SAMPLE:
        cost = sum(_f(r, "router_cost_usd") for r in llm_routed)
        recs.append({
            "key": "distil_router",
            "title": f"{len(llm_routed)} request(s) paid an LLM router call",
            "saving_usd": round(cost, 6),
            "saving_pct": 100.0,
            "quality_evidence": "The local classifier trains on the router's own logged decisions; "
                                "leave-one-out accuracy is reported on the evaluation page.",
            "confidence": "medium", "sample": len(llm_routed), "graded_alternative_sample": 0,
            "action": "Retrain the local classifier on the logged decisions, then lower "
                      "llm_router_confidence_threshold so fewer requests reach the paid rung.",
            "policy_patch": {"tenant": None, "application": None,
                             "patch": {"classifier": {"llm_router_confidence_threshold": 0.45}}},
            "apply_safe": False,
        })

    # 4. exact cache for repeated traffic
    seen = defaultdict(int)
    for r in rs:
        seen[(r.get("application_id"), (r.get("query") or "").strip().lower())] += 1
    repeats = sum(v - 1 for v in seen.values() if v > 1)
    paid_repeats = sum(1 for r in rs if not r.get("cache_hit")
                       and seen[(r.get("application_id"), (r.get("query") or "").strip().lower())] > 1) - \
                   sum(1 for k, v in seen.items() if v > 1)
    if repeats >= MIN_SAMPLE and paid_repeats > 0:
        recs.append({
            "key": "enable_exact_cache",
            "title": f"{repeats} repeated request(s) recorded",
            "saving_usd": round(sum(_f(r, "cost_usd") for r in rs) * repeats / max(1, len(rs)), 6),
            "saving_pct": round(100 * repeats / len(rs), 1),
            "quality_evidence": "An exact cache returns the identical answer for an identical, "
                                "version-matched request; there is no quality risk.",
            "confidence": "high", "sample": repeats, "graded_alternative_sample": 0,
            "action": "Enable the exact response cache (ENABLE_EXACT_CACHE) for these applications.",
            "policy_patch": {"tenant": None, "application": None,
                             "patch": {"cache": {"exact": {"enabled": True}}}},
            "apply_safe": True,
        })

    recs.sort(key=lambda r: -r["saving_usd"])
    return {"recommendations": recs, "requests": len(rs),
            "note": "Recommendations are derived from recorded traffic. Each carries the policy patch "
                    "it would apply; none are applied automatically."}
