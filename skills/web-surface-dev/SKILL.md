---
name: web-surface-dev
description: Build, style, and maintain Companion 3.0's React + TypeScript + Vite Multi-Studio Web UI in frontend/ compiling deterministically into surfaces/static/ served by stdlib surfaces/web_server.py.
required_tools: [read_file, write_file, edit_file, workflow_manage, run_python, run_shell, system_doctor]
---

# Companion 3.0 Multi-Studio Web UI & Stdlib Surface Development (`frontend/` -> `surfaces/`)

## When to Use This Skill
Use this runbook whenever creating, modifying, theming, or debugging Companion's 6-Studio browser UI (`frontend/src/`), compiled static assets (`surfaces/static/`), or Python stdlib HTTP/SSE/CoreActions endpoints (`surfaces/web_server.py`, `surfaces/api_extensions.py`, `surfaces/web_assets.py`).

## Core Invariants
- **ADR-001 (Pure Brain, Zero Node Runtime)**: Ring-0 Python (`surfaces/*.py`) remains 100% Python 3.12 stdlib (`ThreadingHTTPServer`) with zero runtime Node server (`python harness.py --web`). Never import `fastapi`, `flask`, `uvicorn`, or external CDN scripts (`cdn.`).
- **Deterministic Build Pipeline (`frontend/` -> `surfaces/static/`)**: Author UI components in `frontend/src/` (React 19 + TypeScript + Lucide Icons) and compile via `npm run build` inside `frontend/` (or `python skills/web-surface-dev/scripts/build_frontend.py`), which runs `tsc --noEmit && vite build && node scripts/sync_static.mjs` to output IIFE `surfaces/static/app.js`, `styles.css`, and `index.html`.
- **ADR-008 & CoreActions Parity**: Never edit `surfaces/static/app.js` by hand. All studio actions call `POST /api/actions/<name>` (`callAction(name, params)` with `subaction` keys) or live SSE (`POST /api/jobs` + `GET /api/jobs/<id>/stream`).
- **Matte Brutalist Sci-Fi / Obsidian Design System**: Use CSS custom properties in `frontend/src/styles/theme.css` (`--bg-canvas`, `--bg-surface`, `--border-hairline`, `--accent-primary`, `100dvh` shell) so all 4 themes (`obsidian`, `orbital`, `graphite`, `daylight`) and the 1-click Light/Dark toggle stay crisp and legible.

## Step-by-Step Procedure
1. **Inspect Source Studios & Backend Contracts**:
   - Read `references/stdlib-web-patterns.md`, `frontend/src/types.ts`, `frontend/src/api.ts`, and the target studio in `frontend/src/studios/` (`ChatStudio.tsx`, `WikiStudio.tsx`, `JobsApprovalsStudio.tsx`, `VaultEndpointsStudio.tsx`, `FederationMcpStudio.tsx`, `OpsStudio.tsx`).
   - Cross-check every `callAction('<domain>', { subaction: '...' })` parameter and response key against `companion/core_actions.py` and `surfaces/api_extensions.py`.
2. **Preserve Zero-Login LAN Security & Contract Anchors**:
   - Keep `X-Companion-CSRF` header propagation in `frontend/src/api.ts` and preserve `is_allowed_lan_host` + `verify_post_request_security` in `surfaces/web_server.py`.
   - Never display raw Vault secrets in the DOM (`VaultEndpointsStudio.tsx` clears secret inputs immediately upon `subaction: 'set'`).
   - Preserve `100dvh` in `theme.css`, `EventSource` + `X-Companion-CSRF` in `api.ts`, and `#approval-banner` (`Plan First: OFF`, `Approve & Build`) in `frontend/scripts/sync_static.mjs`.
3. **Compile & Sync Static Bundle**:
   - Run `python skills/web-surface-dev/scripts/build_frontend.py` (or `npm run build` inside `frontend/`) to typecheck (`tsc --noEmit`), bundle (`vite build`), and sync `surfaces/static/{index.html,styles.css,app.js}`.
   - Forever Chat PDF attachments delegate text extraction to the Ring-1 PEP 723 helper `scripts/extract_pdf.py` (`--self-test` supported) so Ring-0 stays 100% Python stdlib.
4. **Verify via HTTP Smoke Probe & System Doctor**:
   - Never launch blocking `serve_forever()` servers inside `run_shell`. Run `workflow_manage(action="http_smoke", module="surfaces.web_server", endpoints=["/", "/api/health", "/api/status", "/api/actions"])` and `system_doctor(run_tests=True)`.
