"""Batch experiment: run every test query through all three strategies, then grade.

The work is split into resumable *steps*. A step drains as many jobs as fit in
its time budget, persists progress, and returns. The caller (the dashboard's
poll loop, or run_experiment.py) keeps stepping until the queue is empty.

This shape is what makes the experiment survive a serverless host: no step
outlives its own request, and progress lives in the store rather than in
process memory, so the next step can land on a different instance.
"""
import asyncio
import logging
import os
import time

from backend import storage, telemetry
from backend.config.loader import Settings
from backend.evaluation.metrics import (STRATEGIES, baseline_records, comparison,
                                        compression_breakdown, quality_breakdowns)
from backend.llm.client import ClientPool
from backend.pipeline import run_query

STEP_SECONDS = float(os.getenv("EXPERIMENT_STEP_SECONDS", "50"))
MAX_ERRORS = 10

# User-facing choice -> compression modes each job is run with. "both" is the
# A/B: every query x strategy runs once uncompressed and once through Headroom.
COMPRESSION_CHOICES = {"off": ("off",), "headroom": ("headroom",), "both": ("off", "headroom")}

log = logging.getLogger(__name__)

def idle() -> dict:
    """A fresh idle state. Built per call so the lists are never shared."""
    return {"status": "idle", "completed": 0, "total": 0, "failed": 0, "started_at": None,
            "finished_at": None, "errors": [], "result": None, "pending": []}


async def _load() -> dict:
    """Stored state, backfilled with defaults so a partial record can't KeyError."""
    return {**idle(), **(await storage.get_state() or {})}


def status(state: dict) -> dict:
    """Public view of the state — the job queue is an internal detail."""
    return {k: v for k, v in state.items() if k != "pending"}


async def current() -> dict:
    return status(await _load())


async def start(settings: Settings, limit: int | None = None, fresh: bool = True,
                compressions: tuple = ("off",)) -> dict:
    # Stratified, so a short demo run still covers SIMPLE/MEDIUM/HARD.
    queries = telemetry.select_queries(limit)
    if fresh:
        await storage.clear()
    # Compression mode is the outer loop so the uncompressed set - the one the
    # headline numbers are computed from - completes first if a run is cut short.
    pending = [[tq["id"], s, mode]
               for mode in compressions for tq in queries for s in STRATEGIES]
    state = idle() | {"status": "running", "total": len(pending),
                      "started_at": time.time(), "pending": pending}
    await storage.set_state(state)
    return state


async def _finish(state: dict) -> dict:
    records = await storage.all_records()
    queries = {r["query_id"] for r in records if r.get("query_id")}
    state.update(status="done", finished_at=time.time(), pending=[], result={
        "queries": len(queries),
        "requests": len(records),
        "comparison": comparison(baseline_records(records)),
        "quality": quality_breakdowns(baseline_records(records)),
        "compression": compression_breakdown(records),
    })
    await storage.set_state(state)
    return state


async def step(settings: Settings, pool: ClientPool,
               budget_s: float = STEP_SECONDS) -> dict:
    """Run queued jobs until the time budget runs out. Returns the new state."""
    async with storage.step_lock() as acquired:
        if not acquired:
            return status(await _load())      # another step is already working

        state = await _load()
        if state["status"] != "running":
            return status(state)
        if not state["pending"]:
            return status(await _finish(state))

        by_id = {tq["id"]: tq for tq in telemetry.load_test_queries()}
        deadline = time.monotonic() + budget_s
        width = max(1, settings.experiment_concurrency)

        # Jobs queued before compression existed are 2-element; default them
        # to "off" so an in-flight run survives a deploy of this change.
        async def one(query_id: str, strategy: str, mode: str = "off") -> None:
            try:
                await run_query(by_id[query_id]["query"], strategy, settings, pool,
                                evaluate=True, test_query=by_id[query_id],
                                compression=mode)
            except Exception as e:
                # Only reached when every tier in the fallback chain failed.
                # Kept in the state for the log and the CLI, but the dashboard
                # deliberately does not render it - see frontend/index.html.
                state["failed"] += 1
                log.warning("experiment job failed: %s/%s/%s: %s", query_id, strategy, mode, e)
                if len(state["errors"]) < MAX_ERRORS:
                    state["errors"].append(f"{query_id}/{strategy}/{mode}: {str(e)[:150]}")
            finally:
                state["completed"] += 1

        while state["pending"] and time.monotonic() < deadline:
            wave, state["pending"] = state["pending"][:width], state["pending"][width:]
            await asyncio.gather(*(one(*job) for job in wave))
            await storage.set_state(state)

        if not state["pending"]:
            return status(await _finish(state))
        return status(state)


async def run(settings: Settings, pool: ClientPool, limit: int | None = None,
              fresh: bool = True, budget_s: float = float("inf"), on_progress=None,
              compressions: tuple = ("off",)) -> dict:
    """Drive the experiment to completion in-process (used by the CLI).

    Returns the final status, including `result` and any `errors`. A smaller
    budget_s just means more frequent on_progress callbacks.
    """
    await start(settings, limit, fresh, compressions)
    while True:
        s = await step(settings, pool, budget_s=budget_s)
        if on_progress:
            on_progress(s)
        if s["status"] == "done":
            return s


def format_report(result: dict) -> str:
    lines = ["=" * 49, f"{'EXPERIMENT RESULTS':^49}", "=" * 49, "",
             f"Queries per strategy{result['queries']:>25}", "",
             f"{'':18}{'Cost':>10}{'Quality':>10}{'Latency':>10}", "-" * 49]
    strategies = result["comparison"]["strategies"]
    for s in strategies:
        q = f"{s['avg_quality']:.2f}" if s["avg_quality"] else "n/a"
        lines.append(f"{s['label'][:18]:18}{'$' + format(s['total_cost_usd'], '.4f'):>10}"
                     f"{q:>10}{format(s['avg_latency_ms'] / 1000, '.1f') + 's':>10}")
    lines.append("-" * 49)
    intel = next(s for s in strategies if s["strategy"] == "intelligent")
    if intel["cost_savings_pct"] is not None:
        lines.append(f"\nIntelligent Routing Cost Saving: {intel['cost_savings_pct']}%")
    if intel["quality_retention_pct"] is not None:
        lines.append(f"Quality Retention:               {intel['quality_retention_pct']}%")
    for mode, per in (result.get("compression") or {}).get("delta", {}).items():
        lines += ["", f"Context compression: {mode} vs off (per request)", "-" * 49,
                  f"{'':18}{'Cost':>10}{'Quality':>10}{'Tokens':>10}"]
        for s in strategies:
            d = per.get(s["strategy"])
            if not d:
                continue
            c = f"{d['cost_pct']:+.1f}%" if d["cost_pct"] is not None else "n/a"
            q = f"{d['quality_delta']:+.2f}" if d["quality_delta"] is not None else "n/a"
            lines.append(f"{s['label'][:18]:18}{c:>10}{q:>10}{format(-d['tokens_saved_pct'], '+.1f') + '%':>10}")
    lines.append("=" * 49)
    return "\n".join(lines)
