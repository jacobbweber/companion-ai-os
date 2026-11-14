"""
Ring-1 Build & Verification Helper for Companion 3.0's Frontend (`skills/web-surface-dev/scripts/build_frontend.py`).

Pure Python 3.12 stdlib script to verify `surfaces/static/` integrity (`--self-test`) or trigger
`npm run build` in `frontend/` and validate compiled assets (`index.html`, `styles.css`, `app.js`).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIR = REPO_ROOT / "frontend"
STATIC_DIR = REPO_ROOT / "surfaces" / "static"


def verify_static_bundle(static_dir: Path = STATIC_DIR) -> Dict[str, Any]:
    """Verify compiled static assets satisfy Companion's offline, mobile, CSRF, and syntax contracts."""
    index_p = static_dir / "index.html"
    css_p = static_dir / "styles.css"
    js_p = static_dir / "app.js"
    missing = [p.name for p in (index_p, css_p, js_p) if not p.is_file()]
    if missing:
        return {"ok": False, "error": f"Missing static assets: {missing}"}

    html = index_p.read_text(encoding="utf-8")
    css = css_p.read_text(encoding="utf-8")
    js = js_p.read_text(encoding="utf-8")
    checks = {
        "no_cdn": "cdn." not in (html + css + js).lower(),
        "has_100dvh": "100dvh" in css,
        "has_eventsource": "EventSource" in js,
        "has_csrf_header": "X-Companion-CSRF" in js,
        "has_approval_banner": 'id="approval-banner"' in html,
    }
    failed = [k for k, passed in checks.items() if not passed]
    if failed:
        return {"ok": False, "error": f"Static bundle contract failure: {failed}", "checks": checks}
    return {
        "ok": True,
        "checks": checks,
        "sizes_bytes": {"index.html": len(html), "styles.css": len(css), "app.js": len(js)},
    }


def build_frontend_bundle(run_ui_tests: bool = True) -> Dict[str, Any]:
    """Run `npm test` + `npm run build` inside `frontend/` and verify resulting `surfaces/static/` bundle."""
    npm_bin = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm_bin:
        return {"ok": False, "error": "npm executable not found on PATH."}
    if run_ui_tests:
        t_proc = subprocess.run([npm_bin, "test"], cwd=str(FRONTEND_DIR), capture_output=True, text=True, timeout=180)
        if t_proc.returncode != 0:
            return {"ok": False, "error": f"Interactive UI test failed:\n{(t_proc.stderr or t_proc.stdout).strip()[-600:]}"}
    proc = subprocess.run(
        [npm_bin, "run", "build"],
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[-600:]}
    ver = verify_static_bundle()
    return {**ver, "build_stdout": proc.stdout.strip()[-400:]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify Companion 3.0's React + TypeScript static bundle.")
    parser.add_argument("--self-test", action="store_true", help="Verify existing surfaces/static/ contract invariants.")
    args = parser.parse_args()
    if args.self_test:
        res = verify_static_bundle()
        if not res["ok"]:
            print(json.dumps(res), file=sys.stderr)
            return 1
        print(json.dumps({"self_test": "PASS", **res}))
        return 0
    res = build_frontend_bundle()
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
