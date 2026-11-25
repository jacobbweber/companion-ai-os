---
name: release-readiness
description: Verify GitHub release readiness by checking .gitignore privacy shield, verifying .agents/skills isolation, running Companion Runtime Doctor (`7/7`), and running frontend Vitest + build checks.
---

# Release-Readiness Skill

## When to Use This Skill
Use this runbook before merging a feature/refactor branch to `main` or tagging a Companion release for GitHub / `Spark` promotion.

## Step-by-Step Procedure
1. **Inspect `.gitignore` Privacy Shield & Working Tree**:
   - Read `.gitignore` and run `git status -sb` to verify zero sensitive keys, `.env`, `secrets.env`, `*.db*`, `data/`, or personal workspace paths are tracked.
2. **Run Companion Runtime Doctor (`7/7`)**:
   - Execute `python harness.py --doctor` and verify all 7 stages pass (`Configuration`, `Python Syntax`, `Static Web Bundle`, `Runtime Skills & Builder Boundary`, `Domain Capability Pack SQLite Isolation`, `Core SQLite WAL & Blind Vault`, and `Automated Unit Test Suite`).
3. **Run Frontend Typecheck, Vitest & Static Sync**:
   - Execute `npm --prefix frontend test` and `npm --prefix frontend run build`, confirming `8/8` interactive suites pass and `surfaces/static/` matches `frontend/src/`.
4. **Confirm Live Dogfood Evidence (`product-intent-dogfood`)**:
   - Verify any modified UI surfaces or AI verbs were exercised against a live LLM in a real browser before tagging `vX.Y.Z`.
