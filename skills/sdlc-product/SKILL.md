---
name: sdlc-product
description: Stage 1 Product Management discipline for new greenfield repositories. Shapes raw intent into personas, In-Scope vs Out-of-Scope walls, happy-path workflow, KPIs, and product.md via Guided Co-Design or Express mode.
required_tools: [read_file, write_file, edit_file, search]
user-invocable: true
---

# Stage 1 Skill: Product Management (`sdlc-product`)

## Discipline Mindset & Purpose
Wear the **Product Manager Hat**. Transform raw human intent into crisp agreement on **why** this exists, **who** interacts with or approves it, **where scope stops**, **what the happy-path workflow looks like**, **how success is measured**, and **what 1–2 major domain mistakes must be prevented**—before architectural or coding decisions begin.

*(Note: If adding a feature to an **existing application** that already has app-level docs like `AGENTS.md` or `docs/architecture.md`, do not create a duplicate `product.md`—hand off to `sdlc-architecture` / `sdlc-backlog` to define the feature directly inside the host app.)*

## Upstream Context Recovery (Fresh Chat Safe)
1. Check whether `product.md` or existing notes already exist in the focused repository (`workspace.project_root`) so you build on known context.
2. Read the bundled artifact template via `read_file("skills/sdlc-product/references/product.template.md")`.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Walk through 3 quick narrated stops in chat—proposing concrete drafts from the user's knowns and explaining *why* each section matters—**pausing after each stop for the user's reply before writing `product.md`**:
  1. **Stop 1 — Problem, Vision & Stakeholders:** Propose the concrete problem statement and actors (including hidden approver/security/audit personas). Pause for confirmation or tweaks.
  2. **Stop 2 — Scope Boundaries & Happy Path:** Propose explicit **In-Scope vs. Out-of-Scope** walls and the 4-step user journey. Pause for confirmation or tweaks.
  3. **Stop 3 — KPIs & Top Domain Risks:** Propose measurable Baseline-vs-Target metrics and the 1–2 big domain traps; once confirmed, write `product.md` cleanly to disk (`write_file`).
- **In `Express` mode:** Write the complete `product.md` immediately and output the Checkpoint Review Card below.

## Execution Rules
1. **Stay Grounded & Concrete:** Anchor strictly on the real problem and domain—never generalize into an abstract framework.
2. **Keep Altitude Clean & Teaching in Chat Only:** Populate every section and table of `skills/sdlc-product/references/product.template.md` in 85% simplified technical prose—zero code snippets, function names, error integers, raw schemas, or conversational teaching bloat inside `product.md`. Save micro-edge-cases for Stage 4 (`sdlc-design`).

## Exit Check & Checkpoint Output
Verify `product.md` has zero `TODO`/`TBD` placeholders or implementation clutter, then output:

```markdown
### Checkpoint 1: Product Specification (`product.md`) Complete

**1. Artifact Written:**
- `product.md` — [1-sentence summary]

**2. Standards & Assumptions Applied (Override any item if your context differs):**
- **[Topic]:** Applied [concrete choice] because [brief reason].

**3. Open Decisions Needing Your Call (0–2 Max):**
- [Either `None — ready for Stage 2 (Architecture) via /sdlc-next.` OR 1–2 genuine forks with `(Recommended: Option A)`]
```
