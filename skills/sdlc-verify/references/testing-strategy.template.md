# Testing & Verification Strategy: [System Name]

This document defines how **[System Name]** is verified across isolated unit tests, end-to-end integration tests, and pre-production validation.

---

## 1. Test Pyramid

| Test Tier | Location | Test Tooling | External Dependencies | Target Runtime | Verification Goal |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **Unit Tests** | `[Unit Test Path]` | `[Native Test Runner]` | None (Mocked / fake external adapters) | `< 5s` | Prove domain invariants, boundary edge cases, fail-closed rules, idempotency branches, and redaction in isolation. |
| **Integration Tests** | `[Integration Test Path]` | `[Native Test Runner]` | Local temp state + deterministic simulator/fakes | `< 15s` | Prove end-to-end wiring from entry surface through core logic, error signaling, and structured telemetry output. |
| **Pre-Prod Validation** | `[Staging / Lab]` | `[Preview / Live Check]` | Non-production target environment | `~2–5m` | Confirm live external dependency wire behavior prior to production release. |

---

## 2. Unit & Domain Logic Coverage Matrix

| Area | Scenario Under Test | Expected Deterministic Outcome |
| :--- | :--- | :--- |
| **Input & Type Boundaries** | Valid inputs, aliases, coercion edge cases, and invalid inputs | Valid inputs normalize accurately; invalid inputs fail with expected validation error. |
| **Scope & Candidate Filtering** | In-scope targets mixed with excluded/out-of-scope types | Selects only in-scope targets; ignores excluded items safely. |
| **Safety & Validation Gates** | One dedicated negative test per safety/business rule | Blocks unsafe mutation with exact expected rejection reason. |
| **Fail-Closed Missing Data** | Upstream dependency returns empty, null, or incomplete data | Fails closed rather than assuming permissive defaults. |
| **Idempotency & Partial Recovery** | Re-execution over already-completed or half-completed state | Skips completed work without error; converges remaining targets cleanly. |
| **Secret Redaction** | Telemetry payload includes credentials, tokens, or secret fields | Raw output contains zero secret strings (`[REDACTED]`). |
| **Failure Cleanup Hygiene** | Unhandled exception injected mid-workflow | Cleanup/disconnect logic still executes deterministically. |

---

## 3. End-to-End Integration Scenarios

1. **Scenario 1 — Safe Preview / Non-Destructive Path:** Exercises full entry-to-core pipeline without mutating target state; verifies structured summary and correlated telemetry events.
2. **Scenario 2 — Full Execution & Idempotent Convergence:** Exercises complete workflow including partial-state recovery; verifies final state and zero secret leakage.
3. **Scenario 3 — Caller Input Compatibility:** Verifies real external caller input formats, aliases, and serialized values bind accurately.
4. **Scenario 4 — Auth / Dependency Failure Signaling:** Verifies expected failure signal and sanitized error telemetry when upstream authentication or connectivity fails.

---

## 4. Running Automated Verification

```text
[Exact command(s) to execute the full Unit + Integration suite and verify 100% pass]
```
