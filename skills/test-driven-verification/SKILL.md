---
name: test-driven-verification
description: Enforce anti-hallucination verification via unit tests, placeholder scans, ephemeral HTTP probes, and 7-stage System Doctor gates.
required_tools: [run_python, workflow_manage, sdd_manage, system_doctor, read_file, edit_file]
---

# Test-Driven & Anti-Hallucination Verification Runbook

## When to Use This Skill
Use this runbook during `IMPLEMENT` and `VERIFY` phases before claiming any feature, bug fix, or self-evolution change is complete.

## Anti-Hallucination Rules
- **Never Claim Unexecuted Checks Passed**: Only report tests, hygiene, or SDD checks as passing after observing `[PASS]` in the actual tool output of the current turn.
- **Zero Stubs or Placeholders**: Production modules must never contain `[Echo]` stubs, `TODO: placeholder`, `raise NotImplementedError`, or non-stdlib imports (`fastapi`, `uvicorn`, `requests`).

## Step-by-Step Procedure
1. **Write Real Behavioral Unit Tests (`tests/test_*.py`)**:
   - Exercise real functions, SQLite persistence (`tempfile.TemporaryDirectory`), and ephemeral HTTP servers (`port=0`) with deterministic assertions.
   - Keep every test module `<= 550` lines and every test method `<= 100` lines.
2. **Run Targeted Verification & Smoke Probes**:
   - For web changes, execute `workflow_manage(action="http_smoke", module="surfaces.web_server")`.
   - Run `sdd_manage(action="verify")` to catch unused imports, dead definitions, or undocumented modules immediately.
3. **Execute Full 7-Stage Gate (`workflows/verify-feature.json`)**:
   - Run `system_doctor(run_tests=True)` or `workflow_manage(action="run_recipe", recipe="verify-feature")`.
   - If any check reports `[FAIL]`, transition to `RECOVER` (`tier="reasoning"`) and resolve every issue before transitioning to `COMPLETE`.
