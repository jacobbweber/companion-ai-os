---
name: anti-sprawl-gate
description: 4-question architectural pre-flight gate and proactive pushback checklist to prevent idea sprawl, duplicate subsystems/tables, split-brain seams, and UI clutter before writing code.
required_tools: [read_file, search, sdd_manage, system_doctor]
---

# Anti-Sprawl & System Integrity Gate

Use this skill as an architectural pre-flight filter before designing, planning, or implementing any new feature, database schema, background workflow, API endpoint, or UI surface in Companion. Its purpose is **conceptual integrity**: catching idea sprawl and implementation-model leaks *before* code is written, and giving the AI explicit permission—and obligation—to push back constructively when a request can be solved more cleanly with existing primitives.

## When to Use This Skill
- Before turning a brainstormed idea, feature request, or refactor into new Python modules, SQLite tables, REST endpoints, or React views.
- Whenever a user prompt proposes a new "mode", "manager", "studio", "subsystem", or top-level navigation item.
- During code review or UX dogfooding to verify that state, API contracts, LLM prompt context, and human mental models actually connect end-to-end.
- Whenever starting a new session on an architectural, product, or full-stack task in this repository.

## Proactive Pushback Protocol (Mandatory for AI Collaborators)
Do **not** blindly implement sprawling requests just to be agreeable. Before creating a new module, SQLite table, CoreAction, or top-level screen:
1. Run the proposal through the **4 Gates** below against the live codebase (`companion/`, `surfaces/`, `frontend/src/`, `specs/`).
2. If any gate fails—for example, a proposed feature duplicates `ProjectGraph`, `Routine` + `TriggerPolicy`, `DAGSpec`, or a `skills/<pack>/` capability pack—**pause and tell the user candidly**:
   - Name which gate triggered and why (in plain, concrete terms).
   - Propose the **compressed alternative** that reuses or generalizes an existing primitive.
   - Confirm alignment before writing code (unless the compressed implementation already fulfills 100% of the user's intent cleanly).

## Step-by-Step Procedure: The 4 Pre-Flight Gates

### Gate 1: Conceptual Compression (Orthogonal Primitives over Feature Islands)
> **Question**: *"Can I express this by generalizing an existing noun, table, or policy instead of inventing a new subsystem?"*

Before adding any new table, manager class, or Ring-0 module, check whether the idea is actually an instance of Companion's core orthogonal primitives:
- **Goals, Epics, Sprints, Courses, or Milestones** -> Express as nodes/cards inside `ProjectGraph` (`companion/project_graph.py`) rather than creating parallel tracker tables.
- **Recurring Habits, Scheduled Jobs, Idle Exploration, or Event Watchers** -> Express as a `Routine` + `TriggerPolicy` (`cron`, `interval`, `idle`, `webhook`, `file_watch`) rather than building separate hobby/daemon engines.
- **Multi-Step Deterministic Workflows** -> Express as a code-controlled `DAGSpec` inside `skills/<pack>/dags/<workflow>.py` rather than hardcoding domain pipelines into Ring-0 (`companion/`).
- **Domain Knowledge, Playbooks, or 3rd-Party Integrations** -> Package as a 3-Tier Capability Pack in `skills/<pack>/` (with PEP 723 isolated scripts if external packages are needed) so Ring-0 stays `100%` Python stdlib.

**Pass Condition**: Zero duplicate nouns or parallel state machines. Every new concept either composes existing primitives or earns its existence as a genuinely orthogonal primitive documented in `specs/`.

---

### Gate 2: Single Source of Truth & `F5` / Restart Invariance
> **Question**: *"Which single layer owns this state, and if I press `F5` in the browser or restart Python right now, does everything look identical?"*

Guard against split-brain state across React local state, module-level Python globals, SQLite, and Markdown files:
- **One Canonical Owner**: Identify the exact single source of truth (e.g., SQLite `conversations.db` via `MemoryStore`, `skills/knowledge-wiki/` Markdown files, or `runtime_settings`). Derived caches must never silently diverge.
- **No Destructive Screen Wipes Disguised as Navigation**: Clearing or resetting a view in React without explicit server-side semantics creates an illusion that disappears on `F5`. Prefer continuous, durable state (such as Forever Chat cursor pagination and non-destructive context dividers).
- **Deterministic Boundaries Around Probabilistic AI**: Python code owns state transitions, permissions, routing invariants, retries, fences, and schema validation; the LLM owns bounded synthesis, classification, and drafting inside those guardrails.

**Pass Condition**: Refreshing the browser (`F5`) and restarting the Python server both preserve the exact same user-visible state with zero orphaned or phantom UI state.

---

### Gate 3: Full-Stack 5-Hop Seam Trace
> **Question**: *"If I change this in the UI, does the AI actually know—and if the AI changes it via a tool, does the UI actually reflect it?"*

Never leave a control or schema "90% wired." Trace every interactive capability across all 5 hops before declaring it done:
1. **UI Control** (`frontend/src/`): Button, selector, toggle, or inspector form.
2. **HTTP / CoreAction Contract** (`surfaces/web_server.py`, `surfaces/api_extensions.py`, `companion/core_actions.py`): Verify **exact JSON key names** match between Python return dicts and TypeScript response readers (e.g., `res.hits` vs `res.results`, `res.prompts` vs `res.items`).
3. **Canonical Persistence** (`companion/memory_store.py` / `skills/`): Durable write with deterministic validation.
4. **LLM System Prompt HUD / Tool Schema** (`companion/agent.py`, `companion/tool_schemas.py`): If a UI toggle sets focus or posture (e.g., active project focus), inject it into the live System Prompt HUD so the Companion actually honors it on the very next turn; support batch arrays (`items`) when the LLM commonly creates multiple records at once.
5. **UI Re-render & Feedback**: Immediate visual confirmation on both Desktop and Mobile viewports without requiring a manual reload.

**Pass Condition**: Every hop is verified in code (and live where applicable) with zero mismatched payload keys or write-only dead ends.

---

### Gate 4: Human Mental Model & Progressive Disclosure
> **Question**: *"Does this screen match how a human thinks about the job in the real world, with the 5% daily path calm and obvious and the 95% power knobs tucked one click deeper?"*

Design from the **human task backward**, never from the **database table forward**:
- **Real-World Metaphors over DB Row Dumps**: Show a Calendar as spatial `Day` / `Week` time-slot grids and `Month` day cells with `<` / `Today` / `>` navigation—not a raw `SELECT *` list of timestamps. Show Chat as one calm, continuous timeline—not a session-ID manager.
- **5% / 95% Progressive Disclosure**: Keep daily-driver surfaces calm and centered. Tuck administrative configuration, diagnostics, raw run outputs, and telemetry one click deeper inside `Settings` (`Advanced` expanders) or the closed-by-default Right Auxiliary Inspector Pane (bottom sheet on mobile).
- **Ring-0 Hygiene**: Keep Python modules `<= 700` lines, functions `<= 100` lines, React `.tsx` components modular (`<= 550` lines), `specs/` capped at the 4 canonical living documents, and run `python harness.py --doctor` (`7/7` PASS) after structural changes.

**Pass Condition**: A human opening the screen on phone or desktop immediately understands *where they are*, *what changed*, and *what primary action to take* without reading internal system jargon.
