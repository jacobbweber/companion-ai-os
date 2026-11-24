"""
External Copy-on-Write Checkpoints, Content-Addressed Blobs & Subprocess Guards (`companion/checkpoints.py` — Domain Layer).

Stores checkpoints outside user repositories under `effective_data_dir / "checkpoints" / <project_id>`, tracks
Companion-touched files in a Copy-on-Write ledger so `rollback_checkpoint` never deletes untracked human files,
and guards read-only trees via an O(1)-RAM `(mtime_ns, size)` stat cache.
"""

import ast
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.config import GENERAL_TRACKED_EXTENSIONS, SELF_PROJECT_ID, SKIP_DIRS, settings, slugify_project_id


def _check_py_file_syntax(path: Path) -> Tuple[bool, Optional[str]]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
        compile(tree, filename=str(path), mode="exec")
        return True, None
    except SyntaxError as exc:
        return False, f"SyntaxError L{exc.lineno}: {exc.msg}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_secret_env_name(name: str) -> bool:
    low = name.lower()
    return low in (".env", "secrets.env") or (low.startswith(".env.") and low not in (".env.example", ".env.sample", ".env.template"))


class CheckpointManager:
    """Manages Copy-on-Write file ledgers, external content-addressed checkpoints, and read-only tree guards."""

    SKIP_DIRS = SKIP_DIRS
    TRACKED_EXTENSIONS = set(GENERAL_TRACKED_EXTENSIONS)
    _RO_SKIP_SUFFIXES = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"}
    _RO_SKIP_RELS = {"MEMORY.md", "IDENTITY.md", "secrets.env"}

    def __init__(self, root: Optional[Path] = None, backup_dir: Optional[Path] = None, store: Optional[Any] = None):
        self.root = (root or settings.root).resolve()
        pid = SELF_PROJECT_ID if self.root == settings.root.resolve() else slugify_project_id(self.root.name)
        self.backup_dir = (Path(backup_dir) if backup_dir is not None else (settings.effective_data_dir / "checkpoints" / pid)).resolve()
        self._store = store
        self.file_backups_dir = self.backup_dir / "files"
        self.checkpoints_dir = self.backup_dir / "checkpoints"
        self.blobs_dir = self.backup_dir / "blobs"
        self.ledger_path = self.backup_dir / "touched_ledger.jsonl"
        self._ledger_lock = threading.RLock()
        self._ro_stat_cache: Dict[str, Tuple[int, int, str]] = {}
        self._savepoints_cache: Optional[Tuple[Tuple[float, float, int], List[Dict[str, str]]]] = None

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
        rel = self._rel_posix(path)
        return rel.replace("/", "__") if rel else path.name

    def _rel_posix(self, path: Path) -> Optional[str]:
        try:
            return str(path.resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return None

    def _store_blob(self, data: bytes) -> str:
        sha = _sha256_bytes(data)
        blob_path = self.blobs_dir / sha[:2] / sha
        if not blob_path.is_file():
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(data)
        return sha

    def _read_blob(self, sha: str) -> Optional[bytes]:
        blob_path = (self.blobs_dir / sha[:2] / sha) if sha else None
        return blob_path.read_bytes() if (blob_path and blob_path.is_file()) else None

    def _load_ledger(self) -> List[Dict[str, Any]]:
        if not self.ledger_path.is_file():
            return []
        entries: List[Dict[str, Any]] = []
        with self._ledger_lock:
            for ln in self.ledger_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if ln.strip():
                    with contextlib.suppress(Exception):
                        entries.append(json.loads(ln))
        return entries

    def record_file_touch(
        self, path: Path, existed_before: bool, before_bytes: Optional[bytes], after_bytes: Optional[bytes], op: str = "write_file",
    ) -> Optional[Dict[str, Any]]:
        """Record a Copy-on-Write ledger entry when Companion creates or modifies `path`."""
        rel = self._rel_posix(path)
        if not rel or _is_secret_env_name(path.name):
            return None
        with self._ledger_lock:
            before_sha = self._store_blob(before_bytes) if (existed_before and before_bytes is not None) else ""
            after_sha = self._store_blob(after_bytes) if after_bytes is not None else ""
            entry = {
                "seq": len(self._load_ledger()) + 1, "ts": datetime.now().isoformat(), "rel_path": rel,
                "existed_before": bool(existed_before), "before_sha256": before_sha, "after_sha256": after_sha, "op": op,
            }
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
            return entry

    def backup_file(self, path: Path) -> Optional[Path]:
        """Create a timestamped backup of a single file before modifying it."""
        target = path.resolve()
        if not target.is_file() or not target.is_relative_to(self.root) or _is_secret_env_name(target.name):
            return None
        dest_dir = self.file_backups_dir / self._safe_rel_key(target)
        dest_dir.mkdir(parents=True, exist_ok=True)
        backup_path = dest_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{target.name}"
        shutil.copy2(target, backup_path)
        self._store_blob(target.read_bytes())
        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime)
        while len(backups) > settings.max_backups_per_file:
            with contextlib.suppress(Exception):
                backups.pop(0).unlink()
        return backup_path

    def restore_latest_file_backup(self, path: Path) -> Optional[Path]:
        """Restore the most recent backup of a file."""
        target = path.resolve()
        dest_dir = self.file_backups_dir / self._safe_rel_key(target)
        backups = sorted(dest_dir.glob(f"*_{target.name}"), key=lambda p: p.stat().st_mtime) if dest_dir.is_dir() else []
        if not backups:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backups[-1], target)
        return backups[-1]

    def _iter_workspace_files(self) -> List[Path]:
        out: List[Path] = []
        if not self.root.is_dir():
            return out
        for cur_r, dirs, fnames in os.walk(self.root):
            cur_p = Path(cur_r)
            dirs[:] = sorted(d for d in dirs if d not in self.SKIP_DIRS and not (cur_p / d).is_symlink())
            for fn in sorted(fnames):
                cand = cur_p / fn
                if cand.is_symlink():
                    try:
                        if not cand.resolve().is_relative_to(self.root):
                            continue
                    except OSError:
                        continue
                out.append(cand)
        return out

    def _is_trackable_file(self, item: Path) -> bool:
        return not _is_secret_env_name(item.name) and (
            item.suffix.lower() in self.TRACKED_EXTENSIONS or item.name in (".gitignore", ".env.example", "Dockerfile", "Makefile")
        )

    def create_checkpoint(self, label: str = "auto", paths: Optional[List[Path]] = None) -> Dict[str, Any]:
        """Create a content-addressed Copy-on-Write checkpoint + ledger watermark (never `.env` or `secrets.env`)."""
        safe_label = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in label)[:40]
        cp_id = f"cp_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_label}"
        cp_dir = self.checkpoints_dir / cp_id
        cp_dir.mkdir(parents=True, exist_ok=True)

        candidates = [Path(p).resolve() for p in paths] if paths is not None else self._iter_workspace_files()
        manifest_files = [
            {"path": str(item.relative_to(self.root)).replace("\\", "/"), "sha256": self._store_blob(item.read_bytes())}
            for item in candidates if item.is_file() and item.is_relative_to(self.root) and self._is_trackable_file(item)
        ]
        manifest: Dict[str, Any] = {
            "checkpoint_id": cp_id, "label": label, "created_at": datetime.now().isoformat(),
            "ledger_seq": len(self._load_ledger()), "file_count": len(manifest_files), "files": manifest_files,
        }
        store = self._get_store()
        if store is not None and hasattr(store, "export_state_snapshot"):
            manifest["db_snapshot"] = store.export_state_snapshot(project_id=SELF_PROJECT_ID if self.root == settings.root.resolve() else None)
        (cp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        all_cps = sorted([d for d in self.checkpoints_dir.iterdir() if d.is_dir() and (d / "manifest.json").is_file()], key=lambda d: d.stat().st_mtime)
        while len(all_cps) > 15:
            shutil.rmtree(all_cps.pop(0), ignore_errors=True)
        return manifest

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all available workspace checkpoints, newest first."""
        results: List[Dict[str, Any]] = []
        if not self.checkpoints_dir.is_dir():
            return results
        for cp_dir in sorted(self.checkpoints_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            mf = cp_dir / "manifest.json"
            if mf.is_file():
                with contextlib.suppress(Exception):
                    d = json.loads(mf.read_text(encoding="utf-8"))
                    results.append({"checkpoint_id": d.get("checkpoint_id", cp_dir.name), "label": d.get("label", ""), "created_at": d.get("created_at", ""), "file_count": d.get("file_count", 0)})
        return results

    def _restore_entry_bytes(self, cp_dir: Path, rel_str: str, sha: str) -> Optional[bytes]:
        blob = self._read_blob(sha)
        if blob is not None:
            return blob
        legacy = (cp_dir / "workspace" / Path(rel_str)).resolve()
        return legacy.read_bytes() if (legacy.is_file() and legacy.is_relative_to((cp_dir / "workspace").resolve())) else None

    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "", force: bool = False) -> Dict[str, Any]:
        """Revert Companion-touched files & manifest baselines without ever deleting untracked human files."""
        if checkpoint_id is None:
            cps = self.list_checkpoints()
            if not cps:
                return {"restored": False, "error": "No checkpoints available to rollback."}
            checkpoint_id = cps[0]["checkpoint_id"]
        cp_dir = self.checkpoints_dir / checkpoint_id
        if not (cp_dir / "manifest.json").is_file():
            return {"restored": False, "error": f"Checkpoint '{checkpoint_id}' not found."}

        manifest = json.loads((cp_dir / "manifest.json").read_text(encoding="utf-8"))
        cp_seq = int(manifest.get("ledger_seq", 0))
        post_touches: Dict[str, Dict[str, Any]] = {}
        for item in self._load_ledger():
            rel_p = str(item.get("rel_path") or "") if int(item.get("seq", 0)) > cp_seq else ""
            if not rel_p:
                continue
            if rel_p not in post_touches:
                post_touches[rel_p] = {"existed_before": bool(item.get("existed_before")), "before_sha256": str(item.get("before_sha256") or ""), "after_sha256": str(item.get("after_sha256") or "")}
            else:
                post_touches[rel_p]["after_sha256"] = str(item.get("after_sha256") or "")

        restored_files, removed_files, skipped_user_modified = [], [], []
        manifest_map = {str(e["path"]): str(e.get("sha256") or "") for e in manifest.get("files", [])}
        for rel_str, target_sha in manifest_map.items():
            dst = (self.root / Path(rel_str)).resolve()
            if not dst.is_relative_to(self.root) or (dst.is_file() and _sha256_bytes(dst.read_bytes()) == target_sha):
                continue
            orig = self._restore_entry_bytes(cp_dir, rel_str, target_sha)
            if orig is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                restored_files.append(rel_str)

        for rel_str, touch in post_touches.items():
            dst = (self.root / Path(rel_str)).resolve()
            if rel_str in manifest_map or not dst.is_relative_to(self.root):
                continue
            if touch["existed_before"] and (orig := self._read_blob(touch["before_sha256"])) is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                restored_files.append(rel_str)
            elif not touch["existed_before"] and dst.is_file():
                if not force and touch["after_sha256"] and _sha256_bytes(dst.read_bytes()) != touch["after_sha256"]:
                    skipped_user_modified.append(rel_str)
                else:
                    with contextlib.suppress(Exception):
                        dst.unlink()
                        removed_files.append(rel_str)

        store = self._get_store()
        if "db_snapshot" in manifest and store is not None and hasattr(store, "restore_state_snapshot"):
            store.restore_state_snapshot(manifest["db_snapshot"], rollback_reason=reason or f"Rolled back workspace to checkpoint `{checkpoint_id}` ({manifest.get('label', 'manual')}).")
        return {
            "restored": True, "checkpoint_id": checkpoint_id, "label": manifest.get("label", ""),
            "restored_count": len(restored_files), "restored_files": restored_files,
            "removed_files": removed_files, "skipped_user_modified": skipped_user_modified,
        }

    def _stat_hash(self, item: Path, rel: str) -> Optional[Tuple[int, int, str]]:
        try:
            st = item.stat()
        except OSError:
            return None
        cached = self._ro_stat_cache.get(rel)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and (self.blobs_dir / cached[2][:2] / cached[2]).is_file():
            return cached
        sha = self._store_blob(item.read_bytes())
        entry = (st.st_mtime_ns, st.st_size, sha)
        self._ro_stat_cache[rel] = entry
        return entry

    def snapshot_py_files(self) -> Dict[Path, str]:
        """Take an in-memory snapshot of repository .py files (explicit helper for targeted recovery tests)."""
        snap: Dict[Path, str] = {}
        for item in self._iter_workspace_files():
            if item.suffix.lower() == ".py":
                with contextlib.suppress(Exception):
                    snap[item.resolve()] = item.read_text(encoding="utf-8", errors="replace")
        return snap

    def guard_py_files_after_exec(self, pre_snapshot: Dict[Path, str]) -> Optional[str]:
        """Verify modified/created .py files after a subprocess; auto-restore any file corrupted with SyntaxError."""
        healed: List[str] = []
        for item in self._iter_workspace_files():
            resolved = item.resolve()
            if item.suffix.lower() != ".py" or (resolved in pre_snapshot and pre_snapshot[resolved] == item.read_text(encoding="utf-8", errors="replace")):
                continue
            ok_syn, syn_err = _check_py_file_syntax(resolved)
            if not ok_syn:
                resolved.write_text(pre_snapshot[resolved], encoding="utf-8") if resolved in pre_snapshot else resolved.unlink(missing_ok=True)
                healed.append(f"{resolved.name} ({syn_err})")
        return f"SYNTAX GUARD REJECTED subprocess file modification: {', '.join(healed)}. Corrupted Python file(s) were automatically restored. Use `edit_file` or `write_file` instead." if healed else None

    def snapshot_py_stats(self) -> Dict[Path, Tuple[int, int, str]]:
        """Capture O(1)-RAM `(mtime_ns, size, sha256)` stats for `.py` files backed by `self.blobs_dir`."""
        stats: Dict[Path, Tuple[int, int, str]] = {}
        for item in self._iter_workspace_files():
            if item.suffix.lower() == ".py" and (sh := self._stat_hash(item.resolve(), self._rel_posix(item) or item.name)):
                stats[item.resolve()] = sh
        return stats

    def guard_py_stats_after_exec(self, pre_stats: Dict[Path, Tuple[int, int, str]]) -> Optional[str]:
        """Validate only `.py` files whose `(mtime_ns, size)` changed during a subprocess, restoring from disk blobs."""
        healed: List[str] = []
        for item in self._iter_workspace_files():
            resolved = item.resolve()
            if item.suffix.lower() != ".py":
                continue
            try:
                st = resolved.stat()
            except OSError:
                continue
            prev = pre_stats.get(resolved)
            if prev is not None and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
                continue
            ok_syn, syn_err = _check_py_file_syntax(resolved)
            if not ok_syn:
                orig = self._read_blob(prev[2]) if prev is not None else None
                resolved.write_bytes(orig) if orig is not None else resolved.unlink(missing_ok=True)
                healed.append(f"{resolved.name} ({syn_err})")
        return f"SYNTAX GUARD REJECTED subprocess file modification: {', '.join(healed)}. Corrupted Python file(s) were automatically restored. Use `edit_file` or `write_file` instead." if healed else None

    def _iter_git_guard_files(self) -> List[Path]:
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return []
        cfg, hooks_dir = git_dir / "config", git_dir / "hooks"
        guarded = [cfg] if (cfg.is_file() and not cfg.is_symlink()) else []
        if hooks_dir.is_dir() and not hooks_dir.is_symlink():
            guarded.extend(hp for hp in sorted(hooks_dir.iterdir()) if hp.is_file() and not hp.is_symlink())
        return guarded

    def snapshot_readonly_tree(self) -> Dict[str, Any]:
        """Snapshot `(mtime_ns, size, sha256)` metadata under `self.root` backed by disk blobs (`~0 KB` per-call RAM)."""
        snap: Dict[str, Tuple[int, int, str]] = {}
        for item in [*self._iter_workspace_files(), *self._iter_git_guard_files()]:
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel not in self._RO_SKIP_RELS and not rel.startswith("data/") and item.suffix.lower() not in self._RO_SKIP_SUFFIXES:
                if (sh := self._stat_hash(item, rel)) is not None:
                    snap[rel] = sh
        return snap

    def enforce_readonly_tree(self, baseline: Dict[str, Any]) -> Optional[str]:
        """Detect and immediately revert any subprocess modification, creation, or deletion inside `self.root`."""
        reverted, current_rels = [], set()
        for item in [*self._iter_workspace_files(), *self._iter_git_guard_files()]:
            rel = str(item.relative_to(self.root)).replace("\\", "/")
            if rel in self._RO_SKIP_RELS or rel.startswith("data/") or item.suffix.lower() in self._RO_SKIP_SUFFIXES:
                continue
            current_rels.add(rel)
            if rel not in baseline:
                item.unlink(missing_ok=True)
                reverted.append(f"{rel} (deleted unauthorized file)")
                continue
            base_val = baseline[rel]
            with contextlib.suppress(OSError):
                if isinstance(base_val, bytes):
                    if item.read_bytes() != base_val:
                        item.write_bytes(base_val)
                        reverted.append(f"{rel} (restored modified file)")
                else:
                    b_mtime, b_size, b_sha = base_val
                    st = item.stat()
                    if (st.st_mtime_ns != b_mtime or st.st_size != b_size) and _sha256_bytes(item.read_bytes()) != b_sha and (orig := self._read_blob(b_sha)) is not None:
                        item.write_bytes(orig)
                        reverted.append(f"{rel} (restored modified file)")
        for rel, base_val in baseline.items():
            if rel not in current_rels and (orig := (base_val if isinstance(base_val, bytes) else self._read_blob(base_val[2]))) is not None:
                dst = self.root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(orig)
                reverted.append(f"{rel} (restored deleted file)")
        if not reverted:
            return None
        why = "permanently (`ADR-019` installed `app/` immutability)" if settings.get_workspace().is_self_project else "while working on an external project (`ADR-007`)"
        return f"SAFETY GUARD REJECTED subprocess mutation: Companion core (`{self.root}`) is mechanically locked READ-ONLY {why}. Reverted {len(reverted)} unauthorized change(s): {', '.join(reverted[:5])}."

    def _run_git(self, args: List[str], timeout: int = 20) -> Tuple[int, str]:
        if shutil.which("git") is None:
            return -1, "git executable not found on PATH"
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(self.root), env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return proc.returncode, ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        except Exception as exc:
            return -1, f"{type(exc).__name__}: {exc}"

    def create_git_savepoint(self, message: str = "Verified save-point", init_if_needed: bool = False, paths: Optional[List[str]] = None) -> Dict[str, Any]:
        """Stage Companion-touched paths and commit a local Git save-point (never auto-inits or runs `git add -A`)."""
        if shutil.which("git") is None:
            return {"committed": False, "reason": "git_unavailable", "summary": "Git not installed; skipped Git save-point."}
        freshly_inited = False
        if not (self.root / ".git").exists():
            if not init_if_needed:
                return {"committed": False, "reason": "not_a_git_repo", "summary": "Workspace is not a Git repository; skipped Git save-point."}
            rc_init, out_init = self._run_git(["init"])
            if rc_init != 0:
                return {"committed": False, "reason": "git_init_failed", "summary": f"Git init failed: {out_init[:160]}"}
            freshly_inited, gi = True, self.root / ".gitignore"
            if not gi.exists():
                gi.write_text("__pycache__/\n*.py[cod]\n.env\nsecrets.env\n*.db\n*.db-wal\n*.db-shm\n.companion_backups/\n", encoding="utf-8")

        stage_rels = list(dict.fromkeys(paths or [str(e.get("rel_path")) for e in self._load_ledger() if e.get("rel_path")]))
        if freshly_inited and not stage_rels:
            stage_rels = [str(p.relative_to(self.root)).replace("\\", "/") for p in self._iter_workspace_files() if self._is_trackable_file(p)]
        valid_rels = [
            r for r in stage_rels
            if not _is_secret_env_name(Path(r).name) and not Path(r).name.lower().endswith((".db", ".db-wal", ".db-shm", ".sqlite")) and ".companion_backups" not in Path(r).parts
        ]
        self._run_git(["add", "--", *valid_rels] if valid_rels else ["add", "-u"])
        rc_cached, cached_out = self._run_git(["diff", "--cached", "--name-only"])
        staged = [ln.strip() for ln in cached_out.splitlines() if ln.strip()] if rc_cached == 0 else []
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
        return {"committed": True, "commit": sha_s, "branch": br_s, "files_committed": len(staged), "message": full_msg, "summary": f"Git save-point committed: `{sha_s}` on `{br_s}` ({len(staged)} file(s)) — {full_msg}"}

    def list_git_savepoints(self, limit: int = 10) -> List[Dict[str, str]]:
        """Return recent local Git commits in `self.root`, newest first (cached by `.git/HEAD` mtimes)."""
        git_dir = self.root / ".git"
        if not git_dir.exists():
            return []
        eff_lim = max(1, min(int(limit), 50))
        head_mt = (git_dir / "HEAD").stat().st_mtime if (git_dir / "HEAD").is_file() else 0.0
        logs_mt = (git_dir / "logs" / "HEAD").stat().st_mtime if (git_dir / "logs" / "HEAD").is_file() else 0.0
        cache_key = (head_mt, logs_mt, eff_lim)
        if self._savepoints_cache is not None and self._savepoints_cache[0] == cache_key:
            return list(self._savepoints_cache[1])
        rc, out = self._run_git(["log", f"-n{eff_lim}", "--pretty=format:%h|%cs|%s"])
        if rc != 0 or not out:
            return []
        entries = [{"commit": p[0].strip(), "date": p[1].strip(), "subject": p[2].strip()} for ln in out.splitlines() if len(p := ln.split("|", 2)) == 3]
        self._savepoints_cache = (cache_key, entries)
        return list(entries)


checkpoint_manager = CheckpointManager()
_EXT_CP_CACHE: Dict[str, CheckpointManager] = {}
_EXT_CP_LOCK = threading.Lock()


def get_self_checkpoint_manager() -> CheckpointManager:
    """Return the `CheckpointManager` bound to Companion's `self_root` (`ADR-007`/`ADR-019`)."""
    ws = settings.get_workspace()
    target_bdir = (settings.effective_data_dir / "checkpoints" / SELF_PROJECT_ID).resolve()
    if ws.self_root == checkpoint_manager.root and checkpoint_manager.backup_dir == target_bdir:
        return checkpoint_manager
    key = f"self::{ws.self_root}::{target_bdir}"
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.self_root, backup_dir=target_bdir)
        return _EXT_CP_CACHE[key]


def get_active_checkpoint_manager() -> CheckpointManager:
    """Return the `CheckpointManager` bound to the active `WorkspaceContext` (`companion-self` or external project)."""
    ws = settings.get_workspace()
    if ws.is_self_project:
        return get_self_checkpoint_manager()
    target_bdir = (settings.effective_data_dir / "checkpoints" / ws.project_id).resolve()
    key = f"{ws.project_root}::{target_bdir}"
    with _EXT_CP_LOCK:
        if key not in _EXT_CP_CACHE:
            _EXT_CP_CACHE[key] = CheckpointManager(root=ws.project_root, backup_dir=target_bdir)
        return _EXT_CP_CACHE[key]
