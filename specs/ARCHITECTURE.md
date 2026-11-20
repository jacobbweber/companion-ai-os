# Companion AI OS — Architecture Specification (`ARCHITECTURE.md`)

> **Canonical Architecture Truth**: This document defines the A-Frame layered structure, the authoritative 1-to-1 Module Registry, downward dependency laws, and code simplicity budgets. Every `.py` module in `companion/` and `surfaces/` MUST be registered here.

---

## 1. The A-Frame Layered Architecture

```text
      [ APEX (PRESENTATION): harness.py, surfaces/web_server.py & web_assets.py ]
        CLI/REPL, Disconnect-Proof LAN Web UI & Preemptible IdleAutonomyWatcher
                                    /    \
                                   /      \
  [ UPPER A-FRAME (APPLICATION): agent.py, autonomy.py, blueprint.py, intake.py,      ]
  [   skill_forge.py, eval_harness.py, context.py, compaction.py, tools.py,           ]
  [   sdlc_tools.py, worktree_runner.py, os_tools.py, harvest_tools.py,               ]
  [   tool_schemas.py, routines.py, core_actions.py                                   ]
     Cognitive Triage, Blueprint Gate, Skill/DAG Forge, Phase-Gated Tools (28), Eval
                               /              \
                              /================\
                             /     CROSSBAR     \
              [ workflow_graph.py, workflow_dag.py & dag_runtime.py ]
       State-Machine Graph, Pluggable VerificationSpec & Code-Controlled Python DAGs
                           /                      \
                          /                        \
       [ LEFT LEG: INFRASTRUCTURE ]        [ RIGHT LEG: DOMAIN & SDD ]
       - companion/config.py               - companion/checkpoints.py
       - companion/telemetry.py            - companion/safety.py
       - companion/streaming.py            - companion/semantic_memory.py
       - companion/llm_wire.py             - companion/memory_store.py
       - companion/llm.py                  - companion/trigger_policy.py
                                           - companion/project_graph.py
                                           - companion/disciplines.py
                                           - companion/grounding.py
                                           - companion/hygiene.py
                                           - companion/sdd.py
                                           - configs/architecture.py
                                           - memory/chat_memory.py
```

---

## 2. Authoritative Module Registry & Simplicity Budgets

Every module in the system has a single cohesive responsibility and is subject to deterministic AST hygiene and line-count budgets (`max_module_lines = 700`, `max_function_lines = 100`, zero semicolon-chained statements):

