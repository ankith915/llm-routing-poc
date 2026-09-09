"""Load the simulated IT-ops dataset and build query context.

Context depends only on the query text, so all three routing strategies see
identical input for a given query (fair comparison).
"""
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def load_dataset() -> dict:
    return {
        "logs": json.loads((DATA_DIR / "logs.json").read_text()),
        "metrics": json.loads((DATA_DIR / "metrics.json").read_text()),
        "incidents": json.loads((DATA_DIR / "incidents.json").read_text()),
    }


@lru_cache(maxsize=1)
def load_test_queries() -> list:
    return json.loads((DATA_DIR / "test_queries.json").read_text())


def select_queries(limit: int | None = None) -> list:
    """Pick `limit` queries, keeping the dataset's complexity mix.

    The dataset is ordered SIMPLE -> MEDIUM -> HARD, so a plain slice would
    hand a short run nothing but easy queries (queries[:18] is 14 SIMPLE, 4
    MEDIUM, zero HARD) and the routing comparison would be meaningless: every
    strategy would pick the cheap tier and look identical.

    Seats are allocated per band in proportion to the full dataset using the
    largest-remainder method, with at least one from every band so a quick run
    always exercises the whole routing range. Selection is deterministic and
    returns queries in dataset order.
    """
    queries = load_test_queries()
    if not limit or limit >= len(queries):
        return list(queries)

    bands = {}
    for q in queries:
        bands.setdefault(q["complexity"], []).append(q)

    exact = {b: len(qs) * limit / len(queries) for b, qs in bands.items()}
    if limit < len(bands):
        # Fewer seats than bands: one seat each to the largest bands, none to
        # the rest. "One from every band" is impossible here, so don't try.
        biggest = sorted(bands, key=lambda b: -len(bands[b]))[:limit]
        seats = {b: (1 if b in biggest else 0) for b in bands}
    else:
        # Floor each band's proportional share, guaranteeing one seat each,
        # then hand out or claw back the rounding difference, largest
        # remainder first.
        seats = {b: max(1, int(exact[b])) for b in bands}
        while (diff := limit - sum(seats.values())) != 0:
            if diff > 0:
                b = max(bands, key=lambda b: (exact[b] - seats[b], len(bands[b])))
                seats[b] += 1
            else:
                b = max((b for b in bands if seats[b] > 1),
                        key=lambda b: (seats[b] - exact[b], len(bands[b])))
                seats[b] -= 1

    # Spread each band's picks evenly across it rather than taking a prefix,
    # so a short run samples the whole band instead of its first few entries.
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


def build_context(query: str) -> str:
    """Assemble the telemetry snapshot the assistant answers from.

    Metrics and incidents are always included (they are small); the full log
    stream is included when the query needs event-level evidence.
    """
    ds = load_dataset()
    sections = [
        "## Service metrics (time-ordered snapshots, latest = current)",
        *(_fmt_metric(m) for m in ds["metrics"]),
        "",
        "## Incidents",
        *(_fmt_incident(i) for i in ds["incidents"]),
    ]
    if needs_logs(query):
        sections += ["", "## Log stream (last ~65 minutes)",
                     *(_fmt_log(l) for l in ds["logs"])]
    return "\n".join(sections)


_LOG_TRIGGERS = (
    "error", "log", "why", "cause", "analy", "correlat", "sequence", "timeline",
    "changed", "pattern", "related", "relationship", "down", "healthy", "status",
    "affected", "remediat", "recommend", "blast", "impact", "queue", "incident",
)


def needs_logs(query: str) -> bool:
    """Whether a query needs event-level evidence (the log stream) in its context.

    Exposed so anything that must mirror build_context's footprint - like the
    naive-JSON token comparison - uses the same gate rather than a copy of it.
    """
    q = query.lower()
    return any(w in q for w in _LOG_TRIGGERS)


SYSTEM_PROMPT = (
    "You are an IT Operations assistant. Answer the user's question strictly from the "
    "telemetry provided (metrics, incidents, logs). Current time is 2026-08-18T10:25:30. "
    "Be precise: cite concrete numbers, timestamps and service names from the data. "
    "If the data does not contain the answer, say so. Keep answers concise."
)
