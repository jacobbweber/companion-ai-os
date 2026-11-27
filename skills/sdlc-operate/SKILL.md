---
name: sdlc-operate
description: Stage 7 Operations & Reliability Engineering discipline. Authors docs/operations-runbook.md and updates README.md with Day-2 onboarding, safe preview-before-live SOPs, copy-paste observability queries, and recovery playbooks.
required_tools: [read_file, write_file, edit_file, search]
user-invocable: true
---

# Stage 7 Skill: Operations & Reliability Engineering (`sdlc-operate`)

## Discipline Mindset & Purpose
Wear the **SRE & Operations Engineer Hat**. Write strictly for the tired teammate on call at 2:00 AM six months from now who has **never read the source code**. Keep architectural essays and unit-test theory out—deliver 100% actionable setup prerequisites, safe preview-before-live operating steps, copy-paste observability queries, and failure recovery tables.

## Upstream Context Recovery (Fresh Chat Safe)
1. **Read `architecture.md` / `AGENTS.md`, locked Engineering Design(s) (error signals & telemetry schema), and entrypoint source files from disk first.**
2. Read the bundled template via `read_file("skills/sdlc-operate/references/operations-runbook.template.md")`.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Draft `docs/operations-runbook.md` and update `README.md`, then walk the user briefly through the **Pre-Flight Lab Checklist** (how to move safely from offline tests to first real non-prod run) and key 2:00 AM recovery rows.
- **In `Express` mode:** Write `docs/operations-runbook.md` and `README.md`, verify links, and output the Checkpoint Card directly.

## Execution Procedure
1. **Environment Onboarding Checklist:** Document runtime prerequisites, non-interactive host settings that prevent background hangs, least-privilege permissions, and required config/secret variables.
2. **Safe Standard Operating Procedure (SOP):** Pre-conditions -> Non-destructive Preview/Validation -> Live Execution -> Correlated Telemetry Verification before irreversible downstream actions.
3. **Copy-Paste Observability Queries:** Ready-to-run queries for single-run tracing, compliance auditing, and failure alerting.
4. **Troubleshooting & Recovery Playbook:** Map every failure category and skip reason from Stage 4 to root cause and resolution—explicitly noting when idempotent re-execution is 100% safe.
5. **Root `README.md` Index:** Update `README.md` with quickstart commands, test commands, and clickable links to all SDLC artifacts.

## Exit Check & Checkpoint Output
Verify all markdown links in `README.md` and `docs/` resolve cleanly and zero `TODO`/`TBD` placeholders remain, then output:

```markdown
### Checkpoint 7: Day-2 Operations Runbook & Release Docs Complete

**1. Artifacts Written:**
- `docs/operations-runbook.md` — [Summary]
- `README.md` — [Repository index & quickstart]

**2. Lifecycle Status:**
- All 7 SDLC stages complete and verified. Use `/sdlc-change` in future sessions for Day-2 enhancements or fixes.
```
