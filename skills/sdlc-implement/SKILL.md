---
name: sdlc-implement
description: Stage 5 Software Implementation discipline. Isolates work on a git feature branch or worktree and implements clean, modular, strictly typed source code matching locked Stage 4 Engineering Design contracts verbatim.
required_tools: [read_file, write_file, edit_file, search, git_manage, agile_manage, run_command]
user-invocable: true
---

# Stage 5 Skill: Software Implementation (`sdlc-implement`)

## Discipline Mindset & Purpose
Wear the **Software Engineer Hat**. Implement clean, modular, strictly typed source code inside the focused external repository (`workspace.project_root`) that fulfills the locked Stage 4 Engineering Design contracts **verbatim**—without inventing unapproved scope, violating architectural boundaries, or skipping defensive cleanup.

## Upstream Context Recovery (Fresh Chat Safe)
1. **Read `architecture.md` / `AGENTS.md`, ADRs, and the locked Engineering Design (`docs/engineering-design.md` OR target `docs/features/F0X/inc-0Y-*.md`) from disk first.** (If Stage 4 Design is still `Pending`, switch to `sdlc-design` first.)
2. Check `<repo_sdlc_context>` in `<turn_context>` for repo rules, active branch, and `delivery_policy` (`local_only` default | `ask_me` | `remote_branch_pr`). Never push or open a PR when `delivery_policy == "local_only"`.
3. Inspect existing source files in the workspace so edits integrate cleanly using native host conventions.

## Collaboration Flow
- **In `Guided Co-Design` mode *(default)*:** Isolate on a feature branch, implement cleanly following the Stage 4 file plan, run syntax/build checks, and give a concise **Guided Code Tour** in chat explaining how each created/modified file maps back to its layer in the Stage 4 design (highlighting the highest-risk logic lines in plain English rather than forcing micro-pauses on routine syntax).
- **In `Express` mode:** Isolate on a feature branch, implement all files, run syntax/build checks, and output the Checkpoint Card directly.

## Execution Rules (Strict Craftsmanship & Companion Git Hygiene)
1. **Isolate on a Feature Branch (`git_manage`):**
   - Check `git_manage(action="status")`. Switch in place to a descriptive branch (`feat/<slug>` or `fix/<slug>`) via `git_manage(action="branch", branch="feat/...")` before editing source files. Reserve `git_manage(action="worktree_add", ...)` for multi-worker parallel slices, and never commit directly onto `main` or `master`.
   - If a matching Project Board card exists, mark it `in_progress` via `agile_manage(action="update_item", card_id="...", status="in_progress")`.
2. **Honor Native Boundary Purity:** Keep entrypoints thin (auth/input/response glue), core domain logic pure and side-effect-free, and external I/O isolated inside thin adapters/clients matching Stage 4.
3. **Defensive Hygiene:**
   - Enable strict typing / strict mode natively in the target language.
   - Enforce fail-closed rules on missing data and check live state before mutating per the Stage 4 Idempotency Matrix.
   - Scrub credentials, tokens, and secrets recursively before logging or error reporting.
   - Guarantee session, lock, or transaction cleanup inside deterministic cleanup blocks even on unhandled exceptions.
4. **Compile / Syntax Verification & OODA Recovery:** Run native build, syntax, or manifest checks (`run_command`) before finishing. If a build or runtime failure repeats (`>= 2` failed attempts), consult `skills/ooda-troubleshooting/SKILL.md` to isolate root cause before patching again.

## Exit Check & Checkpoint Output
Confirm zero hardcoded secrets, zero stubbed functions, and clean build/syntax validation, then output:

```markdown
### Checkpoint 5: Source Implementation Complete

**1. Branch & Source Files Created / Updated:**
- Branch: `feat/[slug]`
- `[path]` — [Single-line responsibility]

**2. Engineering Safeguards Verified:**
- Native boundary separation, idempotency checks, secret redaction, failure cleanup, and build/syntax check passed.

**3. Next Step:**
- Ready for Stage 6 (Automated Verification) via `/sdlc-next` or `/sdlc-6-verify`.
```
