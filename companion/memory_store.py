"""SQLite Persistent Memory, Backlog, Task, ADR, Workflow & Two-Lever Mode Store (A-Frame Domain Layer)."""

import contextlib, contextvars, json, os, re, sqlite3, sys, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from companion import __version__
from companion.config import SELF_PROJECT_ID, is_in_agent_tool_turn, settings
from companion.semantic_memory import hybrid_rrf_rank
from companion.telemetry import redact_secrets, telemetry_db_context


def parse_time_window(window_expr: Optional[str], now: Optional[datetime] = None) -> Optional[Tuple[str, str]]:
    """Resolve relative or explicit time-window expressions into UTC `(start_ts, end_ts)` SQLite bounds."""
    w = (window_expr or "").strip().lower()
    if not w:
        return None
    ref = now or datetime.now(timezone.utc)
    if w == "today":
        return ref.strftime("%Y-%m-%d 00:00:00"), ref.strftime("%Y-%m-%d 23:59:59")
    if w == "yesterday":
        y = ref - timedelta(days=1)
        return y.strftime("%Y-%m-%d 00:00:00"), y.strftime("%Y-%m-%d 23:59:59")
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
    "operating_mode": "Spec-Driven, state-machine-governed execution with AST syntax guards, dead-code hygiene checks, and checkpoint rollback protection.",
    "platform": _detect_runtime_platform(),
    "hobby_mode": "off",
    "self_dev_mode": "off",
    "active_project_id": SELF_PROJECT_ID,
}

