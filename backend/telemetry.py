"""Load the simulated IT-ops dataset and build query context.

Context depends only on the query text (and, for the optimizer, the task
type), so every routing strategy sees identical input for a given query.

`build_sections` returns every context section the corpus can supply for a
query, keyed by name; `assemble` joins a chosen subset. `build_context` is the
baseline: what a plain deployment would send - every section the query's
keywords call for. The optimizer's context stage starts from the same sections
and keeps only what the task type needs, recording the difference.
"""
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

SECTION_ORDER = ("metrics", "incidents", "logs", "runbooks", "tickets", "schema")


@lru_cache(maxsize=1)
def load_dataset() -> dict:
    return {
        "logs": json.loads((DATA_DIR / "logs.json").read_text()),
        "metrics": json.loads((DATA_DIR / "metrics.json").read_text()),
        "incidents": json.loads((DATA_DIR / "incidents.json").read_text()),
        "runbooks": json.loads((DATA_DIR / "runbooks.json").read_text()),
        "tickets": json.loads((DATA_DIR / "tickets.json").read_text()),
        "schema": (DATA_DIR / "schema.sql").read_text(),
    }


@lru_cache(maxsize=1)
def load_test_queries() -> list:
    return json.loads((DATA_DIR / "test_queries.json").read_text())


def find_test_query(query: str) -> dict | None:
    key = query.strip().lower()
    return next((t for t in load_test_queries() if t["query"].strip().lower() == key), None)


def select_queries(limit: int | None = None, workloads: tuple | None = None) -> list:
    """Pick `limit` queries, keeping the dataset's complexity mix.

    The dataset is ordered roughly SIMPLE -> MEDIUM -> HARD, so a plain slice
    would hand a short run nothing but easy queries and the routing comparison
    would be meaningless. Seats are allocated per band in proportion to the
    full dataset using the largest-remainder method, with at least one from
    every band. Selection is deterministic and returns queries in dataset order.
    """
    queries = load_test_queries()
    if workloads:
        queries = [q for q in queries if q.get("workload") in workloads]
    if not limit or limit >= len(queries):
        return list(queries)

    bands = {}
    for q in queries:
        bands.setdefault(q["complexity"], []).append(q)

    exact = {b: len(qs) * limit / len(queries) for b, qs in bands.items()}
    if limit < len(bands):
        biggest = sorted(bands, key=lambda b: -len(bands[b]))[:limit]
        seats = {b: (1 if b in biggest else 0) for b in bands}
    else:
        seats = {b: max(1, int(exact[b])) for b in bands}
        while (diff := limit - sum(seats.values())) != 0:
            if diff > 0:
                b = max(bands, key=lambda b: (exact[b] - seats[b], len(bands[b])))
                seats[b] += 1
            else:
                b = max((b for b in bands if seats[b] > 1),
                        key=lambda b: (seats[b] - exact[b], len(bands[b])))
                seats[b] -= 1

    picked = []
    for band, qs in bands.items():
        n = min(seats[band], len(qs))
        picked += [qs[(i * len(qs)) // n] for i in range(n)]
    order = {id(q): i for i, q in enumerate(queries)}
    return sorted(picked, key=lambda q: order[id(q)])


def _fmt_log(l: dict) -> str:
    lat = f" latency={l['latency_ms']}ms" if l.get("latency_ms") is not None else ""
    return f"{l['timestamp']} [{l['level']}] {l['service']} ({l['host']}): {l['message']}{lat}"


def _fmt_metric(m: dict) -> str:
    parts = [f"{m['timestamp']} {m['service']}"]
    for k in ("cpu_percent", "memory_percent", "latency_ms", "db_connections", "db_max_connections"):
        if m.get(k) is not None:
            parts.append(f"{k}={m[k]}")
    if m.get("note"):
        parts.append(f"note={m['note']}")
    return " ".join(parts)


def _fmt_incident(i: dict) -> str:
    return (f"{i['incident_id']} [{i['severity']}/{i['status']}] service={i['service']} "
            f"created={i['created_at']}: {i['description']}")


def _fmt_runbook(r: dict) -> str:
    return f"### {r['id']} {r['title']} (owner: {r['owner']})\n{r['body']}"


def _fmt_ticket(t: dict) -> str:
    return (f"### {t['id']} [{t['priority']}/{t['status']}] {t['title']} "
            f"(from {t['requester']}, {t['created_at']})\n{t['body']}")


def build_sections(query: str) -> dict:
    """Every section the corpus offers for this query, in canonical order."""
    ds = load_dataset()
    secs = {
        "metrics": "\n".join(["## Service metrics (time-ordered snapshots, latest = current)",
                              *(_fmt_metric(m) for m in ds["metrics"])]),
        "incidents": "\n".join(["## Incidents", *(_fmt_incident(i) for i in ds["incidents"])]),
        "logs": "\n".join(["## Log stream (last ~65 minutes)", *(_fmt_log(l) for l in ds["logs"])]),
        "runbooks": "\n\n".join(["## Runbooks and policies", *(_fmt_runbook(r) for r in ds["runbooks"])]),
        "tickets": "\n\n".join(["## Tickets", *(_fmt_ticket(t) for t in ds["tickets"])]),
        "schema": "## Warehouse schema (SQLite)\n" + ds["schema"].strip(),
    }
    return secs


def assemble(sections: dict, keep) -> str:
    keep = [s for s in SECTION_ORDER if s in keep and s in sections]
    return "\n\n".join(sections[s] for s in keep)


def baseline_section_names(query: str) -> list:
    """What a keyword-gated deployment sends: metrics and incidents always, the
    rest when the query's words call for them."""
    q = query.lower()
    keep = ["metrics", "incidents"]
    if needs_logs(query):
        keep.append("logs")
    if any(w in q for w in _RUNBOOK_TRIGGERS):
        keep.append("runbooks")
    if any(w in q for w in _TICKET_TRIGGERS):
        keep.append("tickets")
    if any(w in q for w in _SCHEMA_TRIGGERS):
        keep.append("schema")
    return keep


def build_context(query: str) -> str:
    """The baseline context: everything the query's keywords call for."""
    return assemble(build_sections(query), baseline_section_names(query))


_LOG_TRIGGERS = (
    "error", "log", "why", "cause", "analy", "correlat", "sequence", "timeline",
    "changed", "pattern", "related", "relationship", "down", "healthy", "status",
    "affected", "remediat", "recommend", "blast", "impact", "queue", "incident",
    "outage", "deploy", "summar", "telemetry",
)
_RUNBOOK_TRIGGERS = ("runbook", "procedure", "policy", "on-call", "page", "status-page",
                     "status page", "rollback", "restart", "escalat", "playbook")
_TICKET_TRIGGERS = ("ticket", "tck-")
_SCHEMA_TRIGGERS = ("sql", "schema", "warehouse", "table")


def needs_logs(query: str) -> bool:
    q = query.lower()
    return any(w in q for w in _LOG_TRIGGERS)


SYSTEM_PROMPT = (
    "You are an IT Operations assistant. Answer the user's question strictly from the "
    "context provided (metrics, incidents, logs, runbooks, tickets, schema). Current time is "
    "2026-08-18T10:25:30. Be precise: cite concrete numbers, timestamps and service names from "
    "the data. If the data does not contain the answer, say so. Keep answers concise. When the "
    "user asks for a label, JSON or SQL only, reply with exactly that and nothing else."
)
