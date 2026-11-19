---
name: browser-dogfood
description: Drive headless Chromium against live dev_preview URLs to trap console.error, uncaught JS pageerrors, and HTTP 4xx/5xx failures while capturing Desktop (1440x900) and Mobile (390x844) PNG screenshots for multimodal critique.
required_tools: [dev_preview, workflow_manage, run_python]
---

# Browser Dogfood & Multimodal UI Verification (`browser-dogfood`)

Verify live web applications like a senior product engineer—rendering real DOM/CSS/JS in headless Chromium, trapping runtime console/page/network errors, and capturing Desktop + Mobile screenshots before declaring UI work complete.

## When to Use This Skill
- After modifying frontend components, CSS themes, HTML templates, or HTTP API routes that power a Web UI.
- Whenever `sdlc-engineering` starts a local server via `dev_preview(action="start")` and needs objective visual + runtime verification.
- Reproducing a reported browser console error, broken button interaction, or mobile viewport overflow bug.

## Step-by-Step Procedure
1. **Start Ephemeral Preview Server**:
   - Launch the target app on a free localhost port via `dev_preview(action="start", command="...", ready_path="/api/health")`.
2. **Execute Headless Browser Dogfood Pass**:
   - Invoke `workflow_manage`:
     ```json
     {
       "action": "run_script",
       "skill": "browser-dogfood",
       "script": "browser_dogfood.py",
       "args": [
         "--url", "http://127.0.0.1:<port>/",
         "--out-dir", "data/scratch/dogfood",
         "--expect-selector", "[data-testid='floating-composer']"
       ]
     }
     ```
   - Pass `--click-selector "<css>"` to exercise interactive buttons/tabs and `--expect-text "<substring>"` to assert rendered copy.
   - Pass `--vision-critique` when an `image`-tier VLM is configured so Companion critiques both Desktop (`1440x900`) and Mobile (`390x844`) PNGs for clipping, overlap, or low contrast.
3. **Evaluate Structured Telemetry**:
   - Inspect the JSON output: `ok` MUST be `true`, with `console_errors: []`, `page_errors: []`, and `http_errors: []`.
   - If any error is trapped or `mobile_horizontal_overflow` is `true`, fix the root cause in source code and re-run the dogfood pass.
4. **Stop Preview Server**:
   - Call `dev_preview(action="stop")` once verification passes so zero background processes remain.
