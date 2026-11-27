---
name: sdlc-architecture
description: Stage 2 System Architecture & ADR discipline. Selects or aligns the stack, defines clean component/security/reliability boundaries, chooses Choice A Compact vs Choice B Feature Folders, and authors architecture.md and ADRs.
required_tools: [read_file, write_file, edit_file, search]
user-invocable: true
---

# Stage 2 Skill: System Architecture & ADRs (`sdlc-architecture`)

## Discipline Mindset & Purpose
Wear the **System Architect Hat**. Evaluate real constraints, **select or confirm the technology stack** (or align natively with an existing host application's architecture), draw clean boundaries separating *how work enters*, *core domain rules*, and *external dependencies*, define security/reliability physics, and record honest trade-offs (`ADRs`).

## Upstream Context Recovery (Fresh Chat Safe)
1. **Greenfield:** Read `product.md` from disk first. Read both bundled templates via `read_file("skills/sdlc-architecture/references/architecture.template.md")` and `read_file("skills/sdlc-architecture/references/adr.template.md")`.
2. **Existing Application (Brownfield):** Read existing host architecture docs (`AGENTS.md`, `docs/architecture.md`, `docs/project-layout.md`, etc.) and `read_file("skills/sdlc-architecture/references/adr.template.md")`. Do **not** overwrite the host app's `architecture.md`—produce focused Feature ADRs under `docs/adr/ADR-00X-<slug>.md` (and isolate any external platform API facts into `docs/references/` if needed).

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Walk through 3 narrated stops in chat—**pausing after each stop for user feedback before writing files**:
  1. **Stop 1 — Stack, Context & Scale:** Propose the selected stack (or how the new feature plugs into the host app's existing sockets) and recommend Choice A (Compact) vs. Choice B (Feature Folders). Pause for confirmation.
  2. **Stop 2 — Logical Boundaries, Security & Reliability:** Propose the 3 logical boundaries, secret/trust boundary, and failure/idempotency physics. Pause for confirmation.
  3. **Stop 3 — Key Trade-Offs (ADRs):** Present the 4–6 core decisions with honest pros and cons; once confirmed, write `architecture.md` (if greenfield) and the ADRs (`adr.md` or `docs/adr/ADR-00X-<slug>.md`) cleanly to disk.
- **In `Express` mode:** Write the complete artifact(s) immediately and output the Checkpoint Review Card below.

## Execution Rules
1. **Logical Responsibilities, NOT Literal Folder Names:** The three boundaries (*Entry Surface*, *Core Domain*, *External Adapter*) describe **roles**, not folder names—always map them to the native conventions of the selected language or existing host application in the Planned Repository Layout.
2. **Avoid Speculative Abstraction:** Never invent generic plugin engines or speculative interfaces *"just in case"*. Abstract only where multiple real targets or offline test isolation require it.
3. **Honest ADRs & Clean Altitude:** Every ADR must state at least one real **Negative** trade-off. Keep conversational explanations in chat only; defer exact field types and error matrices to Stage 4 (`sdlc-design`).

## Exit Check & Checkpoint Output
Verify all diagrams and ADRs are complete and free of `TODO`/`TBD` placeholders, then output:

```markdown
### Checkpoint 2: System Architecture & ADRs Complete

**1. Artifacts Written:**
- `[architecture.md and/or docs/adr/*]` — [1-sentence summary + selected stack / host alignment]
- **Selected Backlog Mode:** [Choice A Compact OR Choice B Feature Folders]

**2. Standards & Assumptions Applied (Override any item if your context differs):**
- **[Topic]:** Applied [concrete choice] because [brief reason].

**3. Open Decisions Needing Your Call (0–2 Max):**
- [Either `None — ready for Stage 3 (Backlog) via /sdlc-next.` OR 1–2 genuine forks with `(Recommended: Option A)`]
```
