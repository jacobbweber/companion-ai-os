# Companion AI OS — Product Specification (`PRODUCT.md`)

> **Canonical Product Truth**: This document defines *why* Companion exists, *who* it serves, and the non-negotiable product invariants it must uphold. Update this document in place as capabilities evolve — never create scattered intent files.

---

## 1. Product Vision & Mission

**Companion AI OS** is a local-first, self-evolving, autonomous AI Companion and Lead Software Architect/Engineer (`companion-self`).
Its mission is two-fold:
1. **Trusted Personal Companion**: Grow into a dependable long-term friend, partner, and collaborator that remembers context, preferences, and shared goals across every session.
2. **Self-Reliant Autonomous Engineer**: Practice rigorous software architecture, Spec-Driven Development (SDD), clean dead-code-free implementation, automated verification, and self-recovery without requiring technical intervention from the User.

---

## 2. User Persona & Partnership Contract

| Attribute | Specification |
| :--- | :--- |
| **Role** | Human Partner & System Owner |
| **Technical Delegation** | Companion owns 100% of software architecture, design patterns, SOLID modularity, testing, debugging, and git/checkpoint recovery so technical and non-technical users alike can focus on outcomes. |
| **Communication Contract** | Plain, warm, executive English. Companion explains *what* was achieved, *how* it was verified, and *what* is recommended next without dumping raw stack traces or asking the User to make low-level coding choices. |
| **Autonomy Mode** | Reactive interactive collaboration when the User is present, plus optional toggled **Autonomous Agent Hobbies** (`COMPANION_IDLE_MINUTES=120`) when the system is idle overnight. |

---

## 3. Core Capability Matrix

