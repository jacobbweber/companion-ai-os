# Feature F01 / Increment 02: Pinned Projects Root, Confined Directory Explorer & Human/Companion Workspace Parity

- **Milestone:** `v5.1.1`
- **Status:** SHIPPED (Verified locally & on `Spark` `v5.1.1`)
- **Upstream Increment:** `docs/features/F01-greenfield-sdlc-bootstrap/inc-01-greenfield-git-and-sdlc-skills.md`

## 1. Increment Goal & Anti-Sprawl Gate Verification

Eliminate raw Linux/Windows path guessing when creating or attaching Git repositories on local or remote hosts (such as `Spark` at `192.168.1.218:8080`), and give both the Web UI and Companion's tools an identical **3-Layer Workspace Mental Model**:
1. **Layer 1 — Pinned Projects Root:** User-configurable parent directory persisted in SQLite `user_profile['projects_root']` (defaulting to `COMPANION_PROJECTS_DIR` -> auto-detected `settings.default_projects_dir`, e.g., `/home/spark/projects` on packaged Linux or `D:\Projects` locally).
2. **Layer 2 — Confined Relative Directory Browser:** Visual and tool-callable directory explorer strictly confined under `projects_root` (`resolved.is_relative_to(projects_root)`), supporting breadcrumb/`↑ Up` navigation, `+ New Folder` organizational subfolders (without premature `git init`), rich `[Git Repo · branch]` / `[Folder]` / `[Linked Project]` badges, leading-slash sanitization on relative repo names, and 1-click clone selection.
3. **Layer 3 — Active Scoped Project:** Mechanical `WorkspaceContext` bound to the initialized or attached repository.

### Pre-Flight Anti-Sprawl Gate Audit
1. **Gate 1 — Conceptual Compression:** Zero new `companion/*.py` modules (`25/25` Ring-0 module cap preserved), zero new SQLite tables (`user_profile['projects_root']` in existing `MemoryStore` + existing `projects` table), and zero new top-level `TOOLS` (`18 < 20` tool schema budget preserved by extending `ops_manage(domain="projects")` and `agile_manage`).
2. **Gate 2 — Single Source of Truth & `F5` / Restart Invariance:** `user_profile['projects_root']` in SQLite `conversations.db` is the single canonical override; `get_projects_root(store)` and `settings.projects_dir` resolve identically across Python restarts and browser `F5` reloads. Invalid or stale DB overrides fail closed back to `settings.default_projects_dir`.
3. **Gate 3 — Full-Stack 5-Hop Seam Trace:** `ProjectConfigPanel.tsx` (`Change Root`, `ProjectsFolderExplorer`, `New Repo Folder Name`, 1-click `Attach`) & `SettingsModal.tsx` (`Projects Root Directory`) -> `/api/actions/agile` & `/api/status` -> `set_projects_root` / `browse_projects_dir` / `create_projects_subfolder` -> `build_system_prompt` (`[RUNTIME & GOVERNANCE]` `projects_root=...`) + `ops_manage(domain="projects")` -> immediate UI badge, live path preview, and next-turn LLM awareness.
4. **Gate 4 — Human Mental Model & Progressive Disclosure:** Everyday Personal/Goal Boards keep zero Git clutter; Code Repository mode leads with the visual `Projects Root` explorer and clean relative folder name, while tucking *"Custom path outside Projects Root"* inside a collapsible `<details>` disclosure. Frontend logic splits cleanly between `ProjectConfigPanel.tsx` and `ProjectsFolderExplorer.tsx` so every `.tsx` file stays well under `550` lines.

---

## Stage 4: Engineering Design

### 1. Project & Module File Layout

