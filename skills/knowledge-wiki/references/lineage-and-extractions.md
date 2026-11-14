---
author: user
---

# Evolutionary Lineage, Comparative Forensics & Extraction Reference (`Gen 1`–`Gen 5`)

> **Purpose**: Authoritative architectural reference documenting the five generations of the Companion / Agent-OS lineage (`crucible`, `AutoReiv`, `Agent-OS`, `agent-os`, and `Companion`), the quantitative and code-level forensics across all 8 core subsystems, why each predecessor hit a complexity or latency wall, and the ranked **Extract, Adapt, or Reject** blueprint for `Companion`.

---

## 1. Executive Summary & Evolutionary Lineage

Across the four predecessor prototypes (`crucible`, `AutoReiv`, `Agent-OS`, `agent-os`) and the current `Companion` repository, the Companion / Agent-OS architecture evolved through **five distinct generations** totaling **177,563 lines of production Python**, **134,929 lines of test Python**, **65,172 lines of web UI code**, and **119,344 lines of Markdown specs/ADRs/cards**:

1. **Gen 1 — `crucible`**: *Experimental Multi-Tier Async Prototype* (82 commits through `2026-10-09`; 142 prod `.py` files, 15,619 LOC). Introduced the **Single Companion Paradigm** (`docs/steering/product.md`), `InferenceSemaphore` (`src/agent_os/infrastructure/gateway/semaphore.py`, 86L), model-specific thinking/structured payload builders (`openai_payload.py`, 88L), `ToolsmithWorker` dynamic tool synthesis with mandatory pre-flight dry-run (`kernel/toolsmith.py`, 298L), `ToolPolicyGate` (`kernel/policy_gate.py`, 89L), 5-field stdlib cron (`kernel/scheduler.py`, 249L), and `ADR-0015` Contract-First Ingredients (`CapabilitySignature` `verb.resource.scope.effect` + `fastembed` cosine recall + JSON Schema binding check in `kernel/intent_resolver.py`, 272L).
2. **Gen 2 — `AutoReiv`**: *Full-Stack Control Plane & Multi-Agent Platform* (2,585 commits through `v1.6.0` on `2026-10-26`; 379 prod `.py` files, 89,169 LOC + 320 JS/CSS/HTML files, 57,835 LOC + 1,307 Markdown docs/cards, 88,346 LOC). Built a full FastAPI + vanilla SPA platform with Chat Studio, Agent Studio (including **Tone Picker**), Wiki Studio (mind map, frontmatter editor, weekly rollover), Education Studio, Routines Studio, Voice TTS streaming (`v1.6.0`), SSH Homelab fleet ops, `AllowedTools` (`CARD-539` single-lever skill-to-tool resolution), `CommandGuardrail`, and OpenTelemetry waterfall tracing (`CARD-700`), and retrofitted patterns from `crucible` and `Agent-OS` (`CARD-698`–`CARD-708`). Hit severe monolith and coordination friction (`execute_react_loop` grew to **704 lines** in a single function; 733 task cards).
3. **Gen 3 — `Agent-OS` (uppercase)**: *Zero-Dependency Plain-Language Stdlib Core* (161 commits through `2026-10-31`; 60 prod `.py` files, 36,913 LOC; **0 external runtime dependencies**). Proved the entire companion could run on pure Python 3.11+ stdlib (`urllib.request`, `sqlite3`, `http.server`). Pioneered **Entity- & Token-Verified Compaction** (`agent_os/compaction.py`, 507L), **Hot/Cold SQLite & Trace Retention** (`agent_os/retention.py`, 232L), **Deterministic Time-Window Past-Chat Recall** (`agent_os/time_window.py`, 248L), **Per-Call Foreground-Priority `ModelGate`** (`agent_os/model_gate.py`, 167L), **Child-Process Tool Isolation** (`agent_os/tool_isolate.py`, 188L), **Dual-Author Optimistic-Locked Wiki** (`agent_os/wiki.py`, 1,517L), and **Spec 0030: Agent Hobbies** (`docs/specs/0030-agent-hobbies.md`, 157L — specified, not built). Stalled under massive god-modules (`workflow_doc.py` at **3,824 lines**, `tasks.py` at **2,696 lines**) and brittle English-prose regex guards (`reply_guard.py` at **668 lines**).
4. **Gen 4 — `agent-os` (lowercase)**: *Strict Two-Layer DDD & Capability-Pack Architecture* (152 commits through `2026-11-07 10:34 -0500`; 152 prod `.py` files, 30,357 LOC; **0 external runtime dependencies**). Enforced a strict `scripts/check.py` gate (251L: `<= 500` lines/file on `src/` and `tests/`, Layer 1 `src/companion/` vs. Layer 2 `src/surfaces/` import boundary), replaced `Agent-OS`'s 3,824-line workflow parser with a clean two-file JSON Six-Shape workflow engine (`workflow.py` 486L + `workflow_store.py` 451L), and built the **8-Check `CAPABILITY.md` Pack Loader** (`pack_loader.py`, 483L), **Progressive Pack Disclosure** (`registry.py`, 488L), **3-Tier Make-vs-Borrow Self-Authoring** (`pack_authoring.py`, 499L), **`CloneWorkspaceManager` Git Sandbox** (`sandbox.py`, 384L), **Hierarchical Plan/Roadmap Engine** (`planner.py`, 466L), and **`t81` Empty-Reply Recovery** (`client.py:L307-373`). Stalled when `src/companion/memory/claim_match/` ballooned into **38 files (~11,500+ LOC)** with **19–61s per-turn latency** during `t74`–`t81`, while `capability_packs/sdlc/tools/devops_app_deployer/tool.py` bypassed `scripts/check.py` to reach **937 lines**.
5. **Gen 5 — `Companion` (current repository)**: *A-Frame Regulated Zero-Dependency Core* (`100%` Python 3.12 stdlib). Enforces a 6-layer A-Frame DAG (`configs/architecture.py`, `companion/disciplines.py`), `<= 550` lines/module, `<= 100` lines/function, 0 dead code / 0 unused imports (`companion/hygiene.py`), 4 canonical specs + 3-Tier skill/recipe linting (`companion/sdd.py`), SQLite spine waterfall telemetry (`companion/telemetry.py`), and is the **first generation to ship an autonomous `HobbyEngine`** (`companion/hobbies.py`) with pre-hobby checkpoints, 1-pass self-repair, `system_doctor` gating, rollback lesson memory, and Morning Briefings.

