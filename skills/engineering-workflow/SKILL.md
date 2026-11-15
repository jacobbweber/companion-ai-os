---
name: engineering-workflow
description: Execute safe, state-machine-governed software engineering, feature implementation, bug fixing, and checkpoint rollback recovery.
required_tools: [workflow_manage, checkpoint_manage, task_manage, read_file, edit_file, run_python, system_doctor]
---

# Safe Engineering & Self-Evolution Workflow Runbook

## When to Use This Skill
Use this runbook when implementing a new feature, fixing a bug, refactoring code, or evolving Companion's own capabilities.

## Core Principles
- **Own All Technical Complexity**: Translate the user's plain-English goals into architecture, code, and tests autonomously without asking the user to run tools or read stack traces.
- **Primitives Over Bloat**: Prefer extending existing cohesive modules or adding declarative `skills/<name>/SKILL.md` runbooks rather than creating unnecessary Python modules or single-purpose tools.

## Step-by-Step Procedure
1. **Intake & Discovery (`INTAKE` -> `DISCOVER`)**:
   - For multi-step feature builds, prefer `workflow_manage(action="autonomy", goal="...")` (or Python DAG `dags/deliver_feature.py` via `workflow_manage(action="run_dag", dag="deliver-feature")`) to step through all phases with dedicated per-phase context budgets and mechanical reasoning-tier routing, or start a manual workflow via `workflow_manage(action="start", workflow_type="engineering_feature", goal="...", acceptance_criteria="...")`.
   - Use `search` and `read_file` to inspect target files and existing unit tests in `tests/` before making edits.
2. **Design & Safety Checkpoint (`DESIGN` / `PLAN`)**:
   - For complex architectural decisions or multi-module designs, read `skills/deep-thinking/SKILL.md` or spawn a reasoning subagent (`workflow_manage(action="subagent", tier="reasoning", skill="deep-thinking", goal="...")`).
   - Create a pre-change workspace checkpoint via `checkpoint_manage(action="create", label="pre_feature_x")` so files and SQLite task state can be rolled back cleanly if needed.
3. **Clean Implementation (`IMPLEMENT`)**:
   - Use `edit_file` (including both any new import and its usage in the same edit so no unused import is left behind).
   - On Windows, always use `run_python` for executing Python snippets or one-off checks (never use bash heredocs in `run_shell`).
4. **Verification & OODA Self-Recovery (`VERIFY` / `RECOVER`)**:
   - Add or update unit tests in `tests/` and run `system_doctor(run_tests=True)`.
   - If a test or doctor check fails, never blindly guess: apply the **OODA Loop** (`skills/ooda-troubleshooting/SKILL.md` or `workflow_manage(action="run_recipe", recipe="ooda-diagnose")`). If it cannot be fixed within 2 attempts, call `checkpoint_manage(action="rollback")` to restore the clean state and record a rollback lesson.
5. **Reflection & Sync (`REFLECT` -> `COMPLETE`)**:
   - Update task/backlog status via `task_manage`, sync memory via `sdd_manage(action="sync")`, transition workflow to `COMPLETE`, and summarize the outcome in plain English.
