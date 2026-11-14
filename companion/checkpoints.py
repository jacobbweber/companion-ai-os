"""
Workspace Checkpoints, Per-File Backups & Subprocess Syntax Recovery (`companion/checkpoints.py` — Domain Layer).

Extracted from `companion/safety.py` to keep safety & checkpoint responsibilities modular
and well within simplicity budgets while supporting `ADR-007` multi-project workspaces.
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.config import SELF_PROJECT_ID, SKIP_DIRS, settings
from companion.disciplines import GENERAL_TRACKED_EXTENSIONS, verify_repository_syntax


class CheckpointManager:
    """Manages per-file backups and full workspace checkpoints for instant self-recovery."""

    SKIP_DIRS = SKIP_DIRS
    TRACKED_EXTENSIONS = set(GENERAL_TRACKED_EXTENSIONS)

    def __init__(self, root: Optional[Path] = None, backup_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.root = (root or settings.root).resolve()
        self.backup_dir = (backup_dir or (settings.backup_dir if self.root == settings.root.resolve() else self.root / ".companion_backups")).resolve()
        self._store = store
        self.file_backups_dir = self.backup_dir / "files"
        self.checkpoints_dir = self.backup_dir / "checkpoints"
        self.file_backups_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def _get_store(self) -> Optional[Any]:
        if self._store is False:
            return None
        if self._store is not None:
            return self._store
        ws = settings.get_workspace()
        if self.root in (settings.root.resolve(), ws.project_root):
            from companion.memory_store import get_memory_store

            return get_memory_store()
        return None

    def _safe_rel_key(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root)
            return str(rel).replace("\\", "__").replace("/", "__")
        except ValueError:
            return path.name

    def backup_file(self, path: Path) -> Optional[Path]:
        """Create a timestamped backup of a single file before modifying it."""
        target = path.resolve()
        if not target.is_file() or not target.is_relative_to(self.root):
            return None
        rel_key = self._safe_rel_key(target)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest_dir = self.file_backups_dir / rel_key
        dest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = dest_dir / f"{stamp}_{target.name}"
        shutil.copy2(target, backup_path)

        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime)
        while len(backups) > settings.max_backups_per_file:
            oldest = backups.pop(0)
            try:
                oldest.unlink()
            except Exception:
                pass
        return backup_path

    def restore_latest_file_backup(self, path: Path) -> Optional[Path]:
        """Restore the most recent backup of a file."""
        target = path.resolve()
        rel_key = self._safe_rel_key(target)
        dest_dir = self.file_backups_dir / rel_key
        if not dest_dir.is_dir():
            return None
        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime)
        if not backups:
            return None
        latest = backups[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, target)
        return latest

    def _iter_workspace_files(self) -> List[Path]:
        out: List[Path] = []
        if not self.root.is_dir():
            return out
        for cur_r, dirs, fnames in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in self.SKIP_DIRS)
            out.extend(Path(cur_r) / fn for fn in sorted(fnames))
        return out

    def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]:
        """Snapshot all tracked code, spec, and documentation files in the workspace."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_label = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in label)[:40]
        cp_id = f"cp_{stamp}_{safe_label}"
        cp_dir = self.checkpoints_dir / cp_id
        files_dir = cp_dir / "workspace"
        files_dir.mkdir(parents=True, exist_ok=True)

        manifest_files: List[Dict[str, str]] = []
        for item in self._iter_workspace_files():
            if item.suffix.lower() not in self.TRACKED_EXTENSIONS and item.name not in (".env", ".gitignore", ".env.example"):
                continue
            rel_path = item.relative_to(self.root)
            dest = files_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            sha = hashlib.sha256(item.read_bytes()).hexdigest()
            manifest_files.append({"path": str(rel_path).replace("\\", "/"), "sha256": sha})

        store = self._get_store()
        manifest: Dict[str, Any] = {
            "checkpoint_id": cp_id,
            "label": label,
            "created_at": datetime.now().isoformat(),
            "file_count": len(manifest_files),
            "files": manifest_files,
        }
        if store is not None and hasattr(store, "export_state_snapshot"):
            snap_pid = SELF_PROJECT_ID if self.root == settings.root.resolve() else None
            manifest["db_snapshot"] = store.export_state_snapshot(project_id=snap_pid)
        (cp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        all_cps = sorted(
            [d for d in self.checkpoints_dir.iterdir() if d.is_dir() and (d / "manifest.json").is_file()],
            key=lambda d: d.stat().st_mtime,
        )
        while len(all_cps) > 15:
            shutil.rmtree(all_cps.pop(0), ignore_errors=True)
        return manifest

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all available workspace checkpoints, newest first."""
        results: List[Dict[str, Any]] = []
        if not self.checkpoints_dir.is_dir():
            return results
        for cp_dir in sorted(self.checkpoints_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            manifest_path = cp_dir / "manifest.json"
            if manifest_path.is_file():
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    results.append({
                        "checkpoint_id": data.get("checkpoint_id", cp_dir.name),
                        "label": data.get("label", ""),
                        "created_at": data.get("created_at", ""),
                        "file_count": data.get("file_count", 0),
                    })
                except Exception:
                    continue
        return results

    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """Restore workspace files and reversible SQLite state from a checkpoint while recording a rollback lesson."""
        if checkpoint_id is None:
            cps = self.list_checkpoints()
            if not cps:
                return {"restored": False, "error": "No checkpoints available to rollback."}
            checkpoint_id = cps[0]["checkpoint_id"]

        cp_dir = self.checkpoints_dir / checkpoint_id
        manifest_path = cp_dir / "manifest.json"
        files_dir = cp_dir / "workspace"
        if not manifest_path.is_file() or not files_dir.is_dir():
            return {"restored": False, "error": f"Checkpoint '{checkpoint_id}' not found."}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored_files: List[str] = []
        manifest_paths = {entry["path"] for entry in manifest.get("files", [])}
        for entry in manifest.get("files", []):
            rel = Path(entry["path"])
            src = files_dir / rel
            dst = self.root / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored_files.append(entry["path"])

        removed_files: List[str] = []
        for item in self._iter_workspace_files():
            if item.suffix.lower() not in self.TRACKED_EXTENSIONS and item.name not in (".gitignore", ".env.example"):
                continue
            rel_str = str(item.relative_to(self.root)).replace("\\", "/")
            if rel_str not in manifest_paths:
                try:
                    item.unlink()
                    removed_files.append(rel_str)
                except Exception:
                    pass

        store = self._get_store()
        if "db_snapshot" in manifest and store is not None and hasattr(store, "restore_state_snapshot"):
            lesson = reason or f"Rolled back workspace to checkpoint `{checkpoint_id}` ({manifest.get('label', 'manual')})."
            store.restore_state_snapshot(manifest["db_snapshot"], rollback_reason=lesson)

        return {
            "restored": True,
            "checkpoint_id": checkpoint_id,
            "label": manifest.get("label", ""),
            "restored_count": len(restored_files),
            "restored_files": restored_files,
            "removed_files": removed_files,
        }

    def snapshot_py_files(self) -> Dict[Path, str]:
        """Take an in-memory snapshot of all repository .py files before subprocess execution."""
        snap: Dict[Path, str] = {}
        for item in self._iter_workspace_files():
            if item.suffix.lower() == ".py":
                try:
                    snap[item.resolve()] = item.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        return snap

    def guard_py_files_after_exec(self, pre_snapshot: Dict[Path, str]) -> Optional[str]:
        """Verify repository .py syntax after a subprocess; auto-restore any file corrupted with SyntaxError."""
        syntax_ok, results = verify_repository_syntax(self.root)
        if syntax_ok:
            return None
        healed: List[str] = []
        for res in results:
            if not res.valid_syntax:
                p = Path(res.path).resolve()
                if p in pre_snapshot:
                    p.write_text(pre_snapshot[p], encoding="utf-8")
                elif p.is_file():
                    p.unlink(missing_ok=True)
                healed.append(f"{p.name} ({res.syntax_error})")
        return (
            f"SYNTAX GUARD REJECTED subprocess file modification: {', '.join(healed)}. "
            "Corrupted Python file(s) were automatically restored. Use `edit_file` or `write_file` instead."
        )

    _RO_SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"}
    _RO_SKIP_RELS = {"MEMORY.md", "IDENTITY.md", "secrets.env"}

    def snapshot_readonly_tree(self) -> Dict[str, bytes]:
        """Snapshot raw bytes of tracked files under `self.root` before read-only-locked subprocess execution (`ADR-007`/`ADR-019`)."""
        snap: Dict[str, bytes] = {}
        for item in self._iter_workspace_files():
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel in self._RO_SKIP_RELS or rel.startswith("data/") or item.suffix.lower() in self._RO_SKIP_SUFFIXES:
                continue
            try:
                snap[rel] = item.read_bytes()
            except Exception:
                continue
        return snap

    def enforce_readonly_tree(self, baseline: Dict[str, bytes]) -> Optional[str]:
        """Detect and immediately revert any subprocess modification, creation, or deletion inside `self.root` (`ADR-007`/`ADR-019`)."""
        reverted: List[str] = []
        current_rels: set[str] = set()
        for item in self._iter_workspace_files():
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel in self._RO_SKIP_RELS or rel.startswith("data/") or item.suffix.lower() in self._RO_SKIP_SUFFIXES:
                continue
            current_rels.add(rel)
            if rel not in baseline:
                try:
                    item.unlink(missing_ok=True)
                except Exception:
                    pass
                reverted.append(f"{rel} (deleted unauthorized file)")
            else:
                try:
                    if item.read_bytes() != baseline[rel]:
                        item.write_bytes(baseline[rel])
                        reverted.append(f"{rel} (restored modified file)")
                except Exception:
                    pass
        for rel, orig_bytes in baseline.items():
            if rel not in current_rels:
                dst = self.root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig_bytes)
                reverted.append(f"{rel} (restored deleted file)")
        if not reverted:
            return None
        why = (
            "because `Self-Dev Mode` is OFF (`ADR-019`)"
            if settings.get_workspace().is_self_project
            else "while working on an external project (`ADR-007`)"
        )
        return (
            f"SAFETY GUARD REJECTED subprocess mutation: Companion core (`{self.root}`) is mechanically locked READ-ONLY "
            f"{why}. Reverted {len(reverted)} unauthorized change(s): {', '.join(reverted[:5])}."
        )

    def _run_git(self, args: List[str], timeout: int = 20) -> Tuple[int, str]:
        """Run a local non-interactive `git` subprocess inside `self.root`."""
        if shutil.which("git") is None:
            return -1, "git executable not found on PATH"
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"

    def create_git_savepoint(self, message: str = "Verified save-point", init_if_needed: bool = False) -> Dict[str, Any]:
        """Stage and commit verified workspace changes as a local Git save-point (never pushes or stages `.env`)."""
        if shutil.which("git") is None:
            return {"committed": False, "reason": "git_unavailable", "summary": "Git not installed; skipped Git save-point."}
        if not (self.root / ".git").exists():
            if not init_if_needed:
                return {"committed": False, "reason": "not_a_git_repo", "summary": "Workspace is not a Git repository; skipped Git save-point."}
            rc_init, out_init = self._run_git(["init"])
            if rc_init != 0:
                return {"committed": False, "reason": "git_init_failed", "summary": f"Git init failed: {out_init[:160]}"}
            gi = self.root / ".gitignore"
            if not gi.exists():
                gi.write_text("__pycache__/\n*.py[cod]\n.env\nsecrets.env\n*.db\n*.db-wal\n*.db-shm\n.companion_backups/\n", encoding="utf-8")

        self._run_git(["add", "-A"])
        rc_cached, cached_out = self._run_git(["diff", "--cached", "--name-only"])
        staged = [ln.strip() for ln in cached_out.splitlines() if ln.strip()] if rc_cached == 0 else []
        blocked = [
            p for p in staged
            if Path(p).name.lower() in (".env", "secrets.env")
            or Path(p).name.lower().endswith((".db", ".db-wal", ".db-shm", ".sqlite"))
            or ".companion_backups" in Path(p).parts
        ]
        if blocked:
            self._run_git(["reset", "HEAD", "--", *blocked])
            staged = [p for p in staged if p not in blocked]
        if not staged:
            return {"committed": False, "reason": "clean_worktree", "summary": "Git save-point: working tree already clean."}

        clean_msg = " ".join((message or "Verified save-point").splitlines()).strip()[:120]
        full_msg = clean_msg if clean_msg.startswith("[Companion]") else f"[Companion] {clean_msg}"
        rc_c, out_c = self._run_git(["commit", "-m", full_msg])
        if rc_c != 0 and any(k in out_c for k in ("user.email", "user.name", "Please tell me who you are")):
            self._run_git(["config", "user.name", "Companion AI OS"])
            self._run_git(["config", "user.email", "companion@local"])
            rc_c, out_c = self._run_git(["commit", "-m", full_msg])
        if rc_c != 0:
            return {"committed": False, "reason": "commit_failed", "summary": f"Git save-point skipped: {out_c[:180]}"}

        _, sha = self._run_git(["rev-parse", "--short", "HEAD"])
        _, branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        sha_s, br_s = sha.strip(), branch.strip() or "HEAD"
        return {
            "committed": True,
            "commit": sha_s,
            "branch": br_s,
            "files_committed": len(staged),
            "message": full_msg,
            "summary": f"Git save-point committed: `{sha_s}` on `{br_s}` ({len(staged)} file(s)) — {full_msg}",
        }

    def list_git_savepoints(self, limit: int = 10) -> List[Dict[str, str]]:
        """Return recent local Git commits in `self.root`, newest first."""
        if not (self.root / ".git").exists():
            return []
        rc, out = self._run_git(["log", f"-n{max(1, min(int(limit), 50))}", "--pretty=format:%h|%cs|%s"])
        if rc != 0 or not out:
            return []
        entries: List[Dict[str, str]] = []
        for ln in out.splitlines():
            parts = ln.split("|", 2)
            if len(parts) == 3:
                entries.append({"commit": parts[0].strip(), "date": parts[1].strip(), "subject": parts[2].strip()})
        return entries


checkpoint_manager = CheckpointManager()
_EXT_CP_CACHE: Dict[str, CheckpointManager] = {}
_EXT_CP_LOCK = threading.Lock()


def get_self_checkpoint_manager() -> CheckpointManager:
    """Return the `CheckpointManager` bound to Companion's `self_root` (`ADR-007`/`ADR-019`)."""
    ws = settings.get_workspace()
    if ws.self_root == checkpoint_manager.root:
        return checkpoint_manager
    key = f"self::{ws.self_root}"
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.self_root, backup_dir=ws.self_root / "data" / ".companion_backups")
        return _EXT_CP_CACHE[key]


def get_active_checkpoint_manager() -> CheckpointManager:
    """Return the `CheckpointManager` bound to the active `WorkspaceContext` (`companion-self` or external project)."""
    ws = settings.get_workspace()
    if ws.is_self_project:
        return get_self_checkpoint_manager()
    key = str(ws.project_root)
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.project_root, backup_dir=ws.backup_dir)
        return _EXT_CP_CACHE[key]
