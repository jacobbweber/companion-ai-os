---
name: release-readiness
description: Verify GitHub release readiness by checking .gitignore privacy shield, running sdd_manage verify, and running system_doctor.
required_tools: [read_file, run_python, sdd_manage, system_doctor]
---

# Release‑Readiness Skill

## When to Use This Skill
Use this runbook when you need to confirm that a Companion release is ready to be published on GitHub. It ensures that:
- The `.gitignore` adequately shields private files (privacy shield).
- All canonical SDD specs are aligned and dead‑code free (sdd_manage verify).
- The overall system passes its health diagnostics (system_doctor).

## Step‑by‑Step Procedure
1. **Inspect `.gitignore`**  
   - Open `/.gitignore` (or the repo‑root `.gitignore`) with `read_file`.  
   - Verify that no sensitive keys, tokens, or project‑specific paths are listed.  
   - If any disallowed entries are found, add them to a “privacy shield” list and update `.gitignore` accordingly.

2. **Run SDD Verification**  
   - Invoke `sdd_manage(action="verify")` to check that all four canonical specs (PRODUCT.md, ARCHITECTURE.md, CONTRACTS.md, DECISIONS.md) are present, consistent, and free of dead code.

3. **Run System Doctor**  
   - Invoke `system_doctor(run_tests=True)` to execute the full 7‑stage health diagnostics (config, AST syntax, architecture, SDD sync, dead‑code hygiene, SQLite, unit tests).  
   - Capture the output; ensure every stage reports “pass” or the identified issues are resolved before proceeding.

4. **Synthesize Result**  
   - If both the `.gitignore` privacy check and the two automated verifications pass, mark the release as **ready**.  
   - If any check fails, list the specific failures and recommend remediation steps.

5. **Optional: Automated Recipe or Commit & Tag**  
   - You can execute steps 1–3 deterministically in a single zero-token Python DAG using `dags/release_check.py` (`workflow_manage(action="run_dag", dag="release-check")`).  
   - When all checks pass, you may safely `git add .gitignore` and `git commit -m "Release readiness checks"` followed by `git tag -a vX.Y.Z -m "Release X.Y.Z readiness verified"`.
