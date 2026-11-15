---
name: ooda-troubleshooting
description: Diagnose bugs, test failures, and system issues using the OODA Loop (Observe -> Orient -> Decide -> Act) across fast and reasoning tiers.
required_tools: [system_doctor, telemetry_manage, read_file, search, workflow_manage, checkpoint_manage, edit_file]
---

# OODA Loop Troubleshooting & Root-Cause Diagnosis Runbook

## When to Use This Skill
Use this runbook whenever:
- A unit test, `system_doctor` gate, or tool execution fails unexpectedly
- `LoopGuard` flags repeated tool calls or consecutive tool errors (`>= 2` errors)
- A workflow enters the `RECOVER` state or a hobby cycle requires self-repair
- The user reports a bug, regression, timeout, or unexpected behavior

## The OODA Loop Architecture (`fast` + `reasoning`)
Never guess or blindly retry a failing edit or command. Execute the four stages of Boyd's **OODA Loop** in strict order, pairing deterministic evidence collection (`tier="fast"`) with root-cause orientation (`tier="reasoning"`):
- **Automated Recipe**: Run `skills/ooda-troubleshooting/workflows/ooda-diagnose.json` via `workflow_manage(action="run_recipe", recipe="ooda-diagnose", params={"issue": "..."})`.

## Step-by-Step Procedure
1. **OBSERVE (`tier="fast"` — Gather Raw Evidence, Zero Guessing)**:
   - Run `system_doctor(run_tests=True)` or `telemetry_manage(action="logs", level="WARN")` to capture exact failure output.
   - Use `read_file` or `search` to inspect the exact lines around the failing symbol or test. Do **not** edit any files during `OBSERVE`.
2. **ORIENT (`tier="reasoning"` — Form & Filter Root-Cause Hypotheses)**:
   - If the failure is non-trivial or a first fix attempt failed, delegate to the `reasoning` tier:
     `workflow_manage(action="subagent", tier="reasoning", skill="ooda-troubleshooting", goal="Orient & Decide root cause for: <observed evidence>")`.
   - List 2–3 candidate root causes, cross-check each against the observed evidence, and eliminate hypotheses that contradict the facts.
3. **DECIDE (`tier="reasoning"` — Select the Minimal Reversible Fix)**:
   - Choose the smallest, cleanest code or config change that resolves the proven root cause without violating SDD limits (`max_module_lines = 700`, `max_function_lines = 100`, 0 unused imports).
   - Define the exact verification command that will prove the fix worked.
4. **ACT & VERIFY (`tier="fast"` — Checkpoint, Apply, Verify, or Roll Back)**:
   - Create a safety checkpoint via `checkpoint_manage(action="create", label="pre_ooda_fix")`.
   - Apply the fix with `edit_file` and immediately run `system_doctor(run_tests=True)`.
   - If verification passes, record the root-cause lesson in memory. If it fails after 2 OODA cycles, call `checkpoint_manage(action="rollback")` to restore a known-good state.
