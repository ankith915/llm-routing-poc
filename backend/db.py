"""Postgres connection pool and schema.

Used when DATABASE_URL is set (serverless deployments, where the filesystem is
read-only and process memory is not shared between invocations). Without it the
app falls back to the flat-file store in storage.py.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DSN_PARAMS_ASYNCPG_REJECTS = ("sslmode", "channel_binding")

_pool = None
_pool_lock = asyncio.Lock()
_schema_ready = False

SCHEMA = """
create table if not exists requests (
    id          bigserial primary key,
    request_id  text unique not null,
    strategy    text not null,
    record      jsonb not null
);
create index if not exists requests_strategy_idx on requests (strategy);
create sequence if not exists request_id_seq;
create table if not exists kv (
    key   text primary key,
    value jsonb not null
);
"""


def enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _clean_dsn(dsn: str) -> str:
    """asyncpg takes ssl as a keyword argument, not as a query parameter."""
    parts = urlsplit(dsn)
    kept = [(k, v) for k, v in parse_qsl(parts.query)
            if k not in DSN_PARAMS_ASYNCPG_REJECTS]
    return urlunsplit(parts._replace(query=urlencode(kept)))


async def pool():
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                import asyncpg
                _pool = await asyncpg.create_pool(
                    _clean_dsn(os.environ["DATABASE_URL"]),
                    ssl="require",
                    min_size=0,
                    max_size=4,
                    # Neon's pooled endpoint runs pgbouncer in transaction mode,
                    # which cannot hold server-side prepared statements.
                    statement_cache_size=0,
                )
    return _pool


async def init() -> None:
    """Create the schema. Idempotent, and safe when instances start concurrently."""
    global _schema_ready
    if _schema_ready:
        return
    p = await pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # Serialise concurrent CREATE TABLE IF NOT EXISTS across instances.
            await conn.execute("select pg_advisory_xact_lock(853214)")
            await conn.execute(SCHEMA)
    _schema_ready = True


@asynccontextmanager
async def try_lock(key: int):
    """Yield True if this instance won the advisory lock, False if another holds it.

    Keeps two browser tabs (or two instances) from running the same experiment
    jobs twice, which would double the LLM spend.
    """
    p = await pool()
    async with p.acquire() as conn:
        got = await conn.fetchval("select pg_try_advisory_lock($1)", key)
        try:
            yield got
        finally:
            if got:
                await conn.execute("select pg_advisory_unlock($1)", key)


async def close() -> None:
    global _pool, _schema_ready
    if _pool is not None:
        await _pool.close()
        _pool = None
        _schema_ready = False
