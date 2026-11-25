---
name: devops-ops
description: Safe-by-default release promotion, packaged app/ runtime sync, Blind-Vault remote endpoint operations, pre-deploy snapshot backups, human approval gates, /api/health + 7/7 Doctor verification, and automatic rollback.
---

# Safe Deployment & Infrastructure Operations (`devops-ops`)

Promote verified commits or release tags into a local packaged `app/` runtime directory or a remote host (such as `Spark`)—always verifying tests first, pausing for explicit human sign-off before mutating production, verifying `/api/health` + `7/7` `Companion Runtime Doctor`, and keeping clean rollback readiness.

## Step-by-Step Procedure
1. **Pre-Flight Verification**:
   - Run `python harness.py --doctor` (`7/7 PASS`) and `npm --prefix frontend test` + `npm --prefix frontend run build` locally before promoting.
   - Optionally validate live LAN model tiers via `.agents/skills/devops-ops/scripts/live_model_calibration.py` (or `Settings -> Models & Tiers -> Calibrate`).
2. **Human Approval Gate Before Production Promotion**:
   - Confirm explicit user sign-off before merging to `main`, pushing tags, or updating remote services (e.g., `companion.service` on `Spark`).
3. **Snapshot & Atomic Sync**:
   - For local packaged `app/` promotion, use `.agents/skills/devops-ops/scripts/safe_deploy_ops.py` (captures pre-deploy snapshot before syncing).
   - For git-deployed Linux `systemd` hosts (`Spark`), record the previous git commit SHA (`git rev-parse HEAD`) for instant rollback before `git pull --ff-only` and `systemctl --user restart companion.service`.
4. **Post-Deploy Health + Doctor Verification**:
   - Verify `/api/health` (`200 OK`), `systemctl --user is-active companion.service`, and `python3 harness.py --doctor` (`7/7 PASS`); roll back immediately if any gate fails.