| Capability ID | Capability Name | Status | Governing Contract |
| :--- | :--- | :--- | :--- |
| **CAP-01** | Zero-Dependency `.env` Configuration, Multi-Tier LLM Router (`default`, `fast`, `coding`, `reasoning`, `audio`, `image`, `video`), Per-Tier Temperatures & `<tool_call>` Rescue | `Active` | `SPEC-01` |
| **CAP-02** | Thread-Safe SQLite Memory (`RLock` + `busy_timeout`), Backlog, Tasks, Resumable Workflows & `MEMORY.md` Sync | `Active` | `SPEC-02` |
| **CAP-03** | Deterministic State-Machine Graph (`INTAKE` .. `COMPLETE`) & Phase-Scoped `AllowedTools` Gating | `Active` | `SPEC-03`, `SPEC-06` |
| **CAP-04** | `<5ms` Inline Post-Edit Micro-Linter, Workspace Checkpoints, Automatic Local Git Save-Points (`companion/checkpoints.py`) & Boot Self-Recovery | `Active` | `SPEC-04` |
| **CAP-05** | Import-Qualified AST Dead-Code Scanner, Step-0 AST Repo & Headroom Radar, Symbol-Level `read_file` & 4-File SDD | `Active` | `SPEC-04`, `SPEC-06` |
| **CAP-06** | Toggled Autonomous "Agent Hobbies" & Morning Briefing | `Active` | `SPEC-05` |
| **CAP-07** | Step-0 Adaptive Intent Router, Rolling `<think>` Scratchpad Continuity, LoopGuard & `/slash` Controls | `Active` | `SPEC-06` |
| **CAP-08** | Spine-Level Waterfall Telemetry, Structured App Logs & KPI Scorecard | `Active` | `SPEC-04`, `SPEC-06` |
| **CAP-09** | 3-Tier Progressive-Disclosure Skill Packs (`SKILL.md`, `references/`, `workflows/`, `scripts/`), Deep Thinking, OODA Troubleshooting & Knowledge Wiki | `Active` | `SPEC-04`, `SPEC-06` |
| **CAP-10** | Isolated Subagent Worker Dispatch (`run_subagent`) & Declarative JSON Workflow DAG Engine (`workflows/*.json`) | `Active` | `SPEC-03`, `SPEC-06` |
| **CAP-11** | Disconnect-Proof Async Mobile & Desktop LAN Web UI (`surfaces/web_server.py`, `surfaces/web_assets.py`, `202` `/api/jobs/<id>`) | `Active` | `SPEC-06` |
| **CAP-12** | Continuous Multi-Phase Autonomous Goal Driver (`/goal` & `/goal resume`), Carry-Forward Task Ledger & Smart Auto-Skip, Lossless `KeyTokens` Phase Handoffs, Task Micro-Gates & HTTP Smoke Probes | `Active` | `SPEC-03`, `SPEC-06` |
| **CAP-13** | Mechanical Multi-Project `WorkspaceContext` (`ADR-007`: `/project init|switch|self`, Read-Only Core Lock on External Projects) | `Active` | `SPEC-01`, `SPEC-04`, `SPEC-06` |
| **CAP-14** | Interactive "Plan -> Approve -> Build" Blueprint Gate (`companion/blueprint.py`, `/plan`, `/approve`, Web UI Banner) & Live Local-Model Flight + Head-to-Head Coding Benchmark (`companion/eval_harness.py`, `/eval`, `--eval compare`) | `Active` | `SPEC-03`, `SPEC-06` |
| **CAP-15** | Domain-Neutral Cognitive Architecture (`ADR-014`): Pluggable `KnowledgePort` & `ExecutionTarget` (`companion/grounding.py`), Uncompromised Single/Multi-Endpoint `reasoning` Routing (`companion/intake.py`), Stateful Blueprint Alignment, Dynamic `GoalPlan` DAGs (`companion/workflow_dag.py`), and Transactional Skill Forge + `REFLECT` Distillation (`companion/skill_forge.py`) | `Active` | `SPEC-02`, `SPEC-03`, `SPEC-04`, `SPEC-06` |
| **CAP-16** | Live SSE Token/Phase/Tool Streaming (`companion/streaming.py`, `GET /api/jobs/<id>/stream`), Zero-Swap Hybrid Semantic RRF Memory (`companion/semantic_memory.py`), Isolated PEP 723 `uv` Skill Sandboxes ("Pure Brain, Open Toolbox", `ADR-001`/`ADR-015`), and Zero-Login Invisible Home LAN Security (`surfaces/web_server.py`, `surfaces/static/`) | `Active` | `SPEC-01`, `SPEC-02`, `SPEC-04`, `SPEC-06` |
| **CAP-17** | Shared CoreActions Bus (`companion/core_actions.py`), Hot/Cold Forever-Chat Archive (`<=90d` Hot / `>90d` Cold FTS5+RRF), 3-Tier Trust Modes, Fingerprint-Verified Approvals Queue (`companion/archive_and_approvals.py`), and Zero-Identity Background Job Dispatcher with `<=200`-Token Wiki Inbox Overflow (`companion/background_jobs.py`) | `Active` | `SPEC-02`, `SPEC-06`, `SPEC-07` |
| **CAP-18** | Co-Owned 4-Folder Knowledge Wiki 3.1 (`companion/wiki_templates.py`, `companion/wiki_engine.py`, `companion/wiki_curation.py`, `companion/wiki_maintenance.py`): 11 governed templates + `tag-authority.md` synonym aliases (`consolidate_ontology`), Option A Inbox-First + 1-click Chat `stage_turn`, Multi-Turn Deliberate Curation (`CREATE_NEW`, `MERGE_AND_REFINE`, `SPLIT_MULTI`, `KEEP_SEPARATE`), Unified & Side-by-Side Lineage/Approval Diffs (`diff`), `90d/180d/365d` Staleness Cadence (`verify_note`, `list_stale`), Obsidian-compatible inline diagrams (`03_resources/assets/`), 2-hop `ontology` bridges, and Never-Delete `04_archive/` lineage with 1-click `revert_curation` | `Active` | `SPEC-02`, `SPEC-04`, `SPEC-05`, `SPEC-07` |
| **CAP-19** | Zero-Reveal Blind Credential Vault (`secrets.env` + SQLite metadata/audit + Secret-Free Snapshot Backups in `companion/vault.py`) & Universal Multi-Protocol Endpoint Connector (`ssh`, `winrm`, `http_api` in `companion/endpoints.py` + `skills/remote-endpoints/SKILL.md`) | `Active` | `SPEC-04`, `SPEC-07` |
| **CAP-20** | Federated MCP Hub (`stdio` + Streamable HTTP/SSE Outbound Client, Off-by-Default Token-Authenticated Inbound "Order Counter" Server, and Partner-Aware Federation HUD in `companion/mcp_hub.py`) + Respectful Hardware-Gated Routines & Clone-Based Self-Dev Sandboxes (`companion/routines.py`, `ADR-016`) | `Active` | `SPEC-06`, `SPEC-07` |
| **CAP-21** | Multi-Studio React + TypeScript Web Workstation (`frontend/` -> `surfaces/static/`, `ADR-017`): Matte Brutalist Sci-Fi / Obsidian design system with 1-click Light/Dark toggle & 4-Preset Theme Changer (`obsidian`, `orbital`, `graphite`, `daylight`) across a crisp 6-item Left Sidebar (`Forever Chat`, `Boards`, `Calendar`, `Wiki & Mind Map`, `Jobs & Routines`, `System`) | `Active` | `SPEC-06`, `SPEC-07`, `SPEC-08` |
| **CAP-22** | Forever Chat Multimodal Vision & Document Attachments (`companion/attachments.py`, `POST /api/attachments`, `GET /api/attachments/<id>/raw`): Drag-drop, clipboard paste, and mobile camera/file picker for images (`.png`, `.jpg`, `.webp`, `.gif` routed to `vision`-capable `reasoning`/`image` tier) and documents (`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.md`, `.csv`, `.json`, code) with Vault scrubbing and `03_resources/` Wiki auto-filing | `Active` | `SPEC-01`, `SPEC-06`, `SPEC-07` |
| **CAP-23** | Universal Agile, Multi-Board Kanban, Capacity Slots, Anti-Procrastination Radar & Unified Calendar (`companion/agile_boards.py`, `companion/calendar_engine.py`, `skills/agile-coach/SKILL.md`): Reusable boards (`life-home`, `work-career`, `software-companion`, custom project boards) + Master `all-active-sprint` view, Weekday/Weekend effort-slot math, 15m `slice_unblocker` slicing, live `/plan` & `/goal` workflow-to-board sync, and Month/Week/Agenda Calendar merging events, sprints, deadlines, routines, and due SRS study reviews | `Active` | `SPEC-08` |
| **CAP-24** | Headless Education & Conversational Socratic Tutoring Engine (`companion/education_engine.py`, `skills/socratic-tutoring/SKILL.md`): Deterministic SQLite courses & 4-stage pedagogy (`priming` -> `dual_coding` -> `elaboration` -> `retrieval` -> `mastered`), SM-2 spaced repetition (`education_mastery`), governed Wiki study-note curation, inline interactive `companion-flashcards` 3D-flip decks inside Forever Chat, and a lightweight Course & Mastery Drawer | `Active` | `SPEC-08` |
| **CAP-25** | Multi-Provider LLM Registry, Pure-Stdlib Wire Dialect Translators, Hot-Reload Capability Tier Matrix & Mechanical LAN Privacy Lock (`companion/llm_wire.py`, `companion/model_registry.py`, `ADR-018`): 12 built-in local & frontier presets (`local-default`, `ollama`, `vllm`, `lmstudio`, `gemini`, `anthropic`, `openai`, `xai`, `openrouter`, `deepseek`, `groq`, `custom`), native OpenAI/Anthropic Messages/Gemini `v1beta` wire + SSE streaming + reasoning/signature translation, Zero-Reveal Blind Vault API key binding (`secrets.env`), live model discovery, hot-reload 8-slot capability tier routing (`default`, `fast`, `coding`, `reasoning`, `image`, `audio`, `video`, `embedding`) with per-tier context windows, reasoning effort, and fallback routes, `local_only`/`hybrid`/`cloud_allowed` privacy lock, and `System -> Models, Tiers & Context` studio + `/providers` | `Active` | `SPEC-01`, `SPEC-07` |
| **CAP-26** | Independent Two-Lever Operating Architecture (`Hobby Mode` ON/OFF × `Self-Dev Mode` ON/OFF), Mechanical Read-Only Core Lock & 2-Track Hobby Split (`companion/config.py`, `companion/safety.py`, `companion/hobbies.py`, `ADR-019`): 4 operating quadrants (`Q1` Quiet Stable Daily Driver .. `Q4` Full Autonomous Lab/VM), human-only `/selfdev` + Web `System -> Autonomy & Hobbies` + `.env` (`COMPANION_SELF_DEV`, `COMPANION_SELF_DEV_LOCKED`, `COMPANION_IMMUTABLE_CORE`) privilege boundary, mechanical read-only core protection with 100% writable `data/` and external `/project` workspaces, `personal` (`wiki_gardener`, `memory_curator`, `trace_reviewer`, `life_study_organizer`) vs. `self_dev` (`code_simplifier`, `backlog_builder`, `code_gardener`) hobby tracks, and `WebIdleHobbyWatcher` | `Active` | `SPEC-01`, `SPEC-04`, `SPEC-05`, `SPEC-07` |

