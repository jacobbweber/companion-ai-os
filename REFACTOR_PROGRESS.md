# Frontier Standard Harness Refactor — Live Execution Tracker (`refactor/frontier-standard-harness`)

> Temporary disk progress tracker created per `/goal` execution protocol. Removed at final cleanup before sign-off.
> **Hard Invariants**: Do NOT modify `main` or production `Spark` (`Spark` inference endpoint allowed read-only for LLM calls). Preserve 100% of `frontend/src/` visual styling, themes, and mobile layout.

## Master Phase Checklist

- [x] **Phase 1: Hard-Separate Builder (`.agents/`) from Runtime (`skills/` & `data/`) + Retire Repo Self-Policing**
  - [x] Move builder-only skills (`anti-sprawl-gate`, `product-intent-dogfood`, `web-surface-dev`, `release-readiness`, `devops-ops`) to `.agents/skills/` and point `.agents/skills.json` strictly to `.agents/skills`.
  - [x] Retire obsolete SDD/state-machine policing skills (`sdd-compliance`, `engineering-workflow`, `test-driven-verification`), update `.agents/rules/*.md`, and parameterize hardcoded LAN IPs in `live_model_calibration.py`.
  - [x] Promote `extract_pdf.py` to `companion/scripts/extract_pdf.py` and decouple Ring-0 (`attachments.py`, `worktrees.py`) from reaching into dev skills.
  - [x] Retire `companion/sdd.py`, `companion/hygiene.py`, `companion/disciplines.py` (move `ActionRiskTier` to `approvals.py`), `companion/eval_harness.py`, `configs/architecture.py`, `configs/__init__.py`, and `memory/chat_memory.py` + `specs/` policing checks.
  - [x] Replace `run_system_doctor` with a clean **Runtime Health Doctor** (Python/uv, SQLite WAL & FTS5, LLM provider connectivity, Vault permissions, compiled UI assets, Domain Pack DB health).
  - [x] Run tests & commit Phase 1 checkpoint on `refactor/frontier-standard-harness`.

- [x] **Phase 2: Frontier Single-Agent ReAct Harness, Orthogonal Tools, Spillover & Real Compaction**
  - [x] Consolidate `memory_store.py` (absorbing `semantic_memory.py` minus hardcoded homelab `_CONCEPT_CLUSTERS`), `approvals.py` (from `archive_and_approvals.py` + `ActionRiskTier`), and `vault.py` (absorbing CoW snapshots from `checkpoints.py` + Runtime Health Doctor).
  - [x] Consolidate `context.py` (absorbing skill/domain-pack discovery from `grounding.py` + `compaction.py`): `IDENTITY.md` + `data/CONTEXT.md` + 1-line `name — description (path)` index, fixing external-workspace `data/skills/` & `data/domains/` path resolution.
  - [x] Wire real `fast`-tier LLM `summarizer_fn` + deterministic `KeyTokens` check into `compaction` (`/compact` & watermark) + real durable fact distillation into `MEMORY.md`.
  - [x] Consolidate `tools.py` (`< 20` orthogonal primitive + domain tools, automatic `data/scratch/` spillover for outputs `> 8,000` chars, lazy `call_mcp`, retiring `tool_schemas.py`, `os_tools.py`, `harvest_tools.py`, `safety.py`, `grounding.py`, `checkpoints.py`).
  - [x] Consolidate `agent.py` into a clean Single Primary ReAct Loop (`chat`, `/plan` read-only approval mode, `/goal` extended budget + Board sync, mechanical foreground `ApprovalQueue` pause/resume, isolated subagents), retiring `intake.py`, `autonomy.py`, `blueprint.py`, `workflow_graph.py`.
  - [x] Run tests & commit Phase 2 checkpoint on `refactor/frontier-standard-harness`.

- [x] **Phase 3: Full-Fidelity Attachment Handoff, Declarative JSON Workflows & Domain Capability Packs**
  - [x] Upgrade `attachments.py`: expose `storage_path` & `extracted_path` in API + `<turn_context>` for CSVs, PDFs, Office docs, and images; replace 80-row/`4,000`-char CSV mutilation with schema + sample + full `storage_path`; preserve image vision across follow-up turns & opt-in subagents; fix `dev_preview(vision_critique)` `bytes` vs `base64` bug.
  - [x] Build `companion/workflows.py` (declarative `*.workflow.json` schema + typed Node Catalog: `llm_step` w/ JSON schema validation, `run_script`, `call_tool`, `mcp_step`, `human_gate`, `foreach` + `contextvars.copy_context()` + SQLite `workflow_runs`/`dag_runs` checkpoints), retiring `dag_runtime.py` & `workflow_dag.py` and migrating valid runtime workflows to `*.workflow.json`.
  - [x] Establish `data/CONTEXT.md` + `data/domains/<pack>/` ICM Domain Capability Pack support (`data/domains/<pack>/<pack>.db` WAL SQLite isolation + `scripts/` + `workflows/*.workflow.json`).
  - [x] Consolidate `jobs.py` (from `background_jobs.py` with full runtime skill/pack context) and `routines.py` (absorbing `trigger_policy.py`).
  - [x] Run tests & commit Phase 3 checkpoint on `refactor/frontier-standard-harness`.

