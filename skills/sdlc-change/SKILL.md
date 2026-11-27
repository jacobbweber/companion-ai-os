---
name: sdlc-change
description: Day-2 Change Management & Entry-Hat Ripple Triage discipline. Identifies the highest invalidated SDLC stage for new requests or defects and ripples surgical updates downward through docs, code, tests, and runbooks.
required_tools: [read_file, write_file, edit_file, search, git_manage, agile_manage, run_command]
user-invocable: true
---

# Day-2 Skill: Change Management & Ripple Triage (`sdlc-change`)

## Discipline Mindset & Purpose
Prevent both "cowboy code edits" (causing docs to rot into lies) and unnecessary full-repo rewrites. Apply the **Entry-Hat Ripple Rule**: locate the **highest SDLC stage whose locked decisions are invalidated**, leave every artifact *above* it untouched, and ripple surgical updates downward through docs, code, tests, and runbooks.

## Upstream Context Recovery (Fresh Chat Safe)
Read existing workspace artifacts (`product.md` if present, `architecture.md` / `AGENTS.md`, ADRs, backlog, and engineering designs) from disk first to understand locked contracts.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Present the **Entry-Hat Impact Triage** first in chat—explaining in plain English *which* stage the change enters at, *which* upstream docs stay untouched, *what backward-compatibility risk exists*, and proposing the exact surgical ripple plan—then **pause for go-ahead** before editing files.
- **In `Express` mode:** For bounded, clear changes, isolate on a `feat/*` or `fix/*` branch via `git_manage`, apply the surgical doc + code + regression-test + runbook updates directly, verify 100% green tests, and report the summary card (still pausing if a major product/architectural fork requires a human call).

## Entry-Hat Triage Rules
- Alters business scope, personas, or KPIs -> **Enter at Stage 1** (`product.md` or Feature overview)
- Alters stack, boundaries, auth/secrets, or core trade-offs -> **Enter at Stage 2** (`architecture.md` + mark old ADR `Superseded by ADR-00X` and append new ADR; **never** delete old ADRs!)
- Adds/alters observable capability within existing architecture -> **Enter at Stage 3** (`features` / `stories` / new `Increment` + Project Board card via `agile_manage`)
- Alters thresholds, schemas, parameters, or error categories only -> **Enter at Stage 4** (`engineering-design.md` or target `inc-0Y.md`)
- Pure defect violating existing spec -> **Enter at Stage 5 & 6** (add failing regression test proving the bug, fix code on a `fix/*` branch, confirm 100% green)

## Checkpoint Output

```markdown
### Day-2 Change Ripple Complete

**1. Entry Stage Identified:** Stage [X] ([Discipline]) — Upstream artifacts above Stage [X] left untouched.
**2. Artifacts & Code Updated:**
- `[file]` — [Surgical delta applied]
**3. Regression & New Tests:** `[X Passed, 0 Failed]`
```
