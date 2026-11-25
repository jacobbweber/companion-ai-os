# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Blind-Vault GitHub Pull Request & Actions CI Helper (`skills/github-workflow/scripts/github_pr_ci.py`).

Pure Python 3.12 stdlib GitHub REST v3 client resolving Bearer tokens directly from
`VaultManager` without exposing secret values to argv, stdout, or LLM transcripts.
"""

import argparse
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.config import settings  # noqa: E402
from companion.memory_store import MemoryStore, get_memory_store  # noqa: E402
from companion.projects import get_project_graph  # noqa: E402
from companion.vault import VaultManager  # noqa: E402
from companion.worktrees import inspect_check_runs, merge_pull_request, upsert_pull_request  # noqa: E402


def _run_self_test() -> int:
    state: Dict[str, Any] = {"prs": [], "auth_headers": []}

    class _MockGH(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, data: Any) -> None:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            state["auth_headers"].append(self.headers.get("Authorization", ""))
            if "/pulls?" in self.path:
                self._send(200, state["prs"])
            elif "/check-runs" in self.path:
                self._send(200, {"check_runs": [{"name": "unit-tests", "status": "completed", "conclusion": "success"}]})
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            state["auth_headers"].append(self.headers.get("Authorization", ""))
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln).decode("utf-8"))
            pr = {"number": 42, "title": body["title"], "html_url": "http://127.0.0.1/pr/42", "state": "open"}
            state["prs"] = [pr]
            self._send(201, pr)

        def do_PATCH(self) -> None:  # noqa: N802
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln).decode("utf-8"))
            state["prs"][0]["title"] = body["title"]
            self._send(200, state["prs"][0])

        def do_PUT(self) -> None:  # noqa: N802
            self._send(200, {"merged": True, "sha": "abc1234", "message": "Pull Request successfully merged"})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _MockGH)
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp(prefix="companion_gh_selftest_"))
    old_sec = os.environ.get("COMPANION_SECRETS_FILE")
    os.environ["COMPANION_SECRETS_FILE"] = str(tmp / "secrets.env")
    store = MemoryStore(db_path=tmp / "test.db")
    try:
        secret_tok = "ghp_SelfTestBlindSecretToken987654321"
        vm = VaultManager(store=store, secrets_path=tmp / "secrets.env")
        vm.set_secret("gh-selftest-pat", secret_tok, cred_type="bearer_token", allow_list=["skill:github-workflow"], actor="user")
        created = upsert_pull_request("acme/demo", "feat/ui", "main", "Initial PR", "Body", "gh-selftest-pat", api_base=base_url, vault=vm)
        updated = upsert_pull_request("acme/demo", "feat/ui", "main", "Updated PR", "Body2", "gh-selftest-pat", api_base=base_url, vault=vm)
        checks = inspect_check_runs("acme/demo", "feat/ui", "gh-selftest-pat", api_base=base_url, vault=vm)
        blocked_merge = merge_pull_request("acme/demo", 42, "gh-selftest-pat", approved=False, api_base=base_url, vault=vm)
        merged = merge_pull_request("acme/demo", 42, "gh-selftest-pat", approved=True, api_base=base_url, vault=vm)
        serialized = json.dumps([created, updated, checks, blocked_merge, merged])
        assert created["ok"] and created["action"] == "created" and created["number"] == 42, created
        assert updated["ok"] and updated["action"] == "updated", updated
        assert checks["ok"] and checks["passed"] == ["unit-tests"], checks
        assert blocked_merge["ok"] is False and merged["ok"] is True, (blocked_merge, merged)
        assert secret_tok not in serialized, "Vault token leaked into output!"
        assert f"Bearer {secret_tok}" in state["auth_headers"], "Bearer token was not sent to GitHub API!"
        print(json.dumps({"self_test": "PASS", "pr": created["number"], "checks": checks["passed"]}))
        return 0
    finally:
        store.close()
        srv.shutdown()
        srv.server_close()
        if old_sec is None:
            os.environ.pop("COMPANION_SECRETS_FILE", None)
        else:
            os.environ["COMPANION_SECRETS_FILE"] = old_sec
        shutil.rmtree(tmp, ignore_errors=True)


def verify_delivery_policy(params: Dict[str, Any]) -> Dict[str, Any]:
    head = str(params.get("head") or "").strip()
    if not head or head in ("main", "master"):
        return {"ok": False, "error": f"Feature branch required (`head={head!r}`); direct `main`/`master` PR publish is blocked."}
    pg = get_project_graph(store=get_memory_store())
    pid = str(params.get("project_id") or settings.get_workspace().project_id or "").strip()
    proj = pg.get_project(pid) if pid else None
    policy = str((proj or {}).get("delivery_policy") or params.get("delivery_policy") or "local_only")
    if policy == "local_only":
        return {"ok": False, "error": "Project `delivery_policy` is `local_only`; remote branch push & PR creation are blocked."}
    cred_ref = str(params.get("credential_ref") or (proj or {}).get("git_credential_ref") or "github-pat")
    return {"ok": True, "delivery_policy": policy, "credential_ref": cred_ref, "head": head}


def main() -> int:
    parser = argparse.ArgumentParser(description="Blind-Vault GitHub PR & Actions helper.")
    parser.add_argument("--self-test", action="store_true", help="Run offline loopback mock GitHub API self-test.")
    parser.add_argument("--action", choices=["verify-policy", "upsert-pr", "checks", "merge-pr"], default="checks")
    parser.add_argument("--repo", default="", help="owner/repo")
    parser.add_argument("--head", default="", help="Feature branch or commit SHA")
    parser.add_argument("--base", default="main", help="Base branch")
    parser.add_argument("--title", default="", help="PR title")
    parser.add_argument("--body", default="", help="PR description")
    parser.add_argument("--pr-number", type=int, default=0, help="PR number for merge-pr")
    parser.add_argument("--credential-ref", default="", help="Symbolic Blind Vault secret slug")
    parser.add_argument("--api-base", default="https://api.github.com", help="GitHub REST API base URL")
    parser.add_argument("--approved", action="store_true", help="Confirm human approval for merge-pr")
    args = parser.parse_args()
    if args.self_test:
        return _run_self_test()
    stdin_payload: Dict[str, Any] = {}
    if sys.stdin is not None and not sys.stdin.isatty():
        raw_in = sys.stdin.read().strip()
        if raw_in:
            try:
                stdin_payload = json.loads(raw_in)
            except Exception:
                stdin_payload = {}
    wf_in = dict(stdin_payload.get("input") or stdin_payload.get("params") or {})
    repo = args.repo or str(wf_in.get("repo") or "")
    head = args.head or str(wf_in.get("head") or "")
    base = args.base if args.base != "main" else str(wf_in.get("base") or "main")
    title = args.title or str(wf_in.get("title") or f"feat: {head}")
    body = args.body or str(wf_in.get("body") or "Automated verified PR from Companion SDLC.")
    cred_ref = args.credential_ref or str(wf_in.get("credential_ref") or "github-pat")
    api_base = args.api_base if args.api_base != "https://api.github.com" else str(wf_in.get("api_base") or "https://api.github.com")
    pr_num = args.pr_number or int(wf_in.get("pr_number") or wf_in.get("number") or 0)
    if args.action == "verify-policy":
        res = verify_delivery_policy({**wf_in, "head": head})
    elif args.action == "upsert-pr":
        pr_res = upsert_pull_request(repo, head, base, title, body, cred_ref, api_base=api_base)
        if pr_res.get("ok"):
            chk = inspect_check_runs(repo, head, cred_ref, api_base=api_base)
            res = {**pr_res, "pr_number": int(pr_res.get("number") or 0), "checks": chk}
        else:
            res = pr_res
    elif args.action == "merge-pr":
        res = merge_pull_request(repo, pr_num, cred_ref, approved=bool(args.approved or wf_in.get("approved")), api_base=api_base)
    else:
        res = inspect_check_runs(repo, head, cred_ref, api_base=api_base)
    print(json.dumps(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
