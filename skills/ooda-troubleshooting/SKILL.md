---
name: ooda-troubleshooting
description: Diagnose bugs, test failures, and system issues using the OODA Loop (Observe -> Orient -> Decide -> Act) without blind trial-and-error.
required_tools: [read_file, search, run_command, edit_file]
---

# OODA Loop Troubleshooting & Root-Cause Diagnosis

## When to Use This Skill
Use this runbook whenever:
- A test suite, script, service, or remote endpoint fails unexpectedly
- Repeated retries fail or error logs show non-obvious behavior
- The user reports a bug, regression, timeout, or misconfiguration

## Step-by-Step Procedure
1. **OBSERVE (Gather Raw Evidence, Zero Guessing)**:
   - Run the failing command/test (`run_command`) or inspect logs and source lines (`read_file`, `search`) to capture the exact error message and state. Do **not** edit files during `OBSERVE`.
2. **ORIENT (Form & Filter Hypotheses)**:
   - List 2–3 plausible root causes, cross-check each against the observed evidence, and rule out hypotheses that contradict the facts.
3. **DECIDE (Choose the Minimal Reversible Fix)**:
   - Pick the smallest, cleanest code or configuration change that resolves the proven root cause, and identify the exact verification command that will prove it worked.
4. **ACT & VERIFY**:
   - Apply the fix (`edit_file` / `write_file`) and re-run the verification check (`run_command`) to confirm end-to-end resolution.
