# Companion AI OS — Architecture Decision Records (Living ADR Log)

> **Anti-Pileup Policy**: This single canonical document replaces scattered `ADR-xxx.md` files.
> At most **19 active decisions** are kept in full detail. Superseded decisions
> are automatically compacted into single-line historical notes at the bottom.

## Active Architectural Decisions

### ADR-001: Zero-Dependency Python Stdlib Foundation
- **Status**: `accepted`
- **Context**: External dependencies in core agent loops cause environment fragility when self-evolving.
- **Decision**: Keep Ring-0 core (`harness.py`, `companion/`, `surfaces/`, `configs/`) 100% Python 3.12 stdlib while allowing Ring-1 `skills/*/scripts/*.py` to declare isolated PEP 723 `uv run` dependencies.
- **Consequences**: Unbrickable zero-dependency core brain paired with an extensible, isolated skill toolbox.

### ADR-002: A-Frame Layered Modular Architecture
- **Status**: `accepted`
- **Context**: A single script becomes brittle at scale, while unlinked files create dead code.
- **Decision**: Organize Companion into an A-Frame Architecture: Infrastructure -> Domain -> Workflow Crossbar -> Application -> Presentation Apex.
- **Consequences**: Clear separation of concerns, SOLID compliance, and backward compatibility via harness.py.

### ADR-003: Deterministic State-Machine Graph for Engineering & Self-Evolution
- **Status**: `accepted`
- **Context**: Unstructured tool loops hit MAX_TOOL_ITERS or skip design/testing on multi-step tasks.
- **Decision**: Enforce a persistent directed-graph state machine (INTAKE -> DISCOVER -> DESIGN -> PLAN -> IMPLEMENT -> VERIFY -> REFLECT -> COMPLETE).
- **Consequences**: Deterministic execution, automatic recovery via RECOVER state, and persistent state across restarts.

### ADR-004: Self-Preservation via AST Syntax Guard & Workspace Checkpoints
- **Status**: `accepted`
- **Context**: A self-modifying agent can break its own source code if it writes invalid syntax.
- **Decision**: Every write_file/edit_file on a .py file backs up the original and validates AST syntax before saving.
- **Consequences**: Companion cannot brick itself with syntax errors and can rollback entire workflows.

### ADR-005: Non-Sprawling 4-Document Living Spec-Driven Development (SDD)
- **Status**: `accepted`
- **Context**: Per-feature spec folders and 1:1 file specs cause massive file sprawl and ADR pile-up.
- **Decision**: Maintain strictly 4 living canonical files in specs/ (PRODUCT.md, ARCHITECTURE.md, CONTRACTS.md, DECISIONS.md) verified bidirectionally against code.
- **Consequences**: Zero spec sprawl, self-compacting ADRs, and automated detection of undocumented code.

### ADR-006: AST Dead-Code Hygiene & Bounded Autonomous Agent Hobbies
- **Status**: `accepted`
- **Context**: AI agents tend to leave dead code over iterations and sit idle overnight without structured bounds.
- **Decision**: Enforce AST dead-code/unused-import verification in system_doctor and run checkpoint-guarded Autonomous Hobbies during idle windows.
- **Consequences**: Codebase stays simple and lean while Companion safely improves itself during idle windows.

### ADR-007: Active Project Registry & Rollback Lesson Memory
- **Status**: `accepted`
- **Context**: Companion needs to remember why rollbacks occurred without leaving tasks falsely completed, and must support working on external projects via a mechanical toggle rather than AI memory.
- **Decision**: Snapshot/rewind tasks, backlog, and ADRs inside checkpoints while recording rollback_lesson notes; enforce WorkspaceContext so tools bind to active_project_root and lock Companion core read-only when working on external projects.
- **Consequences**: Companion never relies on LLM memory to stay in the right project folder - path and read-only rules are mechanically enforced.

### ADR-008: Spine-Level Waterfall Telemetry & Headless Core UI Boundary
- **Status**: `accepted`
- **Context**: Companion needs step-by-step waterfall and KPI telemetry decoupled from any Frontend Web UI.
- **Decision**: Build thread-safe TelemetryEngine directly into llm.chat, tools.dispatch, and agent.run_agent_turn persisting traces, spans, and logs in SQLite while enforcing a Headless Core vs. Presentation UI boundary.
- **Consequences**: Automatic SQLite waterfall observability across CLI and Web UI surfaces.

### ADR-009: Two-Tier Progressive Disclosure Skills vs. Primitive Tools
- **Status**: `accepted`
- **Context**: Hardcoding domain runbooks into Python modules or the system prompt causes bloat.
- **Decision**: Separate L1 atomic tools from L2/L3 declarative skill packs (skills/<name>/SKILL.md) loaded progressively on demand.
- **Consequences**: Horizontal scalability for new skills and workflows with minimal prompt overhead.

