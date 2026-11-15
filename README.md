# Companion AI OS

A local-first, privacy-locked, self-hosted **AI Companion & Autonomous Engineering OS** built on a **100% Python 3.12 standard library core** (`zero pip install` required to run!).

Companion combines a warm, long-memory everyday assistant with deterministic engineering guardrails:
- **6 Built-In Web Studios** (pre-compiled offline bundle — no Node.js required at runtime):
  1. **Forever Chat** (streaming thoughts/tokens, multimodal vision & document attachments, Socratic Study Drawer & inline 3D-flip flashcards)
  2. **Agile & Kanban Boards** (Life/Home, Work/Career, Software & Custom boards, Weekday/Weekend slot math, Aging Radar & 15-minute Procrastination Unblocker)
  3. **Unified Calendar** (merges appointments, active sprints, planned tasks, recurring routines & spaced-repetition reviews)
  4. **Co-Owned Knowledge Wiki & 2D Mind Map** (plain Markdown 4-folder warehouse, 11 governed templates, visual diffs, Inbox staging & dual-author safety)
  5. **Background Jobs & Respectful Routines** (Departures Board, fingerprint-verified Human Approvals Queue & crash-safe workers)
  6. **System Studio** (`Identity & Profile`, Multi-Provider LLM Router, Zero-Reveal Blind Credential Vault, SSH/WinRM/HTTP Endpoints, Federated MCP, Autonomy & Hobbies, Snapshots & 7-Stage System Doctor)

---

## 2-Minute Quick Start (Local Ollama)

