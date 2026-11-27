---
name: sdlc-verify
description: Stage 6 Quality & Verification Engineering discipline. Authors docs/testing-strategy.md, executes adversarial unit/integration tests, dev_preview + browser-dogfood UI checks, pre-commit-verify / sdlc-audit workflows, and clean local git commits.
required_tools: [read_file, write_file, edit_file, search, git_manage, dev_preview, workflow_manage, agile_manage, run_python, run_shell]
user-invocable: true
---

# Stage 6 Skill: Quality & Verification Engineering (`sdlc-verify`)

## Discipline Mindset & Purpose
Wear the **Adversarial QA & Verification Engineer Hat**. Assume every safety gate is broken until an automated test deliberately feeds bad, missing, or duplicate state into it and proves it holds. Your deliverable is **executable proof—100% passing offline unit and integration tests** (plus live `dev_preview` + `browser-dogfood` checks for Web UIs).

## Upstream Context Recovery (Fresh Chat Safe)
1. **Read the User Stories (`docs/user-stories.md` or `inc-0Y-*.md`), Engineering Design, and implemented source files from disk first.**
2. Read the bundled template via `read_file("skills/sdlc-verify/references/testing-strategy.template.md")`.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Write and execute the full test suite until 100% pass, run applicable UI preview and SDLC audit checks, commit cleanly onto the feature branch, and present a plain-English **Proof Summary** mapping each real-world risk/story to the test that proved it—explicitly noting any live-environment check that offline tests cannot prove on their own.
- **In `Express` mode:** Write `docs/testing-strategy.md` and tests, execute until 100% pass, commit cleanly onto the feature branch, and output the Checkpoint Card directly.

## Execution Procedure
1. **Author Verification Strategy:** Populate `docs/testing-strategy.md` (or check off the Verification section in Choice B `inc-0Y-*.md`) mapping every story and invariant to concrete tests.
2. **Implement Isolated Unit Tests:** Fake/mock external adapter boundaries to prove in seconds:
   - Input normalization, aliases, and invalid-input rejection
   - Scope filtering (excluding out-of-scope types)
   - Dedicated negative test for **each** safety/readiness gate
   - Fail-closed rejection when upstream data/history is empty or null
   - Idempotent skips over already-completed or half-completed state
   - Raw telemetry/error inspection proving secrets/tokens are `[REDACTED]`
   - Guaranteed cleanup execution when a mid-run fault is injected
3. **Implement Integration Tests:** Exercise the real entry boundary against deterministic local fixtures/fakes—verifying preview mode, live convergence, error signaling, and correlated telemetry.
4. **Execute Until 100% Green:** Run the native test runner (`run_python` / `run_shell`) and fix defects until `0` failures remain (using `skills/ooda-troubleshooting/SKILL.md` if an error repeats).
5. **Live Preview & Browser Dogfooding (Web / HTTP UI Changes):**
   - Start the application on a free localhost port via `dev_preview(action="start", command="...", ready_path="/api/health")`.
   - Run `workflow_manage(action="run_script", skill="browser-dogfood", script="browser_dogfood.py", args=["--url", "http://127.0.0.1:<port>"])` to verify `console_errors: []`, `page_errors: []`, and Desktop/Mobile rendering, then stop via `dev_preview(action="stop")`.
6. **Deterministic Audit, Increment Sign-Off & Clean Feature-Branch Commit:**
   - In Choice B, mark the Increment `STATUS: SHIPPED` in `inc-0Y-*.md` and `00-feature-overview.md`. Sync any matching Project Board card to `done` via `agile_manage(action="update_item", item_id="...", status="done")`.
   - Run `workflow_manage(action="run_dag", dag_name="sdlc-audit")` and/or `workflow_manage(action="run_dag", dag_name="pre-commit-verify")` to verify placeholder hygiene, secret/DB staging safety, and green tests.
   - Commit verified files onto the active `feat/*` or `fix/*` branch via `git_manage(action="commit", files=[...], message="feat: ...")`. Stop at the local commit when `delivery_policy == "local_only"`; invoke `github-workflow` only when remote delivery is authorized.

## Exit Check & Checkpoint Output
Never finish Stage 6 with failing or unexecuted tests. Output:

```markdown
### Checkpoint 6: Automated Verification Complete

**1. Artifacts, Test Suites & Commit:**
- `docs/testing-strategy.md` (or Increment verification sign-off)
- `[test file paths]` — `[X Passed, 0 Failed]`
- Committed cleanly on branch `[feat/slug]`

**2. Boundaries Proven:**
- Happy paths, safety gate rejections, fail-closed missing data, idempotent recovery, secret redaction, and crash cleanup verified.

**3. Next Step:**
- Ready for Stage 7 (Operations Runbook) via `/sdlc-next` (or next Increment in Choice B).
```
