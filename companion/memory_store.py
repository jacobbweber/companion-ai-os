"""
Persistent SQLite State & Memory Store (`companion/memory_store.py` — Domain Layer).

Thread-safe WAL SQLite store managing conversation history (with FTS5 + semantic RRF),
user profile & Background Autonomy state (`background_autonomy`),
ADRs, state-machine workflows, knowledge facts, autonomy journal, and the canonical
Universal Project & Goal Graph (`projects`, `milestones`, `project_timeboxes`, `work_items`).
"""

import contextlib
import contextvars
import json
import math
import os
import re
import sqlite3
import sys
import threading
import urllib.request
import uuid
import weakref
from array import array
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from companion import __version__
from companion.config import SELF_PROJECT_ID, is_in_agent_tool_turn, settings
from companion.llm_wire import build_openai_resource_url, is_local_or_lan_endpoint, urlopen_safe
from companion.telemetry import get_db_writer_lock, notify_db_closed, redact_secrets, sqlite_write_tx, telemetry_db_context


_WORD_RE = re.compile(r"[a-zA-Z0-9_]{2,}")
_STOP_WORDS_SEM = frozenset({
    "a", "about", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by",
    "can", "did", "do", "does", "for", "from", "get", "had", "has", "have", "how", "i", "if", "in",
    "into", "is", "it", "its", "just", "me", "my", "no", "not", "of", "on", "or", "our", "out", "so",
    "some", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "up", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
})


def _stem_word(word: str) -> str:
    """Lightweight deterministic domain-neutral English stemmer (zero IT/homelab cluster bias)."""
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


def _tokenize_stems(text: str) -> List[str]:
    out: List[str] = []
    for tok in _WORD_RE.findall((text or "").lower()):
        for part in tok.split("_"):
            if len(part) >= 2 and part not in _STOP_WORDS_SEM:
                out.append(_stem_word(part))
    return out


_GENERAL_SYNONYMS: Tuple[Tuple[str, ...], ...] = (
    ("gpu", "vram", "graphics", "cuda", "accelerator"),
    ("pause", "delay", "latency", "slow", "swap", "transition", "unload", "reload"),
    ("recipe", "cook", "meal", "dish", "ingredient", "kitchen"),
    ("budget", "expense", "receipt", "spend", "cost", "finance", "invoice"),
    ("appointment", "calendar", "schedule", "meeting", "event", "reminder"),
    ("study", "course", "flashcard", "quiz", "review", "learn"),
)


def expand_semantic_terms(query: str) -> Set[str]:
    """Return stemmed query terms plus lightweight multi-domain English synonyms for offline recall."""
    base = set(_tokenize_stems(query))
    expanded = set(base)
    for grp in _GENERAL_SYNONYMS:
        stemmed_grp = {_stem_word(w) for w in grp}
        if base & stemmed_grp:
            expanded.update(stemmed_grp)
    return expanded


def _sparse_embed(text: str) -> Dict[str, float]:
    """Build an L2-normalized sparse feature vector over stems, synonym clusters, bigrams, and char 4-grams."""
    stems = _tokenize_stems(text)
    if not stems:
        return {}
    weights: Dict[str, float] = {}
    stem_set = set(stems)
    for c_idx, grp in enumerate(_GENERAL_SYNONYMS):
        if stem_set & {_stem_word(w) for w in grp}:
            weights[f"c:{c_idx}"] = 1.6
    for idx, st in enumerate(stems):
        weights[f"w:{st}"] = weights.get(f"w:{st}", 0.0) + 2.0
        if idx + 1 < len(stems):
            bg = f"b:{st}_{stems[idx + 1]}"
            weights[bg] = weights.get(bg, 0.0) + 1.4
        if len(st) >= 4:
            for g_i in range(min(len(st) - 3, 6)):
                gm = f"g:{st[g_i : g_i + 4]}"
                weights[gm] = weights.get(gm, 0.0) + 0.45
    dampened = {k: (1.0 + math.log(v)) if v > 1.0 else v for k, v in weights.items()}
    d_norm = math.sqrt(sum(v * v for v in dampened.values())) or 1.0
    return {k: v / d_norm for k, v in dampened.items()}


def semantic_similarity(text_a: str, text_b: str) -> float:
    va, vb = _sparse_embed(text_a), _sparse_embed(text_b)
    if not va or not vb:
        return 0.0
    if len(va) > len(vb):
        va, vb = vb, va
    return max(0.0, min(1.0, sum(val * vb.get(k, 0.0) for k, val in va.items())))


def hybrid_rrf_rank(
    query: str,
    candidates: Sequence[Tuple[Any, str]],
    lexical_ids: Iterable[Any] = (),
    limit: int = 20,
    min_semantic_score: float = 0.13,
    rrf_k: int = 60,
) -> List[Any]:
    """Fuse lexical (BM25/FTS5) rank order with stem/n-gram cosine similarity via Reciprocal Rank Fusion."""
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
            elif lex_rank and sim >= 0.25 and bool(q_stems) and all(f"w:{st}" in d_vec for st in q_stems):
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
    norm = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return array("f", (float(x) / norm for x in vec)).tobytes()


def cosine_f32_bytes(blob_a: bytes, blob_b: bytes) -> float:
    if not blob_a or not blob_b or len(blob_a) != len(blob_b) or len(blob_a) % 4 != 0:
        return 0.0
    va, vb = array("f"), array("f")
    va.frombytes(blob_a)
    vb.frombytes(blob_b)
    return float(sum(a * b for a, b in zip(va, vb)))


def fetch_optional_embedding(text: str, timeout: float = 3.0) -> Optional[bytes]:
    """Fetch dense embeddings when a dedicated `embedding` tier is configured (respecting Privacy Lock)."""
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


def parse_time_window(window: str, now: Optional[datetime] = None) -> Optional[Tuple[str, str]]:
    """Parse natural/relative time filters into `(start_utc, end_utc)` SQLite timestamps."""
    w = (window or "").strip().lower()
    if not w:
        return None
    ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if w == "today":
        d = ref.strftime("%Y-%m-%d")
        return f"{d} 00:00:00", f"{d} 23:59:59"
    if w == "yesterday":
        d = (ref - timedelta(days=1)).strftime("%Y-%m-%d")
        return f"{d} 00:00:00", f"{d} 23:59:59"
    if w == "this week":
        start = ref - timedelta(days=ref.weekday())
        return start.strftime("%Y-%m-%d 00:00:00"), ref.strftime("%Y-%m-%d 23:59:59")
    if w == "last week":
        end = ref - timedelta(days=ref.weekday() + 1)
        start = end - timedelta(days=6)
        return start.strftime("%Y-%m-%d 00:00:00"), end.strftime("%Y-%m-%d 23:59:59")
    rel_m = re.match(r"^(?:-\s*|(?:last|past)\s+)?(\d+)\s*(w|week|weeks|d|day|days|h|hour|hours|m|min|mins|minute|minutes)$", w)
    if rel_m:
        amt, u = int(rel_m.group(1)), rel_m.group(2)[0]
        delta = timedelta(weeks=amt) if u == "w" else (timedelta(days=amt) if u == "d" else (timedelta(hours=amt) if u == "h" else timedelta(minutes=amt)))
        return (ref - delta).strftime("%Y-%m-%d %H:%M:%S"), ref.strftime("%Y-%m-%d 23:59:59")
    if ".." in w:
        s_raw, _, e_raw = w.partition("..")
        s_clean, e_clean = s_raw.strip(), e_raw.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s_clean) and re.match(r"^\d{4}-\d{2}-\d{2}$", e_clean):
            return f"{s_clean} 00:00:00", f"{e_clean} 23:59:59"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", w):
        return f"{w} 00:00:00", f"{w} 23:59:59"
    return None


