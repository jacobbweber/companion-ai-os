---
name: sdlc-engineering
description: Autonomous repository-native software engineering workflow using git_manage feature branches/worktrees, TDD verification, dev_preview ephemeral servers, and browser-dogfood UI checks.
required_tools: [read_file, write_file, edit_file, search, git_manage, dev_preview, workflow_manage, run_python, run_shell]
---

# Agentic SDLC Engineering (`sdlc-engineering`)

Execute production-grade software engineering inside any external Git repository (`workspace.project_root`) while keeping Companion's own installed runtime directory permanently immutable.

## When to Use This Skill
- Implementing a feature, fixing a bug, or refactoring code inside a Git repository registered on a Board Project (`workspace_path`).
- Running parallel feature slices safely in isolated `git worktree` checkouts without colliding on uncommitted files.
- Starting an ephemeral local dev server (`dev_preview`) to visually verify Web UI changes before committing.

## Step-by-Step Procedure
1. **Orient & Inspect Repo Rules**:
   - Check `<repo_sdlc_context>` in `<turn_context>` for discovered `.agents/rules/*.md`, `AGENTS.md`, `CLAUDE.md`, active branch, and detected stack (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`).
   - Confirm the project's `delivery_policy` (`local_only` default | `ask_me` | `remote_branch_pr`). Never push or open a PR when `delivery_policy == "local_only"`.
2. **Isolate on a Feature Branch**:
   - Use `git_manage(action="status")` to inspect working tree cleanliness.
   - For single-repo tasks, switch the repository in place to a descriptive feature branch (`feat/<slug>` or `fix/<slug>`) via `git_manage(action="branch", branch="feat/...")`. Reserve `git_manage(action="worktree_add", ...)` for multi-worker parallel slices.
   - Never commit directly onto `main` or `master`.
3. **Test-First Implementation**:
   - Read target files (`read_file`, `search`) and write or update automated tests covering the target behavior.
   - Edit source files cleanly (`edit_file` / `write_file`) obeying repository line-budget and architectural rules.
   - Run the project's test suite (`run_python` / `run_shell`) until 100% green.
4. **Live Preview & Browser Dogfooding (Web UI Changes)**:
   - Start the project on a free ephemeral localhost port via `dev_preview(action="start", command="...", ready_path="/api/health")`.
   - Run `workflow_manage(action="run_script", skill="browser-dogfood", script="browser_dogfood.py", args=["--url", "http://127.0.0.1:<port>"])` to capture Desktop (`1440x900`) + Mobile (`390x844`) PNG screenshots and trap `console.error`, uncaught JS exceptions, and HTTP `4xx`/`5xx` failures.
   - Tear down the preview server via `dev_preview(action="stop")` (`PreviewProcessManager` also guarantees process-tree cleanup on turn exit).
5. **Pre-Commit Gate & Clean Local Commit**:
   - Optionally run `workflow_manage(action="run_dag", dag_name="pre-commit-verify")` to verify secret/DB staging safety and repository tests.
   - Stage explicit source/test files and commit cleanly on the feature branch via `git_manage(action="commit", files=[...], message="feat: ...")`.
   - Stop at the local verified commit when `delivery_policy == "local_only"`; invoke `github-workflow` only when remote delivery is authorized.
