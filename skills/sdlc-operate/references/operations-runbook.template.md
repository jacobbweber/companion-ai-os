# Operations Runbook: [System Name]

| Metadata | Value |
| :--- | :--- |
| **Owner / Primary Team** | [Owning Team & Contact] |
| **Escalation / Partner Team** | [Partner Platform / Security Contact] |
| **Execution Environment** | [Target Host / Runtime Platform] |
| **Telemetry / Audit Location** | `[Log Destination / Index / Audit Store]` |

---

## 1. Environment Prerequisites & Onboarding Checklist

Verify the following requirements before deploying or executing jobs:
1. **Runtime & Dependencies:** [Exact runtime/library versions and any non-interactive configuration flags required.]
2. **Access, Network & Storage Permissions:** [Least-privilege permissions, network reachability, and local/remote state or log path access.]
3. **Required Configuration & Secrets:**
   - `[CONFIG_OR_SECRET_1]` — [Purpose]
   - `[CONFIG_OR_SECRET_2]` — [Purpose]

---

## 2. Standard Operating Procedure (Safe Execution / Rollout)

### Step 1: Confirm Pre-Conditions
[Upstream prerequisites, change window, or readiness checks required prior to execution.]

### Step 2: Run Non-Destructive Preview / Validation First
[How to execute preview/assessment mode first and what outputs or skipped-item reasons to inspect.]

### Step 3: Execute Live Operation
[Exact live execution procedure and expected healthy completion signal.]

### Step 4: Verify Telemetry & State Before Downstream Actions
[How to verify correlated completion records before taking irreversible downstream steps.]

---

## 3. Observability Queries & Verification Checks

### Inspect Full Timeline for a Specific Run / Request
```text
[Copy-paste query or command filtering by correlation_id / run_id]
```

### Audit Completed Live Changes
```text
[Copy-paste compliance/audit query over recent executions]
```

### Monitor Failures or Blocked Executions
```text
[Copy-paste error alerting query or health check]
```

---

## 4. Troubleshooting & Recovery Playbook

| Symptom / Failure Signal | Root Cause | Actionable Resolution |
| :--- | :--- | :--- |
| **[Validation / Input Error]** | [Invalid input, missing target, or configuration mismatch] | [Exact check & fix] |
| **[Authentication / Secret Failure]** | [Expired secret, missing variable, or permission denial] | [How to verify & refresh credentials safely; confirm zero state changed] |
| **[Target / Dependency Unreachable]** | [Network timeout or upstream outage] | [Connectivity check + safe retry timing] |
| **[Target Skipped by Safety Rule]** | [Item failed domain readiness invariant] | [How to inspect reason code and resolve or wait safely] |
| **[Interrupted / Partial Failure]** | [Mid-execution timeout or transient fault on subset of work] | [Exact recovery steps—highlighting when idempotent re-execution is 100% safe] |