### Prerequisites
1. **Python 3.12+** (no `pip install` needed — Companion's core uses 100% Python standard library!)
2. **[Ollama](https://ollama.com)** running locally (or any OpenAI-compatible, Anthropic, or Gemini endpoint)

```bash
# 1. Pull any local model in Ollama (Companion auto-detects installed Ollama models if qwen2.5:7b isn't present!)
ollama pull qwen2.5:7b

# 2. Clone and launch the Web Studio
git clone https://github.com/jacobbweber/companion-ai-os.git
cd companion-ai-os
python harness.py --web
```

Open **`http://127.0.0.1:8000`** in your browser!
- On first boot, click the **Welcome Banner** (or the top-left **Companion** header / `System -> Identity & Profile`) to name your Companion, set its tone, and tell it a bit about yourself.
- Prefer the terminal? Run `python harness.py` for the interactive CLI REPL, or `pythonw harness.py --tray` on Windows for a silent System Tray icon.

---

## Clean Separation: Application Code vs. Your Personal Data

Companion strictly separates **application code** from **your personal runtime data** so updates, backups, and container deployments stay effortless:

| What | Where It Lives | Tracked in Git? |
| :--- | :--- | :--- |
| **Application Core & Built-In Skills** | `harness.py`, `companion/`, `surfaces/`, `skills/`, `specs/` | Yes |
| **Your Memories, Chat DB & Profile** | `data/conversations.db`, `data/MEMORY.md`, `data/IDENTITY.md` | **Never** (gitignored) |
| **Your Blind Credential Vault** | `data/secrets.env` (`0600` write-only, zero-reveal) | **Never** (gitignored) |
| **Your Knowledge Wiki & Attachments** | `data/wiki/` (auto-seeded with 11 templates), `data/attachments/` | **Never** (gitignored) |
| **Your Custom / Forged Skills** | `data/skills/<skill-name>/SKILL.md` | **Never** (gitignored) |
| **Backups & Checkpoints** | `data/snapshots/`, `data/.companion_backups/` | **Never** (gitignored) |

> **Custom Data Location (`COMPANION_DATA_DIR`)**: Override where all personal data lives anytime via `COMPANION_DATA_DIR=/path/to/data` (used automatically by Docker `/data`, Linux `/var/lib/companion`, and Windows `%LOCALAPPDATA%\Companion\data`).

---

## Deployment Options

### Option 1: Docker Compose (Recommended for Home Servers / NAS)
Runs as a non-root container with an immutable application core (`COMPANION_IMMUTABLE_CORE=1`) and all user state persisted cleanly in `./companion-data`:

```bash
docker compose up -d
```
- Open **`http://127.0.0.1:8000`** (connects to Ollama on your host automatically via `host.docker.internal:11434`, or override `COMPANION_BASE_URL` in `.env`).
- Upgrade anytime without touching your memories or Wiki: `git pull && docker compose up -d --build`.

### Option 2: Windows System Tray App, Startup Service, or `.exe` Installer
- **Run in Windows System Tray right now**:
  ```powershell
  pythonw harness.py --tray
  ```
  *(Adds a tray icon with right-click menu: **Open Companion Studio**, **Open User Data Folder**, and **Quit Companion**. Double-clicking again smartly opens your existing browser tab instead of starting a second server!)*
- **Start automatically when you sign in to Windows**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-startup-service.ps1
  # Remove anytime (preserves your user data):
  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-startup-service.ps1
  ```
- **Build a standalone `CompanionSetup-v3.4.0.exe` Windows Installer** (bundles Python 3.12 Embeddable Runtime + Inno Setup 6 — requires zero admin rights):
  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1
  ```

### Option 3: Linux `systemd` Background Daemon
Supports both a rootless per-user `systemd` service (`~/.local/share/companion/data`) and a system-wide `/opt/companion` daemon (`/var/lib/companion`):

```bash
# Option 3A — Rootless per-user background service (no sudo required):
bash deploy/linux/install-service.sh --user

# Option 3B — System-wide daemon under dedicated 'companion' user:
sudo bash deploy/linux/install-service.sh

# Uninstall anytime while preserving your user data folder:
bash deploy/linux/uninstall-service.sh --user   # or: sudo bash deploy/linux/uninstall-service.sh
```

---

## Safety & Privacy Architecture

1. **Mechanical `local_only` Privacy Lock (Default)**: Out of the box, Companion blocks outbound cloud LLM/embedding requests at the wire layer unless you explicitly switch `System -> Models & Tiers` to `hybrid` or `cloud_allowed`.
2. **Loopback-Default Web Studio + Token-Gated LAN/Tailscale**: By default, `COMPANION_WEB_HOST` binds strictly to `127.0.0.1` (`localhost`) with unconditional `Host`/`Origin`/`Sec-Fetch-Site`/`X-Companion-CSRF` enforcement. When bound to a LAN/Tailnet IP (`--host 0.0.0.0`), non-loopback requests require `COMPANION_WEB_TOKEN` (`Authorization: Bearer`, cookie, or `?token=` pairing).
3. **Read-Only Application Core by Default (`Self-Dev Mode = OFF`)**: By default (`ADR-019`), Companion can freely manage your `data/` Wiki, Boards, Calendar, Custom User Skills (`data/skills/`), and external `/project` folders, while its own core source code is mechanically locked read-only. Only a human can enable `Self-Dev Mode` (`System -> Autonomy & Hobbies` or `/selfdev on`), and container/service deployments lock it off via `COMPANION_SELF_DEV_LOCKED=1`.
4. **Zero-Reveal Blind Credential Vault**: API keys, SSH keys, and tokens entered in `System -> Vault & Endpoints` are sealed in `data/secrets.env`, never returned to the browser or LLM context, injected just-in-time into approved connectors, and scrubbed from tool outputs.
5. **7-Stage System Doctor**: Run `python harness.py --doctor` anytime to verify configuration, Python & JS AST syntax, A-Frame layer boundaries, 4-file Spec-Driven Development (`specs/`), dead-code/line-ceiling hygiene (`<=700` lines/module, `<=100` lines/function), SQLite WAL integrity, and the full automated test suite.

---

## Useful CLI & Slash Commands

```bash
python harness.py                  # Interactive terminal REPL
python harness.py --web            # Launch LAN/local Web Studio (default port 8000)
python harness.py --tray           # Launch with Desktop System Tray controller
python harness.py --doctor         # Run all 7 health & test gates
python harness.py --status         # Show live status HUD
python harness.py --project ./app  # Work inside an isolated external project folder
```

Inside Forever Chat or the CLI REPL, type `/help` to explore `/identity`, `/providers`, `/model`, `/plan`, `/approve`, `/goal`, `/wiki`, `/board`, `/sprint`, `/calendar`, `/study`, `/jobs`, `/vault`, `/endpoints`, `/mcp`, `/routines`, `/hobby`, `/selfdev`, `/backup`, and `/doctor`.

---

## Developing the Web Studio Frontend (Optional)

Pre-built static assets (`surfaces/static/`) are included in the repository so users never need Node.js to run Companion. If you modify TypeScript/React files under `frontend/src/`:

```bash
cd frontend
npm install
npm test          # Runs interactive Vitest suite against real Python backend
npm run build     # Typechecks, bundles, and syncs surfaces/static/
```

---

## Project Structure (A-Frame + 4-File Canonical SDD)

```text
Companion/
├── .env.example                 # Template configuration (defaults to local Ollama)
├── Dockerfile / docker-compose.yml # Non-root container deployment (`COMPANION_DATA_DIR=/data`)
├── harness.py                   # Self-recovering CLI, REPL, Web & System Tray launcher
├── data/                        # Isolated User Runtime Data (gitignored; auto-created on first boot)
│   ├── conversations.db         # SQLite WAL database (chats, profile, boards, calendar, wiki graph)
│   ├── IDENTITY.md / MEMORY.md  # Compact Soul/Identity card & synced working memory
│   ├── secrets.env              # 0600 Zero-Reveal Blind Credential Vault
│   ├── wiki/                    # 4-Folder Markdown Wiki Warehouse + 11 seeded templates
│   └── skills/                  # Custom / forged user skills (`data/skills/<name>/SKILL.md`)
├── deploy/                      # Linux `systemd` service & Windows Startup / Inno Setup `.exe` installer
├── specs/                       # Living Canonical Spec-Driven Development Pyramid (Strictly 4 Files)
│   ├── PRODUCT.md               # 1. Vision, User Persona, Capability Matrix & Invariants
│   ├── ARCHITECTURE.md          # 2. A-Frame Layers, 1-to-1 Module Registry & Simplicity Budgets
│   ├── CONTRACTS.md             # 3. Bounded Subsystem Contracts (SPEC-01..SPEC-08) & Test Map
│   └── DECISIONS.md             # 4. Self-Compacting Architecture Decision Records (ADRs)
├── skills/                      # Built-in 3-Tier Progressive-Disclosure Skill Packs (`skills/<name>/SKILL.md`)
├── companion/                   # Headless Core A-Frame Engine (45 modules; `<=700L/module`, `<=100L/function`)
│   ├── config.py, telemetry.py, streaming.py, llm_wire.py, llm.py
│   ├── checkpoints.py, safety.py, semantic_memory.py, memory_store.py, trigger_policy.py, project_graph.py
│   ├── disciplines.py, grounding.py, archive_and_approvals.py, vault.py, model_registry.py, wiki_templates.py
│   ├── wiki_engine.py, wiki_curation.py, wiki_maintenance.py, attachments.py, education_engine.py
│   ├── hygiene.py, sdd.py, workflow_graph.py, workflow_dag.py, dag_runtime.py
│   ├── intake.py, skill_forge.py, compaction.py, tool_schemas.py, background_jobs.py
│   ├── endpoints.py, mcp_hub.py, routines.py, calendar_engine.py, core_actions.py, harvest_tools.py
│   └── os_tools.py, tools.py, context.py, autonomy.py, blueprint.py, eval_harness.py, agent.py
├── frontend/                    # React 19 + TypeScript + Vite Multi-Studio UI source (-> surfaces/static/)
├── surfaces/                    # Presentation Layer: web_assets.py, api_extensions.py, web_server.py, windows_tray.py
├── configs/                     # Executable A-Frame Architecture component registry (architecture.py)
└── tests/                       # Automated unittest verification suite
```

---

## License

Released under the [MIT License](LICENSE). Free for personal, academic, and commercial use.