---

## 2. Pass 1: Quantitative Codebase & Structural Forensics

| Metric | Gen 1: `crucible` | Gen 2: `AutoReiv` | Gen 3: `Agent-OS` | Gen 4: `agent-os` | Gen 5: `Companion` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Git Commits & Last Active** | 82 commits (`2026-10-09`, `ecb6104`) | 2,585 commits (`2026-10-26`, `e80503e3`, `v1.6.0`) | 161 commits (`2026-10-31`, `f144bfd`) | 152 commits (`2026-11-07 10:34`, `af1a8b1`) | Active working tree (`2026-11-08`, `v2.1`) |
| **Production Python Files / LOC** | 142 files / 15,619 LOC | 379 files / 89,169 LOC | 60 files / 36,913 LOC | 152 files / 30,357 LOC | 19 files / ~5,500 LOC |
| **Test Python Files / LOC** | 85 files / 10,403 LOC | 570 files / 82,479 LOC | 42 files / 27,252 LOC | 67 files / 13,988 LOC (678 tests) | 7 files / ~850 LOC |
| **Web UI (JS/CSS/HTML) Files / LOC** | 3 files / 2,005 LOC | 320 files / 57,835 LOC | 12 files / 5,332 LOC | Inline in `web_assets.py` (493L) | 0 (Headless Core + CLI REPL) |
| **Markdown Docs / Specs / Cards** | 31 files / 1,898 LOC (15 ADRs) | 1,307 files / 88,346 LOC (733 cards, 68 ADRs) | 212 files / 25,504 LOC (45 decisions, 35 specs) | 103 files / 2,927 LOC (10 domain specs) | 4 canonical specs + 6 skills + Wiki |
| **Runtime Dependencies (`pyproject.toml`)** | 10 (`pydantic`, `httpx`, `fastembed`, `mcp`, `starlette`, `uvicorn`, `cryptography`, `jsonschema`, `pyyaml`) | 11 Python + Node/Playwright | **0** (100% Python 3.11+ stdlib) | **0** (100% Python 3.11+ stdlib) | **0** (100% Python 3.12 stdlib) |
| **Prod Files > 550 Lines** | 0 (max 404L) | 43 files (max 789L) | **26 of 60 files (43.3%!)** | 1 file (`devops_app_deployer/tool.py` at 937L; 15+ files at 483–500L) | **0** (enforced `<= 550L` gate in `companion/hygiene.py`) |
| **Functions > 100 Lines (AST)** | 15 functions (max 317L) | **98 functions** (max 704L!) | 10 functions (max 261L) | 26 functions (max 509L) | **0** (enforced `<= 100L` gate in `companion/hygiene.py`) |

