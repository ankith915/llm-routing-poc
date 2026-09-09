"""Request store.

Two interchangeable backends behind one async interface:

  postgres - used when DATABASE_URL is set. Required on serverless hosts, where
             the filesystem is read-only and no state survives between requests.
  file     - flat JSON file. The default for local runs and the test suite.
"""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from backend import db

STEP_LOCK_KEY = 853215

RESULTS_PATH = Path(__file__).resolve().parent / "data" / "results.json"
_lock = asyncio.Lock()
_step_lock = asyncio.Lock()
_records: list = []
_mtime: float | None = None
_state: dict | None = None
_REQ_ID = re.compile(r"REQ-(\d+)")


def backend() -> str:
    return "postgres" if db.enabled() else "file"


# --------------------------------------------------------------------------- file

def _reload_if_changed() -> None:
    """Pick up results written by another process (e.g. the CLI experiment)."""
    global _records, _mtime
    if not RESULTS_PATH.exists():
        return
    mtime = RESULTS_PATH.stat().st_mtime
    if mtime != _mtime:
        try:
            _records = json.loads(RESULTS_PATH.read_text())
            _mtime = mtime
        except json.JSONDecodeError:
            pass  # file mid-write; keep current records and retry next call


def _flush() -> None:
    global _mtime
    RESULTS_PATH.write_text(json.dumps(_records, indent=1))
    _mtime = RESULTS_PATH.stat().st_mtime


# ------------------------------------------------------------------------ public

async def init() -> None:
    if db.enabled():
        await db.init()
    else:
        _reload_if_changed()


async def all_records() -> list:
    if db.enabled():
        p = await db.pool()
        rows = await p.fetch("select record from requests order by id")
        return [json.loads(r["record"]) for r in rows]
    _reload_if_changed()
    return _records


async def append(record: dict) -> None:
    if db.enabled():
        p = await db.pool()
        await p.execute(
            "insert into requests (request_id, strategy, record) values ($1, $2, $3::jsonb)"
            " on conflict (request_id) do update set record = excluded.record",
            record["request_id"], record["strategy"], json.dumps(record),
        )
        return
    async with _lock:
        _records.append(record)
        _flush()


async def update(request_id: str, patch: dict) -> dict | None:
    if db.enabled():
        p = await db.pool()
        row = await p.fetchrow(
            "update requests set record = record || $2::jsonb"
            " where request_id = $1 returning record",
            request_id, json.dumps(patch),
        )
        return json.loads(row["record"]) if row else None
    async with _lock:
        for r in _records:
            if r["request_id"] == request_id:
                r.update(patch)
                _flush()
                return r
    return None


async def clear() -> None:
    global _records, _mtime, _state
    if db.enabled():
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                await conn.execute("truncate requests")
                await conn.execute("alter sequence request_id_seq restart with 1")
        return
    async with _lock:
        _records = []
        _mtime = None
        _state = None
        if RESULTS_PATH.exists():
            RESULTS_PATH.unlink()


async def next_request_id() -> str:
    """Allocate a request id. Must be unique across concurrent instances."""
    if db.enabled():
        p = await db.pool()
        n = await p.fetchval("select nextval('request_id_seq')")
        return f"REQ-{n:04d}"
    async with _lock:
        _reload_if_changed()
        used = [int(m.group(1)) for r in _records
                if (m := _REQ_ID.fullmatch(r.get("request_id", "")))]
        return f"REQ-{(max(used) + 1) if used else 1:04d}"


# ------------------------------------------------------- experiment state (shared)

async def get_state() -> dict | None:
    if db.enabled():
        p = await db.pool()
        row = await p.fetchrow("select value from kv where key = 'experiment'")
        return json.loads(row["value"]) if row else None
    return _state


@asynccontextmanager
async def step_lock():
    """Ensure only one experiment step runs at a time."""
    if db.enabled():
        async with db.try_lock(STEP_LOCK_KEY) as got:
            yield got
        return
    if _step_lock.locked():
        yield False
        return
    async with _step_lock:
        yield True


async def set_state(state: dict) -> None:
    global _state
    if db.enabled():
        p = await db.pool()
        await p.execute(
            "insert into kv (key, value) values ('experiment', $1::jsonb)"
            " on conflict (key) do update set value = excluded.value",
            json.dumps(state),
        )
        return
    _state = state
