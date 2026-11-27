# Feature F01 / Increment 01: Greenfield Git Bootstrap & Native 7-Stage Co-Design SDLC Skill System

- **Milestone:** `v5.1.0`
- **Status:** SHIPPED (Verified locally & on `Spark` `v5.1.0`)

## 1. Increment Goal & Anti-Sprawl Gate Verification

Enable Companion AI OS to bootstrap brand-new external Git repositories cleanly outside its immutable `app/` and `data/` directories, and natively operate the 8-skill 7-Stage + Day-2 SDLC framework (`Guided Co-Design` default + `Express` mode) across Web Studio, CLI/REPL, Kanban Boards, Git Worktrees, and Declarative Workflows.

### Pre-Flight Anti-Sprawl Gate Audit
1. **Gate 1 — Conceptual Compression:** Zero new `companion/*.py` modules (`25/25` Ring-0 module cap preserved), zero new SQLite tables (`ProjectGraphManager` reused), zero new top-level `TOOLS` (`18 < 20` tool schema budget preserved), and zero duplicate SDLC skills (`skills/sdlc-engineering/` retired and folded into `sdlc-backlog`, `sdlc-implement`, and `sdlc-verify`, preserving `pre-commit-verify` alongside the new `sdlc-audit` workflow).
2. **Gate 2 — Single Source of Truth & Restart Invariance:** Repository SDLC state lives on disk inside `ws.project_root` (`product.md`, `architecture.md`, `docs/adr/`, `docs/features*`, `src/`, `tests/`, `docs/operations-runbook.md`); active project focus is persisted in SQLite (`projects.workspace_path` + `user_profile.active_goal_project_id`) and bound to `settings.set_workspace(...)` + `project_workspace_scope()`. Both `<repo_sdlc_context>` and `sdlc_repo_status.py` call the same deterministic `inspect_sdlc_repo_state()` helper.
3. **Gate 3 — 5-Hop Seam Trace:** `ProjectConfigPanel.tsx` (`Create New Repo` / `Attach Existing Clone`) & `ComposerBar.tsx` (`/sdlc-*` autocomplete) -> `/api/actions/agile` & `/api/jobs` SSE -> `init_greenfield_repo` + SQLite `projects` -> `WorkspaceContext` + `<repo_sdlc_context>` + `[SDLC ROUTER DIRECTIVE]` -> live UI badge, board header, and streamed chat reply.
4. **Gate 4 — Human Mental Model & Domain Isolation:** Non-software Personal/Goal Boards never receive `<repo_sdlc_context>` or developer jargon; Code Repository creation defaults to `Create New Repo` with live `<projects_dir>/<slug>` preview while keeping `Attach Existing Clone` one click away.

---

## Stage 4: Engineering Design

### 1. Project & Module File Layout

