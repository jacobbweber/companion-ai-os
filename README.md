# Companion AI OS

A local-first, privacy-locked, self-hosted **AI Companion & Autonomous Engineering OS** built on a **100% Python 3.12 standard library core** (`zero pip install` required to run!).

Companion combines a warm, long-memory everyday assistant with deterministic engineering guardrails:
- **Progressive-Disclosure Web UI** (pre-compiled offline bundle — no Node.js required at runtime):
  1. **Forever Chat** (continuous timeline with cursor history, non-destructive focus/compaction dividers, multimodal attachments & inline 3D-flip flashcards)
  2. **Board & Goals (`ProjectGraph`)** (unified Life, Work & Software projects, milestones, `human`/`companion`/`collaborative` items & Right Split Inspector)
  3. **Unified Calendar** (Google/Outlook-style `Day`, `Week`, and `Month` grids merging appointments, planned items, recurring routines & spaced-repetition reviews)
  4. **Co-Owned Knowledge Wiki & 2D Mind Map** (4-folder Markdown warehouse, collapsible Directory Tree Explorer, YAML frontmatter search & facet filters, 11 governed templates, visual diffs & Study decks)
  5. **Automations & Background Jobs** (unified `TriggerPolicy` routines supporting `manual`, `scheduled`, and `on_idle` with configurable time windows, Departures Board & Human Approvals Queue)
  6. **Customizations & Settings** (3-Tier `Skills`, Self-Contained Domain Capability Packs (`data/domains/<pack>/`), declarative `*.workflow.json` pipelines, Federated MCP Hub, Multi-Provider LLM Router, Zero-Reveal Blind Credential Vault, SSH/WinRM/HTTP Endpoints & 7-Stage Runtime Doctor)

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
- On first boot, click **Settings** in the bottom-left rail (or type `/identity`) to name your Companion, set its tone, and tell it a bit about yourself.
- Prefer the terminal? Run `python harness.py` for the interactive CLI REPL, or `pythonw harness.py --tray` on Windows for a silent System Tray icon.

---

## Clean Separation: Application Code vs. Your Personal Data

Companion strictly separates **application code** from **your personal runtime data** so updates, backups, and container deployments stay effortless:

| What | Where It Lives | Tracked in Git? |
| :--- | :--- | :--- |
| **Application Core & Built-In Skills** | `harness.py`, `companion/`, `surfaces/`, `skills/` | Yes |
| **Builder / IDE Customizations** | `.agents/` (100% isolated from Companion runtime) | Yes |
| **Your Memories, Chat DB & Profile** | `data/conversations.db`, `data/MEMORY.md`, `data/IDENTITY.md` | **Never** (gitignored) |
| **Your Blind Credential Vault** | `data/secrets.env` (`0600` write-only, zero-reveal) | **Never** (gitignored) |
| **Your Knowledge Wiki & Attachments** | `data/wiki/` (auto-seeded with 11 templates), `data/attachments/` | **Never** (gitignored) |
| **Your Skills & Domain Capability Packs** | `data/skills/<name>/`, `data/domains/<pack>/` (`CONTEXT.md` + `<pack>.db` + `workflows/`) | **Never** (gitignored) |
| **Backups & Checkpoints** | `data/snapshots/`, `data/.companion_backups/` | **Never** (gitignored) |

> **Custom Data Location (`COMPANION_DATA_DIR`)**: Override where all personal data lives anytime via `COMPANION_DATA_DIR=/path/to/data` (used automatically by Docker `/data`, Linux `/var/lib/companion`, and Windows `%LOCALAPPDATA%\Companion\data`).

---

## Deployment Options

### Option 1: Docker Compose (Recommended for Home Servers / NAS)
Runs as a non-root container with an immutable application core (`COMPANION_IMMUTABLE_APP=1`) and all user state persisted cleanly in `./companion-data`:

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
- **Build a standalone `CompanionSetup-v5.0.0.exe` Windows Installer** (bundles Python 3.12 Embeddable Runtime + Inno Setup 6 — requires zero admin rights):
  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\build-installer.ps1
  ```

### Option 3: Linux `systemd` Background Daemon
Supports both a rootless per-user `systemd` service (`~/.local/share/companion/data`) and a system-wide `/opt/companion` daemon (`/var/lib/companion`), preserving your `--host` and `--port` settings across upgrades:

```bash
# Option 3A — Rootless per-user background service (no sudo required):
bash deploy/linux/install-service.sh --user [--host 0.0.0.0] [--port 8000]

# Option 3B — System-wide daemon under dedicated 'companion' user:
sudo bash deploy/linux/install-service.sh [--host 0.0.0.0] [--port 8000]