| Module Path | A-Frame Layer | Single Responsibility | Governing Spec |
| :--- | :--- | :--- | :--- |
| `companion/config.py` | `Infrastructure` | Stdlib `.env` parser, per-tier temperature `TierConfig`, `tier_override_context`, `agent_tool_turn_context`, permanent installed-app immutability (`COMPANION_IMMUTABLE_APP`, `ADR-019`), `scratch_dir` (`data/scratch/`), and mechanical `WorkspaceContext` (`ADR-007`) | `SPEC-01` |
| `companion/telemetry.py` | `Infrastructure` | Thread-safe (`RLock` + `ContextVar`) SQLite Waterfall Tracer, Structured App Logger, SecretRedactor, retention pruner, and KPI Scorecard | `SPEC-04`, `SPEC-06` |
| `companion/streaming.py` | `Infrastructure` | `ContextVar` stream event bus (`stream_event_context`, `emit_stream_event`), `<100ms` `CancelScope` (`cancel_scope_context`), `<turn_context>` wire scope (`wire_turn_context`), `<think>` tag stream splitter (`ThinkTagStreamSplitter`), `<tool_call>` rescue, and OpenAI SSE delta/tool assembler (`read_completion_or_sse_stream`) | `SPEC-01`, `SPEC-06` |
| `companion/llm_wire.py` | `Infrastructure` | Pure-stdlib wire request, multimodal (`image`/`audio`/`video`/`pdf`), reasoning, and SSE stream translators for `openai_compat`, `anthropic_messages`, and `gemini_native` (`thoughtSignature` & `thinking` signature preservation) plus `is_local_or_lan_endpoint` | `SPEC-01` |
| `companion/llm.py` | `Infrastructure` | Multi-dialect HTTP/SSE LLM client, `discover_provider_models`, cancelable `BoundedSemaphore` `ModelGate` (with exclusive VRAM-swap fallback), HTTP 400 parameter self-healing, `local_only` LAN Privacy Lock, and per-tier + global failover | `SPEC-01` |
| `companion/checkpoints.py` | `Domain` | External Copy-on-Write file ledger (`effective_data_dir / "checkpoints"`), `WorkspaceContext`-bound atomic file backups, non-destructive `rollback_checkpoint`, `snapshot_readonly_tree`/`enforce_readonly_tree` guards, local Git save-points, and post-exec syntax healing | `SPEC-04` |
| `companion/safety.py` | `Domain` | `ADR-007` path & `.env`/`secrets.env` confinement, `is_companion_self_repo`-scoped `<5ms` inline post-edit micro-linter (`lint_single_file_edit`), `LoopGuard`, and `run_system_doctor` (supporting packaged `app/` `7/7` verification without `tests/`) | `SPEC-04` |
| `companion/hygiene.py` | `Domain` | Import-qualified AST dead-code & unused-private-method scanner across `companion/`, `surfaces/`, and `configs/`, `<15ms` AST Repo Headroom Map, and symbol slicer | `SPEC-04`, `SPEC-06` |
| `companion/sdd.py` | `Domain` | 4-File Canonical SDD verifier, PEP 723 `parse_pep723_dependencies` + 3-Tier Capability Pack & Python DAG Linter, Wiki provenance guard, and ADR compactor | `SPEC-04` |
| `companion/semantic_memory.py` | `Domain` | Zero-swap morphological stemmer, bidirectional everyday-to-technical concept clusters, sparse cosine similarity, optional float32 vectors, and `hybrid_rrf_rank` | `SPEC-02`, `SPEC-04` |
| `companion/memory_store.py` | `Domain` | Per-`db_path` `RLock` + atomic `write_tx()` SQLite persistence for chats (`conversations.meta_json` trace hydration), hybrid FTS5 + semantic RRF recall, Universal Project & Goal Graph tables (`projects` with `workspace_path`, `milestones`, `project_timeboxes`, `work_items`), `knowledge_facts`, ADRs (`ADR-001`..`ADR-020`), and `autonomy_journal` | `SPEC-02`, `SPEC-08` |
| `companion/trigger_policy.py` | `Domain` | Unified `TriggerPolicy` (`manual`, `scheduled`, `on_idle`), overnight wrap-around `allowed_window` + weekday math, 5-field cron/interval evaluator, and `<5ms` zero-token `PRECHECK_REGISTRY` | `SPEC-05`, `SPEC-07` |
| `companion/project_graph.py` | `Domain` | Domain-blind Universal Project & Goal Graph (`ProjectGraphManager`, `workspace_path`, `delivery_policy` `local_only`\|`remote_branch_pr`\|`ask_me`, `git_credential_ref`, `owner` `companion`\|`human`\|`collaborative`, `depends_on` auto-unblocking, DIKW `stage_kind`, `TriggerPolicy` idle Goal eligibility, and Kanban/Sprint/Roadmap projections) | `SPEC-05`, `SPEC-08` |
| `companion/disciplines.py` | `Domain` | Engineering discipline playbooks, `MODULE_LAYER_MAP`, `ActionRiskTier` (`R0`..`R3`), `WorkspaceContract`, and AST syntax verifier | `SPEC-03`, `SPEC-04` |
| `companion/grounding.py` | `Domain` | Pluggable `KnowledgePort` (`LocalKnowledgeAdapter` bridged to live `WikiEngine` + external adapter hook), `<10ms` `build_epistemic_grounding`, Top-K skill ranking, and `ExecutionTarget` (`LocalWorkspaceTarget`) | `SPEC-04`, `SPEC-06`, `SPEC-07` |
| `companion/archive_and_approvals.py` | `Domain` | 3-Tier `TRUST_MODES`, fingerprint-verified `ApprovalQueue`, Hot (`<=90d`) / Cold (`>90d`) Forever-Chat Archive (`conversations_cold` + FTS5 + Hybrid RRF), session-scoped quiet nudges, and `saved_prompts` library | `SPEC-02`, `SPEC-07` |
| `companion/vault.py` | `Domain` | Write-only Zero-Reveal `VaultManager` (`secrets.env` + `vault_entries` / `vault_audit_log`), JIT `resolve_for_call`, output scrubber, and secret-free snapshot backup/restore (`missing_secrets` checklist) | `SPEC-04`, `SPEC-07` |
| `companion/model_registry.py` | `Domain` | 12-preset local & frontier LLM Provider Registry (`llm_providers`), Zero-Reveal Blind Vault API key binding (`credential_ref`), hot-reload 8-slot Capability Tier Matrix (`llm_tier_routes`), `CORE_ACTIONS["models"]`, `/providers`, and `local_only`/`hybrid`/`cloud_allowed` privacy lock | `SPEC-01`, `SPEC-07` |
| `companion/wiki_templates.py` | `Domain` | 11 built-in non-education Wiki templates (`03_resources/templates/`), `03_resources/tag-authority.md` ontology authority, pure-stdlib YAML frontmatter parser/serializer, template recommender, and curated-note validator | `SPEC-02`, `SPEC-07` |
| `companion/wiki_engine.py` | `Domain` | Co-owned 4-Folder Markdown Warehouse (`01_inbox`..`04_archive` + `trash/`), Option A Inbox-First & Dual-Author guards, `proxy-asset-note` companions, 2-hop `ontology` bridges, SHA-256 `content_hash` locks, and SQLite 2D Mind Map sidecar | `SPEC-02`, `SPEC-07` |
| `companion/wiki_curation.py` | `Domain` | Deliberate Multi-Turn Wiki Curation Engine (`inspect_graduation` -> mandatory full-read gate -> `CREATE_NEW`/`MERGE_AND_REFINE`/`SPLIT_MULTI`/`KEEP_SEPARATE` -> `validate_curation_plan` -> `commit_curation_plan`) with Never-Delete `04_archive/` lineage and 1-click `revert_curation` | `SPEC-02`, `SPEC-07` |
| `companion/wiki_maintenance.py` | `Domain` | Wiki 3.1 Unified & Side-by-Side lineage/approval diffs (`compute_wiki_diff`), governed `alias -> canonical` ontology consolidation (`consolidate_ontology`), `90d/180d/365d` freshness cadences (`verify_note_freshness`, `list_stale_notes`), Obsidian-compatible inline diagrams (`03_resources/assets/`), and 1-click Forever Chat `stage_chat_turn_to_inbox` | `SPEC-02`, `SPEC-07` |
| `companion/attachments.py` | `Domain` | Multimodal Forever Chat image & document attachment engine (`.png`/`.jpg`/`.webp`/`.gif`, `.pdf`, `.docx`/`.xlsx`/`.pptx`, `.md`/`.csv`/`.json`/code), Vault scrubbing, and `03_resources/` Wiki auto-filing | `SPEC-06`, `SPEC-07` |
| `companion/education_engine.py` | `Domain` | Headless Education courses, 4-stage Socratic pedagogy (`priming`..`mastered`), SM-2 spaced repetition (`education_mastery`), Wiki study curation, and fenced `companion-flashcards` Chat decks | `SPEC-08` |
| `companion/dag_runtime.py` | `Workflow` | Pure-stdlib code-controlled Python `DAGSpec` runtime (`python_step`, `tool_or_script_step`, `mcp_step`, `bounded_llm_step`, `human_gate`), typed I/O validation, `foreach` fan-out/fan-in joins, retries/timeouts, SQLite `dag_runs`/`dag_node_runs` checkpoints, and mid-DAG `R3`/`human_gate` pause/resume | `SPEC-03` |
| `companion/workflow_dag.py` | `Workflow` | Dynamic `GoalPlan` & `GoalStep` DAG, pluggable `VerificationSpec` (`evidence_file`, `check_command`, `script_validator`, `domain_validator`, `assertion`, `human_gate`), and `ExecutorBindings` | `SPEC-03` |
| `companion/workflow_graph.py` | `Workflow` | Persistent directed-graph state machine (`INTAKE` .. `COMPLETE` + `RECOVER`), phase gates, and code-controlled DAG facade | `SPEC-03` |
| `companion/intake.py` | `Application` | 2-stage Cognitive Triage Gate (`triage_turn`: `DIRECT_TURN`, `EXECUTE_VERIFIED_DAG`, `GOAL_ALIGN_PLAN`, `ADVANCE_ACTIVE_GOAL`), `assess_turn_readiness`, `select_turn_step_tier`, and `run_http_smoke_probe` | `SPEC-06` |
| `companion/skill_forge.py` | `Application` | `check_make_vs_borrow`, transactional `stage_and_promote_skill` (`scripts/*.py` + `dags/*.py` `dry_run_dag`/`self_test_dag` + checkpoint), `run_skill_script`, and `distill_workflow_reflection` | `SPEC-03`, `SPEC-04`, `SPEC-06` |
| `companion/compaction.py` | `Application` | `KeyTokens` extractor, lossless phase handoffs, workflow task carry-forward ledger (`format_task_carry_forward`, `classify_task_autoskip`), and spam-free 2-stage conversation compactor | `SPEC-06` |
| `companion/tool_schemas.py` | `Application` | 28 rich OpenAI tool schemas with per-parameter descriptions, workspace-scoped repo tools (`git_manage`, `dev_preview`), native `mcp__*` schema exposure, and fine-grained `(tool, action)` `PHASE_ALLOWED_TOOLS` gating | `SPEC-06`, `SPEC-07`, `SPEC-08` |
| `companion/background_jobs.py` | `Application` | SQLite-backed `BackgroundJobManager` (`dag_name`/`dag_params` + Zero-Identity Workers under `ModelGate.background_context()`), crash recovery, and `<=200`-token Wiki Inbox overflow marshalling | `SPEC-07` |
| `companion/endpoints.py` | `Application` | Symbolic-only `EndpointManager` (`ssh`, `winrm`, `http_api`) with approval-staged `propose_endpoint`, JIT Blind Vault injection, SSRF & command safety gates, and remote CPU/RAM/GPU profiling | `SPEC-07` |
| `companion/mcp_hub.py` | `Application` | Outbound `stdio`/HTTP MCP Client (`tools_fingerprint` approval, `propose_server`), MCP-to-Capability-Pack Harvester (`harvest_mcp_capability_pack`), Inbound "Order Counter" MCP Server, and Partner-Aware Federation HUD | `SPEC-07` |
| `companion/routines.py` | `Application` | Unified `TriggerPolicy` `RoutineManager` (`manual`, `scheduled`, `on_idle` with built-in maintenance routines, retiring `hobbies`), `IdleAutonomyWatcher`, and Morning Briefing | `SPEC-05`, `SPEC-07` |
| `companion/calendar_engine.py` | `Application` | Unified Month/Week/Agenda Calendar merging `calendar_events`, active timeboxes, work-item `planned_date`/`due_date`, projected `scheduled` routines, and daily due SRS study counts | `SPEC-08` |
| `companion/core_actions.py` | `Application` | Unified actor-aware `CORE_ACTIONS` registry (including 1-click `endpoint_register`, `register_mcp_server`, and `dag_gate` `resume_dag_run` execution), `execute_core_action`, and `handle_harvest_slash` | `SPEC-03`, `SPEC-07`, `SPEC-08` |
| `companion/harvest_tools.py` | `Application` | Thin `actor="companion"` tool adapters (`job_manage`, `wiki_manage`, `vault_manage`, `endpoint_manage`, `mcp_manage`, `routine_manage`, `agile_manage`, `calendar_manage`, `education_manage`) | `SPEC-07`, `SPEC-08` |
| `companion/sdlc_tools.py` | `Application` | Orthogonal local SDLC primitives: repo rulebook/stack discovery (`discover_repo_sdlc_context`), secret-shielded `git_manage` (branches, worktrees, Blind-Vault SSH/PAT auth, `delivery_policy` gates), and OS-process-group `PreviewProcessManager` (`dev_preview` with guaranteed turn/exit teardown) | `SPEC-06`, `SPEC-07`, `SPEC-08` |
| `companion/worktree_runner.py` | `Application` | Read-only repo workspace probe (`probe_repo_workspace`), parallel isolated `git worktree` Board card orchestrator (`dispatch_work_item_agent`, `dispatch_parallel_ready_items`, `collect_worktree_deliverable_receipt`), 1-click deliverable merge/push-PR/revision actions (`merge_worktree_into_base`, `push_worktree_branch_and_pr`, `request_worktree_changes`), and `<50ms`-cancelable `run_parallel_subagents` fan-out | `SPEC-06`, `SPEC-08` |
| `companion/os_tools.py` | `Application` | Companion OS state, Hot+Cold memory/KnowledgePort, task, workflow/DAG/Skill Forge, checkpoint, SDD, project, telemetry, privacy-aware `web_search`, and doctor tool handlers | `SPEC-06`, `SPEC-07` |
| `companion/tools.py` | `Application` | 28 `WorkspaceContext`-bound, phase- and `R3`-risk-gated tools (plus native `mcp__*` dispatch) with `ExecutionTarget` isolation, AST symbol reading, and inline linting | `SPEC-06`, `SPEC-07`, `SPEC-08` |
| `companion/context.py` | `Application` | KV-cache-stable `~700`-token `messages[0]` system prompt + per-turn `<turn_context>` (`build_turn_context_block`) with `<10ms` Epistemic Grounding, Project Graph HUD, `[CONNECTED FEDERATED PARTNERS]` HUD, Trust Mode, Workspace Radar, and Skill catalog | `SPEC-06`, `SPEC-07`, `SPEC-08` |
| `companion/agent.py` | `Application` | Unified `_run_step_loop` executive engine with Step-0 Cognitive Triage Gate, `<100ms` `CancelScope` turn abort, quiet turn-boundary session nudges, stateful Blueprint Q&A/approval routing, `<think>` scratchpad continuity, and `/slash` handler | `SPEC-06`, `SPEC-07` |
| `companion/autonomy.py` | `Application` | Multi-phase `GoalPlan` DAG driver (`/goal`, `run_autonomous_goal_slice` windowed `on_idle` slices, DIKW milestone seeding, `human_gate`/`R3` pauses, `WorkspaceContract` verification, `REFLECT` distillation) | `SPEC-03`, `SPEC-05`, `SPEC-06`, `SPEC-08` |
| `companion/blueprint.py` | `Application` | Interactive `Plan -> Align -> Approve -> Build` Blueprint gate (`create_or_revise_blueprint`, `handle_blueprint_conversation_turn`, `approve_blueprint_and_build`) synced to `ProjectGraphManager` | `SPEC-03`, `SPEC-06`, `SPEC-08` |
| `companion/eval_harness.py` | `Application` | Isolated multi-flight live behavioral evaluation suite (`quick`, `full`, `general`, `all`) & head-to-head `coding` model benchmark (`run_live_flight_eval`, `run_coding_model_comparison`) | `SPEC-06` |
| `harness.py` | `Presentation` | Self-recovering CLI/REPL entry point, `--autonomy`/`--jobs`/`--wiki`/`--vault`/`--endpoints`/`--mcp`/`--partners`/`--routines` flags, startup routine reconciliation, and `IdleAutonomyWatcher` | `SPEC-05`, `SPEC-06`, `SPEC-07` |
| `surfaces/web_assets.py` | `Presentation` | Static asset loader (`surfaces/static/index.html`, `styles.css`, `app.js` compiled from `frontend/`), safe `<\/script` escaping, mtime caching, and zero-login CSRF meta-tag injection | `SPEC-06`, `SPEC-07` |
| `surfaces/api_extensions.py` | `Presentation` | Headless `GET/POST /api/actions` CoreActions adapter, `POST /api/reload` live service reload coordinator, `GET /api/status` Autonomy + Project Graph enrichments, `IdleAutonomyWatcher` factory, and Bearer-token `POST /api/mcp` Order Counter adapter | `SPEC-05`, `SPEC-06`, `SPEC-07` |
| `surfaces/web_server.py` | `Presentation` | Stdlib `ThreadingHTTPServer` web server (`127.0.0.1` default + `COMPANION_WEB_TOKEN` non-loopback LAN/Tailscale auth) with `Host`/`Origin`/`Sec-Fetch-Site`/unconditional `X-Companion-CSRF`, live SSE (`GET /api/jobs/<id>/stream`), CoreActions, Hosted MCP, and WAL reads | `SPEC-06`, `SPEC-07` |
| `surfaces/windows_tray.py` | `Presentation` | Zero-dependency Win32 `ctypes` System Tray controller (`--tray`, including 1-click `Reload Companion`), single-instance `/api/health` detection, user `data/` folder opener, and graceful port-conflict diagnostics | `SPEC-06` |
| `configs/architecture.py` | `Domain` | Executable A-Frame component registry (53 core components), unregistered-module detector, dependency name validator, and downward layer dependency verifier | `SPEC-01`, `SPEC-03`, `SPEC-07`, `SPEC-08` |
| `memory/chat_memory.py` | `Domain` | Backward-compatible `ChatMemory` facade over `companion/memory_store.py` | `SPEC-02` |

