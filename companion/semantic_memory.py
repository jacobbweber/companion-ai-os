"""
Hybrid Semantic + Lexical Memory Retrieval Engine (`companion/semantic_memory.py` — A-Frame Domain Layer).

Provides `<5ms`, zero-GPU-swap conceptual retrieval combining:
1. Morphological stemming + bidirectional everyday-to-technical concept cluster expansion
2. L2-normalized sparse feature vectors (stems + concept clusters + character n-grams)
3. Reciprocal Rank Fusion (RRF) blending SQLite FTS5 BM25 rankings with semantic cosine similarity
4. Optional dense float32 `/v1/embeddings` helper when a dedicated embedding endpoint is configured
"""

import json
import math
import re
import urllib.request
from array import array
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from companion.config import settings
from companion.llm_wire import build_openai_resource_url, is_local_or_lan_endpoint, urlopen_safe

__all__ = [
    "cosine_f32_bytes",
    "expand_semantic_terms",
    "fetch_optional_embedding",
    "hybrid_rrf_rank",
    "pack_f32_vector",
    "semantic_similarity",
]

_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_STOP = frozenset({
    "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by",
    "can", "did", "do", "does", "for", "from", "get", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "my", "no", "not", "of", "on", "or", "our", "out", "so",
    "some", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "up", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
})

# Bidirectional everyday <-> engineering concept clusters so plain-English queries match technical records.
_CONCEPT_CLUSTERS: Dict[str, FrozenSet[str]] = {
    "gpu_hardware": frozenset({"gpu", "vram", "graphics", "card", "video", "cuda", "ollama", "vllm", "nvidia", "container", "swap", "pause", "delay", "unload", "load", "latency", "slow"}),
    "storage_bloat": frozenset({"disk", "space", "storage", "bloat", "size", "gigabyte", "megabyte", "cleanup", "prune", "retention", "checkpoint", "backup", "cache", "folder", "full"}),
    "auth_lan_security": frozenset({"login", "password", "passcode", "pin", "auth", "authentication", "csrf", "origin", "rebinding", "dns", "host", "lan", "wifi", "shield", "token", "security", "hack"}),
    "mobile_disconnect": frozenset({"phone", "mobile", "iphone", "android", "safari", "screen", "lock", "sleep", "disconnect", "background", "async", "job", "poll", "stream", "sse", "live"}),
    "semantic_recall": frozenset({"memory", "remember", "recall", "forget", "search", "find", "meaning", "concept", "semantic", "vector", "embedding", "fts5", "bm25", "rrf", "history", "fact"}),
    "crash_recovery": frozenset({"crash", "broken", "bug", "error", "fail", "failure", "exception", "traceback", "syntaxerror", "fix", "heal", "recover", "ooda", "rollback", "restore", "revert"}),
    "blueprint_plan": frozenset({"plan", "blueprint", "approve", "approval", "align", "question", "revise", "step", "dag", "workflow", "task", "gate", "roadmap", "checklist"}),
    "skill_toolbox": frozenset({"skill", "toolbox", "package", "library", "dependency", "pip", "uv", "pep723", "sandbox", "script", "stdlib", "external", "install", "plugin"}),
    "context_compaction": frozenset({"context", "window", "token", "compact", "compaction", "summarize", "summary", "handoff", "truncate", "lossless", "keytokens", "long"}),
    "web_interface": frozenset({"web", "browser", "ui", "page", "button", "css", "html", "javascript", "frontend", "dashboard", "theme", "dark", "layout", "chat"}),
    "code_hygiene": frozenset({"dead", "unused", "orphan", "sprawl", "clean", "hygiene", "lint", "linter", "budget", "refactor", "simplify", "architecture", "layer", "sdd", "spec"}),
    "network_fetch": frozenset({"url", "website", "http", "https", "fetch", "scrape", "download", "markdown", "endpoint", "port", "api", "ssrf", "request"}),
}

_STEM_TO_CLUSTERS: Dict[str, List[str]] = {}