FOUNDATIONAL_ADRS: List[Tuple[str, str, str, str]] = [
    ("ADR-001: Zero-Dependency Python Stdlib Foundation", "External dependencies in core agent loops cause environment fragility when self-evolving.", "Keep Ring-0 core (`harness.py`, `companion/`, `surfaces/`, `configs/`, `memory/`) 100% Python 3.12 stdlib while allowing Ring-1 `skills/*/scripts/*.py` to declare isolated PEP 723 `uv run` dependencies.", "Unbrickable zero-dependency core brain paired with an extensible, isolated skill toolbox."),
    ("ADR-002: A-Frame Layered Modular Architecture", "A single script becomes brittle at scale, while unlinked files create dead code.", "Organize Companion into an A-Frame Architecture: Infrastructure -> Domain -> Workflow Crossbar -> Application -> Presentation Apex.", "Clear separation of concerns, SOLID compliance, and backward compatibility via harness.py."),
    ("ADR-003: Deterministic State-Machine Graph for Engineering & Self-Evolution", "Unstructured tool loops hit MAX_TOOL_ITERS or skip design/testing on multi-step tasks.", "Enforce a persistent directed-graph state machine (INTAKE -> DISCOVER -> DESIGN -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE).", "Deterministic execution, automatic recovery via RECOVER state, and persistent state across restarts."),
    ("ADR-004: Self-Preservation via AST Syntax Guard & Workspace Checkpoints", "A self-modifying agent can break its own source code if it writes invalid syntax.", "Every write_file/edit_file on a .py file backs up the original and validates AST syntax before saving.", "Companion cannot brick itself with syntax errors and can rollback entire workflows."),
    ("ADR-005: Non-Sprawling 4-Document Living Spec-Driven Development (SDD)", "Per-feature spec folders and 1:1 file specs cause massive file sprawl and ADR pile-up.", "Maintain strictly 4 living canonical files in specs/ (PRODUCT.md, ARCHITECTURE.md, CONTRACTS.md, DECISIONS.md) verified bidirectionally against code.", "Zero spec sprawl, self-compacting ADRs, and automated detection of undocumented code."),
    ("ADR-006: AST Dead-Code Hygiene & Bounded Autonomous Agent Hobbies", "AI agents tend to leave dead code over iterations and sit idle overnight without structured bounds.", "Enforce AST dead-code/unused-import verification in system_doctor and run checkpoint-guarded Autonomous Hobbies during idle windows.", "Codebase stays simple and lean while Companion safely improves itself during idle windows."),
    ("ADR-007: Active Project Registry & Rollback Lesson Memory", "Companion needs to remember why rollbacks occurred without leaving tasks falsely completed, and must support working on external projects via a mechanical toggle rather than AI memory.", "Snapshot/rewind tasks, backlog, and ADRs inside checkpoints while recording rollback_lesson notes; enforce WorkspaceContext so tools bind to active_project_root and lock Companion core read-only when working on external projects.", "Companion never relies on LLM memory to stay in the right project folder - path and read-only rules are mechanically enforced."),
    ("ADR-008: Spine-Level Waterfall Telemetry & Headless Core UI Boundary", "Companion needs step-by-step waterfall and KPI telemetry decoupled from any Frontend Web UI.", "Build thread-safe TelemetryEngine directly into llm.chat, tools.dispatch, and agent.run_agent_turn persisting traces, spans, and logs in SQLite while enforcing a Headless Core vs. Presentation UI boundary.", "Automatic SQLite waterfall observability across CLI and Web UI surfaces."),
    ("ADR-009: Two-Tier Progressive Disclosure Skills vs. Primitive Tools", "Hardcoding domain runbooks into Python modules or the system prompt causes bloat.", "Separate L1 atomic tools from L2/L3 declarative skill packs (skills/<name>/SKILL.md) loaded progressively on demand.", "Horizontal scalability for new skills and workflows with minimal prompt overhead."),
    ("ADR-010: Stdlib LAN Web Surface (surfaces/) & Multi-Phase Autonomous Goal Driver", "Single-turn loops and external web frameworks caused unverified stub code during self-development.", "Serve zero-dependency ThreadingHTTPServer web interfaces in surfaces/ (defaulting to 127.0.0.1) with disconnect-proof async job streaming, and drive multi-step builds via companion/autonomy.py with mechanical tier routing and System Doctor gates.", "Mobile/desktop browser access with zero external dependencies and non-hallucinative feature delivery."),
    ("ADR-011: Mechanical Cognitive Grounding, Phase Tool Gates & Inline Post-Edit Micro-Linting", "Local open-weight models suffer reasoning amnesia between tool steps, waste turns exploring blindly, or make premature out-of-budget edits.", "Carry rolling 1-step <think> scratchpads, rescue text <tool_call> blocks, inject deterministic AST Repo & Line-Budget Maps, enforce phase-scoped AllowedTools, run inline post-edit AST/hygiene/layer micro-linters, and persist lossless resumable phase handoffs.", "Frontier-grade reliability on local models through deterministic Python harness mechanics."),
    ("ADR-012: Interactive Blueprint Approval Gate, Dedicated Coding Tier & Live Flight Eval", "Non-technical product partners need plain-English design/plan review before code edits, while local coding specialists benefit from dedicated IMPLEMENT routing and empirical flight benchmarks.", "Add pause-at-PLAN Blueprint approval/revision (`companion/blueprint.py`), dedicated `coding` tier routing + task carry-forward auto-skip (`companion/compaction.py`, `companion/autonomy.py`), local Git save-points, and isolated 3-flight behavioral eval (`companion/eval_harness.py`).", "Safe human-in-the-loop steering, higher first-pass implementation quality, and reproducible model benchmarking."),
    ("ADR-013: Web UI Conversation Search & Git Save-Points Surface", "Mobile and desktop browser users need direct access to FTS5 conversation search and recent Git save-points without dropping to CLI commands.", "Expose read-only `GET /api/search?q=<query>&limit=<n>` backed by `MemoryStore.search_conversations()` and include mtime-cached `recent_savepoints` in `GET /api/status` (`surfaces/web_server.py`, `surfaces/web_assets.py`).", "Zero-dependency browser search and save-point visibility with non-blocking WAL reads."),
    ("ADR-014: Domain-Neutral Cognitive Architecture, KnowledgePort, Dynamic GoalPlan DAGs & Skill Forge", "Complex open-ended goals across diverse workspaces require epistemic grounding, uncompromised reasoning routing, action-level risk gates, evidence-verified GoalPlan DAGs, and safe transactional skill promotion without hardcoding domain-specific tools.", "Introduce `KnowledgePort` & `ExecutionTarget` (`companion/grounding.py`), readiness-aware uncompromised reasoning routing (`companion/intake.py`), dynamic `GoalPlan` DAGs (`companion/workflow_dag.py`), and staged capability promotion + `REFLECT` distillation (`companion/skill_forge.py`).", "General-purpose reliability across any domain, clean external Wiki/Sandbox ports, and compounding verified capabilities."),
    ("ADR-015: Live SSE Token Streaming, Hybrid Semantic RRF Memory & Loopback/Token-Gated LAN Security", "Desktop/mobile users expect real-time token/phase streaming, conceptual memory recall without GPU swaps, zero-login convenience on localhost, and strict authentication when exposed on LAN/Tailscale.", "Add ContextVar SSE streaming (`companion/streaming.py`, `GET /api/jobs/<id>/stream`), zero-swap hybrid BM25 + semantic RRF retrieval (`companion/semantic_memory.py`), static Web UI assets (`surfaces/static/`), and loopback-default binding (`127.0.0.1`) with `COMPANION_WEB_TOKEN` non-loopback auth plus unconditional `Host`/`Origin`/`Sec-Fetch-Site`/`X-Companion-CSRF` guards (`surfaces/web_server.py`).", "Responsive live UX, meaning-based memory recall in <5ms, zero-login localhost ergonomics, and authenticated LAN/Tailscale safety."),
    ("ADR-016: Companion 3.0 Harvest Backend — Shared CoreActions, Forever-Chat Jobs, Co-Owned Wiki 2.0, Blind Vault, Universal Endpoints, Partner-Aware Federated MCP & Respectful Routines", "Extracting capabilities from legacy homelab/agent systems risked mixing UI/core concerns, leaking secrets in a zero-login LAN environment, cluttering primary chat, or causing downtime cron storms.", "Introduce `CORE_ACTIONS` (`companion/core_actions.py`), Zero-Identity Background Jobs with <=200-token Wiki Inbox overflow (`companion/background_jobs.py`), 4-folder Dual-Author Knowledge Wiki (`companion/wiki_engine.py`), write-only Zero-Reveal `secrets.env` Vault + secret-free backups (`companion/vault.py`), SSH/WinRM/HTTP Connectors (`companion/endpoints.py`), Outbound + Inbound 'Order Counter' MCP Hub with Partner-Aware Federation (`companion/mcp_hub.py`), and Hardware-Gated Routines + Clone Sandboxes (`companion/routines.py`).", "Complete headless multi-machine & knowledge operating system honoring strict downward A-Frame boundaries and zero-reveal LAN security."),
    ("ADR-017: Stage 6 Multi-Studio React + TypeScript Frontend (`frontend/`) Compiling into Zero-Node Stdlib `surfaces/static/`", "Surfacing all 6 Companion 3.0 Harvest studios (Forever Chat, Wiki & 2D Canvas Mind Map, Jobs & Approvals, Blind Vault & Endpoints, Federation & MCP, Routines & Ops) in raw vanilla JS strings would cause unmaintainable UI sprawl, while adding a runtime Node server would violate `ADR-001`.", "Author a modular React 19 + TypeScript + Vite frontend in `frontend/` with a Matte Brutalist Sci-Fi / Obsidian 4-preset theme system (`obsidian`, `orbital`, `graphite`, `daylight` + 1-click Light/Dark toggle) that compiles deterministically (`iife`) into `surfaces/static/{index.html,styles.css,app.js}` served directly by stdlib `surfaces/web_server.py` (`python harness.py --web`).", "Typed, self-maintainable 6-Studio AI workstation UI with zero runtime Node dependency, 100% offline LAN execution, and strict Ring-0 Python stdlib purity."),
    ("ADR-018: Multi-Provider LLM Registry, Pure-Stdlib Wire Dialects, Blind Vault Key Binding & Mechanical LAN Privacy Lock", "Supporting both local LAN servers (Ollama, vLLM, LM Studio, llama.cpp) and frontier cloud APIs (Gemini, Anthropic Claude, OpenAI, xAI Grok, OpenRouter, DeepSeek, Groq) via static `.env` edits forced restarts, risked API key leakage, and lost provider-native reasoning/multimodal capabilities (`thoughtSignature`, Extended Thinking signatures, native audio/video).", "Introduce `companion/llm_wire.py` (3 pure-stdlib wire/SSE translators: `openai_compat`, `anthropic_messages`, `gemini_native`) and `companion/model_registry.py` (`llm_providers` + `llm_tier_routes` with Zero-Reveal Blind Vault `credential_ref` binding, hot-reload 8-slot capability tier matrix, and mechanical `local_only`-by-default/`hybrid`/`cloud_allowed` outbound privacy lock).", "Instant hot-reload routing across local LAN and frontier models from Web UI (`System -> Models, Tiers & Context`) and CLI (`/providers`) with zero third-party SDK bloat, zero API key exposure, and prompt-injection-proof LAN privacy."),
    ("ADR-019: Independent Two-Lever Operating Architecture (`Hobby Mode` x `Self-Dev Mode`), Mechanical Read-Only Core Lock & 2-Track Hobby Split", "Running a daily-driver Companion release with background hobbies risked unrequested code mutations, while coupling hobbies to self-evolution prevented quiet personal knowledge/study maintenance on stable builds or isolated Lab/VM self-evolution.", "Maintain `Hobby Mode` (`on`/`off`) and `Self-Dev Mode` (`on`/`off`, seedable/lockable via `COMPANION_SELF_DEV`, `COMPANION_SELF_DEV_LOCKED`, `COMPANION_IMMUTABLE_CORE`) as two independent levers; mechanically lock Companion core files read-only (`safety.py`, `checkpoints.py`, `grounding.py`, `skill_forge.py`) while keeping `data/` and external `/project` workspaces writable when `Self-Dev Mode = OFF`; forbid LLM tool turns or subprocesses from enabling `Self-Dev Mode`; and split hobbies into `personal` vs `self_dev` tracks with CLI + Web UI `System` Studio + Web idle parity.", "Safe stable daily-driver operation with personal/ops hobbies (`wiki_gardener`, `memory_curator`, `trace_reviewer`, `life_study_organizer`) alongside deliberate or full Lab/VM self-evolution when `Self-Dev Mode` is explicitly enabled by the human partner."),
]

SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, tool_call_id TEXT, session_tag TEXT)",
    "CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME, priority INTEGER DEFAULT 5, acceptance_criteria TEXT, verification_result TEXT, workflow_id INTEGER, project_id TEXT DEFAULT 'companion-self')",
    "CREATE TABLE IF NOT EXISTS backlog (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL, category TEXT DEFAULT 'general', status TEXT DEFAULT 'pending', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, estimated_effort TEXT, actual_effort TEXT, priority INTEGER DEFAULT 5)",
    "CREATE TABLE IF NOT EXISTS system_notes (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT NOT NULL, category TEXT DEFAULT 'general', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS user_profile (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS architecture_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE, context TEXT NOT NULL, decision TEXT NOT NULL, consequences TEXT NOT NULL, status TEXT DEFAULT 'accepted', created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS workflow_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_type TEXT NOT NULL, goal TEXT NOT NULL, current_state TEXT NOT NULL, acceptance_criteria TEXT DEFAULT '', checkpoint_id TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}', status TEXT DEFAULT 'active', project_id TEXT DEFAULT 'companion-self', created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS workflow_transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, summary TEXT NOT NULL, verification_passed INTEGER DEFAULT 1, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS hobby_journal (id INTEGER PRIMARY KEY AUTOINCREMENT, hobby_name TEXT NOT NULL, status TEXT NOT NULL, checkpoint_id TEXT DEFAULT '', doctor_passed INTEGER DEFAULT 1, summary TEXT NOT NULL, seen_by_user INTEGER DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
    "CREATE TABLE IF NOT EXISTS knowledge_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT DEFAULT 'companion-self', key TEXT NOT NULL, value TEXT NOT NULL, source TEXT DEFAULT 'agent', confidence REAL DEFAULT 1.0, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(project_id, key))",
    "CREATE INDEX IF NOT EXISTS idx_tasks_proj_status ON tasks(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_wf_runs_proj_status ON workflow_runs(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_facts_proj_key ON knowledge_facts(project_id, key)",
    "CREATE INDEX IF NOT EXISTS idx_conv_session_tag ON conversations(session_tag)",
)