```text
companion/
├── __init__.py                 # Version bump to 5.1.0 (Step 4)
├── config.py                   # settings.projects_dir (COMPANION_PROJECTS_DIR) & to_dict() exposure
├── worktrees.py                # validate_external_repo_path, init_greenfield_repo, git_manage(action='init'),
│                               # inspect_sdlc_repo_state, resolve_sdlc_slash_turn, enriched <repo_sdlc_context>
├── projects.py                 # create_project(..., init_git, focus, require_existing) & set_focus_project sync fix
├── core_actions.py             # _act_agile wiring for init_git, focus, require_existing, and projects_dir metadata
├── context.py                  # /project init greenfield git bootstrap via settings.projects_dir + build_turn_context SDLC preload
├── tools.py                    # Cross-workspace read_file('skills/<name>/...') resolution (self_root/skills & user_skills_dir)
└── agent.py                    # /sdlc-* interactive ReAct pass-through, /sdlc status|audit slash, & Guided Co-Design triage guard
surfaces/
├── api_extensions.py           # Expose projects_dir in GET /api/status
└── web_server.py               # Stream /sdlc* via async SSE jobs (202 Accepted) & refresh system prompt on agile project updates
skills/
├── sdlc-product/               # Stage 1 skill + references/product.template.md
├── sdlc-architecture/          # Stage 2 skill + references/{architecture,adr}.template.md
├── sdlc-backlog/               # Stage 3 skill + references/features-and-stories.template.md + agile_manage Board sync
├── sdlc-design/                # Stage 4 Hardening Pass skill + references/engineering-design.template.md
├── sdlc-implement/             # Stage 5 skill + git_manage feature branch/worktree & ooda-troubleshooting hooks
├── sdlc-verify/                # Stage 6 skill + references/testing-strategy.template.md + dev_preview/browser-dogfood hooks
│   ├── scripts/
│   │   ├── pre_commit_check.py # Migrated from retired sdlc-engineering (--self-test preserved)
│   │   └── sdlc_repo_status.py # Deterministic SDLC stage, Increment Pending/Shipped & placeholder audit (--self-test)
│   └── workflows/
│       ├── pre_commit_verify.workflow.json # Preserved built-in workflow ('pre-commit-verify')
│       └── sdlc_audit.workflow.json        # New built-in deterministic SDLC audit workflow ('sdlc-audit')
├── sdlc-operate/               # Stage 7 skill + references/operations-runbook.template.md
└── sdlc-change/                # Day-2 Entry-Hat Ripple Triage skill
frontend/src/
├── types.ts                    # projects_dir & RepoProbeResult fields; create_project init_git/focus types
└── surfaces/
    ├── ProjectConfigPanel.tsx  # 'Create New Repo' vs 'Attach Existing Clone' toggle + live <projects_dir>/<slug> path preview
    └── ComposerBar.tsx         # /sdlc-next, /sdlc-1-product..7-operate, /sdlc-change slash menu (<=28 char prefix window)
```

---

### 2. Entry Surface & Input Contracts

#### 2.1 Greenfield Repo Bootstrap & `agile` Project Creation
| Name / Field | Type | Required | Default | Validation, Normalization & Edge-Case Rules |
| :--- | :--- | :---: | :--- | :--- |
| `COMPANION_PROJECTS_DIR` | `str` (env) | No | Auto | Resolved via `expanduser().resolve()`. When unset: `(Path.home() / "projects").resolve()` when running inside packaged runtime (`~/.local/share/companion/app` or `/opt/companion`) or if `root.parent` is inside `.local`/`AppData`/filesystem root; otherwise `settings.root.parent.resolve()`. |
| `title` | `str` | Yes | — | Trimmed non-empty string; derives `slug = slugify_project_id(slug or title)`. |
| `workspace_path` | `str` | No | `""` | When `init_git=True` and empty, auto-derives `settings.projects_dir / slug`. Relative paths resolve against `settings.projects_dir`. Mechanically rejected by `validate_external_repo_path()` if equal to or inside `settings.root` (`app/`), `settings.effective_data_dir` (`data/`), `Path.home()`, or filesystem root. |
| `init_git` | `bool` | No | `False` | Coerced via `bool_arg` / `_as_bool`. When `True`, executes `init_greenfield_repo()`, binds `workspace_path`, and forces `focus=True`. |
| `require_existing` | `bool` | No | `False` | Sent as `True` by UI `Attach Existing Clone` mode; fails closed if `workspace_path` does not exist on disk. |
| `focus` | `bool` | No | `False` | When `True` (or `init_git=True`), calls `set_focus_project(project_id)` immediately so `WorkspaceContext` updates atomically. |

#### 2.2 `/sdlc-*` & `/sdlc-next` Slash Command Router
| Command | Arguments | Execution Route & Mode Rules |
| :--- | :--- | :--- |
| `/sdlc-next` | `[optional notes \| express]` | Runs `inspect_sdlc_repo_state(ws.project_root)` deterministically, selects next Stage (`1..7`) or `sdlc-change`, preloads matching `skills/sdlc-*/SKILL.md` into `<turn_context>`, and runs in interactive ReAct loop (`DIRECT_TURN`). Mode is `Express` iff `\bexpress\b` appears in arguments; otherwise `Guided Co-Design`. |
| `/sdlc-1-product` .. `/sdlc-7-operate` | `[optional notes \| express]` | Routes directly to the specified stage skill (`sdlc-product`, `sdlc-architecture`, `sdlc-backlog`, `sdlc-design`, `sdlc-implement`, `sdlc-verify`, `sdlc-operate`) with `inspect_sdlc_repo_state` upstream artifact context. Also accepts short aliases `/sdlc-product` .. `/sdlc-operate`. |
| `/sdlc-change` | `[change request \| express]` | Routes to `skills/sdlc-change/SKILL.md` for Entry-Hat Impact Triage and downward surgical ripple. |
| `/sdlc status` \| `/sdlc audit` | `[optional repo_path]` | Deterministic zero-LLM slash output summarizing `inspect_sdlc_repo_state()` + placeholder hygiene audit. |

