---
name: devops-ops
description: Safe-by-default release promotion, packaged app/ runtime sync, Blind-Vault remote endpoint operations, pre-deploy snapshot backups, human approval gates, /api/health + 7/7 Doctor verification, and automatic rollback.
required_tools: [workflow_manage, endpoint_manage, vault_manage, backup_manage, system_doctor]
---

# Safe Deployment & Infrastructure Operations (`devops-ops`)

Promote verified commits or release tags into a local packaged `app/` runtime directory or a registered `EndpointManager` SSH/HTTP host—always taking a pre-deploy snapshot first, pausing for `ApprovalQueue` sign-off, verifying `/api/health` + `7/7` `System Doctor`, and automatically rolling back on any health failure.

## When to Use This Skill
- Promoting a verified git commit or tag from a development clone into an immutable `app/` runtime install directory.
- Inspecting or restarting a registered remote host (`EndpointManager`) using symbolic Blind Vault credentials (`credential_ref`).
- Exercising `safe-deploy` verification and rollback readiness before production cutovers.

## Step-by-Step Procedure
1. **Verify Target & Blind Vault Credentials**:
   - Never hardcode hostnames, IPs, or SSH keys in skills or prompts. For remote hosts, inspect registered endpoints via `endpoint_manage(action="list")` and verify `credential_ref` via `vault_manage(action="check_secret")`.
2. **Run the `safe-deploy` Deterministic DAG**:
   - Invoke `workflow_manage`:
     ```json
     {
       "action": "run_dag",
       "dag_name": "safe-deploy",
       "params": {
         "source_repo": "D:/Projects/Companion",
         "target_app_dir": "D:/CompanionRuntime/app"
       }
     }
     ```
3. **Human Gate Approval (`deploy_approval_gate`)**:
   - `pre_deploy_snapshot` captures a clean backup of `target_app_dir` (and optional `data/` SQLite/Wiki snapshot) before pausing at `deploy_approval_gate` (`ApprovalQueue` `R3` gate in `balanced`/`strict` trust mode).
   - Once approved by the user, resume the DAG via `workflow_manage(action="resume_dag", run_id="...")`.
4. **Post-Deploy Health + Doctor Gate & Automatic Rollback**:
   - `verify_or_rollback` probes `/api/health` (`200 OK`) AND runs packaged `7/7` `System Doctor` (`tests/` absent).
   - If either check fails, `safe-deploy` automatically restores `target_app_dir` from `pre_deploy_snapshot` and verifies the restored runtime is healthy.
