---
name: deep-thinking
description: Perform first-principles reasoning, multi-option architectural tradeoff analysis, and strategic planning using the reasoning model tier.
required_tools: [read_file, search, delegate_subagent]
---

# Deep Thinking & First-Principles Tradeoff Analysis

## When to Use This Skill
Use this runbook whenever a request or decision involves:
- Multi-component design, complex personal/financial planning, or new system architecture
- Evaluating competing options, tradeoffs, or second-order failure modes
- Any user question asking *"why"*, *"what is the best approach"*, or *"how should we structure this"*

## Step-by-Step Procedure
1. **Gather Ground-Truth Context First**:
   - Read the 1–3 most relevant files, `CONTEXT.md` maps, or domain notes using `read_file` or `search` before forming hypotheses.
2. **First-Principles Decomposition**:
   - If running on a fast model and deeper synthesis is helpful, delegate an isolated reasoning pass via `delegate_subagent(tier="reasoning", goal="<problem + gathered facts>")`.
   - Structure your analysis around:
     1. **Core Invariants & Hard Constraints** (what must hold true)
     2. **Candidate Options (Option A vs. Option B)** with explicit pros, cons, and operational simplicity
     3. **Second-Order Risks & Edge Cases** (failure modes, reversibility, maintenance burden)
     4. **Clear Recommendation & Concrete Next Steps**
