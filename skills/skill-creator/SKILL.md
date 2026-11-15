---
name: skill-creator
description: Author or update a modular `skills/<skill-name>/` or `data/skills/<skill-name>/` pack (`SKILL.md`, `references/`, `dags/`, `scripts/`) to teach Companion a verified capability or code-controlled Python DAG.
required_tools: [list_dir, read_file, write_file, edit_file, workflow_manage, system_doctor]
---

# Skill Creator & Capability Pack Forge Guide

## When to Use This Skill
Use this runbook whenever:
- The user asks you to learn a new skill, build a deterministic workflow DAG, or package a domain capability into `skills/` or `data/skills/`.
- You notice a repeatable multi-step procedure, domain validator, or MCP tool surface that should be promoted via `workflow_manage(action="promote_skill", ...)` rather than hardcoded into Ring-0 `companion/*.py`.

## The 3-Tier Capability Pack Structure
Every skill pack lives in `skills/<skill-name>/` (built-in) or `data/skills/<skill-name>/` (forged/harvested) and supports **3 Tiers of Progressive Disclosure**:
```text
skills/<skill-name>/
├── SKILL.md              # Tier 1 (YAML frontmatter in prompt) + Tier 2 (Runbook & asset map)
├── references/           # Tier 3 (Optional): Domain reference docs & MCP schemas (*.md)
├── dags/                 # Tier 3 (Optional): Code-controlled Python DAGs (*.py exposing `DAG: DAGSpec`)
└── scripts/              # Tier 3 (Optional): Helper Python scripts & validators (stdlib OR PEP 723 `uv` tools)
```
- **Tier 1 (~25 tokens in System Prompt)**: Only `name` and `description` from `SKILL.md` are injected into the System Prompt.
- **Tier 2 (`SKILL.md` loaded on demand)**: Procedural runbook linking to `references/`, `dags/`, or `scripts/`.
- **Tier 3 (`0 tokens` until needed)**: Specific references, Python DAGs, or helper scripts loaded or executed on demand.

## How to Create or Forge a Capability Pack
1. **Check Existing Skills & Wiki**: Inspect `/skills` and `wiki_manage`. Prefer adding a `references/*.md` guide or `dags/*.py` workflow to a cohesive existing pack before creating a new folder.
2. **Author `SKILL.md` with YAML Frontmatter**:
   - `name` must be kebab-case (`[a-z0-9-]+`) matching the folder name.
   - `description` must be at least 10 characters.
   - `required_tools` (non-empty list, e.g. `[read_file, workflow_manage]`) lists primitive tools used by the skill.
   - Body must include `## When to Use This Skill` and `## Step-by-Step Procedure` (or `## How to ...`).
3. **Author Code-Controlled Python DAGs (`dags/<dag_name>.py`)**:
   - Import `DAGNode, DAGSpec` from `companion.dag_runtime`.
   - Expose `DAG = DAGSpec(name="<kebab-name>", description="...", input_schema={...}, nodes=[...], self_test_fn=...)` supporting `python_step`, `tool_or_script_step`, `mcp_step`, `bounded_llm_step`, `human_gate`, `foreach` fan-out/fan-in, and `retries`:
     ```python
     from companion.dag_runtime import DAGNode, DAGSpec

     def _step(ctx):
         items = ctx["params"].get("items", [])
         return {"count": len(items)}

     DAG = DAGSpec(
         name="example-dag",
         description="Deterministic Python DAG template.",
         input_schema={"items": "list"},
         nodes=[DAGNode(node_id="count", kind="python_step", output_schema={"count": "int"}, fn=_step)],
         self_test_fn=lambda: _step({"params": {"items": [1, 2]}})["count"] == 2,
     )
     ```
4. **Author Helper Scripts & Domain Validators (`scripts/<helper>.py`)**:
   - Default to 100% Python 3.12 stdlib (`<= 700` lines/module, `<= 100` lines/function, `--self-test` flag).
   - When specialized third-party libraries (CAD, vision, PDF, data) are needed, declare `# /// script` PEP 723 dependencies for isolated `uv run` execution.
5. **Stage, Lint & Promote**:
   - Call `workflow_manage(action="bootstrap_skill", skill="<name>", scope="user", skill_md="...", dags={"my_dag.py": "..."}, scripts={...})` (which runs AST linting, DAG `dry_run_dag` & `self_test_fn`, and script `--self-test` in staging before atomic promotion into `data/skills/<name>/`).