def _eff_pid(project_id: Optional[str] = None) -> str:
    return (project_id or settings.get_workspace().project_id or SELF_PROJECT_ID).strip()


class MemoryStore:
    """Thread-safe SQLite-backed memory and state store for Companion AI OS."""

    def __init__(self, db_path: Optional[Path] = None):
        self._lock = threading.RLock()
        self._initialized_schemas: set[str] = set()
        self.db_path = Path(db_path or settings.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=15000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._init_and_migrate_db()

    def ensure_schema(self, key: str, init_fn: Callable[["MemoryStore"], None]) -> None:
        """Run a subsystem DDL initializer at most once per `MemoryStore` instance under `self._lock`."""
        if key in self._initialized_schemas:
            return
        with self._lock:
            if key not in self._initialized_schemas:
                init_fn(self)
                self.conn.commit()
                self._initialized_schemas.add(key)

    def _table_columns(self, table_name: str) -> set[str]:
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    def _ensure_column(self, table: str, column: str, col_def: str) -> None:
        if column not in self._table_columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def _fetch_rows(self, sql: str, params: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _fetch_dicts(self, cols: List[str], sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        return [dict(zip(cols, r)) for r in self._fetch_rows(sql, params)]

    def _init_and_migrate_db(self) -> None:
        cursor = self.conn.cursor()
        for ddl in SCHEMA_SQL:
            cursor.execute(ddl)
        cursor.execute("CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(content)")
        if cursor.execute(
            "SELECT 1 FROM conversations WHERE id NOT IN (SELECT rowid FROM conversations_fts) LIMIT 1"
        ).fetchone():
            cursor.execute(
                "INSERT INTO conversations_fts(rowid, content) "
                "SELECT id, content FROM conversations WHERE id NOT IN (SELECT rowid FROM conversations_fts)"
            )
        self._ensure_column("tasks", "acceptance_criteria", "TEXT")
        self._ensure_column("tasks", "verification_result", "TEXT")
        self._ensure_column("tasks", "workflow_id", "INTEGER")
        self._ensure_column("tasks", "project_id", f"TEXT DEFAULT '{SELF_PROJECT_ID}'")
        self._ensure_column("workflow_runs", "project_id", f"TEXT DEFAULT '{SELF_PROJECT_ID}'")
        self._ensure_column("backlog", "priority", "INTEGER DEFAULT 5")
        self.conn.commit()
        self._seed_defaults()
        if self.db_path.resolve() == settings.db_path.resolve():
            prof = self.get_user_profile()
            proj_path_str = prof.get("active_project_path", "").strip()
            if proj_path_str and Path(proj_path_str).is_dir():
                settings.set_workspace(Path(proj_path_str), prof.get("active_project_id") or None)
            if not settings.memory_md_path.is_file():
                self.sync_memory_md(target_path=settings.memory_md_path)

    def _seed_defaults(self) -> None:
        cursor = self.conn.cursor()
        existing_map = dict(cursor.execute("SELECT key, value FROM user_profile").fetchall())
        prev_ver = existing_map.get("app_version", "")
        dirty = False
        if existing_map and prev_ver != __version__:
            bdir = self.db_path.parent / ".companion_backups"
            bdir.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(bdir / f"pre_upgrade_{prev_ver or 'v0'}_to_{__version__}.db"))) as bconn:
                self.conn.backup(bconn)
        for key, val in DEFAULT_USER_PROFILE.items():
            if key not in existing_map:
                seed_val = ("on" if settings.self_dev_default else "off") if key == "self_dev_mode" else val
                cursor.execute("INSERT INTO user_profile (key, value) VALUES (?, ?)", (key, seed_val))
                dirty = True
        if prev_ver != __version__:
            cursor.execute("INSERT OR REPLACE INTO user_profile (key, value) VALUES ('app_version', ?)", (__version__,))
            dirty = True
        existing_by_code = {
            r[0].split(":", 1)[0].strip(): r
            for r in cursor.execute("SELECT title, context, decision, consequences, status FROM architecture_decisions").fetchall()
        }
        for title, context, decision, consequences in FOUNDATIONAL_ADRS:
            code = title.split(":", 1)[0].strip()
            prev_row = existing_by_code.get(code)
            if prev_row is None:
                cursor.execute("INSERT INTO architecture_decisions (title, context, decision, consequences, status) VALUES (?, ?, ?, ?, 'accepted')", (title, context, decision, consequences))
                dirty = True
            elif prev_row[4] == "accepted" and prev_row[:4] != (title, context, decision, consequences):
                cursor.execute("UPDATE architecture_decisions SET title = ?, context = ?, decision = ?, consequences = ? WHERE title = ?", (title, context, decision, consequences, prev_row[0]))
                dirty = True
        if dirty:
            self.conn.commit()

    def log_message(self, role: str, content: str, tool_call_id: Optional[str] = None, session_tag: Optional[str] = None) -> int:
        safe_text = redact_secrets(str(content))
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO conversations (role, content, tool_call_id, session_tag) VALUES (?, ?, ?, ?)", (role, safe_text, tool_call_id, session_tag))
            row_id = int(cursor.lastrowid)
            cursor.execute("INSERT OR REPLACE INTO conversations_fts(rowid, content) VALUES (?, ?)", (row_id, safe_text))
            self.conn.commit()
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
        if k_clean.lower() in ("self_dev_mode", "self_dev_locked", "immutable_core"):
            raise PermissionError("PRIVILEGE GUARD REJECTED: `self_dev_mode` can only be changed via explicit human surfaces (`/selfdev` or Web System Studio), never via `set_user_profile`.")
        with self._lock:
            self.conn.execute("INSERT INTO user_profile (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP", (k_clean, value.strip()))
            self.conn.commit()

    def get_user_profile(self) -> Dict[str, str]:
        return {k: v for k, v in self._fetch_rows("SELECT key, value FROM user_profile ORDER BY key ASC")}

    def is_hobby_mode_enabled(self) -> bool:
        return self.get_user_profile().get("hobby_mode", "off").strip().lower() in ("on", "true", "1", "enabled")

    def set_hobby_mode(self, enabled: bool) -> None:
        self.set_user_profile("hobby_mode", "on" if enabled else "off")

    def is_self_dev_mode_enabled(self) -> bool:
        override = settings.get_self_dev_override()
        if override is not None:
            return bool(override)
        if settings.immutable_core:
            return False
        if settings.self_dev_locked:
            return bool(settings.self_dev_default)
        default_str = "on" if settings.self_dev_default else "off"
        return self.get_user_profile().get("self_dev_mode", default_str).strip().lower() in ("on", "true", "1", "enabled")

    def set_self_dev_mode(self, enabled: bool, actor: str = "user") -> None:
        if is_in_agent_tool_turn() or (actor or "").strip().lower() not in ("user", "cli", "web"):
            raise PermissionError("PRIVILEGE GUARD REJECTED: Companion LLM/tools cannot toggle `Self-Dev Mode`; only explicit human CLI (`/selfdev`) or Web System Studio actions may change it.")
        if settings.is_self_dev_locked_by_env():
            raise PermissionError(f"DEPLOYMENT LOCK REJECTED: `Self-Dev Mode` cannot be changed at runtime ({settings.self_dev_lock_reason()}).")
        with self._lock:
            self.conn.execute("INSERT INTO user_profile (key, value, updated_at) VALUES ('self_dev_mode', ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP", ("on" if enabled else "off",))
            self.conn.commit()

    def add_task(self, description: str, status: str = "pending", priority: int = 5, acceptance_criteria: Optional[str] = None, workflow_id: Optional[int] = None, project_id: Optional[str] = None) -> int:
        pid = _eff_pid(project_id)
        with self._lock:
            cur = self.conn.execute("INSERT INTO tasks (description, status, priority, acceptance_criteria, workflow_id, project_id) VALUES (?, ?, ?, ?, ?, ?)", (description, status, int(priority), acceptance_criteria, workflow_id, pid))
            self.conn.commit()
            return int(cur.lastrowid)

    def update_task(self, task_id: int, status: str, completed_at: Optional[str] = None, verification_result: Optional[str] = None) -> bool:
        if status == "completed" and not completed_at:
            completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self.conn.execute("UPDATE tasks SET status = ?, completed_at = COALESCE(?, completed_at), verification_result = COALESCE(?, verification_result) WHERE id = ?", (status, completed_at, verification_result, task_id))
            self.conn.commit()
            return cur.rowcount > 0

    def get_pending_tasks(self, project_id: Optional[str] = None) -> List[Tuple[int, str, str, int]]:
        return self._fetch_rows("SELECT id, description, status, priority FROM tasks WHERE project_id = ? AND status IN ('pending', 'in_progress') ORDER BY priority ASC, id ASC", (_eff_pid(project_id),))

    def get_all_tasks(self, status_filter: Optional[str] = None, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        pid, cols = _eff_pid(project_id), ["id", "description", "status", "priority", "acceptance_criteria", "verification_result", "workflow_id", "created_at", "completed_at"]
        sql = f"SELECT {', '.join(cols)} FROM tasks WHERE project_id = ?" + (" AND status = ?" if status_filter else "") + " ORDER BY priority ASC, id ASC"
        return self._fetch_dicts(cols, sql, (pid, status_filter) if status_filter else (pid,))

    def add_backlog_item(self, description: str, category: str = "general", status: str = "pending", estimated_effort: Optional[str] = None, priority: int = 5) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO backlog (description, category, status, estimated_effort, priority) VALUES (?, ?, ?, ?, ?)", (description, category, status, estimated_effort, int(priority)))
            self.conn.commit()
            return int(cur.lastrowid)

    add_backlog = add_backlog_item

    def update_backlog_item(self, backlog_id: int, status: str, actual_effort: Optional[str] = None, priority: Optional[int] = None) -> bool:
        with self._lock:
            cur = self.conn.execute("UPDATE backlog SET status = ?, actual_effort = COALESCE(?, actual_effort), priority = COALESCE(?, priority) WHERE id = ?", (status, actual_effort, int(priority) if priority is not None else None, backlog_id))
            self.conn.commit()
            return cur.rowcount > 0

    def get_backlog(self, category: Optional[str] = None, status: Optional[str] = None) -> List[Tuple[int, str, str, str]]:
        clauses = ([("category = ?", category)] if category else []) + ([("status = ?", status)] if status else [])
        where_sql = (" WHERE " + " AND ".join(c for c, _ in clauses)) if clauses else ""
        return self._fetch_rows(f"SELECT id, description, category, status FROM backlog{where_sql} ORDER BY priority ASC, id ASC", tuple(v for _, v in clauses))

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

    def log_hobby_run(self, hobby_name: str, status: str, summary: str, checkpoint_id: str = "", doctor_passed: bool = True) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO hobby_journal (hobby_name, status, checkpoint_id, doctor_passed, summary, seen_by_user) VALUES (?, ?, ?, ?, ?, 0)", (hobby_name, status, checkpoint_id, 1 if doctor_passed else 0, summary))
            self.conn.commit()
            return int(cur.lastrowid)

    def get_hobby_journal(self, unseen_only: bool = False, mark_seen: bool = False, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT id, hobby_name, status, checkpoint_id, doctor_passed, summary, seen_by_user, created_at FROM hobby_journal" + (" WHERE seen_by_user = 0" if unseen_only else "") + " ORDER BY id DESC LIMIT ?"
        cols = ["id", "hobby_name", "status", "checkpoint_id", "doctor_passed", "summary", "seen_by_user", "created_at"]
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(sql, (limit,))
            rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
            if mark_seen and rows:
                ids = [r["id"] for r in rows]
                self.conn.execute(f"UPDATE hobby_journal SET seen_by_user = 1 WHERE id IN ({','.join('?' for _ in ids)})", tuple(ids))
                self.conn.commit()
            return rows

    def create_workflow_run(self, workflow_type: str, goal: str, initial_state: str, acceptance_criteria: str = "", checkpoint_id: str = "", metadata: Optional[Dict[str, Any]] = None, project_id: Optional[str] = None) -> int:
        pid = _eff_pid(project_id)
        with self._lock:
            self.conn.execute("UPDATE workflow_runs SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE status = 'active' AND project_id = ?", (pid,))
            cursor = self.conn.cursor()
            cursor.execute("INSERT INTO workflow_runs (workflow_type, goal, current_state, acceptance_criteria, checkpoint_id, metadata_json, status, project_id) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)", (workflow_type, goal, initial_state, acceptance_criteria, checkpoint_id, json.dumps(metadata or {}), pid))
            wf_id = int(cursor.lastrowid)
            cursor.execute("INSERT INTO workflow_transitions (workflow_id, from_state, to_state, summary, verification_passed) VALUES (?, 'IDLE', ?, ?, 1)", (wf_id, initial_state, f"Initialized {workflow_type} workflow: {goal}"))
            self.conn.commit()
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
        """Return the active workflow, or the latest non-recipe incomplete workflow (`superseded`/`aborted`) with saved state."""
        pid = _eff_pid(project_id)
        active = self.get_active_workflow(project_id=pid)
        if active and not str(active.get("workflow_type", "")).startswith("recipe:"):
            return active
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, workflow_type, goal, current_state, acceptance_criteria, checkpoint_id, metadata_json, status, created_at, updated_at FROM workflow_runs WHERE project_id = ? AND status IN ('active', 'superseded', 'aborted') AND workflow_type NOT LIKE 'recipe:%' AND current_state NOT IN ('COMPLETE', 'IDLE') ORDER BY id DESC LIMIT 1", (pid,))
            return self._hydrate_workflow_row(cursor.fetchone())

    def update_workflow_metadata(self, workflow_id: int, updates: Dict[str, Any]) -> None:
        """Persist lossless phase handoffs or task progress into `workflow_runs.metadata_json`."""
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT metadata_json FROM workflow_runs WHERE id = ?", (workflow_id,))
            row = cursor.fetchone()
            meta: Dict[str, Any] = json.loads(row[0] or "{}") if row and row[0] else {}
            meta.update(updates)
            cursor.execute("UPDATE workflow_runs SET metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (json.dumps(meta), workflow_id))
            self.conn.commit()

    def update_workflow_state(self, workflow_id: int, from_state: str, to_state: str, summary: str, status: str = "active", checkpoint_id: Optional[str] = None, verification_passed: bool = True, metadata_updates: Optional[Dict[str, Any]] = None, acceptance_criteria: Optional[str] = None) -> None:
        with self._lock:
            cursor = self.conn.cursor()
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
            self.conn.commit()

    def restore_superseded_workflow(self, workflow_id: int) -> None:
        """Restore a temporarily superseded parent workflow back to `active` after a nested/recipe run."""
        with self._lock:
            self.conn.execute("UPDATE workflow_runs SET status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'superseded'", (int(workflow_id),))
            self.conn.commit()

    def rebind_workflow_project(self, workflow_id: int, project_id: str, task_ids: Optional[List[int]] = None, checkpoint_id: Optional[str] = None) -> None:
        """Migrate an active workflow run (and its planned tasks) to a newly switched `WorkspaceContext`."""
        pid = _eff_pid(project_id)
        with self._lock:
            self.conn.execute("UPDATE workflow_runs SET status = 'superseded', updated_at = CURRENT_TIMESTAMP WHERE status = 'active' AND project_id = ? AND id != ?", (pid, int(workflow_id)))
            self.conn.execute("UPDATE workflow_runs SET project_id = ?, checkpoint_id = COALESCE(?, checkpoint_id), updated_at = CURRENT_TIMESTAMP WHERE id = ?", (pid, checkpoint_id, int(workflow_id)))
            self.conn.execute("UPDATE tasks SET project_id = ? WHERE workflow_id = ?", (pid, int(workflow_id)))
            if task_ids:
                self.conn.execute(f"UPDATE tasks SET project_id = ? WHERE id IN ({','.join('?' for _ in task_ids)})", (pid, *(int(t) for t in task_ids)))
            self.conn.commit()

    def get_workflow_transitions(self, workflow_id: int) -> List[Dict[str, Any]]:
        cols = ["id", "from_state", "to_state", "summary", "verification_passed", "created_at"]
        return self._fetch_dicts(cols, "SELECT id, from_state, to_state, summary, verification_passed, created_at FROM workflow_transitions WHERE workflow_id = ? ORDER BY id ASC", (workflow_id,))

    def sync_memory_md(self, target_path: Optional[Path] = None) -> Path:
        """Render the authoritative SQLite memory state into a clean, structured MEMORY.md."""
        default_md = settings.memory_md_path if self.db_path.resolve() == settings.db_path.resolve() else (self.db_path.parent / "MEMORY.md")
        out_path = target_path or default_md
        profile, adrs, active_wf = self.get_user_profile(), self.get_adrs(), self.get_active_workflow()
        pending_tasks, backlog_items, notes = self.get_pending_tasks(), self.get_backlog(), self.get_system_notes(limit=12)

        lines: List[str] = ["# Companion AI OS — Persistent Memory", "Authoritative living memory synchronized with `data/conversations.db`.", "", "## 1. User Profile & Partnership Ethos", *[f"- **{k}**: {v}" for k, v in profile.items()], "", "## 2. Active State-Machine Workflow"]
        if active_wf:
            lines.extend([f"- **Workflow ID**: #{active_wf['id']} (`{active_wf['workflow_type']}`)", f"- **Goal**: {active_wf['goal']}", f"- **Current State**: `{active_wf['current_state']}`"])
        else:
            lines.append("- **Current State**: `IDLE` (Ready for new engineering or companion workflow)")
        lines.extend(["", "## 3. Architecture Decision Records (ADRs)", *[f"- **{a['title']}** [{a['status']}]: {a['decision']} *(Outcome: {a['consequences']})*" for a in adrs if a.get("status", "accepted") == "accepted"][-20:]])
        lines.extend(["", "## 4. Active Tasks", *([f"- `[Task #{tid} | P{prio} | {st}]` {desc}" for tid, desc, st, prio in pending_tasks] if pending_tasks else ["- No pending immediate tasks."])])
        lines.extend(["", "## 5. Engineering Backlog", *([f"- [{'x' if st == 'completed' else ' '}] `[Backlog #{bid} | {cat} | {st}]` {desc}" for bid, desc, cat, st in backlog_items] if backlog_items else ["- Backlog is currently empty."])])
        lines.extend(["", "## 6. System Notes & Lessons Learned", *([f"- **[{cat}]** {note_text}" for note_text, cat, _ in notes] if notes else ["- Always use `run_python` for multiline Python execution on Windows."]), ""])
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path

    def export_state_snapshot(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Export reversible state tables scoped by `project_id` for checkpointing."""
        pid = _eff_pid(project_id)
        out: Dict[str, Any] = {"project_id": pid}
        scoped = (("tasks", " WHERE project_id = ?", (pid,)), ("knowledge_facts", " WHERE project_id = ?", (pid,)))
        global_tbls = (("backlog", "", ()), ("architecture_decisions", "", ())) if pid == SELF_PROJECT_ID else ()
        with self._lock:
            cur = self.conn.cursor()
            for tbl, where_sql, params in (*scoped, *global_tbls):
                cur.execute(f"SELECT * FROM {tbl}{where_sql} ORDER BY id ASC", params)
                cols = [d[0] for d in cur.description]
                out[tbl] = [dict(zip(cols, row)) for row in cur.fetchall()]
        return out

    def restore_state_snapshot(self, snapshot: Dict[str, Any], rollback_reason: str = "", project_id: Optional[str] = None) -> None:
        """Rewind scoped tasks/facts (and self backlog/ADRs) to a checkpoint snapshot while logging a rollback lesson."""
        pid = str(snapshot.get("project_id") or _eff_pid(project_id))
        scoped = (("tasks", " WHERE project_id = ?", (pid,)), ("knowledge_facts", " WHERE project_id = ?", (pid,)))
        global_tbls = (("backlog", "", ()), ("architecture_decisions", "", ())) if pid == SELF_PROJECT_ID else ()
        with self._lock:
            cur = self.conn.cursor()
            for tbl, where_sql, params in (*scoped, *global_tbls):
                if tbl not in snapshot:
                    continue
                cur.execute(f"DELETE FROM {tbl}{where_sql}", params)
                for row in snapshot[tbl]:
                    cols = list(row.keys())
                    cur.execute(f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [row[c] for c in cols])
            self.conn.commit()
        if rollback_reason.strip():
            self.add_system_note(rollback_reason.strip()[:500], category="rollback_lesson")

    def close(self) -> None:
        with self._lock:
            with contextlib.suppress(Exception):
                self.conn.close()


_store_instance: Optional[MemoryStore] = None
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
    """Return the active scoped `MemoryStore`, or the singleton `MemoryStore` instance."""
    global _store_instance
    if db_path is not None:
        return MemoryStore(db_path=db_path)
    override = _STORE_OVERRIDE.get()
    if override is not None:
        return override
    with _store_lock:
        if _store_instance is None:
            _store_instance = MemoryStore()
        return _store_instance


settings.register_self_dev_resolver(lambda _d: get_memory_store().is_self_dev_mode_enabled())
