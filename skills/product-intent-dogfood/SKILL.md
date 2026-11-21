---
name: product-intent-dogfood
description: MANDATORY live UX & semantic product-intent verification whenever asked to "dogfood", "go dogfood it", "live test", "test it", "try it out", "use the tool", or before declaring any UI button, workflow, or feature complete. Replaces shallow unit-test / ok:true rubber-stamping with actually operating the live UI against a real LLM, out-of-domain everyday data, verb-promise reasoning audits, and visual screenshot critique of every rendered path, folder, YAML field, tag, and badge.
required_tools: [dev_preview, workflow_manage, read_file, run_python]
---

# Product-Intent & Live Human-UX Dogfooding (`product-intent-dogfood`)

Unit and integration tests only prove that code executes without crashing—they **do not** prove that a feature actually fulfills human intent. Whenever you build, fix, "test", "live test", or "dogfood" a capability, you MUST **actually use the live running application** with critical human product judgment.

## When to Use This Skill
- **Automatically** whenever the user says **"dogfood"**, **"go dogfood it"**, **"live test"**, **"test it"**, **"try using it"**, **"witness it working"**, or **"verify UX"**.
- Before claiming **any** UI button, workflow, curation flow, Board action, or agent behavior is complete.
- Whenever evaluating whether an action's output makes common-sense human meaning rather than merely returning HTTP `200` / `{"ok": true}`.

## Core Principles (Non-Negotiable Laws)
1. **`ok: true` Is Never Proof of Correctness (Semantic Output Audit)**:
   - A check that only verifies `res.ok === true` or `path.startsWith("02_knowledge/")` will pass garbage like `02_knowledge/01-inbox/cooking-recipe.md` tagged `#inbox` and `preventive-maintenance`!
   - You MUST read the **actual human-visible content**: exact folder hierarchy, filename slug, title, prose summary, YAML frontmatter values, tags, ontology concepts, links, and wall-clock timing. Ask: *"If a thoughtful human looked at every word, folder name, and pill on screen right now, would anything look robotic, misclassified, lazy, or broken?"*
2. **The "Chicken Stir-Fry" Out-of-Domain Rule**:
   - Never test general-purpose features (Wiki, Boards, Calendar, Chat, Memory) exclusively with developer/IT/homelab examples (`"Fix API bug"`, `"Proxmox server"`), which hide hardcoded IT biases.
   - Always exercise general features with **everyday non-technical human content** (e.g., cooking recipes, household chores, fitness, family travel) and verify zero developer/IT jargon (`operational-runbook`, `preventive-maintenance`, `sprint_todo`) leaks onto everyday items.
3. **Verb-Promise & Reasoning Audit**:
   - Any UI control or action whose verb promises judgment—such as **`Graduate`**, **`Organize`**, **`Curate`**, **`Plan`**, **`Recommend`**, **`Slice`**, or **`Synthesize`**—MUST genuinely inspect surrounding context and apply AI reasoning.
   - If clicking a judgment button finishes in `5ms` via a naive string/regex fallback without considering folder taxonomy, sibling items, or content meaning, **flag and fix it as a broken feature**.
4. **Operate the Real UI & Critique Every Pixel**:
   - Do not stop at backend function calls when a UI exists. Open the live Web UI in a real browser (against a real live LLM, never a fake mock), click the actual UI buttons as a user would, witness the loading/reasoning state and final rendered state, and inspect every visible badge, breadcrumb, sidebar tree node, and footer version label in screenshots.

## Step-by-Step Procedure
1. **State the Human Intent Contract First**:
   - Before testing, write down in plain English what a **good** outcome looks like to a human (e.g., *"Graduating 6 inbox recipes should place them under a clean culinary folder hierarchy like `02_knowledge/cooking/...` named after each dish—never `01-inbox` or the template name—with clean recipe summaries, culinary tags/ontology, `#inbox` removed, and related dishes cross-linked"*).
2. **Drive the Live Running App Against a Real Model**:
   - Launch or connect to the live Web Studio wired to a real LLM endpoint (never mocked `fake_chat`).
   - Navigate through the actual browser UI (`browser` subagent or `browser-dogfood` Playwright runner) and click the real buttons/flows end-to-end across **multiple real items** (not just one synthetic happy-path stub).
3. **Perform Line-by-Line Semantic & Visual Inspection**:
   - Capture screenshots of both the intermediate review/reasoning state and the final rendered result.
   - Read the generated files/records on disk AND the UI screenshot side-by-side, checking:
     - **Naming & Hierarchy**: Are folder names, slugs, and titles specific, human-natural, and collision-free?
     - **Metadata & Tags**: Are summaries genuine prose (not raw bullet dumps)? Are stale staging tags (`#inbox`, `Test:`) cleaned up? Are domain/ontology labels accurate for the subject matter?
     - **Surrounding UI**: Did the sidebar tree, counts, breadcrumbs, and version labels update cleanly without stale state?
4. **Fix Root Causes & Re-Witness Live**:
   - If **anything** looks mechanical, awkward, misclassified, or unintuitive, fix the underlying logic/prompt/UI and **re-run the live browser flow** until you personally witness clean, human-grade results on every item.