---

## 3. Pass 2 & Pass 3: Deep Subsystem Forensics Across All 8 Subsystems

### Subsystem 1: Executive Loop, Turn Control, Model Client & Concurrency
- **Per-Call Foreground-Priority `ModelGate` (`Agent-OS/agent_os/model_gate.py:L36-167`)**:
  - When a local model server (`vllm serve` with `--max-num-seqs 2`) runs both interactive user turns and multi-step background hobbies, holding a coarse lock across a 20-step hobby blocks user chat.
  - `ModelGate` uses stdlib `threading.Condition`: foreground turns acquire `foreground_turn()`, while background tasks acquire `call(priority="background")` **per individual LLM HTTP call**, immediately yielding between calls whenever a foreground turn is waiting or active.
- **`t81` Empty-Reply Recovery & `<think>` Tag Extraction (`agent-os/src/companion/models/client.py:L80-124, L307-373`)**:
  - Local reasoning models (`deepseek-r1`, `qwen`) emit `<think>...</think>` blocks or `reasoning_content` and occasionally return `content: ""` with `tool_calls: []` after spending tokens in reasoning (`t78`/`t81`).
  - Stripping `<think>...</think>` blocks into a separate reasoning field and retrying once on empty synthesis (`tools=None`) prevents `(Empty response from model.)` failures.

### Subsystem 2: Context Assembly, Memory Architecture, Compaction & Recall
- **Entity-Preserving Token-Verified Compaction (`Agent-OS/agent_os/compaction.py:L55-248` & `AutoReiv/src/application/kernel/compaction_verifier.py`)**:
  - Standard LLM summarization drops file paths, port numbers, dates, or `snake_case` identifiers, and is vulnerable to prompt injection from old transcript lines.
  - `normalize_text` folds `NFKC`, maps Unicode dashes `U+2010`..`U+2015` and `U+2212` to ASCII `-` (`_DASH_TO_HYPHEN`), and strips `Cf` format chars.
  - `extract_key_tokens` scans user turns and prior summaries to extract `KeyTokens(paths, numbers, names)`.
  - `verify_compaction_summary` verifies that every extracted path, number, and identifier is preserved in the compacted summary (or deterministically appends/restores missing tokens).

### Subsystem 3: Skills, Capability Packs & Tool Execution
- **3-Tier `skills/<name>/` (`Companion`) vs. `CAPABILITY.md` (`agent-os`)**:
  - Keeping L1 atomic tools in `companion/tools.py` separate from L2/L3 declarative skill packs (`skills/<kebab-name>/SKILL.md` + `references/*.md` + `workflows/*.json` + `scripts/*.py`) prevents unpoliced Python monoliths inside capability packs (such as `devops_app_deployer/tool.py` reaching 937 lines in Gen 4).

### Subsystem 4: Workflows, Recipes, State Machines & Subagents
- `companion/workflow_graph.py` unifies an 11-state engineering state machine (`IDLE`, `INTAKE`, `DISCOVER`, `DESIGN`, `PLAN`, `IMPLEMENT`, `VERIFY`, `RECOVER`, `REFLECT`, `COMPLETE`, `ABORTED`) with declarative JSON workflow DAGs (`skills/*/workflows/*.json`) and isolated subagent workers (`run_subagent` in `companion/agent.py`).

### Subsystem 5: Autonomy, Background Jobs & Agent Hobbies
- `companion/hobbies.py` implements the Night-Shift Safety Sandbox (`cp_hobby_...` checkpoint, bounded iterations, 1-pass self-repair, 7-stage `run_system_doctor` gate, automatic file + SQLite rollback on failure, rollback lesson memory, and `/morning` briefing).

### Subsystem 6: Safety, Command Guardrail & Path Confinement
- Combining `CommandGuardrail` (`crucible/.../policy_gate.py` & `AutoReiv/.../command_guardrail.py`) with `WorkspaceContext` path confinement (`ADR-007`) blocks destructive shell patterns (`rm -rf /`, `git push --force`, `git reset --hard`, `del /s /q`, `Remove-Item -Recurse -Force`, `format`, `mkfs`, `dd if=`, `shutdown`/`reboot`) and prevents writes outside allowed workspace roots.

