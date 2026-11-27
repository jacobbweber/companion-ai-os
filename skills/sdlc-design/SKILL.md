---
name: sdlc-design
description: Stage 4 Design Engineering & Production Hardening Pass. Designs exact native file trees, input/interface contracts, typed data models, noise filters, idempotency matrices, error taxonomies, and telemetry before coding.
required_tools: [read_file, write_file, edit_file, search]
user-invocable: true
---

# Stage 4 Skill: Design Engineering & Hardening Pass (`sdlc-design`)

## Discipline Mindset & Purpose
Wear the **Lead Design Engineer Hat**. This stage is the **Missing Middle** between stories and source code—and our dedicated **Production Hardening Pass**. Leave **zero implementation guesswork**, using native idioms of the stack locked in `architecture.md` (or the existing host app's layers), so Stage 5 coding becomes clean and deterministic.

## Upstream Context Recovery (Fresh Chat Safe)
1. **Read upstream artifacts from disk first:** `product.md` (if greenfield), `architecture.md` / `AGENTS.md`, ADRs, any external API docs in `docs/references/`, and the target backlog (`docs/features.md` + `docs/user-stories.md` OR the specific target `docs/features/F0X/inc-0Y-*.md`).
2. Read the bundled template via `read_file("skills/sdlc-design/references/engineering-design.template.md")`.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default — highest-value learning stage!)*:** Walk layer-by-layer in **3–4 narrated Design Stops** matching the stack's native layers (e.g., *Stop 1: Pure Core Contracts & Rules* -> *Stop 2: External I/O & Persistence* -> *Stop 3: Coordination / Application Flow & Hardening* -> *Stop 4: Entry Surface / UI / Verification Gate*), **pausing after each stop for user confirmation**:
  - At each stop, explicitly narrate **what internal piece we are inventing** (Contract/Type, Entity, Interface/Adapter, Service method), **why** it lives in this layer, and **what real bug or trap it prevents** (calling out any upstream schema/data gap caught live!).
  - After the final stop is confirmed, write the complete, locked Stage 4 Design cleanly to disk (`docs/engineering-design.md` in Choice A, OR replace `(Pending)` in `## Stage 4: Engineering Design` inside the single target `inc-0Y-*.md` in Choice B).
- **In `Express` mode:** Write the complete Stage 4 Design artifact immediately and output the Checkpoint Review Card below.

## Proactive Hardening Checklist (Verify Before Locking)
1. **Native File & Module Tree:** Exact paths matching native language or host-app conventions (never literal abstract names unless native).
2. **Input & Serialization Edge Cases:** Exact parameters/DTOs, defaults, aliases, and type-coercion traps.
3. **Typed Domain & Persistence Contracts:** Exact field names, enum values, DB tables/indexes, and computed-vs-stored field decisions.
4. **Domain Noise Filters & Fail-Closed Rules:** Harmless background metadata ignored vs. blockers; fail-closed on missing upstream state.
5. **Idempotency, Concurrency & Resource Hygiene:** Explicit state/retry matrix + guaranteed cleanup on failure.
6. **Error Taxonomy & Secret-Scrubbed Telemetry:** Native error signals/codes + correlated structured events with automatic redaction.

## Exit Check & Checkpoint Output
Confirm two engineers in separate rooms could code compatible modules and tests from this design alone (and zero `(Pending)`/`TODO`/`TBD` markers remain in the locked Stage 4 design), then output:

```markdown
### Checkpoint 4: Engineering Design & Hardening Pass Complete

**1. Artifact Written / Updated:**
- `[docs/engineering-design.md OR docs/features/F0X/inc-0Y.md]` — [1-sentence summary]

**2. Production Hardening & Assumptions Applied (Review before coding):**
- **[Hardening Rule]:** Engineered [specific rule/contract] to prevent [failure mode].

**3. Open Decisions Needing Your Call (0–2 Max):**
- [Either `None — ready for Stage 5 (Implementation) via /sdlc-next.` OR 1–2 genuine forks with `(Recommended: Option A)`]
```