---

### 3. Core Domain Contracts & Data Models

#### 3.1 Core Functions & Single Responsibilities
| Component / Function | Single Responsibility | Mutates External State? |
| :--- | :--- | :---: |
| `Settings.projects_dir` | Compute host-safe default parent folder for user Git repositories outside `app/` and `data/`. | No |
| `validate_external_repo_path(target)` | Enforce hard containment guard blocking `app/`, `data/`, `$HOME`, and `/` before directory/Git mutations. | No |
| `init_greenfield_repo(path, title, summary)` | Idempotently create directory, run `git init -b main`, write `.gitignore` + `README.md` if absent, configure repo-local git identity fallback if needed, and create initial commit on `main`. | Yes (External Repo Only) |
| `resolve(p, for_write)` | Resolve `skills/<name>/...` reads across `ws.self_root / "skills"` and `settings.user_skills_dir` while confining all writes to `ws.project_root`. | No |
| `inspect_sdlc_repo_state(repo_root)` | Deterministically classify greenfield vs brownfield, Choice A vs Choice B, active `inc-0Y` `Pending` vs `SHIPPED` state, next SDLC stage (`1..7`), and template placeholder hygiene. | No |
| `resolve_sdlc_slash_turn(query, root)` | Parse `/sdlc-*` slash turns, detect `Guided Co-Design` vs `Express`, and build `[SDLC ROUTER DIRECTIVE]` + preloaded `SKILL.md` block for `<turn_context>`. | No |

#### 3.2 Typed Data Contracts

```python
# Output of init_greenfield_repo()
{
    "ok": bool,
    "repo_root": str,                # Resolved external repository path
    "branch": str,                   # e.g. "main"
    "commit_sha": str,               # Short HEAD SHA (guarantees git worktree readiness)
    "created_repo": bool,            # True if .git was newly initialized
    "initial_commit_created": bool,  # True if initial commit was created on this call
    "idempotent": bool,              # True when repo + HEAD already existed (safe NoOp)
    "summary": str,
}

# Output of inspect_sdlc_repo_state() & sdlc_repo_status.py
{
    "ok": True,
    "repo_root": str,
    "is_git_repo": bool,
    "repo_kind": "greenfield" | "brownfield",
    "backlog_mode": "none" | "choice_a" | "choice_b",
    "detected_stage": int,           # 1..7 (or 8 when all 7 stages complete -> sdlc-change)
    "stage_label": str,              # e.g. "Stage 4: Design Engineering & Hardening Pass"
    "recommended_skill": str,        # e.g. "sdlc-design"
    "active_increment": str,         # Relative path e.g. "docs/features/F01-auth/inc-02-tokens.md" or ""
    "upstream_artifacts": list[str], # Existing locked SDLC files to read first
    "increments": list[{
        "path": str,
        "design_pending": bool,
        "shipped": bool,
    }],
    "source_file_count": int,
    "test_file_count": int,
    "placeholder_violations": list[str],
    "hygiene_clean": bool,
    "lifecycle_complete": bool,
    "summary": str,
}
```

---

### 4. Production Hardening: Invariants, Edge Filters & Idempotency Matrix

