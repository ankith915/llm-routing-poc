"""Context compression: an optional stage between build_context and the model.

Two things live here.

`compress_context` is the pluggable stage. Mode "off" is the identity; mode
"headroom" hands the telemetry block to Headroom (github.com/headroomlabs-ai)
and returns whatever it gives back, with real before/after token counts. It
never raises: a compressor failure, or a host without headroom-ai installed
(Vercel - the dependency tree is over the function size limit), degrades to a
passthrough and records why in the stats. The answer is never at risk.

`naive_json_tokens` measures the alternative most teams ship - dumping the raw
dataset as JSON into the prompt - for the same query footprint build_context
would use. The gap between that and our key=value format is a saving the
pipeline already banks on every request.

Token counts use tiktoken's o200k_base, the tokenizer gpt-4.1 and gpt-4o-mini
actually use, so before/after are real numbers rather than chars/4.
"""
import json
import logging

from backend import telemetry

log = logging.getLogger(__name__)

MODES = ("off", "headroom")

try:
    import tiktoken
    _enc = tiktoken.get_encoding("o200k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text)) if text else 0
except Exception:                                   # pragma: no cover - fallback only
    def count_tokens(text: str) -> int:
        return max(0, len(text) // 4)


def _wrap(context: str, query: str) -> list:
    """The exact message shape the answer call uses, so Headroom sees what the model sees."""
    return [
        {"role": "system", "content": telemetry.SYSTEM_PROMPT},
        {"role": "user", "content": f"Telemetry:\n{context}\n\nQuestion: {query}"},
    ]


def _unwrap(user_content: str) -> str:
    return user_content.split("Telemetry:\n", 1)[-1].rsplit("\n\nQuestion:", 1)[0]


try:
    import headroom as _headroom
    from headroom import CompressConfig as _CompressConfig

    def _headroom_compress(context: str, query: str) -> tuple:
        # Headroom protects user messages and the most recent turns by default,
        # which on a two-message prompt means it compresses nothing. These two
        # settings are what make it act on the telemetry block at all.
        result = _headroom.compress(
            _wrap(context, query), model="gpt-4.1", optimize=True,
            config=_CompressConfig(compress_user_messages=True, protect_recent=0))
        out = _unwrap(result.messages[-1]["content"])
        return out, list(getattr(result, "transforms_applied", None) or [])
except Exception:                                   # headroom-ai absent or broken
    _headroom_compress = None


def available() -> bool:
    return _headroom_compress is not None


def naive_json_tokens(query: str) -> int:
    """Tokens a raw compact-JSON dump of the same data would cost."""
    ds = telemetry.load_dataset()
    subset = {"metrics": ds["metrics"], "incidents": ds["incidents"]}
    if telemetry.needs_logs(query):
        subset["logs"] = ds["logs"]
    return count_tokens(json.dumps(subset, separators=(",", ":")))


def compress_context(context: str, query: str, mode: str = "off") -> tuple:
    """Return (context_to_send, stats). Never raises for a supported mode."""
    if mode not in MODES:
        raise ValueError(f"unknown compression mode '{mode}' (use one of {MODES})")
    before = count_tokens(context)
    stats = {"mode": mode, "tokens_before": before, "tokens_after": before,
             "saved_pct": 0.0, "transforms": [], "error": None}
    if mode == "off":
        return context, stats
    if _headroom_compress is None:
        stats["error"] = "headroom-ai not installed on this host; sent uncompressed"
        return context, stats
    try:
        out, transforms = _headroom_compress(context, query)
        if not out or not out.strip():
            raise ValueError("compressor returned an empty context")
        after = count_tokens(out)
        stats.update(tokens_after=after, transforms=transforms,
                     saved_pct=round(100 * (before - after) / before, 1) if before else 0.0)
        return out, stats
    except Exception as e:
        log.warning("context compression failed, sending uncompressed: %s", e)
        stats["error"] = str(e)[:200]
        return context, stats