---

## 3. Architectural Laws (Enforced by `system_doctor` & Inline Micro-Linter)

1. **No Undocumented Modules**: Adding a `.py` file to `companion/`, `surfaces/`, or `memory/` without registering it in `configs/architecture.py`, `specs/ARCHITECTURE.md`, and `specs/CONTRACTS.md` fails Stages 3 & 4 of `system_doctor`.
2. **Import-Qualified Dead-Code & Budget Law**: `companion/hygiene.py` resolves cross-file `from companion.<mod> import <sym>` and `alias.<sym>` references so unimported definitions, unused imports, semicolon-chained statements, `>700L` modules, or `>100L` functions fail both inline `lint_single_file_edit` and Stage 5 of `system_doctor`.
3. **Strict 4-File `specs/` Cap**: `specs/` may only ever contain `PRODUCT.md`, `ARCHITECTURE.md`, `CONTRACTS.md`, and `DECISIONS.md`.
4. **Mandatory Spine Telemetry**: Every LLM call (`companion/llm.py`), tool dispatch (`companion/tools.py`), and agent turn (`companion/agent.py`) is traced via thread-safe `companion/telemetry.py`.
5. **Headless Core vs. Frontend UI Boundary Law**: Modules inside `companion/` remain 100% presentation-agnostic; `harness.py` and `surfaces/` act strictly as presentation views over `companion/` APIs and `data/conversations.db`.