def _stem_word(word: str) -> str:
    """Lightweight deterministic English stemmer without external NLP dependencies."""
    w = word.lower().strip("_")
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ization", "ational", "fulness", "ousness", "iveness", "ication"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    for suf in ("ing", "tion", "sion", "ment", "ness", "able", "ible", "ally", "edly"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)]
            return base[:-1] if len(base) >= 4 and base[-1] == base[-2] else base
    for suf in ("ed", "es", "er", "or", "ly", "al", "ic", "ou", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not w.endswith(("ss", "us", "is")):
            return w[: -len(suf)]
    return w


for _cid, _words in _CONCEPT_CLUSTERS.items():
    for _w in _words:
        _STEM_TO_CLUSTERS.setdefault(_stem_word(_w), []).append(_cid)
        _STEM_TO_CLUSTERS.setdefault(_w, []).append(_cid)


def _tokenize_stems(text: str) -> List[str]:
    raw_tokens: List[str] = []
    for tok in _WORD_RE.findall((text or "").lower()):
        for part in tok.split("_"):
            if len(part) >= 2 and part not in _STOP:
                raw_tokens.append(_stem_word(part))
    return raw_tokens


def expand_semantic_terms(query: str) -> Set[str]:
    """Return stemmed query terms plus triggered concept-cluster terms for broad candidate recall."""
    stems = _tokenize_stems(query)
    expanded: Set[str] = set(stems)
    for st in stems:
        for cid in _STEM_TO_CLUSTERS.get(st, ()):
            expanded.update(_CONCEPT_CLUSTERS[cid])
    return expanded


def _sparse_embed(text: str) -> Dict[str, float]:
    """Build an L2-normalized sparse feature vector over stems, concept clusters, bigrams, and char 4-grams."""
    stems = _tokenize_stems(text)
    if not stems:
        return {}
    weights: Dict[str, float] = {}
    cluster_hits: Dict[str, int] = {}
    for idx, st in enumerate(stems):
        weights[f"w:{st}"] = weights.get(f"w:{st}", 0.0) + 2.0
        for cid in _STEM_TO_CLUSTERS.get(st, ()):
            cluster_hits[cid] = cluster_hits.get(cid, 0) + 1
        if idx + 1 < len(stems):
            bg = f"b:{st}_{stems[idx + 1]}"
            weights[bg] = weights.get(bg, 0.0) + 1.4
        if len(st) >= 4:
            for g_i in range(min(len(st) - 3, 6)):
                gm = f"g:{st[g_i : g_i + 4]}"
                weights[gm] = weights.get(gm, 0.0) + 0.45
    for cid, count in cluster_hits.items():
        # Multi-term co-occurrence in a concept cluster boosts conceptual precision
        weights[f"c:{cid}"] = 2.2 + min(count - 1, 3) * 0.9
    norm = math.sqrt(sum(v * v for v in weights.values()))
    if norm <= 0.0:
        return {}
    # Sublinear TF dampening before L2 normalization so repetitive long texts don't dominate
    dampened = {k: (1.0 + math.log(v)) if v > 1.0 else v for k, v in weights.items()}
    d_norm = math.sqrt(sum(v * v for v in dampened.values())) or 1.0
    return {k: v / d_norm for k, v in dampened.items()}


def semantic_similarity(text_a: str, text_b: str) -> float:
    """Compute `[0.0, 1.0]` conceptual similarity between `text_a` and `text_b` in microseconds."""
    va, vb = _sparse_embed(text_a), _sparse_embed(text_b)
    if not va or not vb:
        return 0.0
    if len(va) > len(vb):
        va, vb = vb, va
    return max(0.0, min(1.0, sum(val * vb.get(k, 0.0) for k, val in va.items())))


def _covers_query_concepts(q_stems: Sequence[str], d_vec: Dict[str, float]) -> bool:
    """Return `True` when every query stem is matched by stem or concept cluster in `d_vec`."""
    for st in q_stems:
        if f"w:{st}" in d_vec:
            continue
        if any(f"c:{cid}" in d_vec for cid in _STEM_TO_CLUSTERS.get(st, ())):
            continue
        return False
    return bool(q_stems)


def hybrid_rrf_rank(
    query: str,
    candidates: Sequence[Tuple[Any, str]],
    lexical_ids: Iterable[Any] = (),
    limit: int = 20,
    min_semantic_score: float = 0.13,
    rrf_k: int = 60,
) -> List[Any]:
    """Fuse lexical (BM25/FTS5) rank order with semantic cosine similarity via Reciprocal Rank Fusion."""
    q_stems = _tokenize_stems(query)
    q_vec = _sparse_embed(query)
    lex_list = list(lexical_ids)
    lex_rank = {doc_id: rank for rank, doc_id in enumerate(lex_list, 1)}

    sem_scored: List[Tuple[float, Any]] = []
    if q_vec:
        for doc_id, text in candidates:
            d_vec = _sparse_embed(text)
            if not d_vec:
                continue
            sim = sum(val * d_vec.get(k, 0.0) for k, val in q_vec.items())
            if doc_id in lex_rank:
                sem_scored.append((sim, doc_id))
            elif not lex_rank and sim >= min_semantic_score:
                sem_scored.append((sim, doc_id))
            elif lex_rank and sim >= 0.25 and _covers_query_concepts(q_stems, d_vec):
                sem_scored.append((sim, doc_id))
    sem_scored.sort(key=lambda item: item[0], reverse=True)
    sem_rank = {doc_id: (rank, sim) for rank, (sim, doc_id) in enumerate(sem_scored, 1)}

    all_ids: List[Any] = []
    seen: Set[Any] = set()
    for did in (*lex_list, *(did for _, did in sem_scored)):
        if did not in seen:
            seen.add(did)
            all_ids.append(did)

    fused: List[Tuple[float, Any]] = []
    for did in all_ids:
        score = 0.0
        if did in lex_rank:
            score += 1.0 / (rrf_k + lex_rank[did])
        if did in sem_rank:
            s_rk, sim = sem_rank[did]
            score += (1.15 * (0.5 + sim)) / (rrf_k + s_rk)
        fused.append((score, did))
    fused.sort(key=lambda item: item[0], reverse=True)
    return [did for _, did in fused[: max(1, limit)]]


def pack_f32_vector(vec: Sequence[float]) -> bytes:
    """Pack a dense float sequence into a compact L2-normalized little-endian `float32` BLOB."""
    norm = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    arr = array("f", (float(x) / norm for x in vec))
    return arr.tobytes()


def cosine_f32_bytes(blob_a: bytes, blob_b: bytes) -> float:
    """Compute exact dot-product cosine similarity between two normalized `float32` BLOBs."""
    if not blob_a or not blob_b or len(blob_a) != len(blob_b) or len(blob_a) % 4 != 0:
        return 0.0
    va, vb = array("f"), array("f")
    va.frombytes(blob_a)
    vb.frombytes(blob_b)
    return float(sum(a * b for a, b in zip(va, vb)))


def fetch_optional_embedding(text: str, timeout: float = 3.0) -> Optional[bytes]:
    """Fetch dense embeddings when a dedicated `embedding` tier or `COMPANION_EMBEDDING_MODEL` is configured (respecting Privacy Lock)."""
    emb_cfg = settings.resolve_tier("embedding")
    if emb_cfg.is_dedicated and emb_cfg.model and emb_cfg.base_url:
        base, model, api_key, dialect = emb_cfg.base_url.rstrip("/"), emb_cfg.model, emb_cfg.api_key, emb_cfg.dialect
    else:
        model, base, api_key, dialect = (settings.embedding_model or "").strip(), (settings.embedding_base_url or "").strip().rstrip("/"), settings.api_key, "openai_compat"
    if not model or not base or base == settings.base_url.rstrip("/"):
        return None
    if settings.effective_privacy_mode() == "local_only" and not is_local_or_lan_endpoint(base):
        return None
    try:
        if dialect == "gemini_native":
            g_base = base if base.lower().endswith(("/v1beta", "/v1alpha", "/v1")) else f"{base}/v1beta"
            m_id = model.removeprefix("models/")
            payload = json.dumps({"model": f"models/{m_id}", "content": {"parts": [{"text": (text or "")[:4000]}]}}).encode("utf-8")
            hdrs = {"Content-Type": "application/json", **({"x-goog-api-key": api_key} if api_key else {})}
            with urlopen_safe(urllib.request.Request(f"{g_base}/models/{m_id}:embedContent", data=payload, headers=hdrs, method="POST"), timeout=timeout, allow_private_lan=True, allow_redirects=False) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            emb = (data.get("embedding") or {}).get("values")
        else:
            payload = json.dumps({"model": model, "input": (text or "")[:4000]}).encode("utf-8")
            hdrs = {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
            with urlopen_safe(urllib.request.Request(build_openai_resource_url(base, "embeddings"), data=payload, headers=hdrs, method="POST"), timeout=timeout, allow_private_lan=True, allow_redirects=False) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            emb = ((data.get("data") or [{}])[0]).get("embedding")
        return pack_f32_vector(emb) if isinstance(emb, list) and emb else None
    except Exception:
        return None
