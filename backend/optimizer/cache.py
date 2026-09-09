"""Exact and semantic response caches, with explanations.

Both caches are namespaced by tenant and application, and both key on the
prompt version, knowledge version and policy version, so an entry written
under one configuration can never be served under another.

Exact cache: SHA-256 of the canonical request (normalised query + versions).
Semantic cache: embedding similarity is *one* of several guards. A candidate
is served only if every guard passes; rejected candidates and the failing
guard are returned so the trace can show why a near-miss was not served.

Entries live in the store's kv namespace (`cache:<tenant>:<app>:<kind>:<id>`)
so they survive restarts and are shared between instances.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field

from backend import storage, telemetry
from backend.optimizer.embeddings import cosine

_WS = re.compile(r"\s+")

# ---------------------------------------------------------------------------
# Subject guards.
#
# Embedding similarity is not answer equivalence, and on a lexical embedder it
# is not even close. Measured on this corpus, "the current CPU usage of
# payment-api" scores 0.73 against "the current CPU usage of auth-svc" but only
# 0.38 against its own paraphrase "what's the CPU utilization on payment-api
# right now". Similarity alone would serve the wrong service's number.
#
# So two requests are only ever candidates for the same answer if they are
# about the same subject: the same entities (services, hosts, incident ids) and
# the same aspect (cpu, memory, latency, ...). These are deterministic, cheap,
# and they fail closed.
_ENTITY = re.compile(r"\b(?:[a-z][a-z0-9]*-(?:api|svc|web|primary-\d+|replica-\d+|lb-\d+)|server-\d+|"
                     r"db-\d+|net-\d+|inc-\d+|tck-\d+|rb-\d+|pol-\d+)\b", re.IGNORECASE)
_ALIASES = {"auth service": "auth-svc", "payment api": "payment-api", "checkout web": "checkout-web",
            "search service": "search-svc", "inventory service": "inventory-svc",
            "notification service": "notification-svc", "the database": "db-primary-01",
            "primary database": "db-primary-01"}
ASPECTS = {
    "cpu": ("cpu", "processor"),
    "memory": ("memory", "heap", "ram"),
    "latency": ("latency", "slow", "response time", "duration"),
    "connections": ("connection", "pool", "max connections"),
    "errors": ("error", "exception", "failure", "502", "504", "timeout"),
    "severity": ("severity", "how severe", "priority"),
    "status": ("status", "health", "healthy", "up", "down", "reachable", "unreachable"),
    "queue": ("queue", "backlog", "retry"),
    "cost": ("cost", "spend", "price"),
}


def _entities(text: str) -> set:
    t = text.lower()
    for alias, canon in _ALIASES.items():
        t = t.replace(alias, canon)
    return {e.lower() for e in _ENTITY.findall(t)}


def _aspects(text: str) -> set:
    t = text.lower()
    return {name for name, words in ASPECTS.items() if any(w in t for w in words)}


def subject_guard(a: str, b: str) -> tuple:
    """(ok, reason). Fails closed when the two requests are about different things."""
    ea, eb = _entities(a), _entities(b)
    if ea != eb:
        only = sorted((ea | eb) - (ea & eb))
        return False, f"different subject: {only} appears in only one of the two requests"
    aa, ab = _aspects(a), _aspects(b)
    if aa != ab:
        only = sorted((aa | ab) - (aa & ab))
        return False, f"different aspect: {only} asked in only one of the two requests"
    return True, (f"same subject ({sorted(ea) or 'no named entity'}) and "
                  f"aspect ({sorted(aa) or 'none'})")


def threshold_for(embedder, cfg: dict) -> tuple:
    """A lexical embedder needs a different operating point than a semantic one,
    and it must say so."""
    if getattr(embedder, "semantic", False):
        return float(cfg.get("threshold", 0.92)), "semantic embedding similarity"
    return float(cfg.get("lexical_threshold", 0.30)), \
        "lexical (bag-of-words) similarity - subject guards carry the safety here"


def measure_separation(embedder_fn=None) -> dict:
    """How well does the embedder separate true paraphrases from near-misses?

    Uses the labelled `paraphrase_of` pairs in the query set as positives and
    same-band non-paraphrases as negatives. Reported in the UI so nobody has to
    take the semantic cache's threshold on trust.
    """
    from backend.optimizer.embeddings import local_vector
    embedder_fn = embedder_fn or local_vector
    qs = telemetry.load_test_queries()
    by_id = {q["id"]: q for q in qs}
    pos, neg = [], []
    for q in qs:
        src = by_id.get(q.get("paraphrase_of") or "")
        if src:
            pos.append((cosine(embedder_fn(q["query"]), embedder_fn(src["query"])),
                        q["query"], src["query"]))
    pairs = [(a, b) for i, a in enumerate(qs) for b in qs[i + 1:]
             if a.get("task_type") == b.get("task_type")
             and a.get("paraphrase_of") != b["id"] and b.get("paraphrase_of") != a["id"]]
    for a, b in pairs[:400]:
        neg.append((cosine(embedder_fn(a["query"]), embedder_fn(b["query"])), a["query"], b["query"]))
    guarded_neg = [n for n in neg if subject_guard(n[1], n[2])[0]]
    worst_pos = min((p[0] for p in pos), default=0.0)
    best_neg = max((n[0] for n in neg), default=0.0)
    best_guarded = max((n[0] for n in guarded_neg), default=0.0)
    return {
        "paraphrase_pairs": len(pos), "distractor_pairs": len(neg),
        "min_paraphrase_similarity": round(worst_pos, 3),
        "max_distractor_similarity": round(best_neg, 3),
        "max_distractor_similarity_after_guards": round(best_guarded, 3),
        "separable_on_similarity_alone": worst_pos > best_neg,
        "separable_with_subject_guards": worst_pos > best_guarded,
        "distractors_removed_by_guards": len(neg) - len(guarded_neg),
    }


def normalise(query: str) -> str:
    q = query.strip().lower()
    q = _WS.sub(" ", q)
    return q.rstrip(" ?.!")


def versions_of(policy) -> dict:
    return {"prompt_version": str(policy.get("prompt_version")),
            "knowledge_version": str(policy.get("knowledge_version")),
            "policy_version": str(policy.version)}


def exact_key(tenant: str, app: str, query: str, versions: dict) -> str:
    """Canonical request hash. Every version dimension is part of the key, so an
    answer produced under one configuration can never be served under another."""
    canon = "|".join([tenant, app, normalise(query)]
                     + [f"{k}={versions[k]}" for k in sorted(versions)])
    return hashlib.sha256(canon.encode()).hexdigest()


def _ns(tenant: str, app: str, kind: str) -> str:
    return f"cache:{tenant}:{app}:{kind}:"


@dataclass
class CacheHit:
    kind: str                        # exact | semantic
    entry: dict
    age_seconds: float
    similarity: float | None = None
    guards: list = field(default_factory=list)

    def public(self) -> dict:
        return {"kind": self.kind, "age_seconds": round(self.age_seconds),
                "similarity": None if self.similarity is None else round(self.similarity, 4),
                "source_request_id": self.entry.get("request_id"), "guards": self.guards,
                "model": self.entry.get("model"), "avoided_cost_usd": self.entry.get("cost_usd"),
                "avoided_latency_ms": self.entry.get("latency_ms")}


@dataclass
class CacheLookup:
    hit: CacheHit | None
    checked: int
    rejected: list = field(default_factory=list)   # [{similarity, reason, request_id}]
    embedder: str | None = None
    embedding_cost_usd: float = 0.0
    embedding_tokens: int = 0
    embedding_latency_ms: int = 0
    note: str = ""

    def public(self) -> dict:
        return {"hit": self.hit.public() if self.hit else None, "checked": self.checked,
                "rejected": self.rejected[:5], "embedder": self.embedder,
                "embedding_cost_usd": round(self.embedding_cost_usd, 8),
                "embedding_tokens": self.embedding_tokens, "note": self.note}


# ------------------------------------------------------------------- exact

async def exact_lookup(tenant: str, app: str, query: str, versions: dict, ttl_s: int,
                       now: float | None = None) -> CacheLookup:
    now = now or time.time()
    key = exact_key(tenant, app, query, versions)
    entry = await storage.kv_get(_ns(tenant, app, "exact") + key)
    if not entry:
        return CacheLookup(None, 0, note="no identical request on record")
    age = now - float(entry.get("stored_at", 0))
    if age > ttl_s:
        await storage.kv_delete(_ns(tenant, app, "exact") + key)
        return CacheLookup(None, 1, [{"reason": f"expired ({age:.0f}s > TTL {ttl_s}s)",
                                      "request_id": entry.get("request_id")}], note="stale entry evicted")
    guards = ["canonical request hash matches", f"age {age:.0f}s <= TTL {ttl_s}s",
              "versions match (" + ", ".join(f"{k}={v}" for k, v in sorted(versions.items())) + ")"]
    return CacheLookup(CacheHit("exact", entry, age, None, guards), 1)


async def exact_store(tenant: str, app: str, query: str, versions: dict, entry: dict) -> str:
    key = exact_key(tenant, app, query, versions)
    await storage.kv_set(_ns(tenant, app, "exact") + key, {**entry, "stored_at": time.time(),
                                                            "query": query, **versions})
    return key


# ---------------------------------------------------------------- semantic

async def semantic_lookup(tenant: str, app: str, query: str, task_type: str, versions: dict,
                          embedder, threshold: float, ttl_s: int, now: float | None = None,
                          threshold_basis: str = "") -> CacheLookup:
    now = now or time.time()
    emb = await embedder.embed(query)
    out = CacheLookup(None, 0, embedder=emb.embedder, embedding_cost_usd=emb.cost_usd,
                      embedding_tokens=emb.tokens, embedding_latency_ms=emb.latency_ms)
    keys = await storage.kv_keys(_ns(tenant, app, "semantic"))
    best = None
    for k in keys:
        e = await storage.kv_get(k)
        if not e:
            continue
        out.checked += 1
        sim = cosine(emb.vector, e.get("vector") or [])
        rej = {"similarity": round(sim, 4), "request_id": e.get("request_id"),
               "query": (e.get("query") or "")[:80]}
        age = now - float(e.get("stored_at", 0))
        if e.get("embedder") != emb.embedder:
            rej["reason"] = "embedded with a different embedder"
        elif normalise(e.get("query", "")) == normalise(query):
            rej["reason"] = "identical query (exact-cache territory)"
        elif sim < threshold:
            rej["reason"] = f"similarity {sim:.3f} < threshold {threshold}"
        elif any(e.get(v) != versions[v] for v in versions):
            rej["reason"] = "prompt/knowledge/policy version differs"
        elif e.get("task_type") != task_type:
            rej["reason"] = f"task type differs ({e.get('task_type')} vs {task_type})"
        elif not subject_guard(query, e.get("query", ""))[0]:
            rej["reason"] = subject_guard(query, e.get("query", ""))[1]
        elif age > ttl_s:
            rej["reason"] = f"expired ({age:.0f}s > TTL {ttl_s}s)"
        elif not e.get("quality_ok", True):
            rej["reason"] = "stored answer failed its quality gate"
        else:
            if best is None or sim > best[0]:
                best = (sim, e, age)
            continue
        out.rejected.append(rej)
    out.rejected.sort(key=lambda r: -r["similarity"])
    if best:
        sim, e, age = best
        guards = [f"similarity {sim:.3f} >= threshold {threshold}"
                  + (f" ({threshold_basis})" if threshold_basis else ""),
                  subject_guard(query, e.get("query", ""))[1],
                  "same tenant and application", f"same task type ({task_type})",
                  "same prompt/knowledge/policy versions",
                  f"age {age:.0f}s <= TTL {ttl_s}s", "stored answer passed its quality gate"]
        out.hit = CacheHit("semantic", e, age, sim, guards)
        out.note = "safe semantic hit"
    else:
        out.note = "no safe candidate" if out.checked else "no prior requests to compare"
    return out


async def semantic_store(tenant: str, app: str, query: str, task_type: str, versions: dict,
                         embedder, entry: dict) -> str:
    emb = await embedder.embed(query)
    key = hashlib.sha256((embedder.name + "|" + normalise(query)).encode()).hexdigest()
    await storage.kv_set(_ns(tenant, app, "semantic") + key,
                         {**entry, "stored_at": time.time(), "query": query, "task_type": task_type,
                          "vector": emb.vector, "embedder": emb.embedder, **versions})
    return key


async def clear(tenant: str | None = None, app: str | None = None) -> int:
    prefix = "cache:" + (f"{tenant}:" if tenant else "") + (f"{app}:" if tenant and app else "")
    return await storage.kv_clear(prefix)


async def stats(tenant: str | None = None) -> dict:
    keys = await storage.kv_keys("cache:" + (f"{tenant}:" if tenant else ""))
    return {"exact_entries": sum(1 for k in keys if ":exact:" in k),
            "semantic_entries": sum(1 for k in keys if ":semantic:" in k)}