# Uninstall anytime while preserving your user data folder:
bash deploy/linux/uninstall-service.sh --user   # or: sudo bash deploy/linux/uninstall-service.sh
```

---

## Safety & Privacy Architecture

1. **Mechanical `local_only` Privacy Lock (Default)**: Out of the box, Companion blocks outbound cloud LLM/embedding requests at the wire layer unless you explicitly switch `Settings -> Models & Routing` to `hybrid` or `cloud_allowed`.
2. **Loopback-Default Web Studio + Token-Gated LAN/Tailscale**: By default, `COMPANION_WEB_HOST` binds strictly to `127.0.0.1` (`localhost`) with unconditional `Host`/`Origin`/`Sec-Fetch-Site`/`X-Companion-CSRF` enforcement. When bound to a LAN/Tailnet IP (`--host 0.0.0.0`), non-loopback requests require `COMPANION_WEB_TOKEN` (`Authorization: Bearer`, cookie, or `?token=` pairing).
3. **Permanent Installed-App Immutability & Per-Project Delivery Policy (`COMPANION_IMMUTABLE_APP=1`, `ADR-019`)**: Companion's own installed `app/` runtime is permanently immutable while `data/`, `data/scratch/`, Wiki, Boards, Calendar, User Skills (`data/skills/`), and external Board Project clones (`workspace_path`) stay writable, governed by each Project's `delivery_policy` (`local_only` | `remote_branch_pr` | `ask_me`).
4. **Zero-Reveal Blind Credential Vault**: API keys, SSH keys, and tokens entered in `Settings -> Vault & Endpoints` are sealed in `data/secrets.env`, never returned to the browser or LLM context, injected just-in-time into approved connectors, and scrubbed from tool outputs.
5. **7-Stage Runtime Doctor**: Run `python harness.py --doctor` anytime to verify configuration, Python syntax integrity, compiled Web Studio bundle sync, `.agents/` vs. `skills/` builder/runtime isolation, Domain Capability Pack SQLite health, Core SQLite WAL & Blind Vault integrity, and the full automated test suite.

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

Inside Forever Chat or the CLI REPL, type `/help` to explore `/identity`, `/providers`, `/model`, `/plan`, `/approve`, `/goal`, `/wiki`, `/board`, `/sprint`, `/calendar`, `/study`, `/jobs`, `/vault`, `/endpoints`, `/mcp`, `/routines`, `/customizations`, `/autonomy`, `/backup`, and `/doctor`.

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

## Project Structure (25-Module Frontier Single-Agent Harness)

```text
Companion/
├── .agents/                     # Antigravity/IDE builder rules & skills (isolated from runtime)
├── .env.example                 # Template configuration (defaults to local Ollama)
├── Dockerfile / docker-compose.yml # Non-root container deployment (`COMPANION_DATA_DIR=/data`)
├── harness.py                   # Self-recovering CLI, REPL, Web & System Tray launcher
├── data/                        # Isolated User Runtime Data (gitignored; auto-created on first boot)
│   ├── conversations.db         # Core SQLite WAL database (chats, profile, boards, calendar, wiki index)
│   ├── IDENTITY.md / MEMORY.md  # Compact Soul/Identity card & synced working memory
│   ├── secrets.env              # 0600 Zero-Reveal Blind Credential Vault
│   ├── wiki/                    # 4-Folder Markdown Wiki Warehouse + 11 seeded templates
│   ├── skills/                  # Custom / forged user skills (`data/skills/<name>/SKILL.md`)
│   └── domains/                 # Self-contained Domain Capability Packs (`CONTEXT.md`, `<pack>.db`, `workflows/`)
├── deploy/                      # Linux `systemd` service & Windows Startup / Inno Setup `.exe` installer
├── skills/                      # Built-in 3-Tier Progressive-Disclosure Skill Packs (`skills/<name>/SKILL.md`)
├── companion/                   # Pure-Stdlib Core Frontier Harness (25 cohesive modules)
│   ├── config.py, telemetry.py, streaming.py, llm_wire.py, llm.py, model_registry.py, vault.py
│   ├── memory_store.py, approvals.py, attachments.py, context.py, tools.py, agent.py
│   ├── wiki.py, wiki_curation.py, education_engine.py, projects.py, calendar_engine.py, workflows.py
│   └── jobs.py, routines.py, integrations.py, worktrees.py, core_actions.py, scripts/extract_pdf.py
├── frontend/                    # React 19 + TypeScript + Vite Multi-Studio UI source (-> surfaces/static/)
├── surfaces/                    # Presentation Layer: web_assets.py, api_extensions.py, web_server.py, windows_tray.py
└── tests/                       # Automated unittest verification suite
```

---

## License

Released under the [MIT License](LICENSE). Free for personal, academic, and commercial use.
