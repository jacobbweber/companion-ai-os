# Multi-Studio React + Stdlib Web Surface Patterns (`ADR-001`, `ADR-008`, `ADR-017`)

## 1. Frontend Source Map (`frontend/src/`)
- `App.tsx`: Top Tactical Header (Workspace, Active Turn HUD, Trust Mode, 1-click Light/Dark toggle, 4-Preset Theme Changer) + Left Studio Command Rail.
- `styles/theme.css`: CSS custom-property tokens for `data-theme="obsidian"` (default dark), `"orbital"` (brutalist sci-fi), `"graphite"` (minimal warm dark), and `"daylight"` (crisp light paper), using `height: 100dvh`.
- `api.ts`: `apiGet`, `apiPost` (attaches `X-Companion-CSRF` from `<meta name="companion-csrf">`), `callAction(name, params)` (`POST /api/actions/<name>`), and `subscribeJobStream(jobId, callbacks)` (`EventSource`).
- `components/MarkdownView.tsx` & `MindMapCanvas.tsx`: Safe Markdown renderer with clickable `[[wikilinks]]` + HTML5 `<canvas>` 2D force-directed Mind Map.
- `studios/`: `ChatStudio.tsx`, `WikiStudio.tsx`, `JobsApprovalsStudio.tsx`, `VaultEndpointsStudio.tsx`, `FederationMcpStudio.tsx`, `OpsStudio.tsx`.

## 2. Deterministic Build & Static Sync (`surfaces/static/`)
`surfaces/web_assets.py` reads `surfaces/static/{index.html,styles.css,app.js}`, escapes `</script` -> `<\/script` when inlining IIFE `app.js`, and injects the live CSRF meta tag. `companion/disciplines.py` runs `node --check` on both `app.js` and the rendered inline `<script>` during tests.

## 3. Ephemeral Port Testing (`port=0`)
Always expose `create_web_server(host="127.0.0.1", port=0, store=..., chat_fn=...)` returning `(server, session)`. Binding `port=0` lets the OS assign a free port for deterministic unit tests and `workflow_manage(action="http_smoke")` without port conflicts.
