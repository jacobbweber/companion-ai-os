# Companion Repository SDLC & Architecture Rules

When developing or verifying a checkout of Companion:

1. **Zero Builder/Runtime Cross-Contamination**: Keep Antigravity/IDE developer rules and build scripts strictly inside `.agents/`. Never leak repo linters or internal dev skills into `skills/` or `data/domains/`, which belong exclusively to Companion's runtime and end-user `Customizations` UI.
2. **Standard Frontier Single-Agent Harness**: Keep `companion/` lean and cohesive around the 6 Frontier Primitives (Layered Context Assembler, Single Primary ReAct Loop, Orthogonal Tool Registry with scratch spillover, Isolated Subagents, LLM-summarized Compactor with deterministic `KeyTokens` verification, and Self-Contained `data/domains/<pack>/` Capability Packs + declarative `*.workflow.json` pipelines).
3. **Python 3.12 Stdlib Core**: Keep `harness.py`, `companion/`, and `surfaces/` pure Python 3.12 standard library; isolate optional third-party packages inside PEP 723 helper scripts (`companion/scripts/*.py` or `data/domains/<pack>/scripts/*.py`) and `frontend/` build-time tooling.
4. **Verification Gate**: Run `python harness.py --doctor`, `npm --prefix frontend test` + `npm --prefix frontend run build` when touching `frontend/` or `surfaces/static/`, and execute live `product-intent-dogfood` browser verification before declaring UI/workflow work complete.
