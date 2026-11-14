---
name: skill-creator
description: Author or update a modular `skills/<skill-name>/` pack (`SKILL.md`, `references/`, `workflows/`, `scripts/`) to teach Companion a new capability.
required_tools: [list_dir, read_file, write_file, edit_file, system_doctor]
---

# Skill Creator & 3-Tier Progressive Disclosure Guide

## When to Use This Skill
Use this runbook whenever:
- The user asks you to learn a new skill, create a runbook, or package a workflow into `skills/`.
- You notice a repeatable multi-step procedure or domain reference that should be saved in `skills/<skill-name>/` rather than hardcoded into `companion/*.py`.

## The Flat 3-Tier Skill Folder Structure
Every skill lives in a single flat folder `skills/<skill-name>/` and supports **3 Tiers of Progressive Disclosure**:
```text
skills/<skill-name>/
├── SKILL.md              # Tier 1 (YAML frontmatter in prompt) + Tier 2 (Runbook & folder map)
├── references/           # Tier 3 (Optional): Domain reference docs & cheat sheets (*.md)
├── workflows/            # Tier 3 (Optional): Reusable declarative JSON DAG recipes (*.json)
└── scripts/              # Tier 3 (Optional): Helper Python scripts (pure stdlib OR isolated PEP 723 `uv` power tools)
```
- **Tier 1 (~25 tokens in System Prompt)**: Only `name` and `description` from `SKILL.md` are shown in the System Prompt.
- **Tier 2 (`SKILL.md` loaded on demand)**: Procedural runbook that also points to any files in `references/`, `workflows/`, or `scripts/`.
- **Tier 3 (`0 tokens` until needed)**: Specific files in `references/`, `workflows/`, or `scripts/` are loaded only when a step requires them.

## Pre-Flight: The 3-Tier "Make-vs-Borrow" Check
Before creating a brand-new `skills/<skill-name>/` directory, always run this quick check:
1. **Check Existing Skills & Wiki**: Inspect `/skills` and `skills/knowledge-wiki/references/INDEX.md`. If a closely related skill already exists, prefer adding a new `references/*.md` guide or `workflows/*.json` recipe inside that existing skill rather than proliferating folders.
2. **Check Installed Host CLIs**: Use `run_python` (`import shutil; print(shutil.which("..."))`) to check whether a standard CLI tool (`git`, `gh`, `rg`, `python`, `ssh`, `curl`) already solves the problem and only needs a lightweight `SKILL.md` wrapper.
3. **Dry-Run Any Helper Script ("Pure Brain, Open Toolbox" — `ADR-001`)**:
   - **Pure Stdlib Scripts (Default)**: Keep `skills/<skill-name>/scripts/<helper>.py` 100% Python stdlib (`<= 550` lines, `<= 100` lines/function, zero unused imports, with `--self-test` CLI flag) so it runs instantly anywhere via `sys.executable`.
   - **Isolated Power-Tool Scripts (PEP 723 + `uv`)**: When a skill genuinely needs specialized libraries (such as PDF/Office parsing, Playwright browser automation, or image/data tools), declare them in a PEP 723 inline header at the top of `scripts/<helper>.py`:
     ```python
     # /// script
     # dependencies = ["pypdf>=4.0"]
     # ///
     ```
     `workflow_manage(action="run_script", ...)` and `bootstrap_skill` automatically execute PEP 723 scripts inside an isolated ephemeral `uv run` environment without ever polluting Companion's zero-dependency Ring-0 core!

## How to Create or Extend a Skill
1. **Create `skills/<skill-name>/SKILL.md` with YAML Frontmatter**:
   ```markdown
   ---
   name: <skill-name>
   description: <1-sentence description of what the skill does and WHEN to use it>
   required_tools: [read_file, run_python]
   ---
   ```
   - **Deterministic Linter Rules (`SKILL LINTER`)**:
     - `name` must be kebab-case (`[a-z0-9-]+`) and match the parent folder `skills/<skill-name>/`.
     - `description` must be at least 10 characters.
     - `required_tools` must list the primitive tools used by the skill.
     - Body must contain `## When to Use This Skill` and `## Step-by-Step Procedure` (or `## How to ...`).
2. **Add Optional Tier-3 `references/` or `workflows/`**:
   - Put deep domain facts or cheat sheets in `skills/<skill-name>/references/<topic>.md` and link them in `SKILL.md`.
   - For general cross-domain facts or concepts, add them to `skills/knowledge-wiki/references/` and register them in `skills/knowledge-wiki/references/INDEX.md`.
   - Note: Wiki articles carrying `author: user` (or `human` / `partner`) in their YAML frontmatter are write-protected by `WIKI AUTHOR GUARD` and cannot be overwritten by Companion.
3. **Authoring Declarative JSON Workflow Recipes (`workflows/<recipe-name>.json`)**:
   - Workflow recipes define reusable deterministic DAGs executed by `workflow_manage(action="run_recipe", recipe="<name>")`.
   - Supports `{{inputs.<key>}}`, `{{last_output}}`, and `{{steps.<node_id>.output.<field>}}` (JSON dot-paths, `key=value` lines, or `first_line`).
   - **Deterministic Linter Rules (`WORKFLOW RECIPE LINTER`)**:
     ```json
     {
       "name": "<recipe-name>",
       "description": "Concise summary of the automated workflow pipeline.",
       "inputs": {"target": "default_value"},
       "nodes": [
         {"id": "step_1", "type": "tool", "tool": "sdd_manage", "args": {"action": "verify"}},
         {"id": "step_2", "type": "subagent", "skill": "sdd-compliance", "prompt": "Fix {{steps.step_1.output.first_line}} for {{inputs.target}}"}
       ],
       "edges": [
         {"from": "step_1", "to": "COMPLETE", "condition": "passed"},
         {"from": "step_1", "to": "step_2", "condition": "failed"},
         {"from": "step_2", "to": "COMPLETE", "condition": "always"}
       ]
     }
     ```
     - `name` must be kebab-case and match the `.json` filename stem.
     - Each node `type` must be `"tool"`, `"subagent"`, `"llm"`, or `"recipe"`.
     - Edge `to` targets must be a valid node `id`, `"COMPLETE"`, or `"ABORTED"`.
     - Edge `condition` must be `"always"`, `"passed"`, `"failed"`, `"contains:<text>"`, or `"not_contains:<text>"`.
4. **Verify Discovery & Linter Compliance**:
   - Run `system_doctor(run_tests=True)` to confirm all checks pass. The new skill and its bundled Tier-3 resources will automatically appear in `/skills`.