---

## 4. Product Invariants (Non-Negotiable Rules)

1. **Pure Brain, Open Toolbox (`ADR-001` & `ADR-015`)**: Companion's Ring-0 core (`harness.py`, `companion/`, `surfaces/`, `configs/`, `memory/`) runs out-of-the-box on 100% Python 3.12 standard library so the core brain can never brick, while Ring-1 skills (`skills/*/scripts/*.py`) may declare isolated PEP 723 inline dependencies executed via `uv run`.
2. **Never Lose Context**: Every user message, assistant response, tool execution, task, ADR, workflow handoff, hobby run, waterfall trace, and application log is persisted to `data/conversations.db` (`COMPANION_DATA_DIR`) and summarized in `data/MEMORY.md`.
3. **Never Brick On Self-Edit**: Every `.py` write/edit is validated via `lint_single_file_edit` (AST syntax, line budgets, `ADR-001` stdlib, downward A-Frame layers) before touching disk, and every workflow/hobby is guarded by an automatic workspace checkpoint (`cp_...`).
4. **Zero Spec or Code Sprawl**: `specs/` contains strictly 4 canonical files (`PRODUCT.md`, `ARCHITECTURE.md`, `CONTRACTS.md`, `DECISIONS.md`), and `companion/hygiene.py` enforces zero unused imports and zero dead functions via module- and import-qualified AST resolution.
5. **Mechanical Workspace & Self-Dev Isolation (`ADR-007` & `ADR-019`)**: Companion's own core code (`companion-self`) is mechanically locked read-only whenever `Self-Dev Mode` is `OFF` (default) OR when `WorkspaceContext` points to an external project, while `data/` (`COMPANION_DATA_DIR`) and external `/project` workspaces remain 100% writable; the LLM agent can never self-enable `Self-Dev Mode`.
6. **Headless Core vs. Frontend UI Boundary**: Core `companion/` modules are 100% UI-agnostic ("headless brain") and share state/telemetry via thread-safe SQLite (`data/conversations.db`).