```text
companion/
├── __init__.py                 # Version bump to 5.1.1 (Step 4)
├── config.py                   # settings.default_projects_dir, runtime projects_root resolver hook & to_dict()
├── worktrees.py                # Hardened validate_external_repo_path (system dir & raw POSIX guards),
│                               # get_projects_root_info, set_projects_root, browse_projects_dir,
│                               # create_projects_subfolder, _fast_git_branch, agile subaction wiring
├── projects.py                 # Bind runtime projects_root resolver on settings; resolve relative repo paths via projects_root
├── context.py                  # Surface projects_root in [RUNTIME & GOVERNANCE]; /project root|browse|mkdir; exact-case switch fix
├── core_actions.py             # Expose projects_root & projects_dir across agile responses
├── approvals.py                # R0 classification for browse_projects / probe_repo & project_manage(browse)
└── tools.py                    # ops_manage(domain='projects', action='browse'|'mkdir'|'set_root'|'reset_root')
                                # + agile_manage schema enums/fields + phase-gate read-only allowance for browse
surfaces/
├── api_extensions.py           # Expose projects_root, projects_root_is_custom, default_projects_root in GET /api/status
└── web_server.py               # Mark browse_projects_dir read-only; refresh system prompt on set_projects_root
frontend/src/
├── types.ts                    # ProjectsExplorerEntry, ProjectsBrowseResult, WebStatus & WorkspaceInfo projects_root fields
├── settings/
│   └── SettingsModal.tsx       # Editable 'Projects Root Directory' row (Save / Reset Default) in General & Autonomy
└── surfaces/
    ├── ProjectsFolderExplorer.tsx # Reusable Pinned Root Header + Change Root editor + Breadcrumbs/Up/+New Folder + Entry list
    └── ProjectConfigPanel.tsx  # Create New Repo (relative folder name stripping '/' + live preview banner) & 1-click Attach
```

---

### 2. Entry Surface & Input Contracts

#### 2.1 `projects_root` Resolution & Mutation (`set_projects_root` / `GET /api/status`)
| Name / Field | Type | Required | Default | Validation, Normalization & Edge-Case Rules |
| :--- | :--- | :---: | :--- | :--- |
| `projects_root` (`path` / `target`) | `str` | Cond. | `""` | Trimmed string. Expands `~`; relative names resolve under `Path.home()`. Validated via `validate_external_repo_path(..., raw_input=raw)` before `mkdir(parents=True, exist_ok=True)`. Persisted in `user_profile['projects_root']`. |
| `reset` | `bool` | No | `False` | When `True` (or `projects_root` in `{"reset", "default"}`), clears `user_profile['projects_root']` and restores `COMPANION_PROJECTS_DIR` -> auto-detected `settings.default_projects_dir`. |
| Resolution Order | — | — | — | `1.` `user_profile['projects_root']` (if non-empty and passes `validate_external_repo_path`) -> `2.` `COMPANION_PROJECTS_DIR` env -> `3.` Auto-detected (`~/projects` on packaged Linux/AppData, `settings.root.parent` in local dev). |

#### 2.2 Confined Explorer & Subfolder API (`agile` / `ops_manage(domain="projects")`)
| Subaction / Action | Parameters | Mutates Disk? | Confinement & Validation Rules |
| :--- | :--- | :---: | :--- |
| `browse_projects_dir` (`browse_projects` / `browse`) | `rel_path: str = ""` | No (`R0`) | Joins `(projects_root / rel_path).resolve()` (or accepts exact subpath of `projects_root`); rejects any `..` escape, absolute path outside `projects_root`, or symlink resolving outside `projects_root` with `SAFETY GUARD REJECTED`. If `projects_root` does not exist yet at `rel_path=""`, returns `ok: True, exists: False, entries: []`. |
| `create_projects_subfolder` (`create_subfolder` / `mkdir`) | `rel_path: str = ""`, `parent_rel_path: str = ""`, `name: str = ""` | Yes (`mkdir -p` only, no `.git`) | Strips leading/trailing whitespace and accidental leading/trailing `/` or `\`; rejects empty names, `.` or `..` segments, dot-prefixed hidden folders (`.git`, `.ssh`, `.env`), noise dirs (`node_modules`), invalid chars (`<>:"\|?*`), or targets escaping `projects_root`. |
| `set_projects_root` (`set_root` / `reset_projects_root`) | `projects_root: str`, `reset: bool = False` | Yes (`mkdir -p` + SQLite) | Rejects `/`, `$HOME`, `/home`/`C:\Users`, `app/`, `data/`, `~/.local/share/companion`, `.ssh`/`.gnupg`, POSIX system trees (`/etc`, `/usr`, `/var`, `/bin`, `/sbin`, `/lib`, `/proc`, `/sys`, `/dev`, `/boot`, `/root`, except OS `tempfile.gettempdir()`), and Windows system trees (`C:\Windows`, `C:\Program Files*`, `C:\ProgramData`). |