def _detect_runtime_platform() -> str:
    in_docker = Path("/.dockerenv").exists() or bool(os.environ.get("COMPANION_IN_DOCKER"))
    os_lbl = "Windows" if sys.platform == "win32" else ("macOS" if sys.platform == "darwin" else "Linux")
    return f"{os_lbl} ({'Docker container' if in_docker else 'native host'}) — Python {sys.version_info.major}.{sys.version_info.minor} stdlib-first architecture."


DEFAULT_USER_PROFILE: Dict[str, str] = {
    "role": "Human Partner & System Owner",
    "technical_delegation": "Own technical execution end-to-end with Spec-Driven Development (SDD), clean dead-code-free implementation, automated testing, and self-recovery while keeping explanations clear and actionable.",
    "communication_style": "Clear, warm, outcome-focused, and free of unnecessary jargon. Explain what was accomplished, verified, and what is recommended next.",
    "operating_mode": "Spec-Driven, state-machine & Python-DAG governed execution with AST syntax guards, dead-code hygiene checks, and checkpoint rollback protection.",
    "platform": _detect_runtime_platform(),
    "background_autonomy": "off",
    "active_project_id": SELF_PROJECT_ID,
}

FOUNDATIONAL_ADRS: List[Tuple[str, str, str, str]] = [
    ("ADR-001: Zero-Dependency Python Stdlib Foundation", "External dependencies in core agent loops cause environment fragility when self-evolving.", "Keep Ring-0 core (`harness.py`, `companion/`, `surfaces/`, `configs/`, `memory/`) 100% Python 3.12 stdlib while allowing Ring-1 `skills/*/scripts/*.py` to declare isolated PEP 723 `uv run` dependencies.", "Unbrickable zero-dependency core brain paired with an extensible, isolated skill toolbox."),
    ("ADR-002: A-Frame Layered Modular Architecture", "A single script becomes brittle at scale, while unlinked files create dead code.", "Organize Companion into an A-Frame Architecture: Infrastructure -> Domain -> Workflow Crossbar -> Application -> Presentation Apex.", "Clear separation of concerns, SOLID compliance, and backward compatibility via harness.py."),
    ("ADR-003: Deterministic State-Machine Graph for Engineering & Self-Evolution", "Unstructured tool loops hit MAX_TOOL_ITERS or skip design/testing on multi-step tasks.", "Enforce a persistent directed-graph state machine (INTAKE -> DISCOVER -> DESIGN -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE).", "Deterministic execution, automatic recovery via RECOVER state, and persistent state across restarts."),
    ("ADR-004: Self-Preservation via AST Syntax Guard & Workspace Checkpoints", "A self-modifying agent can break its own source code if it writes invalid syntax.", "Every write_file/edit_file on a .py file backs up the original and validates AST syntax before saving.", "Companion cannot brick itself with syntax errors and can rollback entire workflows."),
    ("ADR-005: Non-Sprawling 4-Document Living Spec-Driven Development (SDD)", "Per-feature spec folders and 1:1 file specs cause massive file sprawl and ADR pile-up.", "Maintain strictly 4 living canonical files in specs/ (PRODUCT.md, ARCHITECTURE.md, CONTRACTS.md, DECISIONS.md) verified bidirectionally against code.", "Zero spec sprawl, self-compacting ADRs, and automated detection of undocumented code."),
    ("ADR-006: AST Dead-Code Hygiene & Windowed Idle Maintenance Routines", "AI agents tend to leave dead code over iterations and waste tokens if background tasks poll blindly.", "Enforce AST dead-code/unused-import verification in system_doctor and run `<5ms` precheck-gated `on_idle` Maintenance Routines (`companion/routines.py`, `companion/trigger_policy.py`) during allowed idle windows.", "Codebase stays simple and lean while Companion curates knowledge and audits health without wasting tokens."),
    ("ADR-007: Active Project Registry & Rollback Lesson Memory", "Companion needs to remember why rollbacks occurred without leaving tasks falsely completed, and must support working on external projects via a mechanical toggle rather than AI memory.", "Snapshot/rewind work items and ADRs inside checkpoints while recording rollback_lesson notes; enforce WorkspaceContext so tools bind to active_project_root and lock Companion core read-only when working on external projects.", "Companion never relies on LLM memory to stay in the right project folder - path and read-only rules are mechanically enforced."),
    ("ADR-008: Spine-Level Waterfall Telemetry & Headless Core UI Boundary", "Companion needs step-by-step waterfall and KPI telemetry decoupled from any Frontend Web UI.", "Build thread-safe TelemetryEngine directly into llm.chat, tools.dispatch, and agent.run_agent_turn persisting traces, spans, and logs in SQLite while enforcing a Headless Core vs. Presentation UI boundary.", "Automatic SQLite waterfall observability across CLI and Web UI surfaces."),
    ("ADR-009: Two-Tier Progressive Disclosure Skills vs. Primitive Tools", "Hardcoding domain runbooks into Python modules or the system prompt causes bloat.", "Separate L1 atomic tools from L2/L3 declarative skill packs (`skills/<name>/SKILL.md`, `dags/*.py`, `scripts/*.py`) loaded progressively on demand.", "Horizontal scalability for new skills, deterministic Python DAGs, and domain validators with minimal prompt overhead."),
    ("ADR-010: Stdlib LAN Web Surface (surfaces/) & Multi-Phase Autonomous Goal Driver", "Single-turn loops and external web frameworks caused unverified stub code during self-development.", "Serve zero-dependency ThreadingHTTPServer web interfaces in surfaces/ (defaulting to 127.0.0.1) with disconnect-proof async job streaming, and drive multi-step builds via companion/autonomy.py with mechanical tier routing and System Doctor gates.", "Mobile/desktop browser access with zero external dependencies and non-hallucinative feature delivery."),
    ("ADR-011: Mechanical Cognitive Grounding, Phase Tool Gates & Inline Post-Edit Micro-Linting", "Local open-weight models suffer reasoning amnesia between tool steps, waste turns exploring blindly, or make premature out-of-budget edits.", "Carry rolling 1-step <think> scratchpads, rescue text <tool_call> blocks, inject deterministic AST Repo & Line-Budget Maps, enforce phase-scoped AllowedTools, run inline post-edit AST/hygiene/layer micro-linters, and persist lossless resumable phase handoffs.", "Frontier-grade reliability on local models through deterministic Python harness mechanics."),
    ("ADR-012: Interactive Blueprint Approval Gate, Dedicated Coding Tier & Live Flight Eval", "Non-technical product partners need plain-English design/plan review before code edits, while local coding specialists benefit from dedicated IMPLEMENT routing and empirical flight benchmarks.", "Add pause-at-PLAN Blueprint approval/revision (`companion/blueprint.py`), dedicated `coding` tier routing + task carry-forward auto-skip (`companion/compaction.py`, `companion/autonomy.py`), local Git save-points, and isolated behavioral eval (`companion/eval_harness.py`).", "Safe human-in-the-loop steering, higher first-pass implementation quality, and reproducible model benchmarking."),
    ("ADR-013: Web UI Conversation Search & Git Save-Points Surface", "Mobile and desktop browser users need direct access to FTS5 conversation search and recent Git save-points without dropping to CLI commands.", "Expose read-only `GET /api/search?q=<query>&limit=<n>` backed by `MemoryStore.search_conversations()` and include mtime-cached `recent_savepoints` in `GET /api/status` (`surfaces/web_server.py`, `surfaces/web_assets.py`).", "Zero-dependency browser search and save-point visibility with non-blocking WAL reads."),
    ("ADR-014: Domain-Neutral Cognitive Architecture, KnowledgePort, Dynamic GoalPlan DAGs & Skill Forge", "Complex open-ended goals across diverse workspaces require epistemic grounding, uncompromised reasoning routing, action-level risk gates, evidence-verified GoalPlan DAGs, and safe transactional skill promotion without hardcoding domain-specific tools.", "Introduce `KnowledgePort` & `ExecutionTarget` (`companion/grounding.py`), Cognitive Triage (`companion/intake.py`), dynamic `GoalPlan` & pluggable `VerificationSpec` validators (`companion/workflow_dag.py`), and staged capability + DAG promotion (`companion/skill_forge.py`).", "General-purpose reliability across any domain, clean external Wiki/Sandbox ports, and compounding verified capabilities."),
    ("ADR-015: Live SSE Token Streaming, Hybrid Semantic RRF Memory & Loopback/Token-Gated LAN Security", "Desktop/mobile users expect real-time token/phase streaming, conceptual memory recall without GPU swaps, zero-login convenience on localhost, and strict authentication when exposed on LAN/Tailscale.", "Add ContextVar SSE streaming (`companion/streaming.py`, `GET /api/jobs/<id>/stream`), zero-swap hybrid BM25 + semantic RRF retrieval (`companion/semantic_memory.py`), static Web UI assets (`surfaces/static/`), and loopback-default binding (`127.0.0.1`) with `COMPANION_WEB_TOKEN` non-loopback auth plus unconditional `Host`/`Origin`/`Sec-Fetch-Site`/`X-Companion-CSRF` guards (`surfaces/web_server.py`).", "Responsive live UX, meaning-based memory recall in <5ms, zero-login localhost ergonomics, and authenticated LAN/Tailscale safety."),
    ("ADR-016: Companion 3.0 Harvest Backend — Shared CoreActions, Forever-Chat Jobs, Co-Owned Wiki 2.0, Blind Vault, Universal Endpoints, Partner-Aware Federated MCP & Respectful Routines", "Extracting capabilities from legacy homelab/agent systems risked mixing UI/core concerns, leaking secrets in a zero-login LAN environment, cluttering primary chat, or causing downtime cron storms.", "Introduce `CORE_ACTIONS` (`companion/core_actions.py`), Zero-Identity Background Jobs with <=200-token Wiki Inbox overflow (`companion/background_jobs.py`), 4-folder Dual-Author Knowledge Wiki (`companion/wiki_engine.py`), write-only Zero-Reveal `secrets.env` Vault + secret-free backups (`companion/vault.py`), SSH/WinRM/HTTP Connectors (`companion/endpoints.py`), Outbound + Inbound 'Order Counter' MCP Hub with Partner-Aware Federation (`companion/mcp_hub.py`), and Hardware-Gated Routines + Clone Sandboxes (`companion/routines.py`).", "Complete headless multi-machine & knowledge operating system honoring strict downward A-Frame boundaries and zero-reveal LAN security."),
    ("ADR-017: Stage 6 Multi-Studio React + TypeScript Frontend (`frontend/`) Compiling into Zero-Node Stdlib `surfaces/static/`", "Surfacing all 6 Companion 3.0 Harvest studios in raw vanilla JS strings would cause unmaintainable UI sprawl, while adding a runtime Node server would violate `ADR-001`.", "Author a modular React 19 + TypeScript + Vite frontend in `frontend/` with a Matte Brutalist Sci-Fi / Obsidian 4-preset theme system (`obsidian`, `orbital`, `graphite`, `daylight` + 1-click Light/Dark toggle) that compiles deterministically (`iife`) into `surfaces/static/{index.html,styles.css,app.js}` served directly by stdlib `surfaces/web_server.py` (`python harness.py --web`).", "Typed, self-maintainable 6-Studio AI workstation UI with zero runtime Node dependency, 100% offline LAN execution, and strict Ring-0 Python stdlib purity."),
    ("ADR-018: Multi-Provider LLM Registry, Pure-Stdlib Wire Dialects, Blind Vault Key Binding & Mechanical LAN Privacy Lock", "Supporting both local LAN servers (Ollama, vLLM, LM Studio, llama.cpp) and frontier cloud APIs (Gemini, Anthropic Claude, OpenAI, xAI Grok, OpenRouter, DeepSeek, Groq) via static `.env` edits forced restarts, risked API key leakage, and lost provider-native reasoning/multimodal capabilities.", "Introduce `companion/llm_wire.py` (3 pure-stdlib wire/SSE translators: `openai_compat`, `anthropic_messages`, `gemini_native`) and `companion/model_registry.py` (`llm_providers` + `llm_tier_routes` with Zero-Reveal Blind Vault `credential_ref` binding, hot-reload 8-slot capability tier matrix, and mechanical `local_only`-by-default/`hybrid`/`cloud_allowed` outbound privacy lock).", "Instant hot-reload routing across local LAN and frontier models from Web UI and CLI (`/providers`) with zero third-party SDK bloat, zero API key exposure, and prompt-injection-proof LAN privacy."),
    ("ADR-019: Permanent Installed-App Immutability, Orthogonal SDLC & Per-Project Delivery Policy", "In-place self-editing of a live service risked runtime corruption and coupled coding to GitHub assumptions.", "Enforce permanent installed-app immutability (`COMPANION_IMMUTABLE_APP`, locking `settings.root` read-only outside `data/` while keeping `data/scratch/` and external Project clones writable); retire legacy `Self-Dev` toggles and `.sandbox_meta.json` clones; and govern external repo work via per-project `delivery_policy` (`local_only` | `remote_branch_pr` | `ask_me`), `git_credential_ref`, `git_manage`, `dev_preview`, and modular SDLC/Browser/GitHub/DevOps skills.", "Unbrickable daily-driver OS runtime paired with air-gap-safe local SDLC, visual browser dogfooding, Blind-Vault remote Git, and rollback-verified deployments."),
    ("ADR-020: Agent-Native Headless Core Consolidation — Unified TriggerPolicy, Code-Controlled Python DAG Runtime, Universal Project & Goal Graph, Cognitive Triage & MCP Capability Harvesting", "Separate Hobbies vs. Routines subsystems, rigid 20-step JSON workflow recipes, human-chore-coupled Agile boards vs. flat task tables, regex keyword-spotting intake, and manual-only MCP/endpoint wiring prevented true multi-session agent autonomy.", "Consolidate all time/idle triggers into `TriggerPolicy` (`companion/trigger_policy.py`) and `Routines` (`companion/routines.py`); introduce pure-stdlib Python `DAGSpec` execution with `foreach` fan-out/fan-in and SQLite node checkpointing (`companion/dag_runtime.py`); unify tasks, backlog, `/plan`, `/goal`, and Kanban projections onto the domain-blind Universal Project & Goal Graph (`companion/project_graph.py`) with `skills/personal-agile-coach/` in Ring-1; replace regex intake with Cognitive Triage (`companion/intake.py`); and add ApprovalQueue-staged endpoint/MCP self-wiring, MCP-to-Skill harvesting, and pluggable `VerificationSpec` domain validators.", "Cohesive, domain-blind headless core capable of forging deterministic Python DAGs, wiring external tools via human approval, and advancing multi-session Companion + Human goals safely."),
]

SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, tool_call_id TEXT, session_tag TEXT, meta_json TEXT DEFAULT '{}')",
    "CREATE TABLE IF NOT EXISTS system_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT NOT NULL, category TEXT DEFAULT 'general', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS user_profile (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS architecture_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE, context TEXT NOT NULL, decision TEXT NOT NULL, consequences TEXT NOT NULL, status TEXT DEFAULT 'accepted', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_type TEXT NOT NULL, goal TEXT NOT NULL, current_state TEXT NOT NULL, acceptance_criteria TEXT DEFAULT '', checkpoint_id TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}', status TEXT DEFAULT 'active', project_id TEXT DEFAULT 'companion-self', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS workflow_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, summary TEXT NOT NULL, verification_passed INTEGER DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS autonomy_journal (id INTEGER PRIMARY KEY AUTOINCREMENT, entry_name TEXT NOT NULL, entry_kind TEXT DEFAULT 'routine', status TEXT NOT NULL, checkpoint_id TEXT DEFAULT '', doctor_passed INTEGER DEFAULT 1, summary TEXT NOT NULL, seen_by_user INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS knowledge_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT DEFAULT 'companion-self', key TEXT NOT NULL, value TEXT NOT NULL, source TEXT DEFAULT 'agent', confidence REAL DEFAULT 1.0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(project_id, key))",
    "CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, title TEXT NOT NULL, goal_summary TEXT DEFAULT '', kind TEXT DEFAULT 'project', status TEXT DEFAULT 'active', workspace_id TEXT DEFAULT 'companion-self', workspace_path TEXT DEFAULT '', delivery_policy TEXT DEFAULT 'local_only', git_credential_ref TEXT DEFAULT '', max_parallel_workers INTEGER DEFAULT 2, wiki_hub_path TEXT DEFAULT '', risk_ceiling TEXT DEFAULT 'R2', trigger_policy_json TEXT DEFAULT '{}', metadata_json TEXT DEFAULT '{}', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS milestones (milestone_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT DEFAULT '', stage_kind TEXT DEFAULT 'DELIVER_AND_VERIFY', status TEXT DEFAULT 'active', sort_order INTEGER DEFAULT 0, target_date TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS project_timeboxes (timebox_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, goal TEXT DEFAULT '', start_date TEXT NOT NULL, end_date TEXT NOT NULL, status TEXT DEFAULT 'planning', metadata_json TEXT DEFAULT '{}', retro_notes TEXT DEFAULT '', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS work_items (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL DEFAULT 'companion-self', milestone_id TEXT DEFAULT '', timebox_id TEXT DEFAULT '', workflow_id INTEGER, item_kind TEXT NOT NULL DEFAULT 'task', title TEXT NOT NULL, description TEXT DEFAULT '', category TEXT DEFAULT 'general', status TEXT NOT NULL DEFAULT 'pending', owner TEXT NOT NULL DEFAULT 'companion', stage_kind TEXT NOT NULL DEFAULT 'DELIVER_AND_VERIFY', priority INTEGER DEFAULT 3, depends_on_json TEXT DEFAULT '[]', verification_json TEXT DEFAULT '{}', dag_name TEXT DEFAULT '', checkpoint_id TEXT DEFAULT '', acceptance_criteria TEXT DEFAULT '', verification_result TEXT DEFAULT '', active_job_id TEXT DEFAULT '', worktree_path TEXT DEFAULT '', branch_name TEXT DEFAULT '', deliverable_json TEXT DEFAULT '{}', estimated_effort TEXT DEFAULT '', actual_effort TEXT DEFAULT '', estimate_minutes INTEGER DEFAULT 30, planned_date TEXT DEFAULT '', due_date TEXT DEFAULT '', checklist_json TEXT DEFAULT '[]', tags_json TEXT DEFAULT '[]', position INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME)",
    "CREATE INDEX IF NOT EXISTS idx_work_items_proj_status ON work_items(project_id, item_kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_milestones_proj ON milestones(project_id, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_wf_runs_proj_status ON workflow_runs(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_facts_proj_key ON knowledge_facts(project_id, key)",
    "CREATE INDEX IF NOT EXISTS idx_conv_session_tag ON conversations(session_tag)",
)