### Subsystem 7: Observability, Waterfall Tracing & `SecretRedactor`
- `companion/telemetry.py` stores `telemetry_traces`, `telemetry_spans`, and `telemetry_logs` directly in SQLite (`memory/conversations.db`), avoiding thousands of loose `.jsonl` files. Adding `SecretRedactor` (`agent-os/src/companion/observability/redact.py`) guarantees API keys, Bearer tokens, and `.env` secrets (`>= 8` chars) are scrubbed before persistence.

### Subsystem 8: Surface Layer, UX & Knowledge Wiki
- `skills/knowledge-wiki/` provides a 3-Tier Personal Knowledge Base (`SKILL.md` + `references/INDEX.md` + topic articles), enforced by `verify_skills_directory()` in `companion/sdd.py`.

---

## 4. Pass 4: Root-Cause Autopsy — Why Each Predecessor Hit a Wall

1. **Gen 1 (`crucible`, stopped `2026-10-09`)**: `anyio`/FastMCP cross-task cancel scope crashes during dynamic tool compilation (`91e9cd7`) + split regex `[DISPATCH: ...]` vs. native tool calling + stubbed `ToolsmithWorker` fallbacks.
2. **Gen 2 (`AutoReiv`, stopped `2026-10-26`, 2,585 commits)**: Monolith accretion (`execute_react_loop` reached **704 lines** in one function; 43 files > 550L; 98 functions > 100L; 733 task cards).
3. **Gen 3 (`Agent-OS`, stopped `2026-10-31`)**: Uncapped file sizes (`workflow_doc.py` 3,824L, `tasks.py` 2,696L; 26 of 60 files > 550L) and brittle English-prose regex policing (`reply_guard.py` 668L).
4. **Gen 4 (`agent-os`, stopped `2026-11-07 10:34`)**: The `src/companion/memory/claim_match/` dual-model LLM memory judge grew to 38 files (~11.5k LOC) and caused **19–61s latency per explicit memory write and 24s per no-op turn** (`952845c`), plus UUID corruption (`t78`) and empty synthesis replies (`t81`), while `capability_packs/` bypassed `scripts/check.py`.

---

## 5. Pass 5: Ranked "Extract, Adapt, or Reject" Blueprint

### Wave 1 (Core Safety, Model Resilience, Lossless Compaction & Foreground Priority)
- **P0.1 (`KeyTokens` Entity-Verified Compaction)**: `normalize_text` (`_DASH_TO_HYPHEN`), `extract_key_tokens` (`paths`, `numbers`, `names`), and `verify_compaction_summary` in `companion/context.py`.
- **P0.2 (`CommandGuardrail` + `WorkspaceContext` Path Lock)**: Destructive shell blocking (`check_command_safety`) and symlink-resolved workspace path confinement (`check_path_safety`) in `companion/safety.py` and `companion/tools.py` (`ADR-007`).
- **P0.3 (`t81` Empty-Reply Recovery + `<think>` Extraction)**: `strip_reasoning_tags` and 1-retry empty-synthesis recovery in `companion/llm.py` and `companion/agent.py`.
- **P0.4 (`ModelGate` Foreground Preemption)**: `ModelGate` (`threading.Condition`) in `companion/llm.py` + non-blocking/preemptible idle hobbies in `harness.py`.
- **P1.1 (`SecretRedactor`)**: Regex + known-secret scrubbing in `companion/telemetry.py`.

### Wave 2 (Future Optional Enhancements — On Demand Only)
- **P1.2**: SQLite FTS5 (`conversations_fts`) + `parse_time_window` in `companion/memory_store.py`.
- **P1.3**: `{{steps.<id>.output.<field>}}` structured JSON interpolation in `companion/workflow_graph.py`.
- **P1.4**: Optimistic-locked `content_hash` + `author` provenance on `skills/knowledge-wiki/references/`.

### Explicit "DO NOT EXTRACT" (Reject) Anti-Patterns
1. **REJECT** Gen 4's 38-file `claim_match/` dual-model LLM memory judge (19–61s turn latency).
2. **REJECT** Gen 3's 668-line `reply_guard.py` and 3,824-line `workflow_doc.py` English-prose regex policing.
3. **REJECT** Gen 2's 704-line monolithic `execute_react_loop` and multi-persona roundtable.
4. **REJECT** Unpoliced Python code outside AST hygiene and line-budget gates.
