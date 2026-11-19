---
name: sdd-compliance
description: Verify 4-File Canonical Spec-Driven Development (SDD) alignment, AST dead-code hygiene, simplicity budgets, and downward A-Frame layer dependencies.
required_tools: [read_file, edit_file, sdd_manage, system_doctor]
---

# SDD Compliance & Codebase Hygiene Runbook

## When to Use This Skill
Use this runbook before completing any code or architectural change, when diagnosing a Stage 3/4/5 `system_doctor` failure, or when asked to audit code quality, SOLID principles, and spec sync.

## Architectural Invariants
1. **Strict 4-File `specs/` Cap (`ADR-005`)**:
   - `specs/` may ONLY contain `PRODUCT.md`, `ARCHITECTURE.md`, `CONTRACTS.md`, and `DECISIONS.md`.
   - Never create extra markdown files inside `specs/`. Operational runbooks belong in `skills/<skill-name>/SKILL.md`.
2. **1-to-1 Module Registration**:
   - Every `.py` module in `companion/`, `surfaces/`, `configs/`, and `memory/` must be documented in both `specs/ARCHITECTURE.md` and `specs/CONTRACTS.md`, and mapped in `MODULE_LAYER_MAP` (`companion/disciplines.py`) and `CORE_COMPONENTS` (`configs/architecture.py`).
3. **AST Hygiene & Simplicity Budgets (`ADR-006`)**:
   - Zero unused imports across the repository and zero unreferenced top-level functions/classes in `companion/`.
   - Every module must stay `<= 700` lines (`COMPANION_MAX_MODULE_LINES`) and every function `<= 100` lines (`COMPANION_MAX_FUNCTION_LINES`), with zero semicolon-chained statements (`stmt1; stmt2`).
4. **Downward-Only A-Frame Layer Flow**:
   - `Presentation (harness.py, surfaces/)` -> `Application (agent, autonomy, blueprint, intake, skill_forge, eval_harness, context, compaction, background_jobs, endpoints, mcp_hub, routines, calendar_engine, core_actions, harvest_tools, sdlc_tools, tools, os_tools, tool_schemas)` -> `Workflow (workflow_graph, workflow_dag, dag_runtime)` -> `Domain (checkpoints, safety, sdd, hygiene, disciplines, grounding, trigger_policy, project_graph, archive_and_approvals, vault, model_registry, wiki_templates, wiki_engine, wiki_curation, wiki_maintenance, attachments, education_engine, semantic_memory, memory_store, configs/architecture, memory/chat_memory)` -> `Infrastructure (config, telemetry, streaming, llm_wire, llm)`.

## Step-by-Step Procedure
1. **Run SDD & Hygiene Verification**:
   - Call `sdd_manage(action="verify")` to inspect both the 4-File SDD report and the AST Hygiene report.
2. **Resolve Any Drift or Hygiene Issues**:
   - **Unused Import / Dead Definition**: Use `read_file` around the reported line number and remove or wire the symbol via `edit_file`.
   - **Undocumented Module / Contract Drift**: Update `specs/ARCHITECTURE.md` and `specs/CONTRACTS.md` via `edit_file`.
   - **ADR Sync**: Call `sdd_manage(action="sync")` to synchronize and compact `specs/DECISIONS.md` and `MEMORY.md`.
3. **Final Verification Gate (or Automated Python DAG)**:
   - Call `system_doctor(run_tests=True)` and confirm all 7 stages report `[PASS]`, OR run the bundled Tier-3 Python DAG (`dags/full_audit.py`) via `workflow_manage(action="run_dag", dag="full-audit")`.
