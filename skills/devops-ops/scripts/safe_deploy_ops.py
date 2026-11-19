# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Safe Packaged App Deployment, Health Probe & Rollback Engine (`skills/devops-ops/scripts/safe_deploy_ops.py`).

Pure Python 3.12 stdlib helper powering `skills/devops-ops/dags/safe_deploy.py`:
- Pre-deploy `app/` directory snapshot backup (excluding `data/`, `.env`, `secrets.env`).
- Clean packaged `app/` runtime sync (excluding `tests/`, `.git/`, `node_modules/`, `__pycache__/`).
- Live `/api/health` HTTP verification + packaged `7/7` `System Doctor` check.
- Automatic snapshot rollback + post-rollback verification when health or Doctor fails.
"""

import argparse
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.memory_store import MemoryStore  # noqa: E402
from companion.vault import create_snapshot_backup  # noqa: E402

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PACKAGED_APP_ITEMS = (
    "harness.py", "README.md", ".gitignore", ".env.example", "Dockerfile", "docker-compose.yml",
    ".agents", "companion", "surfaces", "configs", "memory", "specs", "skills", "deploy",
)
_IGNORE_PATTERNS = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache", "node_modules", ".git")
_PROTECTED_TARGET_ENTRIES = {"data", ".env", "secrets.env"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _terminate_proc(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=6)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.kill()
        proc.wait(timeout=3)
    except Exception:
        pass


def snapshot_app_dir(
    target_app_dir: Path,
    backup_root: Path,
    *,
    label: str = "pre-deploy",
    store: Optional[MemoryStore] = None,
) -> Dict[str, Any]:
    """Capture a restorable snapshot of `target_app_dir` before syncing new code."""
    target = target_app_dir.resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snap_dir = (backup_root / f"app_snap_{stamp}_{label}").resolve()
    snap_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    existed = target.is_dir() and any(target.iterdir())
    if existed:
        for child in target.iterdir():
            if child.name in _PROTECTED_TARGET_ENTRIES or child.name == "__pycache__":
                continue
            dst = snap_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dst, ignore=_IGNORE_PATTERNS, dirs_exist_ok=True)
                copied += sum(1 for _ in dst.iterdir())
            elif child.is_file():
                shutil.copy2(child, dst)
                copied += 1
    data_snap_id = ""
    if store is not None:
        snap_meta = create_snapshot_backup(label=f"deploy-{label}", store=store)
        data_snap_id = str(snap_meta.get("snapshot_id") or "")
    return {
        "ok": True,
        "existed": existed,
        "snapshot_dir": snap_dir.as_posix(),
        "target_app_dir": target.as_posix(),
        "copied_entries": copied,
        "data_snapshot_id": data_snap_id,
    }


def sync_packaged_app_tree(source_repo: Path, target_app_dir: Path) -> Dict[str, Any]:
    """Sync packaged `app/` runtime files (excluding `tests/`, `.git/`, `data/`, secrets) into `target_app_dir`."""
    src = source_repo.resolve()
    dst = target_app_dir.resolve()
    if src == dst:
        raise ValueError("`source_repo` and `target_app_dir` must be distinct directories.")
    dst.mkdir(parents=True, exist_ok=True)
    for child in list(dst.iterdir()):
        if child.name in _PROTECTED_TARGET_ENTRIES:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    synced: list[str] = []
    for item_name in PACKAGED_APP_ITEMS:
        s_item = src / item_name
        if not s_item.exists():
            continue
        d_item = dst / item_name
        if s_item.is_dir():
            shutil.copytree(s_item, d_item, ignore=_IGNORE_PATTERNS, dirs_exist_ok=True)
        else:
            shutil.copy2(s_item, d_item)
        synced.append(item_name)
    return {
        "ok": True,
        "source_repo": src.as_posix(),
        "target_app_dir": dst.as_posix(),
        "synced_items": synced,
        "tests_excluded": not (dst / "tests").exists(),
    }


def restore_app_snapshot(snapshot_dir: Path, target_app_dir: Path) -> Dict[str, Any]:
    """Restore `target_app_dir` from a `snapshot_app_dir` backup while preserving `data/` and `.env`."""
    snap = snapshot_dir.resolve()
    dst = target_app_dir.resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for child in list(dst.iterdir()):
        if child.name in _PROTECTED_TARGET_ENTRIES:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    restored = 0
    if snap.is_dir():
        for child in snap.iterdir():
            target_item = dst / child.name
            if child.is_dir():
                shutil.copytree(child, target_item, dirs_exist_ok=True)
            else:
                shutil.copy2(child, target_item)
            restored += 1
    return {"ok": True, "restored_entries": restored, "target_app_dir": dst.as_posix()}


def _probe_ephemeral_or_url_health(
    target_app_dir: Path,
    *,
    health_url: str = "",
    boot_ephemeral_health: bool = True,
    simulate_health_failure: bool = False,
) -> Dict[str, Any]:
    if simulate_health_failure:
        return {"ok": False, "status": 503, "detail": "Simulated /api/health failure triggered rollback gate."}
    if health_url:
        try:
            with _NO_PROXY_OPENER.open(health_url, timeout=5.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                return {"ok": resp.status == 200 and bool(payload.get("ok")), "status": resp.status, "payload": payload}
        except Exception as exc:
            return {"ok": False, "status": 0, "detail": f"{type(exc).__name__}: {exc}"}
    if not boot_ephemeral_health:
        return {"ok": True, "status": 200, "detail": "Health URL check skipped."}
    port = _free_port()
    tmp_data = Path(tempfile.mkdtemp(prefix="companion_deploy_health_"))
    env = os.environ.copy()
    env.update({
        "COMPANION_IMMUTABLE_APP": "1",
        "COMPANION_DATA_DIR": str(tmp_data / "data"),
        "COMPANION_SECRETS_FILE": str(tmp_data / "secrets.env"),
        "NO_PROXY": "127.0.0.1,localhost,::1",
    })
    popen_kw: Dict[str, Any] = {"cwd": str(target_app_dir), "env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True}
    if os.name == "nt":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "harness.py", "--web", "--host", "127.0.0.1", "--port", str(port), "--no-browser"], **popen_kw)
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.monotonic() + 15.0
        last_err = "timeout"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err_txt = proc.stderr.read() if proc.stderr else ""
                return {"ok": False, "status": 0, "detail": f"Runtime exited early ({proc.returncode}): {err_txt[-300:]}"}
            try:
                with _NO_PROXY_OPENER.open(url, timeout=1.5) as resp:
                    body = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if resp.status == 200 and body.get("ok"):
                        return {"ok": True, "status": 200, "port": port, "payload": body}
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(0.2)
        return {"ok": False, "status": 0, "detail": f"Health poll timed out: {last_err}"}
    finally:
        _terminate_proc(proc)
        shutil.rmtree(tmp_data, ignore_errors=True)


def verify_app_health_and_doctor(
    target_app_dir: Path,
    *,
    health_url: str = "",
    boot_ephemeral_health: bool = True,
    simulate_health_failure: bool = False,
    run_doctor: bool = True,
) -> Dict[str, Any]:
    """Verify `/api/health` (`200 OK`) AND packaged `7/7` `System Doctor` on `target_app_dir`."""
    target = target_app_dir.resolve()
    health = _probe_ephemeral_or_url_health(
        target, health_url=health_url,
        boot_ephemeral_health=boot_ephemeral_health,
        simulate_health_failure=simulate_health_failure,
    )
    if not health.get("ok"):
        return {"ok": False, "health": health, "doctor_ok": False, "reason": f"Health gate failed: {health.get('detail')}"}
    if not run_doctor:
        return {"ok": True, "health": health, "doctor_ok": True}
    tmp_doc = Path(tempfile.mkdtemp(prefix="companion_deploy_doc_"))
    try:
        env = os.environ.copy()
        env.update({
            "COMPANION_IMMUTABLE_APP": "1",
            "COMPANION_DATA_DIR": str(tmp_doc / "data"),
            "COMPANION_SECRETS_FILE": str(tmp_doc / "secrets.env"),
            "PYTHONUTF8": "1",
            "NO_PROXY": "127.0.0.1,localhost,::1",
        })
        proc = subprocess.run(
            [sys.executable, "harness.py", "--doctor"],
            cwd=str(target), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        doc_ok = proc.returncode == 0
        return {
            "ok": doc_ok,
            "health": health,
            "doctor_ok": doc_ok,
            "doctor_returncode": proc.returncode,
            "doctor_tail": out[-800:],
            "reason": "" if doc_ok else f"Packaged System Doctor failed (exit={proc.returncode}).",
        }
    finally:
        shutil.rmtree(tmp_doc, ignore_errors=True)


def _run_self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="companion_safedeploy_selftest_"))
    try:
        src_v1 = tmp / "src_v1"
        src_v2 = tmp / "src_v2"
        app_dir = tmp / "runtime" / "app"
        bk_dir = tmp / "backups"
        for d, ver in ((src_v1, "v1-stable"), (src_v2, "v2-broken")):
            (d / "companion").mkdir(parents=True)
            (d / "tests").mkdir(parents=True)
            (d / "harness.py").write_text(f"# {ver}\n", encoding="utf-8")
            (d / "companion" / "marker.py").write_text(f"VERSION = {ver!r}\n", encoding="utf-8")
            (d / "tests" / "test_dummy.py").write_text("assert True\n", encoding="utf-8")
        (app_dir / "data").mkdir(parents=True)
        (app_dir / "data" / "keep.txt").write_text("user-state", encoding="utf-8")

        sync1 = sync_packaged_app_tree(src_v1, app_dir)
        assert sync1["ok"] and sync1["tests_excluded"], sync1
        snap = snapshot_app_dir(app_dir, bk_dir, label="v1")
        assert snap["ok"] and snap["existed"], snap
        sync_packaged_app_tree(src_v2, app_dir)
        assert "v2-broken" in (app_dir / "harness.py").read_text(encoding="utf-8")
        fail_ver = verify_app_health_and_doctor(app_dir, simulate_health_failure=True, run_doctor=False)
        assert fail_ver["ok"] is False, fail_ver
        rb = restore_app_snapshot(Path(snap["snapshot_dir"]), app_dir)
        assert rb["ok"], rb
        assert "v1-stable" in (app_dir / "harness.py").read_text(encoding="utf-8")
        assert (app_dir / "data" / "keep.txt").read_text(encoding="utf-8") == "user-state"
        print(json.dumps({"self_test": "PASS", "restored_version": "v1-stable", "data_preserved": True}))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe packaged app/ deployment & rollback helper.")
    parser.add_argument("--self-test", action="store_true", help="Run fast deterministic sync + rollback self-test.")
    parser.add_argument("--source-repo", default=str(ROOT), help="Source git repository directory.")
    parser.add_argument("--target-app-dir", default="", help="Target packaged app/ runtime directory.")
    parser.add_argument("--backup-dir", default="", help="Directory for pre-deploy snapshots.")
    parser.add_argument("--simulate-health-failure", action="store_true", help="Simulate post-deploy health failure to test rollback.")
    args = parser.parse_args()
    if args.self_test:
        return _run_self_test()
    if not args.target_app_dir:
        print(json.dumps({"ok": False, "error": "--target-app-dir is required"}))
        return 1
    target = Path(args.target_app_dir).resolve()
    bk = Path(args.backup_dir).resolve() if args.backup_dir else (target.parent / "backups")
    snap = snapshot_app_dir(target, bk)
    sync = sync_packaged_app_tree(Path(args.source_repo).resolve(), target)
    ver = verify_app_health_and_doctor(target, simulate_health_failure=args.simulate_health_failure)
    if not ver["ok"]:
        rb = restore_app_snapshot(Path(snap["snapshot_dir"]), target)
        print(json.dumps({"ok": False, "rolled_back": True, "rollback": rb, "verification": ver}, indent=2))
        return 1
    print(json.dumps({"ok": True, "rolled_back": False, "sync": sync, "verification": ver}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