#### 2.3 Companion Tool & Slash Command Parity
| Surface | Invocation | Behavior |
| :--- | :--- | :--- |
| `ops_manage` | `domain="projects", action="browse", target="[rel_path]"` | Lists folders & Git repos inside `projects_root[/rel_path]` even when `WorkspaceContext` is locked to `companion-self` (`app/`). Permitted in all workflow phases (`R0`). |
| `ops_manage` | `domain="projects", action="mkdir", target="<rel_path>"` | Creates organizational subfolder(s) under `projects_root` without initializing Git (`R1`). |
| `ops_manage` | `domain="projects", action="set_root", target="<path\|reset>"` | Validates, `mkdir -p`s, and pins `user_profile['projects_root']` (or resets to default) (`R1`). |
| `ops_manage` | `domain="projects", action="init" \| "switch" \| "status"` | Bootstraps greenfield Git repo under `<projects_root>/<target>`, switches workspace focus (preserving exact case on Linux), or prints status including `Projects Root`. |
| `/project` slash | `/project root [path\|reset]`, `/project browse [rel_path]`, `/project mkdir <rel_path>` | Deterministic zero-LLM CLI & Web Chat slash parity updating `messages[0]` system prompt immediately. |

---

### 3. Core Domain Contracts & Data Models

#### 3.1 Backend `browse_projects_dir` Response Contract
```python
{
    "ok": True,
    "projects_root": str,            # Resolved pinned or default Projects Root
    "projects_dir": str,             # Mirror of projects_root for backward compatibility
    "default_projects_root": str,    # Underlying env/auto fallback path
    "is_custom_root": bool,          # True when user_profile['projects_root'] override is active
    "root_source": "user_profile" | "env" | "auto",
    "exists": bool,                  # Whether current_abs_path exists on disk yet
    "current_rel_path": str,         # "" at Projects Root, else POSIX path e.g. "experiments/agents"
    "current_abs_path": str,         # Resolved directory path being listed
    "parent_rel_path": str,          # "" when at root or depth 1, else parent POSIX rel_path
    "can_go_up": bool,               # resolved != projects_root
    "breadcrumbs": list[{
        "label": str,                # "Root" for index 0, folder segment name thereafter
        "rel_path": str,             # "" for Root, cumulative POSIX rel_path thereafter
    }],
    "entries": list[{
        "name": str,                 # Directory basename (case-preserved)
        "rel_path": str,             # POSIX path relative to projects_root
        "abs_path": str,             # Full resolved path on host
        "is_git_repo": bool,         # True iff (entry / ".git").exists()
        "branch": str,               # Fast-parsed HEAD branch (e.g. "main") when is_git_repo else ""
        "has_children": bool,        # True iff >=1 visible non-noise subdirectory exists
        "linked_project_id": str,    # Active Companion project_id bound to abs_path, or ""
        "linked_project_title": str, # Active Companion project title, or ""
    }],
    "summary": str,
}
```