### ADR-010: Stdlib LAN Web Surface (surfaces/) & Multi-Phase Autonomous Goal Driver
- **Status**: `accepted`
- **Context**: Single-turn loops and external web frameworks caused unverified stub code during self-development.
- **Decision**: Serve zero-dependency ThreadingHTTPServer LAN web interfaces in surfaces/ with disconnect-proof async job streaming, and drive multi-step builds via companion/autonomy.py with mechanical tier routing and System Doctor gates.
- **Consequences**: Mobile/desktop LAN browser access with zero external dependencies and non-hallucinative feature delivery.

### ADR-011: Mechanical Cognitive Grounding, Phase Tool Gates & Inline Post-Edit Micro-Linting
- **Status**: `accepted`
- **Context**: Local open-weight models suffer reasoning amnesia between tool steps, waste turns exploring blindly, or make premature out-of-budget edits.
- **Decision**: Carry rolling 1-step <think> scratchpads, rescue text <tool_call> blocks, inject deterministic AST Repo & Line-Budget Maps, enforce phase-scoped AllowedTools, run inline post-edit AST/hygiene/layer micro-linters, and persist lossless resumable phase handoffs.
- **Consequences**: Frontier-grade reliability on local models through deterministic Python harness mechanics.

### ADR-012: Interactive Blueprint Approval Gate, Dedicated Coding Tier & Live Flight Eval
- **Status**: `accepted`
- **Context**: Non-technical product partners need plain-English design/plan review before code edits, while local coding specialists benefit from dedicated IMPLEMENT routing and empirical flight benchmarks.
- **Decision**: Add pause-at-PLAN Blueprint approval/revision (`companion/blueprint.py`), dedicated `coding` tier routing + task carry-forward auto-skip (`companion/compaction.py`, `companion/autonomy.py`), local Git save-points, and isolated 3-flight behavioral eval (`companion/eval_harness.py`).
- **Consequences**: Safe human-in-the-loop steering, higher first-pass implementation quality, and reproducible model benchmarking.

### ADR-013: Web UI Conversation Search & Git Save-Points Surface
- **Status**: `accepted`
- **Context**: Mobile and desktop LAN browser users need direct access to FTS5 conversation search and recent Git save-points without dropping to CLI commands.
- **Decision**: Expose read-only `GET /api/search?q=<query>&limit=<n>` backed by `MemoryStore.search_conversations()` and include `recent_savepoints` in `GET /api/status` rendered in the Chat and Workflow & Doctor tabs (`surfaces/web_server.py`, `surfaces/web_assets.py`).
- **Consequences**: Zero-dependency browser search and save-point visibility with non-blocking WAL reads.

### ADR-014: Domain-Neutral Cognitive Architecture, KnowledgePort, Dynamic GoalPlan DAGs & Skill Forge
- **Status**: `accepted`
- **Context**: Complex open-ended goals across diverse workspaces require epistemic grounding, uncompromised reasoning routing, action-level risk gates, evidence-verified GoalPlan DAGs, and safe transactional skill promotion without hardcoding domain-specific tools.
- **Decision**: Introduce `KnowledgePort` & `ExecutionTarget` (`companion/grounding.py`), readiness-aware uncompromised reasoning routing (`companion/intake.py`), dynamic `GoalPlan` DAGs (`companion/workflow_dag.py`), and staged capability promotion + `REFLECT` distillation (`companion/skill_forge.py`).
- **Consequences**: General-purpose reliability across any domain, clean external Wiki/Sandbox ports, and compounding verified capabilities.

### ADR-015: Live SSE Token Streaming, Hybrid Semantic RRF Memory & Zero-Login Home LAN Security
- **Status**: `accepted`
- **Context**: Home LAN desktop/mobile users expect real-time token/phase streaming, conceptual memory recall without GPU swaps, and invisible browser protection without login screens.
- **Decision**: Add ContextVar SSE streaming (`companion/streaming.py`, `GET /api/jobs/<id>/stream`), zero-swap hybrid BM25 + semantic RRF retrieval (`companion/semantic_memory.py`), static Web UI assets (`surfaces/static/`), and Host/Origin/Sec-Fetch-Site/CSRF LAN guards (`surfaces/web_server.py`).
- **Consequences**: Responsive Antigravity/Claude-grade live UX, meaning-based memory recall in <5ms, and zero-login LAN safety.

