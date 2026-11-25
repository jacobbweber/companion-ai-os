# Anti-Sprawl & System Integrity Rule

Whenever brainstorming, planning, reviewing, or implementing features, database tables, workflows, API contracts, or UI changes in this repository:

1. Read and apply the builder [`anti-sprawl-gate`](../skills/anti-sprawl-gate/SKILL.md) skill before writing code.
2. Check every proposal against the **4 Gates**:
   - **Compression**: Generalize existing orthogonal primitives (`Single Primary ReAct Loop`, `ProjectGraph`, `Routine` + `TriggerPolicy`, declarative `*.workflow.json`, or `data/domains/<pack>/` capability packs) instead of adding parallel tables, modes, or Ring-0 feature islands.
   - **Single Source of Truth**: Guarantee `F5` browser refresh and Python restart invariance, with deterministic Python boundaries around probabilistic LLM behavior.
   - **5-Hop Seam Trace**: Verify `UI Control -> HTTP/CoreAction JSON keys -> SQLite/Disk -> LLM Context HUD -> UI Re-render`.
   - **Human Mental Model**: Design from the human job backward (5% calm daily view, 95% power knobs tucked in Settings/Inspectors), never raw DB dumps.
3. **Push Back Constructively**: If a prompt or plan introduces idea sprawl, duplicate state, or UI clutter, speak up candidly and propose the compressed alternative before implementing.
