---
name: deep-thinking
description: Perform first-principles reasoning, multi-option architectural tradeoff analysis, and strategic planning using the reasoning model tier.
required_tools: [read_file, search, sdd_manage, workflow_manage, memory_manage]
---

# Deep Thinking & First-Principles Architecture Runbook

## When to Use This Skill
Use this runbook whenever a request or engineering decision involves:
- Multi-module architecture or new subsystem design (`DESIGN` / `PLAN` states)
- Evaluating competing technical approaches, tradeoffs, or second-order failure modes
- Synthesizing complex strategic plans, ADRs (`specs/DECISIONS.md`), or system contracts (`specs/CONTRACTS.md`)
- Any user question asking *"why"*, *"what is the best pattern"*, or *"how should we architect this"*

## Core Principles
- **Fast Context Gathering, Deep Reasoning Synthesis**: Gather factual context (files, specs, memory notes) deterministically first, then route synthesis and tradeoff evaluation to `tier="reasoning"`.
- **Structured Tradeoff Matrix**: Never jump to the first plausible solution. Always compare at least two distinct options against simplicity, SDD invariants, failure modes, and maintainability.

## Step-by-Step Procedure
1. **Gather Ground-Truth Context (`tier="fast"`)**:
   - Read only the 1–3 most relevant files or specs (e.g., `specs/ARCHITECTURE.md` or target module) using `read_file` or `search` so you preserve your tool iteration budget for synthesis.
   - Or trigger the deterministic Python DAG `skills/deep-thinking/dags/deep_analysis.py` via `workflow_manage(action="run_dag", dag="deep-analysis", params={"question": "..."})`.
2. **First-Principles Decomposition (`tier="reasoning"`)**:
   - If running interactively on `fast`, delegate the analysis to an isolated reasoning subagent:
     `workflow_manage(action="subagent", tier="reasoning", skill="deep-thinking", goal="<problem + constraints>")`.
   - Decompose the problem into:
     1. **Core Invariants & Hard Constraints** (what must never break)
     2. **Candidate Approaches (Option A vs. Option B)** with explicit pros, cons, and line-budget impact (`<= 700` lines/module, `<= 100` lines/function)
     3. **Second-Order Risks & Edge Cases** (concurrency, failure recovery, config fallback)
     4. **Recommended Architecture & Verification Plan**
3. **Persist Architectural Decisions**:
   - If a durable architectural choice was made, record an ADR via `memory_manage(action="add_adr", ...)` or distill a note into `skills/knowledge-wiki/references/`.