- [x] **Phase 4: Eliminate All Hollow Verbs in `Knowledge` & `Study` Studios**
  - [x] Consolidate `wiki.py` (`wiki_engine.py` + `wiki_templates.py` + `wiki_maintenance.py`) and absorb `skill_forge.py` into `workflows.py`.
  - [x] Upgrade `propose_graduation` to synthesize both metadata AND complete structured Markdown prose via real LLM (zero `"Operational context recorded in..."` fake boilerplate, zero silent `"fallback"` green badges), enable `MERGE_AND_REFINE` overlap absorption in `WikiGraduateModal.tsx`, upgrade Chat `"Save to Wiki"` (`stage_turn`) with `user_prompt` + real `fast` LLM synthesis, and make `Verify Fresh` perform a real LLM claim/freshness audit (plus optional `quick_stamp`) while keeping staleness age math deterministic.
  - [x] Upgrade `education_engine.py` + `KnowledgeStudyView.tsx`: `+ Add Course` generates a real multi-module syllabus via LLM; add 1-click `Generate Flashcards` & `Generate Quiz` (`education.generate_items`) with schema validation; upgrade `curate_study_note` to synthesize real prose study notes while keeping SM-2 interval math & stage FSM deterministic.
  - [x] Build frontend (`npm --prefix frontend test` & `npm --prefix frontend run build`), run backend tests, & commit Phase 4 checkpoint.

- [x] **Phase 5: Eliminate All Hollow Verbs in `Board & Goals`, `Automations` & `Customizations`**
  - [x] Consolidate `projects.py` (`project_graph.py`) and `worktrees.py` (`worktree_runner.py` + `sdlc_tools.py` + native GitHub PR client).
  - [x] Upgrade `Slice 15m Unblocker` (`WorkItemInspector.tsx` -> `agile.slice_unblocker`) and `Seed 4 DIKW Stages` / Milestone Breakdown (`BoardGoalsSurface.tsx` -> `agile.seed_dikw`) to use real LLM domain-aware decomposition + deterministic SQLite persistence.
  - [x] Consolidate `integrations.py` (`endpoints.py` + `mcp_hub.py` with persistent `stdio` MCP client & real LLM MCP `Harvest Pack` generating `SKILL.md` + `*.workflow.json`), update `core_actions.py`, and upgrade `/checkin` to synthesize a real LLM priority coaching brief over deterministic state.
  - [x] Build frontend (`npm --prefix frontend test` & `npm --prefix frontend run build`), run backend tests, & commit Phase 5 checkpoint.

- [x] **Phase 6: Comprehensive Test Suite Alignment & Static UI Build Verification**
  - [x] Align `tests/` suite with the lean ~25-module architecture; verify 100% of 18 HTTP/SSE routes, 18 `callAction` domains, declarative workflows, attachment `storage_path` handoff, domain SQLite packs, and LLM-validated verbs.
  - [x] Run `npm --prefix frontend test`, `npm --prefix frontend run build`, full Python test suite, and Runtime Doctor; commit Phase 6 checkpoint.

- [ ] **Phase 7: Mandatory Live End-to-End Dogfood Gate & Final Cleanup**
  - [ ] Launch refactored Companion web server on ephemeral local port wired to a real live LLM (isolated test `COMPANION_DATA_DIR` so personal `data/` stays clean, or verified domain sandbox).
  - [ ] Drive `browser` subagent across ALL surfaces (`Chat`, `Board & Goals`, `Calendar`, `Knowledge` + `Study`, `Automations`, `Customizations`, `Settings`) on Desktop (`1440x900`) and Mobile (`390x844`) using everyday non-tech data (cooking recipes, household tasks, multi-row CSV + receipt image attachment, domain SQLite capability pack).
  - [ ] Verify `F5` reload + server restart survival, zero console/page/HTTP 5xx errors, and inspect screenshots + generated files/SQLite rows on disk; fix any issues discovered and re-verify live.
  - [ ] Remove temporary `REFACTOR_PROGRESS.md`, verify clean `git status` & tests, make final commit on `refactor/frontier-standard-harness`.
