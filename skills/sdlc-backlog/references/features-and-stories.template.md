# PART A — `docs/features.md` Structure (Choice A)

# Feature Specifications: [Product Name]

This document decomposes the scope in [product.md](../product.md) and boundaries in [architecture.md](../architecture.md) into cohesive capability pillars.

---

## Feature Traceability Map

| Feature ID | Feature Name | Priority | Target Milestone | Linked Stories |
| :--- | :--- | :---: | :--- | :--- |
| **FEAT-01** | [Access, Auth & Input Handling] | P0 | v1.0 | `US-101`, `US-102` |
| **FEAT-02** | [Scoped Intake / Discovery] | P0 | v1.0 | `US-201`, `US-202` |
| **FEAT-03** | [Validation & Safety Invariants] | P0 | v1.0 | `US-301`, `US-302` |
| **FEAT-04** | [Core Idempotent Execution] | P0 | v1.0 | `US-401`, `US-402` |
| **FEAT-05** | [Safe Preview / Confirmation Mode] | P0 | v1.0 | `US-501` |
| **FEAT-06** | [Structured Telemetry & Resource Cleanup] | P0 | v1.0 | `US-601`, `US-602` |

---

## FEAT-01: [Feature Name]

### Value Delivered
[1–2 sentences stating who benefits and what capability or protection this feature provides.]

### Functional Capabilities
- [Concrete capability bullet 1]
- [Concrete capability bullet 2]
- [Concrete capability bullet 3]

### Boundary & Failure Rules
- [Invalid / failure condition 1 -> Expected safe system behavior]
- [Invalid / failure condition 2 -> Expected safe system behavior]

*(Repeat complete section for `FEAT-02` through `FEAT-0N`)*

---

# PART B — `docs/user-stories.md` Structure (Choice A)

# User Stories & Acceptance Criteria

This backlog translates the features in [features.md](features.md) into verifiable engineering stories.

---

## Epic 1: [Feature Name] (`FEAT-01`)

### US-101: [Clear Behavior-Oriented Story Title]
- **As a** [Persona / Actor from `product.md`],
- **I want** [Observable capability or protection],
- **So that** [Concrete outcome or risk reduction].
- **Size:** [1 / 2 / 3 / 5 Points] | **Priority:** P0

**Acceptance Criteria:**
- **Given** [valid starting condition], **When** [action occurs], **Then** [expected happy-path outcome].
- **Given** [invalid, missing, or unsafe condition], **When** [action occurs], **Then** [expected fail-safe rejection or error outcome].

*(Include complete stories across `FEAT-01..0N` with zero overlap or placeholder stubs.)*

---

# PART C — `docs/features/F0X-<slug>/00-feature-overview.md` Structure (Choice B)

# Feature F0X: [Feature Name]

- **Milestone:** [Target Release]
- **Status:** Planned (`inc-01` Next)

## 1. Feature Intent
[1 short paragraph explaining the capability and operational risk it addresses.]

## 2. Sequential Increment Plan
| Increment | File | Deliverable End-to-End Outcome | Milestone |
| :--- | :--- | :--- | :--- |
| **Increment 01** | `inc-01-<slug>.md` | [Foundational / read-only / core working slice] | [Milestone] |
| **Increment 02** | `inc-02-<slug>.md` | [Next working capability building on Increment 01] | [Milestone] |

## 3. Out of Feature Scope
- [Adjacent concerns handled in other features.]
