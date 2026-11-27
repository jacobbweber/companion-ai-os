---
name: sdlc-backlog
description: Stage 3 Delivery & Backlog Planning discipline. Decomposes approved scope and architecture vertically into Features, sequential Increments, verifiable Given/When/Then User Stories, and synced Project Board cards.
required_tools: [read_file, write_file, edit_file, search, agile_manage]
user-invocable: true
---

# Stage 3 Skill: Delivery & Backlog Planning (`sdlc-backlog`)

## Discipline Mindset & Purpose
Wear the **Technical Delivery Lead Hat**. Carve approved product/feature scope and architectural rules into small, vertically sliced, end-to-end verifiable capabilities with objective **Given / When / Then** pass/fail rules—keeping low-level schemas deferred to Stage 4 (`sdlc-design`).

## Upstream Context Recovery (Fresh Chat Safe)
1. **Read upstream docs from disk first:** `product.md` (if greenfield), `architecture.md` / `AGENTS.md`, and ADRs (`adr.md` or `docs/adr/*`).
2. Read the bundled template via `read_file("skills/sdlc-backlog/references/features-and-stories.template.md")`.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Walk through 2–3 narrated stops in chat—**pausing after each stop before writing files**:
  1. **Stop 1 — Capability Map & Delivery Order:** Propose the Features (Choice A) OR the Feature Overview + ordered **Increments** (`inc-01..inc-0N`, placing safe read-only/foundational increments ahead of high-risk writes so the Increment sequence *is* the roadmap!). Explain *why* this slicing minimizes risk and avoids bloated roadmaps. Pause for confirmation.
  2. **Stop 2 — Verifiable Stories & Safety Rules:** Walk through the core `Given / When / Then` happy-path and rejection criteria (catching missing edge behaviors early); once confirmed, write the clean backlog files to disk and sync the Project Board.
- **In `Express` mode:** Write the complete backlog artifacts immediately, sync the Project Board, and output the Checkpoint Review Card below.

## Execution Rules
1. **Slice Vertically by Observable Capability:** Never slice horizontally by technical layers (*"Create DB table first"*). Every story or increment must deliver observable behavior tied to a persona.
2. **Match Documentation Scale Cleanly:**
   - **Choice A (Compact — <= 6 features):** Write `docs/features.md` AND `docs/user-stories.md`.
   - **Choice B (Feature Folders — multi-feature apps or features inside existing apps):** Write `docs/features/F0X-<slug>/00-feature-overview.md` AND one Stage 3 story file per Increment (`inc-01-<slug>.md`, `inc-02-<slug>.md`, ...) containing Increment Goal, Rules, User Stories + `Given / When / Then` criteria, and a `## Stage 4: Engineering Design (Pending)` section ready for `sdlc-design` to populate one Increment at a time.
3. **Companion Project Board Sync (`agile_manage`):**
   - After writing the backlog artifacts to disk, mirror the approved Feature Milestones (`agile_manage(action="create_milestone", title="...")`) and sequential Increment / Story cards (`agile_manage(action="create_item", title="...", epic_id="...", owner="collaborative", status="ready", description="...", acceptance_criteria="...", checklist=[...], depends_on=[...])`) onto the focused Project Board so Kanban and Roadmap views track live SDLC progress.

## Exit Check & Checkpoint Output
Confirm every story has both happy-path and fail-safe acceptance criteria and maps cleanly to upstream scope/ADRs, then output:

```markdown
### Checkpoint 3: Feature & Story Backlog Complete

**1. Artifacts Written & Board Synced:**
- `[docs/features.md & docs/user-stories.md OR docs/features/F0X/00-feature-overview.md + inc-01..0N]` — [Counts & recommended first Increment]
- Synced Milestones & Increment/Story cards onto the focused Project Board.

**2. Standards & Assumptions Applied (Override any item if your context differs):**
- **[Topic]:** Applied [concrete choice] because [brief reason].

**3. Open Decisions Needing Your Call (0–2 Max):**
- [Either `None — ready for Stage 4 (Design Hardening Pass) via /sdlc-next.` OR 1–2 genuine forks with `(Recommended: Option A)`]
```