#### 3.2 `ProjectConfigPanel.tsx` State & Path Derivation Rules
1. **Sanitizing Relative Repo Folder Names (`sanitizeRelativeRepoSegment`):**
   - Strips leading `/` and `\` characters immediately on input (`"/my-app"` -> `"my-app"`, `"\\sub\\my-app"` -> `"sub/my-app"`).
   - Collapses duplicate slashes and rejects `..` segments.
   - Clearing `New Repo Folder Name` back to `""` resets `folderManuallyEdited = false` so editing **Project / Goal Title** immediately resumes auto-deriving `<slug>`.
2. **Host-Aware Path Joining (`joinHostPath`):**
   - Detects separator from `projectsRoot` (`"\\"` when `projectsRoot` uses Windows backslashes, `"/"` on Linux/POSIX `Spark`), joining `[projectsRoot, currentRelPath, repoFolderName]` cleanly without double separators.
3. **Mode-Specific Explorer Click Transitions:**
   - **`Create New Repo` mode:**
     - Clicking `[Folder]` -> drills explorer into `entry.rel_path` (`currentRelPath = entry.rel_path`), updating live preview banner: `Will initialize Git repo on main at: <projectsRoot>/<currentRelPath>/<slug>`.
     - Clicking `[Git Repo · branch]` -> populates `projWsPath` with `entry.abs_path` and probes it so the user sees it is already initialized (and can either attach it or re-use idempotently).
   - **`Attach Existing Clone` mode:**
     - Clicking `[Git Repo · branch]` -> 1-click selects `entry.abs_path`, highlights the row, and runs `probe_repo` immediately to display branch/clean/stack badge.
     - Clicking `[Folder]` -> drills explorer into `entry.rel_path` to locate nested clones.
     - Clicking a `[Git Repo]`'s optional drill-down button (when `entry.has_children` is true) -> drills inside without losing 1-click row selection.

---

### 4. Production Hardening: Invariants, Noise Filters & Idempotency Matrix

#### 4.1 Security & Hygiene Invariants
- **Hard Containment Barrier (`resolved.is_relative_to(projects_root)` & `disallow_projects_root`):** `browse_projects_dir`, `create_projects_subfolder`, and relative repo paths in `init_greenfield_repo` / `create_project` / `update_project` / `probe_repo_workspace` / `manage_active_project` resolve symlinks via `.resolve()` prior to enforcing `.is_relative_to(projects_root)`. Layer-3 repo operations also pass `disallow_projects_root=True` so `projects_root` itself can never be `git init`ed or bound as a single repository.
- **Cross-Platform System Directory Blocklist:** `validate_external_repo_path()` rejects `/`, Drive roots (`C:\`, `D:\`), `$HOME`, `$HOME.parent` (`/home`, `/Users`, `C:\Users`), `tempfile.gettempdir()` directly, Companion `app/` & `data/`, `.ssh`/`.gnupg`, POSIX system roots (`/etc`, `/usr`, `/var`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/boot`, `/dev`, `/proc`, `/sys`, `/run`, `/root`, while permitting `tempfile.gettempdir()` subtrees for isolated test runs), and Windows system roots (`C:\Windows`, `C:\Program Files`, `C:\Program Files (x86)`, `C:\ProgramData`).
- **Collision-Free Sibling & Nested Repo Identity:** `_persist_workspace_selection` disambiguates `project_id` across nested subfolders (`experiments/my-app` -> `my-app`, `products/my-app` -> `products-my-app`) and case-sensitive siblings (`Agent-OS` vs `agent-os`) without overwriting existing `workspace_path` bindings.
- **Bidirectional `Projects Root` UI Sync:** `App.tsx` propagates `status.projects_root` into `ProjectConfigPanel.tsx`, and inline `Change Root` triggers `onSaved()` so `SettingsModal.tsx` and open `ProjectConfigPanel` drawers stay synchronized immediately.
- **Zero-Subprocess Branch Inspection (`_fast_git_branch`):** Reads `.git/HEAD` (and `gitdir:` worktree/submodule `HEAD`) directly in pure Python in `<0.1ms` per directory, keeping `browse_projects_dir` fast (`<10ms`) across dozens of repos.
- **Explorer Noise Filter:** Excludes `.git`, `.worktrees`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build`, `coverage`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.companion_backups`, `companion-data`, all dot-prefixed hidden directories (`.*`), and Companion's own `settings.root` / `settings.effective_data_dir`.

#### 4.2 State Transition & Idempotency Matrix
| Observed State | User / Agent Action | Deterministic Outcome |
| :--- | :--- | :--- |
| `projects_root` unset in `user_profile` | `GET /api/status` or `browse_projects_dir("")` | Uses `COMPANION_PROJECTS_DIR` -> `settings.default_projects_dir` (`is_custom_root=False`). |
| `projects_root` target does not exist yet | `set_projects_root("/home/spark/dev")` | Passes `validate_external_repo_path`, creates directory (`mkdir -p`), saves in `user_profile['projects_root']`. |
| `projects_root` target is `/`, `$HOME`, `/etc`, `app/`, or `data/` | `set_projects_root(...)` via UI, `/project root`, or `ops_manage` | Fails closed with `SAFETY GUARD REJECTED` prior to `mkdir` or SQLite write; existing root unchanged. |
| Corrupt/manual invalid path in `user_profile['projects_root']` | Server boot or `get_projects_root()` | Detects validation failure and falls back cleanly to `settings.default_projects_dir`. |
| Subfolder already exists on disk | `create_projects_subfolder("experiments")` | Idempotent `exist_ok=True` (`created=False`); never initializes `.git` or overwrites files. |
| User types `/my-service` in `New Repo Folder Name` | `ProjectConfigPanel` input change | Strips leading `/`, keeps relative `"my-service"`, previews `<projects_root>/<parent>/my-service`. |

---

### 5. Error Taxonomy & Observability

| Failure Category | Signal / Status | Safe to Retry? | Handling |
| :--- | :---: | :---: | :--- |
| **Path Escape / Protected Root** | `SAFETY GUARD REJECTED: ...` / HTTP `400` | No | Displayed inline in UI toast/badge or returned to LLM tool with actionable guidance. |
| **Missing Browse Subfolder** | `{"ok": False, "error": "Directory ..."}` / HTTP `400` | Yes | UI resets explorer to `projects_root` (`rel_path=""`). |
| **Unreadable Child Folder (`PermissionError`)** | Suppressed per-entry (`OSError`) | — | Skips unreadable child metadata without failing the parent directory listing. |
| **Secret Redaction** | `VaultManager.scrub_output` | — | Applied automatically to all CoreAction JSON payloads and tool outputs. |
