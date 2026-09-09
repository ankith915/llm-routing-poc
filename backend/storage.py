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
_kv: dict = {}
_kv_loaded: bool = False
_last_id: int = 0
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


async def get(request_id: str) -> dict | None:
    if db.enabled():
        p = await db.pool()
        row = await p.fetchrow("select record from requests where request_id = $1", request_id)
        return json.loads(row["record"]) if row else None
    _reload_if_changed()
    return next((r for r in _records if r.get("request_id") == request_id), None)


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
    global _records, _mtime, _state, _last_id
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
        _last_id = 0
        if RESULTS_PATH.exists():
            RESULTS_PATH.unlink()


async def next_request_id() -> str:
    """Allocate a request id. Must be unique across concurrent instances."""
    if db.enabled():
        p = await db.pool()
        n = await p.fetchval("select nextval('request_id_seq')")
        return f"REQ-{n:04d}"
    global _last_id
    async with _lock:
        _reload_if_changed()
        # A process-wide counter, advanced under the lock, so two concurrent
        # requests can never be handed the same id (they could when the id was
        # derived from the records list alone, because neither had appended yet).
        used = [int(m.group(1)) for r in _records
                if (m := _REQ_ID.fullmatch(r.get("request_id", "")))]
        _last_id = max([_last_id, *used])
        _last_id += 1
        return f"REQ-{_last_id:04d}"


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


# ------------------------------------------------------------------ generic kv
# Namespaced key/value state: budgets, caches, chaos, learned classifier data.
# Postgres: the kv table. File: kv.json next to results.json, so it survives
# restarts locally and tests can point it at a temp dir.

KV_PATH = RESULTS_PATH.with_name("kv.json")


def _kv_load() -> None:
    global _kv, _kv_loaded
    if _kv_loaded:
        return
    _kv_loaded = True
    if KV_PATH.exists():
        try:
            _kv = json.loads(KV_PATH.read_text())
        except json.JSONDecodeError:
            _kv = {}


def _kv_flush() -> None:
    KV_PATH.write_text(json.dumps(_kv))


async def kv_get(key: str):
    if db.enabled():
        p = await db.pool()
        row = await p.fetchrow("select value from kv where key = $1", key)
        return json.loads(row["value"]) if row else None
    async with _lock:
        _kv_load()
        return _kv.get(key)


async def kv_set(key: str, value) -> None:
    if db.enabled():
        p = await db.pool()
        await p.execute("insert into kv (key, value) values ($1, $2::jsonb)"
                        " on conflict (key) do update set value = excluded.value",
                        key, json.dumps(value))
        return
    async with _lock:
        _kv_load()
        _kv[key] = value
        _kv_flush()


async def kv_update(key: str, fn) -> None:
    """Read-modify-write. fn receives the current value ({} if unset)."""
    if db.enabled():
        p = await db.pool()
        async with p.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("select value from kv where key = $1 for update", key)
                cur = json.loads(row["value"]) if row else {}
                await conn.execute("insert into kv (key, value) values ($1, $2::jsonb)"
                                   " on conflict (key) do update set value = excluded.value",
                                   key, json.dumps(fn(cur)))
        return
    async with _lock:
        _kv_load()
        _kv[key] = fn(_kv.get(key) or {})
        _kv_flush()


async def kv_delete(key: str) -> None:
    if db.enabled():
        p = await db.pool()
        await p.execute("delete from kv where key = $1", key)
        return
    async with _lock:
        _kv_load()
        _kv.pop(key, None)
        _kv_flush()


async def kv_keys(prefix: str) -> list:
    if db.enabled():
        p = await db.pool()
        rows = await p.fetch("select key from kv where key like $1", prefix + "%")
        return [r["key"] for r in rows]
    async with _lock:
        _kv_load()
        return [k for k in _kv if k.startswith(prefix)]


async def kv_clear(prefix: str) -> int:
    keys = await kv_keys(prefix)
    for k in keys:
        await kv_delete(k)
    return len(keys)


def _reset_for_tests(results_path: Path) -> None:
    """Point the file backend at a temp location and forget cached state."""
    global RESULTS_PATH, KV_PATH, _records, _mtime, _state, _kv, _kv_loaded, _last_id
    RESULTS_PATH = results_path
    KV_PATH = results_path.with_name("kv.json")
    _records, _mtime, _state, _kv, _kv_loaded, _last_id = [], None, None, {}, False, 0
