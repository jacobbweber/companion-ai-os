# Agentic AI Harness Architecture & Scaling Patterns

## 1. The 3-Layer Frontier Harness Model (Tools vs. Skills/Domains vs. Loop — 25 Core Modules)
- **L1 — Orthogonal Tools (`companion/tools.py`, `companion/worktrees.py`, `companion/integrations.py`, `companion/attachments.py`)**: `<20` high-leverage primitives (`read_file`, `write_file`, `edit_file`, `search_files`, `run_shell`, `run_script`, `git_manage`, `dev_preview`, `wiki_manage`, `board_manage`, `calendar_manage`, `education_manage`, `workflow_manage`, `automation_manage`, `endpoint_manage`, `call_mcp`, `subagent`, `background_job`), with automatic scratch spillover (`data/scratch/`) for outputs `> 8,000` chars and full `storage_path` + `extracted_path` attachment handoff.
- **L2 — Progressive Disclosure Skills & ICM Domain Capability Packs (`skills/<name>/`, `data/skills/<name>/`, `data/domains/<pack>/` + `companion/workflows.py`)**:
  - **Tier 1 (Compact 1-Line Index in `companion/context.py`)**: `name — description (path)` + top-level `data/CONTEXT.md` routing table.
  - **Tier 2 (`SKILL.md` / `CONTEXT.md` — loaded on demand via `read_file`)**: Procedural instructions, domain routing map, and SQLite schema documentation.
  - **Tier 3 (`references/*.md`, `workflows/*.workflow.json`, `scripts/*.py`, `<pack>.db` — 0 tokens until needed)**: Deep reference docs, declarative JSON workflows, `--self-test` verified Python scripts, and isolated WAL SQLite domain databases (`data/domains/<pack>/<pack>.db`).
- **L3 — The Single-Agent ReAct Harness (`companion/`)**:
  - **Infrastructure (`config.py`, `telemetry.py`, `streaming.py`, `llm_wire.py`, `llm.py`, `model_registry.py`)**: Multi-provider & multi-dialect (`openai_compat`, `anthropic_messages`, `gemini_native`) HTTP/SSE gateway, `ContextVar` stream bus, `ModelGate` swap mutex, `<think>`/`<tool_call>` normalization, and Runtime Health Doctor.
  - **Domain (`memory_store.py`, `approvals.py`, `vault.py`, `projects.py`, `wiki.py`, `wiki_curation.py`, `education_engine.py`, `calendar_engine.py`, `attachments.py`)**: SQLite WAL + FTS5 working memory, Blind Credential Vault & CoW snapshots, Universal Project & Goal Graph, Co-Owned 4-Folder Knowledge Wiki, Multimodal Attachments, SM-2 Education Engine, and Approval Queue (`R0..R3`).
  - **Application (`agent.py`, `context.py`, `tools.py`, `workflows.py`, `jobs.py`, `routines.py`, `worktrees.py`, `integrations.py`, `core_actions.py`)**: Single Primary ReAct Loop (`chat`, `/plan`, `/goal`), `KeyTokens`-verified LLM semantic compaction, Declarative JSON Workflow Engine (`llm_step`, `run_script`, `call_tool`, `mcp_step`, `human_gate`, `foreach`), Forever-Chat Background Jobs, TriggerPolicy Routines, Parallel Git Worktrees, and Federated MCP Hub.

## 2. Memory Taxonomy (Episodic vs. Procedural vs. Semantic/Structured)
1. **Episodic & Working Memory (`conversations.db` + `MEMORY.md` via `companion/memory_store.py`)**: Conversation history (FTS5 BM25 + RRF search), cold archive snapshots, and LLM-distilled durable user preferences.
2. **Procedural & Domain Capability Packs (`skills/`, `data/skills/`, `data/domains/` + `companion/workflows.py`)**: Step-by-step runbooks (`SKILL.md`), domain context maps (`CONTEXT.md`), declarative `*.workflow.json` pipelines, verified CLI scripts, and isolated domain SQLite databases (`<pack>.db`).
3. **Co-Owned Knowledge Warehouse (`01_inbox`..`04_archive` via `companion/wiki.py` & `companion/wiki_curation.py`)**: 11 governed Markdown templates, multi-turn overlap absorption, 2-hop `ontology` bridges, real LLM prose synthesis & freshness verification, and Never-Delete `04_archive/` lineage.

## 3. Scaling Beyond a Single Context Window
- **2-Stage `KeyTokens`-Verified Semantic Compaction (`companion/context.py`)**:
  - **Stage 1**: Zero-loss pruning of stale verbose tool outputs (`read_file`, `run_shell`) from older turns plus automatic `data/scratch/` spillover (`> 8,000` chars).
  - **Stage 2**: Folds older turns via `fast`-tier LLM summarization verified deterministically against extracted `KeyTokens` (file paths, identifiers, IPs/numbers) while archiving verbatim messages in SQLite.
- **Isolated Subagents (`companion/agent.py:run_subagent`)**:
  - Spawns disposable worker loops (`run_parallel_subagents`) with clean context windows and optional multimodal vision attachment forwarding, returning only concise syntheses to the primary loop.
- **Declarative JSON Workflows (`companion/workflows.py`)**:
  - Executes multi-step `*.workflow.json` pipelines combining deterministic `run_script`/`call_tool`/`mcp_step` nodes, schema-validated `llm_step` nodes, `foreach` fan-out, and `human_gate` approval pauses backed by SQLite `workflow_runs` checkpoints.