### ADR-016: Companion 3.0 Harvest Backend — Shared CoreActions, Forever-Chat Jobs, Co-Owned Wiki 2.0, Blind Vault, Universal Endpoints, Partner-Aware Federated MCP & Respectful Routines
- **Status**: `accepted`
- **Context**: Extracting capabilities from legacy homelab/agent systems risked mixing UI/core concerns, leaking secrets in a zero-login LAN environment, cluttering primary chat, or causing downtime cron storms.
- **Decision**: Introduce `CORE_ACTIONS` (`companion/core_actions.py`), Zero-Identity Background Jobs with <=200-token Wiki Inbox overflow (`companion/background_jobs.py`), 4-folder Dual-Author Knowledge Wiki (`companion/wiki_engine.py`), write-only Zero-Reveal `secrets.env` Vault + secret-free backups (`companion/vault.py`), SSH/WinRM/HTTP Connectors (`companion/endpoints.py`), Outbound + Inbound 'Order Counter' MCP Hub with Partner-Aware Federation (`companion/mcp_hub.py`), and Hardware-Gated Routines + Clone Sandboxes (`companion/routines.py`).
- **Consequences**: Complete headless multi-machine & knowledge operating system honoring strict downward A-Frame boundaries and zero-reveal LAN security.

### ADR-017: Stage 6 Multi-Studio React + TypeScript Frontend (`frontend/`) Compiling into Zero-Node Stdlib `surfaces/static/`
- **Status**: `accepted`
- **Context**: Surfacing all 6 Companion 3.0 Harvest studios (Forever Chat, Wiki & 2D Canvas Mind Map, Jobs & Approvals, Blind Vault & Endpoints, Federation & MCP, Routines & Ops) in raw vanilla JS strings would cause unmaintainable UI sprawl, while adding a runtime Node server would violate `ADR-001`.
- **Decision**: Author a modular React 19 + TypeScript + Vite frontend in `frontend/` with a Matte Brutalist Sci-Fi / Obsidian 4-preset theme system (`obsidian`, `orbital`, `graphite`, `daylight` + 1-click Light/Dark toggle) that compiles deterministically (`iife`) into `surfaces/static/{index.html,styles.css,app.js}` served directly by stdlib `surfaces/web_server.py` (`python harness.py --web`).
- **Consequences**: Typed, self-maintainable 6-Studio AI workstation UI with zero runtime Node dependency, 100% offline LAN execution, and strict Ring-0 Python stdlib purity.

### ADR-018: Multi-Provider LLM Registry, Pure-Stdlib Wire Dialects, Blind Vault Key Binding & Mechanical LAN Privacy Lock
- **Status**: `accepted`
- **Context**: Supporting both local LAN servers (Ollama, vLLM, LM Studio, llama.cpp) and frontier cloud APIs (Gemini, Anthropic Claude, OpenAI, xAI Grok, OpenRouter, DeepSeek, Groq) via static `.env` edits forced restarts, risked API key leakage, and lost provider-native reasoning/multimodal capabilities (`thoughtSignature`, Extended Thinking signatures, native audio/video).
- **Decision**: Introduce `companion/llm_wire.py` (3 pure-stdlib wire/SSE translators: `openai_compat`, `anthropic_messages`, `gemini_native`) and `companion/model_registry.py` (`llm_providers` + `llm_tier_routes` with Zero-Reveal Blind Vault `credential_ref` binding, hot-reload 8-slot capability tier matrix, and mechanical `local_only`/`hybrid`/`cloud_allowed` outbound privacy lock).
- **Consequences**: Instant hot-reload routing across local LAN and frontier models from Web UI (`System -> Models, Tiers & Context`) and CLI (`/providers`) with zero third-party SDK bloat, zero API key exposure, and prompt-injection-proof LAN privacy.

### ADR-019: Independent Two-Lever Operating Architecture (`Hobby Mode` x `Self-Dev Mode`), Mechanical Read-Only Core Lock & 2-Track Hobby Split
- **Status**: `accepted`
- **Context**: Running a daily-driver Companion release with background hobbies risked unrequested code mutations, while coupling hobbies to self-evolution prevented quiet personal knowledge/study maintenance on stable builds or isolated Lab/VM self-evolution.
- **Decision**: Maintain `Hobby Mode` (`on`/`off`) and `Self-Dev Mode` (`on`/`off`, seedable/lockable via `COMPANION_SELF_DEV`, `COMPANION_SELF_DEV_LOCKED`, `COMPANION_IMMUTABLE_CORE`) as two independent levers; mechanically lock Companion core files read-only (`safety.py`, `checkpoints.py`, `grounding.py`, `skill_forge.py`) while keeping `data/` and external `/project` workspaces writable when `Self-Dev Mode = OFF`; forbid LLM tool turns or subprocesses from enabling `Self-Dev Mode`; and split hobbies into `personal` vs `self_dev` tracks with CLI + Web UI `System` Studio + Web idle parity.
- **Consequences**: Safe stable daily-driver operation with personal/ops hobbies (`wiki_gardener`, `memory_curator`, `trace_reviewer`, `life_study_organizer`) alongside deliberate or full Lab/VM self-evolution when `Self-Dev Mode` is explicitly enabled by the human partner.

## Historical / Superseded Decisions (Compacted)
- *(None yet — all foundational ADRs are currently active.)*
