---
name: skill-creator
description: Author or update a modular `data/skills/<skill-name>/` skill or `data/domains/<pack>/` Domain Capability Pack (`SKILL.md` / `CONTEXT.md`, `scripts/`, `workflows/*.workflow.json`, and isolated `<pack>.db` SQLite).
required_tools: [list_dir, read_file, write_file, edit_file, run_command, ops_manage]
---

# Skill Creator & Domain Capability Pack Guide

## When to Use This Skill
Use this runbook whenever:
- The user asks you to create a new reusable skill (`data/skills/<name>/`) or self-contained Domain Capability Pack (`data/domains/<pack>/` for finances, recipes, inventory, fitness, homelab, etc.).
- You notice a repeatable multi-step pipeline that should be captured as a declarative JSON workflow (`workflows/<name>.workflow.json`) paired with deterministic CLI scripts (`scripts/<helper>.py`).

## Two Clean Extension Patterns

### 1. Procedural Skill Pack (`data/skills/<skill-name>/`)
```text
data/skills/<skill-name>/
├── SKILL.md                          # Tier 1 frontmatter + Tier 2 step-by-step runbook
├── references/                       # Optional Markdown reference docs (*.md)
├── scripts/                          # Deterministic Python CLI helpers (--self-test)
└── workflows/                        # Declarative JSON workflows (*.workflow.json)
```

### 2. Self-Contained Domain Capability Pack (`data/domains/<pack>/`)
```text
data/domains/<pack>/
├── CONTEXT.md                        # Front-door schema & convention map (~50-150 lines)
├── <pack>.db                         # Isolated WAL SQLite database for exact rows/math
├── 01_inbox/                         # Raw drops, CSVs, receipts
├── 02_records/                       # Curated domain notes
├── 03_reports/                       # Generated summaries & exports
├── scripts/                          # Deterministic SQLite/CSV CLI scripts (--self-test)
└── workflows/                        # Declarative JSON workflows (*.workflow.json)
```

## How to Author Declarative JSON Workflows (`workflows/<name>.workflow.json`)
Never write arbitrary Python-lambda `DAGSpec` files. Always author pure declarative JSON (`*.workflow.json`) using the typed Node Catalog (`run_script`, `call_tool`, `llm_step`, `mcp_step`, `human_gate`, plus optional `foreach` and `edges`):
```json
{
  "name": "example-pipeline",
  "description": "Deterministic script + schema-validated LLM workflow.",
  "input_schema": {"csv_path": "str"},
  "nodes": [
    {
      "id": "ingest_rows",
      "type": "run_script",
      "config": {"script": "scripts/ingest.py", "cli_args": ["--csv", "{{params.csv_path}}"]},
      "output_schema": {"ok": "bool", "row_count": "int"}
    },
    {
      "id": "summarize",
      "type": "llm_step",
      "depends_on": ["ingest_rows"],
      "config": {"tier": "fast", "prompt_template": "Summarize {{ingest_rows.row_count}} ingested rows."},
      "output_schema": {"headline": "str"}
    }
  ]
}
```

## Stage, Self-Test & Promote
- For `data/skills/<name>/`: call `ops_manage(domain="workflows", action="bootstrap_skill", target="<name>", params={"scope": "user", "skill_md": "...", "scripts": {...}, "workflows": {"example.workflow.json": {...}}})`.
- For `data/domains/<pack>/`: create `CONTEXT.md`, initialize `<pack>.db` with `PRAGMA journal_mode=WAL`, and place deterministic scripts in `scripts/` and workflows in `workflows/*.workflow.json`.
