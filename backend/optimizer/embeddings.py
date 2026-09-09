"""Embedders for the semantic cache.

  openai - text-embedding-3-small through the provider API. Real semantic
           similarity; costs real money, which the ledger books as
           embedding_cost.
  local  - a hashed bag-of-words vector (unigrams + bigrams, L2-normalised).
           Free, offline, deterministic; it measures *lexical* similarity, not
           meaning, and the UI says so wherever the semantic cache is shown.
"""
import hashlib
import math
import re
from dataclasses import dataclass

from backend.optimizer import pricing

LOCAL_DIM = 512
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-']*")
_STOP = {"the", "a", "an", "of", "to", "is", "are", "in", "on", "and", "for", "at", "by",
         "what", "which", "how", "does", "do", "it", "its", "currently", "right", "now", "please"}


@dataclass
class Embedding:
    vector: list
    embedder: str
    tokens: int
    cost_usd: float
    latency_ms: int


def _words(text: str) -> list:
    return [w for w in _TOKEN.findall(text.lower()) if w not in _STOP]


def local_vector(text: str, dim: int = LOCAL_DIM) -> list:
    v = [0.0] * dim
    ws = _words(text)
    feats = ws + [f"{a} {b}" for a, b in zip(ws, ws[1:])]
    for f in feats:
        h = int(hashlib.blake2b(f.encode(), digest_size=8).hexdigest(), 16)
        v[h % dim] += 1.0 if " " in f else 1.5   # unigrams weigh a little more
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list, b: list) -> float:
    if len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


class LocalEmbedder:
    name = "local-lexical"
    semantic = False

    async def embed(self, text: str) -> Embedding:
        return Embedding(local_vector(text), self.name, 0, 0.0, 0)


class OpenAIEmbedder:
    semantic = True

    def __init__(self, client, model_id: str = "text-embedding-3-small"):
        self.client = client
        self.model_id = model_id
        self.name = f"openai:{model_id}"

    async def embed(self, text: str) -> Embedding:
        data = await self.client.embed(self.model_id, text)
        cost = pricing.embedding_usd(data["tokens"], self.model_id)
        return Embedding(data["vector"], self.name, data["tokens"], cost, data["latency_ms"])


def choose(pool, preference: str = "auto"):
    """auto -> OpenAI when a key is configured and the client supports embeddings."""
    if preference in ("auto", "openai") and pool is not None and pool.has("openai"):
        client = pool.get("openai")
        if hasattr(client, "embed"):
            return OpenAIEmbedder(client)
    if preference == "openai":
        raise RuntimeError("OpenAI embedder requested but OPENAI_API_KEY is not configured")
    return LocalEmbedder()
