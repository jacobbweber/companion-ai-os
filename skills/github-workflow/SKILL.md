---
name: github-workflow
description: Optional Blind-Vault GitHub branch push, idempotent Pull Request creation/update, Actions CI check inspection, and human-gated merge strictly governed by project delivery_policy.
required_tools: [git_manage, workflow_manage, vault_manage, agile_manage]
---

# GitHub PR & Actions Workflow (`github-workflow`)

Publish verified local feature branches to GitHub, open or update Pull Requests, inspect GitHub Actions check runs, and optionally merge behind an `ApprovalQueue` human gate—without ever exposing tokens or SSH keys to LLM prompts.

## When to Use This Skill
- ONLY when the active project's `delivery_policy` is `"remote_branch_pr"` or `"ask_me"` (or when the user explicitly requests pushing a branch / opening a PR).
- Never invoke this skill on a `"local_only"` project unless the user explicitly changes `delivery_policy` or asks to publish.

## Step-by-Step Procedure
1. **Verify Project Delivery Policy & Blind Vault Credential**:
   - Confirm `delivery_policy != "local_only"` and check that `git_credential_ref` (e.g., `github-pat`) is set in Blind Vault via `vault_manage(action="check_secret", name="...")`.
   - Ensure `skill:github-workflow` and `git_manage` scopes are allowed on the secret (`vault_manage(action="grant_scope", ...)` if needed).
2. **Push Feature Branch Safely**:
   - Push the `feat/*` or `fix/*` branch via `git_manage(action="push", remote="origin", branch="feat/...")`. `git_manage` automatically injects `GIT_ASKPASS` / `GIT_SSH_COMMAND` from Blind Vault and blocks `--force` or direct `main`/`master` pushes.
3. **Open/Update Pull Request & Inspect Actions CI**:
   - Run `workflow_manage(action="run_dag", dag_name="publish-pr", params={"repo": "owner/name", "head": "feat/...", "base": "main", "title": "...", "body": "..."})` OR call `github_pr_ci.py` directly via `workflow_manage(action="run_script", skill="github-workflow", script="github_pr_ci.py", args=[...])`.
   - Inspect `checks` output; if any GitHub Actions check run fails, diagnose the failure locally, commit a fix on the feature branch, push, and re-check.
4. **Human-Gated Merge**:
   - Merging into `main` always pauses at `merge_approval_gate` (`ApprovalQueue`) unless explicitly approved by the user.