def _eff_pid(project_id: Optional[str] = None) -> str:
    return (project_id or settings.get_workspace().project_id or SELF_PROJECT_ID).strip()


class MemoryStore:
    """Thread-safe SQLite-backed memory and state store for Companion AI OS."""

    def __init__(self, db_path: Optional[Path] = None):
        self._initialized_schemas: set[str] = set()
        self._closed = False
        self.db_path = Path(db_path or settings.db_path)
        self._lock = get_db_writer_lock(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=15000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._init_and_migrate_db()
        _LIVE_STORES[_db_cache_key(self.db_path)] = self

    @contextlib.contextmanager
    def write_tx(self) -> Iterator[sqlite3.Cursor]:
        """Run a multi-statement SQLite write transaction under the shared DB writer lock with automatic rollback."""
        with sqlite_write_tx(self.conn, self._lock) as cur:
            yield cur

    def ensure_schema(self, key: str, init_fn: Callable[["MemoryStore"], None]) -> None:
        """Run a subsystem DDL initializer at most once per `MemoryStore` instance under `self.write_tx()`."""
        if key in self._initialized_schemas:
            return
        with self.write_tx():
            if key not in self._initialized_schemas:
                init_fn(self)
                self._initialized_schemas.add(key)

    def _fetch_rows(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _fetch_dicts(self, cols: List[str], sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        return [dict(zip(cols, r)) for r in self._fetch_rows(sql, params)]

    def _ensure_project_record(self, project_id: str) -> None:
        slug = re.sub(r"[^a-z0-9-]+", "-", project_id.lower()).strip("-") or "companion-self"
        self.conn.execute(
            "INSERT OR IGNORE INTO projects (project_id, slug, title, kind, workspace_id) VALUES (?, ?, ?, 'project', ?)",
            (project_id, slug, project_id.replace("-", " ").title(), project_id),
        )

    def _init_and_migrate_db(self) -> None:
        with self.write_tx() as cursor:
            for ddl in SCHEMA_SQL:
                cursor.execute(ddl)
            proj_cols = {str(r[1]) for r in cursor.execute("PRAGMA table_info(projects)").fetchall()}
            if "workspace_path" not in proj_cols:
                cursor.execute("ALTER TABLE projects ADD COLUMN workspace_path TEXT DEFAULT ''")
            if "delivery_policy" not in proj_cols:
                cursor.execute("ALTER TABLE projects ADD COLUMN delivery_policy TEXT DEFAULT 'local_only'")
            if "git_credential_ref" not in proj_cols:
                cursor.execute("ALTER TABLE projects ADD COLUMN git_credential_ref TEXT DEFAULT ''")
            if "max_parallel_workers" not in proj_cols:
                cursor.execute("ALTER TABLE projects ADD COLUMN max_parallel_workers INTEGER DEFAULT 2")
            wi_cols = {str(r[1]) for r in cursor.execute("PRAGMA table_info(work_items)").fetchall()}
            for wc_name, wc_def in (("active_job_id", "TEXT DEFAULT ''"), ("worktree_path", "TEXT DEFAULT ''"), ("branch_name", "TEXT DEFAULT ''"), ("deliverable_json", "TEXT DEFAULT '{}'")):
                if wc_name not in wi_cols:
                    cursor.execute(f"ALTER TABLE work_items ADD COLUMN {wc_name} {wc_def}")
            conv_cols = {str(r[1]) for r in cursor.execute("PRAGMA table_info(conversations)").fetchall()}
            if "meta_json" not in conv_cols:
                cursor.execute("ALTER TABLE conversations ADD COLUMN meta_json TEXT DEFAULT '{}'")
            cursor.execute("CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(content)")
            if cursor.execute("SELECT 1 FROM conversations WHERE id NOT IN (SELECT rowid FROM conversations_fts) LIMIT 1").fetchone():
                cursor.execute("INSERT INTO conversations_fts(rowid, content) SELECT id, content FROM conversations WHERE id NOT IN (SELECT rowid FROM conversations_fts)")
            self._ensure_project_record(SELF_PROJECT_ID)
        self._seed_defaults()
        if self.db_path.resolve() == settings.db_path.resolve():
            prof = self.get_user_profile()
            proj_path_str = prof.get("active_project_path", "").strip()
            if proj_path_str and Path(proj_path_str).is_dir():
                settings.set_workspace(Path(proj_path_str), prof.get("active_project_id") or None)
            if not settings.memory_md_path.is_file():
                self.sync_memory_md(target_path=settings.memory_md_path)

    def _seed_defaults(self) -> None:
        with self.write_tx() as cursor:
            existing_map = dict(cursor.execute("SELECT key, value FROM user_profile").fetchall())
            prev_ver = existing_map.get("app_version", "")
            if existing_map and prev_ver != __version__:
                bdir = self.db_path.parent / ".companion_backups"
                bdir.mkdir(parents=True, exist_ok=True)
                with contextlib.closing(sqlite3.connect(str(bdir / f"pre_upgrade_{prev_ver or 'v0'}_to_{__version__}.db"))) as bconn:
                    self.conn.backup(bconn)
            for key, val in DEFAULT_USER_PROFILE.items():
                if key not in existing_map:
                    cursor.execute("INSERT INTO user_profile (key, value) VALUES (?, ?)", (key, val))
            if prev_ver != __version__:
                cursor.execute("INSERT OR REPLACE INTO user_profile (key, value) VALUES ('app_version', ?)", (__version__,))
            existing_by_code = {
                r[0].split(":", 1)[0].strip(): r
                for r in cursor.execute("SELECT title, context, decision, consequences, status FROM architecture_decisions").fetchall()
            }
            for title, context, decision, consequences in FOUNDATIONAL_ADRS:
                code = title.split(":", 1)[0].strip()
                prev_row = existing_by_code.get(code)
                if prev_row is None:
                    cursor.execute("INSERT INTO architecture_decisions (title, context, decision, consequences, status) VALUES (?, ?, ?, ?, 'accepted')", (title, context, decision, consequences))
                elif prev_row[4] == "accepted" and prev_row[:4] != (title, context, decision, consequences):
                    cursor.execute("UPDATE architecture_decisions SET title = ?, context = ?, decision = ?, consequences = ? WHERE title = ?", (title, context, decision, consequences, prev_row[0]))

    def log_message(
        self, role: str, content: str, tool_call_id: Optional[str] = None,
        session_tag: Optional[str] = None, meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        safe_text = redact_secrets(str(content))
        safe_meta = redact_secrets(json.dumps(meta, ensure_ascii=False)) if meta else "{}"
        with self.write_tx() as cursor:
            cursor.execute("INSERT INTO conversations (role, content, tool_call_id, session_tag, meta_json) VALUES (?, ?, ?, ?, ?)", (role, safe_text, tool_call_id, session_tag, safe_meta))
            row_id = int(cursor.lastrowid)
            cursor.execute("INSERT OR REPLACE INTO conversations_fts(rowid, content) VALUES (?, ?)", (row_id, safe_text))
            return row_id

    def get_recent_messages(self, limit: int = 50, role_filter: Optional[str] = None) -> List[Tuple[str, str, str]]:
        if role_filter:
            return self._fetch_rows("SELECT role, content, timestamp FROM conversations WHERE role = ? ORDER BY id DESC LIMIT ?", (role_filter, limit))
        return self._fetch_rows("SELECT role, content, timestamp FROM conversations ORDER BY id DESC LIMIT ?", (limit,))

    def get_conversation_history(self, limit: int = 100) -> List[Tuple[str, str]]:
        return self._fetch_rows("SELECT role, content FROM (SELECT id, role, content FROM conversations ORDER BY id DESC LIMIT ?) ORDER BY id ASC", (limit,))

    def search_conversations(self, query: str = "", limit: int = 20, time_window: Optional[str] = None) -> List[Tuple[int, str, str, str]]:
        bounds = parse_time_window(time_window) if time_window else None
        clean_q = (query or "").strip()
        if not clean_q:
            where_sql = "WHERE timestamp BETWEEN ? AND ? " if bounds else ""
            params: Tuple[Any, ...] = (*bounds, limit) if bounds else (limit,)
            return self._fetch_rows(f"SELECT id, role, content, timestamp FROM conversations {where_sql}ORDER BY id DESC LIMIT ?", params)
        tokens = [t for t in re.findall(r"[a-zA-Z0-9_]+", clean_q) if t.lower() not in ("and", "or")]
        fts_rows: List[Tuple[int, str, str, str]] = []
        with self._lock:
            cursor = self.conn.cursor()
            if tokens:
                for joiner in (" AND ", " OR "):
                    fts_q = joiner.join(f'"{t}"*' for t in tokens)
                    sql = "SELECT c.id, c.role, c.content, c.timestamp FROM conversations_fts f JOIN conversations c ON c.id = f.rowid WHERE conversations_fts MATCH ? " + ("AND c.timestamp BETWEEN ? AND ? " if bounds else "") + "ORDER BY rank, c.id DESC LIMIT ?"
                    cursor.execute(sql, (fts_q, *bounds, max(limit * 2, 25)) if bounds else (fts_q, max(limit * 2, 25)))
                    rows = cursor.fetchall()
                    if rows:
                        fts_rows = rows
                        break
            cand_where = "WHERE timestamp BETWEEN ? AND ? " if bounds else ""
            cand_params: Tuple[Any, ...] = (*bounds, 250) if bounds else (250,)
            cursor.execute(f"SELECT id, role, content, timestamp FROM conversations {cand_where}ORDER BY id DESC LIMIT ?", cand_params)
            recent_rows = cursor.fetchall()
        by_id: Dict[int, Tuple[int, str, str, str]] = {int(r[0]): r for r in recent_rows}
        for r in fts_rows:
            by_id[int(r[0])] = r
        ranked_ids = hybrid_rrf_rank(clean_q, [(cid, row[2]) for cid, row in by_id.items()], lexical_ids=[int(r[0]) for r in fts_rows], limit=limit)
        return [by_id[cid] for cid in ranked_ids if cid in by_id]

    def set_user_profile(self, key: str, value: str) -> None:
        k_clean = key.strip()
        if k_clean.lower() in ("immutable_app", "immutable_core", "self_dev_mode", "self_dev_locked") or (k_clean.lower() == "trust_mode" and is_in_agent_tool_turn()):
            raise PermissionError(f"PRIVILEGE GUARD REJECTED: `{k_clean}` is protected and cannot be modified via `set_user_profile`.")
        with self._lock:
            self.conn.execute("INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP", (k_clean, value.strip()))
            self.conn.commit()

    def get_user_profile(self) -> Dict[str, str]:
        return {k: v for k, v in self._fetch_rows("SELECT key, value FROM user_profile ORDER BY key ASC")}

    def is_background_autonomy_enabled(self) -> bool:
        prof = self.get_user_profile()
        val = prof.get("background_autonomy", "off").strip().lower()
        return val in ("on", "true", "1", "enabled")

    def set_background_autonomy(self, enabled: bool) -> None:
        self.set_user_profile("background_autonomy", "on" if enabled else "off")

    def add_task(self, description: str, status: str = "pending", priority: int = 5, acceptance_criteria: Optional[str] = None, workflow_id: Optional[int] = None, project_id: Optional[str] = None, owner: str = "companion", stage_kind: str = "DELIVER_AND_VERIFY") -> int:
        pid = _eff_pid(project_id)
        item_uid = f"wi_{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._ensure_project_record(pid)
            cur = self.conn.execute(
                "INSERT INTO work_items (item_id, project_id, workflow_id, item_kind, title, status, owner, stage_kind, priority, acceptance_criteria) VALUES (?, ?, ?, 'task', ?, ?, ?, ?, ?, ?)",
                (item_uid, pid, workflow_id, description, status, owner, stage_kind, int(priority), acceptance_criteria or ""),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_task(self, task_id: int, status: str, completed_at: Optional[str] = None, verification_result: Optional[str] = None) -> bool:
        if status in ("completed", "done") and not completed_at:
            completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self.conn.execute(
                "UPDATE work_items SET status = ?, completed_at = COALESCE(?, completed_at), verification_result = COALESCE(?, verification_result), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, completed_at, verification_result, int(task_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_pending_tasks(self, project_id: Optional[str] = None) -> List[Tuple[int, str, str, int]]:
        return self._fetch_rows(
            "SELECT id, title, CASE WHEN status = 'ready' THEN 'pending' ELSE status END, priority FROM work_items "
            "WHERE project_id = ? AND item_kind = 'task' AND status IN ('pending', 'ready', 'in_progress', 'blocked', 'waiting_approval', 'verify') "
            "ORDER BY priority ASC, id ASC",
            (_eff_pid(project_id),),
        )

    def get_all_tasks(self, status_filter: Optional[str] = None, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        pid = _eff_pid(project_id)
        cols = ["id", "description", "status", "priority", "acceptance_criteria", "verification_result", "workflow_id", "created_at", "completed_at"]
        where = "WHERE project_id = ? AND item_kind = 'task'"
        params: Tuple[Any, ...] = (pid,)
        if status_filter:
            if status_filter == "pending":
                where += " AND status IN ('pending', 'ready')"
            elif status_filter == "completed":
                where += " AND status IN ('completed', 'done')"
            else:
                where += " AND status = ?"
                params = (pid, status_filter)
        sql = (
            "SELECT id, title, CASE WHEN status = 'ready' THEN 'pending' WHEN status = 'done' THEN 'completed' ELSE status END, "
            f"priority, acceptance_criteria, verification_result, workflow_id, created_at, completed_at FROM work_items {where} ORDER BY priority ASC, id ASC"
        )
        return self._fetch_dicts(cols, sql, params)

    def add_backlog_item(self, description: str, category: str = "general", status: str = "pending", estimated_effort: Optional[str] = None, priority: int = 5, project_id: Optional[str] = None) -> int:
        pid = _eff_pid(project_id)
        item_uid = f"wi_{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._ensure_project_record(pid)
            cur = self.conn.execute(
                "INSERT INTO work_items (item_id, project_id, item_kind, title, category, status, estimated_effort, priority) VALUES (?, ?, 'backlog', ?, ?, ?, ?, ?)",
                (item_uid, pid, description, category, status, estimated_effort or "", int(priority)),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    add_backlog = add_backlog_item

    def update_backlog_item(self, backlog_id: int, status: str, actual_effort: Optional[str] = None, priority: Optional[int] = None) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE work_items SET status = ?, actual_effort = COALESCE(?, actual_effort), priority = COALESCE(?, priority), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, actual_effort, int(priority) if priority is not None else None, int(backlog_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_backlog(self, category: Optional[str] = None, status: Optional[str] = None) -> List[Tuple[int, str, str, str]]:
        clauses: List[Tuple[str, Any]] = [("item_kind = 'backlog'", None)]
        if category:
            clauses.append(("category = ?", category))
        if status:
            if status == "pending":
                clauses.append(("status IN ('pending', 'backlog', 'ready')", None))
            else:
                clauses.append(("status = ?", status))
        where_sql = " WHERE " + " AND ".join(c for c, _ in clauses)
        params = tuple(v for _, v in clauses if v is not None)
        return self._fetch_rows(f"SELECT id, title, category, status FROM work_items{where_sql} ORDER BY priority ASC, id ASC", params)

    def add_system_note(self, note: str, category: str = "general") -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO system_notes (note, category) VALUES (?, ?)", (redact_secrets(note), category))
            self.conn.commit()
            return int(cur.lastrowid)

    def get_system_notes(self, category: Optional[str] = None, limit: int = 30) -> List[Tuple[str, str, str]]:
        if category:
            return self._fetch_rows("SELECT note, category, created_at FROM system_notes WHERE category = ? ORDER BY id DESC LIMIT ?", (category, limit))
        return self._fetch_rows("SELECT note, category, created_at FROM system_notes WHERE category != 'context-compaction' ORDER BY id DESC LIMIT ?", (limit,))

    def upsert_knowledge_fact(self, key: str, value: str, project_id: Optional[str] = None, source: str = "agent", confidence: float = 1.0) -> int:
        pid, k_clean = _eff_pid(project_id), key.strip()[:120]
        with self._lock:
            cur = self.conn.execute("INSERT INTO knowledge_facts (project_id, key, value, source, confidence, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value, source = excluded.source, confidence = excluded.confidence, updated_at = CURRENT_TIMESTAMP", (pid, k_clean, redact_secrets(value), source.strip() or "agent", float(confidence)))
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def query_knowledge_facts(self, query: str = "", project_id: Optional[str] = None, include_global: bool = True, limit: int = 15) -> List[Dict[str, Any]]:
        pid, cols = _eff_pid(project_id), ["id", "project_id", "key", "value", "source", "confidence", "updated_at"]
        pids = (pid, "global") if include_global and pid != "global" else (pid,)
        q_sql = " AND (key LIKE ? OR value LIKE ?)" if (query or "").strip() else ""
        params: Tuple[Any, ...] = (*pids, f"%{query.strip()}%", f"%{query.strip()}%", limit) if q_sql else (*pids, limit)
        return self._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM knowledge_facts WHERE project_id IN ({','.join('?' for _ in pids)}){q_sql} ORDER BY confidence DESC, id DESC LIMIT ?", params)

    def add_adr(self, title: str, context: str, decision: str, consequences: str, status: str = "accepted") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO architecture_decisions (title, context, decision, consequences, status) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(title) DO UPDATE SET context = excluded.context, decision = excluded.decision, consequences = excluded.consequences, status = excluded.status",
                (title, context, decision, consequences, status),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_adrs(self) -> List[Dict[str, Any]]:
        cols = ["id", "title", "context", "decision", "consequences", "status", "created_at"]
        return self._fetch_dicts(cols, "SELECT id, title, context, decision, consequences, status, created_at FROM architecture_decisions ORDER BY CASE WHEN title LIKE 'ADR-%' THEN substr(title, 1, 8) ELSE printf('~%09d', id) END ASC, id ASC")

    def log_autonomy_run(self, entry_name: str, status: str, summary: str, checkpoint_id: str = "", doctor_passed: bool = True, entry_kind: str = "routine") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO autonomy_journal (entry_name, entry_kind, status, checkpoint_id, doctor_passed, summary, seen_by_user) VALUES (?, ?, ?, ?, ?, ?, 0)",
                (entry_name, entry_kind, status, checkpoint_id, 1 if doctor_passed else 0, redact_secrets(summary)),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_autonomy_journal(self, unseen_only: bool = False, mark_seen: bool = False, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT id, entry_name, entry_kind, status, checkpoint_id, doctor_passed, summary, seen_by_user, created_at FROM autonomy_journal" + (" WHERE seen_by_user = 0" if unseen_only else "") + " ORDER BY id DESC LIMIT ?"
        cols = ["id", "entry_name", "entry_kind", "status", "checkpoint_id", "doctor_passed", "summary", "seen_by_user", "created_at"]
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(sql, (limit,))
            rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
            if mark_seen and rows:
                ids = [r["id"] for r in rows]
                self.conn.execute(f"UPDATE autonomy_journal SET seen_by_user = 1 WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))
                self.conn.commit()
            return rows

    def create_workflow_run(self, workflow_type: str, goal: str, initial_state: str, acceptance_criteria: str = "", checkpoint_id: str = "", metadata: Optional[Dict[str, Any]] = None, project_id: Optional[str] = None) -> int:
        pid = _eff_pid(project_id)
        with self.write_tx() as cursor:
            cursor.execute("UPDATE workflow_runs SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE status = 'active' AND project_id = ?", (pid,))
            cursor.execute("INSERT INTO workflow_runs (workflow_type, goal, current_state, acceptance_criteria, checkpoint_id, metadata_json, status, project_id) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)", (workflow_type, goal, initial_state, acceptance_criteria, checkpoint_id, json.dumps(metadata or {}), pid))
            wf_id = int(cursor.lastrowid)
            cursor.execute("INSERT INTO workflow_transitions (workflow_id, from_state, to_state, summary, verification_passed) VALUES (?, 'IDLE', ?, ?, 1)", (wf_id, initial_state, f"Initialized {workflow_type} workflow: {goal}"))
            return wf_id

    def _hydrate_workflow_row(self, row: Optional[Tuple[Any, ...]]) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        cols = ["id", "workflow_type", "goal", "current_state", "acceptance_criteria", "checkpoint_id", "metadata_json", "status", "created_at", "updated_at"]
        data = dict(zip(cols, row))
        try:
            data["metadata"] = json.loads(data.get("metadata_json") or "{}")
        except Exception:
            data["metadata"] = {}
        return data

    def get_active_workflow(self, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        pid = _eff_pid(project_id)
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, workflow_type, goal, current_state, acceptance_criteria, checkpoint_id, metadata_json, status, created_at, updated_at FROM workflow_runs WHERE status = 'active' AND project_id = ? ORDER BY id DESC LIMIT 1", (pid,))
            return self._hydrate_workflow_row(cursor.fetchone())

    def get_resumable_workflow(self, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        pid = _eff_pid(project_id)
        active = self.get_active_workflow(project_id=pid)
        if active and not str(active.get("workflow_type", "")).startswith("dag:"):
            return active
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, workflow_type, goal, current_state, acceptance_criteria, checkpoint_id, metadata_json, status, created_at, updated_at FROM workflow_runs WHERE project_id = ? AND status IN ('active', 'superseded', 'aborted') AND workflow_type NOT LIKE 'dag:%' AND current_state NOT IN ('COMPLETE', 'IDLE') ORDER BY id DESC LIMIT 1", (pid,))
            return self._hydrate_workflow_row(cursor.fetchone())

    def update_workflow_metadata(self, workflow_id: int, updates: Dict[str, Any]) -> None:
        with self.write_tx() as cursor:
            cursor.execute("SELECT metadata_json FROM workflow_runs WHERE id = ?", (workflow_id,))
            row = cursor.fetchone()
            meta: Dict[str, Any] = json.loads(row[0] or "{}") if row and row[0] else {}
            meta.update(updates)
            cursor.execute("UPDATE workflow_runs SET metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(meta), workflow_id))

    def update_workflow_state(self, workflow_id: int, from_state: str, to_state: str, summary: str, status: str = "active", checkpoint_id: Optional[str] = None, verification_passed: bool = True, metadata_updates: Optional[Dict[str, Any]] = None, acceptance_criteria: Optional[str] = None) -> None:
        with self.write_tx() as cursor:
            cursor.execute("SELECT metadata_json, checkpoint_id FROM workflow_runs WHERE id = ?", (workflow_id,))
            row = cursor.fetchone()
            meta: Dict[str, Any] = json.loads(row[0] or "{}") if row and row[0] else {}
            if metadata_updates:
                meta.update(metadata_updates)
            final_cp = checkpoint_id if checkpoint_id is not None else (row[1] if row else "")
            cursor.execute(
                "UPDATE workflow_runs SET current_state = ?, status = ?, checkpoint_id = ?, metadata_json = ?, acceptance_criteria = COALESCE(?, acceptance_criteria), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (to_state, status, final_cp, json.dumps(meta), acceptance_criteria or None, workflow_id),
            )
            cursor.execute("INSERT INTO workflow_transitions (workflow_id, from_state, to_state, summary, verification_passed) VALUES (?, ?, ?, ?, ?)", (workflow_id, from_state, to_state, summary, 1 if verification_passed else 0))

    def restore_superseded_workflow(self, workflow_id: int) -> None:
        with self.write_tx() as cursor:
            cursor.execute("UPDATE workflow_runs SET status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'superseded'", (int(workflow_id),))

    def rebind_workflow_project(self, workflow_id: int, project_id: str, task_ids: Optional[List[int]] = None, checkpoint_id: Optional[str] = None) -> None:
        pid = _eff_pid(project_id)
        with self.write_tx() as cursor:
            self._ensure_project_record(pid)
            cursor.execute("UPDATE workflow_runs SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE status = 'active' AND project_id = ? AND id != ?", (pid, int(workflow_id)))
            cursor.execute("UPDATE workflow_runs SET project_id = ?, checkpoint_id = COALESCE(?, checkpoint_id), updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pid, checkpoint_id, int(workflow_id)))
            cursor.execute("UPDATE work_items SET project_id = ? WHERE workflow_id = ?", (pid, int(workflow_id)))
            if task_ids:
                cursor.execute(f"UPDATE work_items SET project_id = ? WHERE id IN ({','.join('?' for _ in task_ids)})", (pid, *(int(t) for t in task_ids)))

    def get_workflow_transitions(self, workflow_id: int) -> List[Dict[str, Any]]:
        cols = ["id", "from_state", "to_state", "summary", "verification_passed", "created_at"]
        return self._fetch_dicts(cols, "SELECT id, from_state, to_state, summary, verification_passed, created_at FROM workflow_transitions WHERE workflow_id = ? ORDER BY id ASC", (workflow_id,))

    def sync_memory_md(self, target_path: Optional[Path] = None) -> Path:
        default_md = settings.memory_md_path if self.db_path.resolve() == settings.db_path.resolve() else (self.db_path.parent / "MEMORY.md")
        out_path = target_path or default_md
        profile, adrs, active_wf = self.get_user_profile(), self.get_adrs(), self.get_active_workflow()
        pending_tasks, backlog_items, notes = self.get_pending_tasks()[:5], self.get_backlog()[:4], self.get_system_notes(limit=5)
        facts = self.query_knowledge_facts(limit=6)
        core_keys = ("companion_name", "user_name", "role", "companion_tone", "background_autonomy", "active_project_id")
        prof_lines = [f"- **{k}**: {profile[k][:90]}" for k in core_keys if k in profile]
        lines: List[str] = ["# Companion AI OS — Persistent Memory", "", "## 1. User Profile & Partnership Ethos", *prof_lines, "", "## 2. Active Workflow"]
        lines.append(f"- **Workflow #{active_wf['id']}** (`{active_wf['current_state']}`): {active_wf['goal'][:90]}" if active_wf else "- **State**: `IDLE`")
        accepted_adrs = [a for a in adrs if a.get("status", "accepted") == "accepted"]
        adr_codes = ", ".join(str(a["title"]).split(":", 1)[0].strip() for a in accepted_adrs)
        lines.extend(["", "## 3. Architecture Decision Records (ADRs)", f"- Active ({len(accepted_adrs)}): {adr_codes or 'none'}"])
        if facts:
            lines.extend(["", "## 4. Durable Knowledge Facts", *[f"- `{f['key']}`: {str(f['value'])[:80]}" for f in facts]])
        lines.extend(["", "## 5. Active Work & Backlog", *([f"- `[#{tid} | {st}]` {desc[:75]}" for tid, desc, st, _ in pending_tasks] or ["- No pending work items."]), *([f"- `[Backlog #{bid}]` {desc[:65]}" for bid, desc, _c, _s in backlog_items])])
        lines.extend(["", "## 6. Recent Lessons", *([f"- **[{cat}]** {nt[:90]}" for nt, cat, _ in notes] or ["- Use `run_python` for multiline Python snippets."]), ""])
        text = "\n".join(lines)
        if len(text.encode("utf-8")) > 1980:
            text = text[:1950].rsplit("\n", 1)[0] + "\n"
        out_path.write_text(text, encoding="utf-8")
        return out_path

    def export_state_snapshot(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = _eff_pid(project_id)
        out: Dict[str, Any] = {"project_id": pid}
        scoped = (
            ("work_items", " WHERE project_id = ? ORDER BY id ASC", (pid,)),
            ("milestones", " WHERE project_id = ? ORDER BY sort_order ASC", (pid,)),
            ("knowledge_facts", " WHERE project_id = ? ORDER BY id ASC", (pid,)),
        )
        global_tbls = (("architecture_decisions", " ORDER BY id ASC", ()),) if pid == SELF_PROJECT_ID else ()
        with self._lock:
            cur = self.conn.cursor()
            for tbl, where_sql, params in (*scoped, *global_tbls):
                cur.execute(f"SELECT * FROM {tbl}{where_sql}", params)
                cols = [d[0] for d in cur.description]
                out[tbl] = [dict(zip(cols, row)) for row in cur.fetchall()]
        return out

    def restore_state_snapshot(self, snapshot: Dict[str, Any], rollback_reason: str = "", project_id: Optional[str] = None) -> None:
        pid = str(snapshot.get("project_id") or _eff_pid(project_id))
        scoped = (("work_items", " WHERE project_id = ?", (pid,)), ("milestones", " WHERE project_id = ?", (pid,)), ("knowledge_facts", " WHERE project_id = ?", (pid,)))
        global_tbls = (("architecture_decisions", "", ()),) if pid == SELF_PROJECT_ID else ()
        with self.write_tx() as cur:
            for tbl, where_sql, params in (*scoped, *global_tbls):
                if tbl not in snapshot:
                    continue
                cur.execute(f"DELETE FROM {tbl}{where_sql}", params)
                for row in snapshot[tbl]:
                    cols = list(row.keys())
                    cur.execute(f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])
        if rollback_reason.strip():
            self.add_system_note(rollback_reason.strip()[:500], category="rollback_lesson")

    def _is_usable(self) -> bool:
        if self._closed:
            return False
        try:
            with self._lock:
                self.conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        global _store_instance
        self._closed = True
        key = _db_cache_key(self.db_path)
        with _store_lock:
            if _PATH_STORES.get(key) is self:
                _PATH_STORES.pop(key, None)
            if _LIVE_STORES.get(key) is self:
                _LIVE_STORES.pop(key, None)
            if _store_instance is self:
                _store_instance = None
        notify_db_closed(self.db_path)
        with self._lock:
            with contextlib.suppress(Exception):
                self.conn.close()


def _db_cache_key(db_path: Path) -> str:
    resolved = str(Path(db_path).resolve())
    return resolved.lower() if os.name == "nt" else resolved


_store_instance: Optional[MemoryStore] = None
_PATH_STORES: Dict[str, MemoryStore] = {}
_LIVE_STORES: "weakref.WeakValueDictionary[str, MemoryStore]" = weakref.WeakValueDictionary()
_store_lock = threading.Lock()
_STORE_OVERRIDE: contextvars.ContextVar[Optional[MemoryStore]] = contextvars.ContextVar("companion_store_override", default=None)


@contextlib.contextmanager
def memory_store_context(store: Optional[MemoryStore]) -> Iterator[Optional[MemoryStore]]:
    """Bind a scoped `MemoryStore` (and matching `telemetry_db_context`) so all tool dispatches stay isolated."""
    if store is None:
        yield None
        return
    tok = _STORE_OVERRIDE.set(store)
    try:
        with telemetry_db_context(store.db_path):
            yield store
    finally:
        _STORE_OVERRIDE.reset(tok)


def has_memory_store_override() -> bool:
    """Return True when a scoped `memory_store_context` override is currently active."""
    return _STORE_OVERRIDE.get() is not None


def get_memory_store(db_path: Optional[Path] = None) -> MemoryStore:
    """Return the active scoped `MemoryStore`, live/cached per-path store, or default singleton `MemoryStore`."""
    global _store_instance
    if db_path is not None:
        resolved = Path(db_path).resolve()
        key = _db_cache_key(resolved)
        with _store_lock:
            cached = _PATH_STORES.get(key) or _LIVE_STORES.get(key)
            if cached is not None and cached._is_usable():
                return cached
        created = MemoryStore(db_path=resolved)
        with _store_lock:
            _PATH_STORES[key] = created
        return created
    override = _STORE_OVERRIDE.get()
    if override is not None:
        return override
    target_default = settings.db_path.resolve()
    def_key = _db_cache_key(target_default)
    with _store_lock:
        live = _LIVE_STORES.get(def_key)
        if live is not None and live._is_usable():
            return live
        if _store_instance is not None and _store_instance._is_usable():
            return _store_instance
    fresh = MemoryStore(db_path=target_default)
    with _store_lock:
        _store_instance = fresh
        return fresh