#### 4.1 Domain Invariants, Noise Filters & Fail-Closed Rules
- **`app/` & `data/` Immutability Barrier:** `validate_external_repo_path()` runs prior to `Path.mkdir()` in `init_greenfield_repo()`, `/project init`, `git_manage(action="init")`, and `agile.create_project`. Any path equal to or inside `settings.root` or `settings.effective_data_dir` fails closed with `SAFETY GUARD REJECTED`.
- **Host Git Identity Fail-Safe:** Fresh Linux hosts (including headless systemd user units on `Spark`) may lack global `~/.gitconfig` `user.name`/`user.email`. When `git commit` in `init_greenfield_repo` encounters missing author config, it sets **repository-local** `git config user.name "Companion AI OS"` and `git config user.email "companion@localhost"` (never mutating global user `~/.gitconfig`) and retries the initial commit so `HEAD` and `git worktree add` always work out-of-the-box.
- **Choice B `Pending` Header Noise Filter:** During placeholder hygiene scanning (`sdlc_repo_status.py`), `## Stage 4: Engineering Design (Pending)` inside an unshipped `inc-0Y-*.md` story file is recognized as a valid Stage 3 Choice B marker rather than a false-positive stub violation, whereas `[Product / System Name]`, `TODO:`, `TBD`, or `FIXME` inside locked sections are flagged.
- **Interactive Co-Design Protection:** `triage_turn` and `run_agent_turn` detect `/sdlc-*` commands and active `Guided Co-Design` conversation turns (`[SDLC ROUTER`, `Checkpoint`, `Stop 1..4`) and pin `route="DIRECT_TURN"`, `needs_blueprint=False`, and `auto_promote=False` so multi-artifact SDLC discussions never hijack into runaway background `WorkflowEngine` builds.
- **Personal Domain Isolation ("Chicken Stir-Fry Rule"):** `<repo_sdlc_context>` is omitted completely when focused on `companion-self` or any Personal/Household Goal Board (`workspace_path == ""`), preventing SDLC terminology from leaking into everyday non-software chats.

#### 4.2 State Transition & Idempotency Matrix

| Observed State on Entry | Trigger / Retry Condition | System Action Taken | Resulting State / Outcome |
| :--- | :--- | :--- | :--- |
| Target directory does not exist | `init_greenfield_repo` / UI `Create New Repo` / `/project init` | Validate path outside `app/` & `data/`, `mkdir -p`, `git init -b main`, write `.gitignore` + `README.md`, create initial commit, bind & focus `WorkspaceContext` | `Completed` (`main` + `HEAD` ready) |
| Directory exists with files, no `.git` | `init_greenfield_repo` on pre-populated folder | Run `git init -b main`, preserve existing `README.md`/`.gitignore`, stage only files passing `check_git_stage_safety`, create initial commit | `Completed (Bootstrapped Existing Folder)` |
| `.git` exists, unborn `HEAD` (0 commits) | Re-run after interrupted `git init` | Skip `git init`, ensure `.gitignore` + `README.md` exist, create initial commit on `main` | `Completed (Recovered HEAD)` |
| `.git` + `HEAD` commit already exist | Duplicate `Create New Repo` or `init_git=True` on existing clone | Detect `.git` and verified `HEAD`; skip `git init` and commit (`idempotent=True`) | `Completed (Idempotent NoOp)` |
| Path inside `app/` or `data/` | User/tool passes runtime or data directory | Halt before `mkdir`; return `SAFETY GUARD REJECTED` | `Aborted Safely` |

---

### 5. Error Taxonomy & Failure Signaling

| Failure Category | Native Status / Signal / Code | Safe to Retry? | Caller / Operator Handling |
| :--- | :---: | :---: | :--- |
| **Success / Idempotent No-Op** | `{"ok": True, ...}` / HTTP `200` | — | Focuses project workspace and updates UI badge / chat context. |
| **Boundary / Path Validation Error** | `SAFETY GUARD REJECTED` / HTTP `400` | No | Rejects prior to filesystem mutation; user selects path under `projects_dir`. |
| **Missing Clone on Attach** | `{"ok": False, "error": "..."}` / HTTP `400` | No | User switches toggle to `Create New Repo` or corrects path. |
| **Git Binary Missing / Subprocess Fault** | `error: git ...` | Yes | Surfaces scrubbed stderr; zero partial SQLite corruption. |

---

### 6. Structured Observability & Secret Redaction

- **Automatic Redaction:** All Git subprocess outputs (`_run_git_cmd`), CoreAction responses (`execute_core_action`), and `sdlc_repo_status.py` outputs pass through `VaultManager.scrub_output` / `redact_secrets`.
- **Telemetry Spans:** Tool calls (`git_manage`, `agile_manage`, `read_file`, `write_file`) and `/sdlc-*` turns record spans into `telemetry_traces` / `telemetry_spans` with secret-scrubbed details.
