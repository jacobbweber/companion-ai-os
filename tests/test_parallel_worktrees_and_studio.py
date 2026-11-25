"""
Comprehensive Unit & Integration Tests for Stage A Dev Studio & Parallel Agents:
1. `ModelGate` `BoundedSemaphore` concurrency (`1..8`), exclusive VRAM swap serialization, `<50ms` `CancelScope` abort & `model_registry` `max_concurrency`
2. `probe_repo_workspace` non-mutating Git & stack/rules inspection (`Check Repo`)
3. `dispatch_parallel_ready_items` isolated `git worktree` execution, unified/split `<DiffView>` payload, screenshot ingestion & `merge_worktree_into_base` auto-unblocking
4. `request_worktree_changes` iterative revision on existing branch, `push_worktree_branch_and_pr`, and `discard_worktree`
5. `run_parallel_subagents` (`workflow_manage(action='parallel_subagents')`) fan-out, SSE `subagent_progress`, and `<50ms` child `CancelScope` abort
"""

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from typing import Any, Dict, List

from companion.attachments import read_attachment_raw
from companion.jobs import BackgroundJobManager
from companion.config import settings
from companion.core_actions import execute_core_action
from companion.llm import ModelGate
from companion.memory_store import MemoryStore, memory_store_context
from companion.tools import summarize_tool_step
from companion.projects import get_project_graph
from companion.streaming import CancelScope, cancel_scope_context, check_cancelled, stream_event_context
from companion.vault import get_vault_manager
from companion.worktrees import (
    discard_worktree,
    dispatch_parallel_ready_items,
    dispatch_work_item_agent,
    merge_worktree_into_base,
    probe_repo_workspace,
    push_worktree_branch_and_pr,
    request_worktree_changes,
    run_parallel_subagents,
)

_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Demo Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)


def _msg_resp(content: str = "", tool_calls: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}], "usage": {"total_tokens": 42}}


class TestParallelWorktreesAndStudio(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.tmp_path / "studio_test.db")
        self._orig_tiers = {k: settings.tiers[k] for k in settings.tiers}

    def tearDown(self) -> None:
        for k, v in self._orig_tiers.items():
            settings.tiers[k] = v
        self.store.close()
        self._tmp.cleanup()

    def test_model_gate_bounded_semaphore_concurrency_exclusive_swap_and_cancel(self) -> None:
        gate = ModelGate(max_concurrency=4)
        lock = threading.Lock()
        active = 0
        max_overlap = 0

        def _worker() -> None:
            nonlocal active, max_overlap
            with gate.acquire_call(max_concurrency=4):
                with lock:
                    active += 1
                    max_overlap = max(max_overlap, active)
                time.sleep(0.08)
                with lock:
                    active -= 1

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: _worker(), range(3)))
        elapsed = time.perf_counter() - t0
        self.assertEqual(max_overlap, 3)
        self.assertLess(elapsed, 0.22)

        # Exclusive mode waits until in-flight slots drain and blocks concurrent callers
        ready_evt = threading.Event()
        release_evt = threading.Event()

        def _hold_exclusive() -> None:
            with gate.acquire_call(max_concurrency=4, exclusive=True):
                ready_evt.set()
                release_evt.wait(timeout=1.0)

        th = threading.Thread(target=_hold_exclusive, daemon=True)
        th.start()
        self.assertTrue(ready_evt.wait(timeout=1.0))

        # Blocked waiter aborts in <50ms when its CancelScope is cancelled
        wait_scope = CancelScope()
        abort_ms: List[float] = []

        def _blocked_caller() -> None:
            with cancel_scope_context(wait_scope):
                try:
                    with gate.acquire_call(max_concurrency=4):
                        pass
                except InterruptedError:
                    abort_ms.append(1.0)

        waiter = threading.Thread(target=_blocked_caller, daemon=True)
        waiter.start()
        time.sleep(0.02)
        c0 = time.perf_counter()
        wait_scope.cancel()
        waiter.join(timeout=0.5)
        cancel_elapsed_ms = (time.perf_counter() - c0) * 1000.0
        release_evt.set()
        th.join(timeout=0.5)
        self.assertEqual(len(abort_ms), 1)
        self.assertLess(cancel_elapsed_ms, 60.0)

        # Provider max_concurrency persists in model_registry and propagates to TierConfig
        saved = execute_core_action(
            "models",
            {"subaction": "save_provider", "provider_id": "vllm-spark", "label": "Spark vLLM", "base_url": "http://192.168.1.250:8000/v1", "dialect": "openai_compat", "max_concurrency": 6},
            actor="web", store=self.store,
        )
        self.assertTrue(saved["ok"])
        prov = next(p for p in saved["providers"] if p["provider_id"] == "vllm-spark")
        self.assertEqual(prov["max_concurrency"], 6)
        execute_core_action("models", {"subaction": "save_tier", "tier": "coding", "provider_id": "vllm-spark", "model": "qwen3-coder"}, actor="web", store=self.store)
        with memory_store_context(self.store):
            self.assertEqual(settings.resolve_tier("coding").max_concurrency, 6)

    def test_probe_repo_workspace_detects_git_branch_rules_and_non_git_dirs(self) -> None:
        missing = probe_repo_workspace(str(self.tmp_path / "does_not_exist"))
        self.assertTrue(missing["ok"])
        self.assertFalse(missing["exists"])
        self.assertFalse(missing["is_git_repo"])

        plain_dir = self.tmp_path / "plain_notes"
        plain_dir.mkdir()
        (plain_dir / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        non_git = probe_repo_workspace(str(plain_dir))
        self.assertTrue(non_git["exists"])
        self.assertFalse(non_git["is_git_repo"])
        self.assertIn("Python", non_git["stack_summary"])

        repo_dir = self.tmp_path / "demo_repo"
        _init_temp_git_repo(repo_dir)
        rules_dir = repo_dir / ".agents" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "architecture.md").write_text("# Rule\n", encoding="utf-8")
        (repo_dir / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (repo_dir / "frontend").mkdir()
        (repo_dir / "frontend" / "package.json").write_text('{"name":"ui"}\n', encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add project scaffolding"], cwd=str(repo_dir), check=True, capture_output=True)

        probed = execute_core_action("agile", {"subaction": "probe_repo", "workspace_path": str(repo_dir)}, actor="web", store=self.store)
        self.assertTrue(probed["ok"])
        self.assertTrue(probed["is_git_repo"])
        self.assertEqual(probed["branch"], "main")
        self.assertTrue(probed["clean"])
        self.assertEqual(probed["stack_summary"], "Python + Frontend")
        self.assertIn(".agents/rules/architecture.md", probed["rules_files"])

        (repo_dir / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        dirty_probe = probe_repo_workspace(str(repo_dir))
        self.assertFalse(dirty_probe["clean"])
        self.assertEqual(dirty_probe["changed_count"], 1)

    def test_parallel_worktrees_zero_collision_diff_screenshots_and_merge_unblocks_dependent(self) -> None:
        repo_dir = self.tmp_path / "parallel_app"
        _init_temp_git_repo(repo_dir)
        pg = get_project_graph(self.store)
        proj = pg.create_project(
            title="Parallel App", kind="project", workspace_path=str(repo_dir),
            delivery_policy="local_only", max_parallel_workers=2,
        )
        pid = proj["project_id"]
        card_a = pg.create_item(pid, "Build Alpha Feature", owner="companion", status="ready")
        card_b = pg.create_item(pid, "Build Beta Feature", owner="companion", status="ready")
        card_c = pg.create_item(pid, "Integrate Alpha & Beta", owner="companion", status="ready", depends_on=[card_a["item_id"], card_b["item_id"]])
        self.assertEqual(pg.get_item(card_c["item_id"])["column_key"], "blocked")

        barrier = threading.Barrier(2, timeout=5.0)
        seen_roots: Dict[str, Path] = {}

        def _fake_chat(messages: List[Dict[str, Any]], *_args: Any, **_kw: Any) -> Dict[str, Any]:
            has_tool_res = any(m.get("role") == "tool" for m in messages)
            prompt_blob = "\n".join(str(m.get("content") or "") for m in messages)
            is_alpha = "Build Alpha Feature" in prompt_blob
            if not has_tool_res:
                fname = "alpha.py" if is_alpha else "beta.py"
                return _msg_resp(tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": fname, "content": f"VALUE = '{fname}'\n"})}}])
            return _msg_resp(
                f"Implemented {'Alpha' if is_alpha else 'Beta'} cleanly.\nRan 4 tests in 0.01s - OK\n"
                + ("Screenshot saved to preview_desktop.png" if is_alpha else "")
            )

        def _fake_tool(name: str, args: Dict[str, Any]) -> str:
            ws_root = settings.get_workspace().project_root
            barrier.wait()  # Prove both worktree workers execute simultaneously
            rel = str(args.get("path") or "out.txt")
            seen_roots[rel] = ws_root
            (ws_root / rel).write_text(str(args.get("content") or ""), encoding="utf-8")
            if rel == "alpha.py":
                (ws_root / "preview_desktop.png").write_bytes(_MIN_PNG)
            return f"wrote {rel}"

        jm = BackgroundJobManager(store=self.store, chat_fn=_fake_chat, tool_fn=_fake_tool)
        res = dispatch_parallel_ready_items(project_id=pid, run_in_background=False, store=self.store, chat_fn=_fake_chat, tool_fn=_fake_tool, job_mgr=jm)
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 2)
        self.assertNotEqual(seen_roots["alpha.py"], seen_roots["beta.py"])
        self.assertFalse((repo_dir / "alpha.py").exists())
        self.assertFalse((repo_dir / "beta.py").exists())

        fresh_a = pg.get_item(card_a["item_id"])
        fresh_b = pg.get_item(card_b["item_id"])
        self.assertEqual(fresh_a["column_key"], "verify")
        self.assertEqual(fresh_b["column_key"], "verify")
        self.assertIn("alpha.py", fresh_a["deliverable"]["changed_files"])
        self.assertIn("+VALUE = 'alpha.py'", fresh_a["deliverable"]["unified_diff"])
        self.assertEqual(fresh_a["deliverable"]["diff_payload"]["files"][0]["title"], "alpha.py")
        self.assertIn("Ran 4 tests", fresh_a["deliverable"]["test_summary"])
        self.assertEqual(len(fresh_a["deliverable"]["screenshot_ids"]), 1)
        shot_id = fresh_a["deliverable"]["screenshot_ids"][0]
        raw_pair = read_attachment_raw(shot_id, store=self.store)
        self.assertIsNotNone(raw_pair)
        assert raw_pair is not None
        mime, raw_bytes, _ = raw_pair
        self.assertEqual(mime, "image/png")
        self.assertEqual(raw_bytes, _MIN_PNG)

        # Merge Card A -> Card C remains blocked until Card B also merges
        m_a = merge_worktree_into_base(card_a["item_id"], store=self.store)
        self.assertTrue(m_a["ok"])
        self.assertEqual(m_a["unblocked_ids"], [])
        self.assertTrue((repo_dir / "alpha.py").is_file())
        self.assertFalse(Path(fresh_a["worktree_path"]).exists())

        m_b = merge_worktree_into_base(card_b["item_id"], store=self.store)
        self.assertTrue(m_b["ok"])
        self.assertIn(card_c["item_id"], m_b["unblocked_ids"])
        self.assertTrue((repo_dir / "beta.py").is_file())
        self.assertEqual(pg.get_item(card_c["item_id"])["column_key"], "ready")

    def test_request_changes_reuses_worktree_discard_cleans_up_and_push_pr_vault_flow(self) -> None:
        from companion import worktrees as sdlc_tools

        repo_dir = self.tmp_path / "iter_repo"
        _init_temp_git_repo(repo_dir)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/iter.git"], cwd=str(repo_dir), check=True, capture_output=True)

        get_vault_manager(store=self.store).set_secret("gh-test-pat", "ghp_testSecretTokenValue123456", description="Test GitHub PAT", actor="user")
        pg = get_project_graph(self.store)
        proj = pg.create_project(
            title="Iterative Service", kind="project", workspace_path=str(repo_dir),
            delivery_policy="local_only", git_credential_ref="gh-test-pat",
        )
        card = pg.create_item(proj["project_id"], "Add Health Endpoint", owner="companion", status="ready")

        pass_num = {"n": 0}
        pass_lock = threading.Lock()

        def _iter_chat(messages: List[Dict[str, Any]], *_args: Any, **_kw: Any) -> Dict[str, Any]:
            if not any(m.get("role") == "tool" for m in messages):
                with pass_lock:
                    pass_num["n"] += 1
                    ver = pass_num["n"]
                return _msg_resp(tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "health.py", "content": f"VERSION = {ver}\n"})}}])
            return _msg_resp(f"Health endpoint pass {pass_num['n']} complete.")

        def _iter_tool(_name: str, args: Dict[str, Any]) -> str:
            ws_root = settings.get_workspace().project_root
            (ws_root / str(args["path"])).write_text(str(args["content"]), encoding="utf-8")
            return "ok"

        jm = BackgroundJobManager(store=self.store, chat_fn=_iter_chat, tool_fn=_iter_tool)
        r1 = dispatch_work_item_agent(card["item_id"], run_in_background=False, store=self.store, chat_fn=_iter_chat, tool_fn=_iter_tool, job_mgr=jm)
        self.assertTrue(r1["ok"])
        wt_first, br_first = r1["worktree_path"], r1["branch_name"]
        self.assertIn("VERSION = 1", pg.get_item(card["item_id"])["deliverable"]["unified_diff"])

        # Request Changes reuses exact same worktree & branch and records revision feedback
        r2 = request_worktree_changes(card["item_id"], feedback="Bump VERSION to 2", run_in_background=False, store=self.store, chat_fn=_iter_chat, tool_fn=_iter_tool, job_mgr=jm)
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["worktree_path"], wt_first)
        self.assertEqual(r2["branch_name"], br_first)
        upd = pg.get_item(card["item_id"])
        self.assertIn("VERSION = 2", upd["deliverable"]["unified_diff"])
        self.assertEqual(upd["deliverable"]["revision_history"][0]["feedback"], "Bump VERSION to 2")

        # Local-only policy blocks push_worktree_branch_and_pr
        blocked_push = push_worktree_branch_and_pr(card["item_id"], store=self.store)
        self.assertFalse(blocked_push["ok"])
        self.assertIn("Local Only", blocked_push["error"])

        # Switch to remote_branch_pr and verify Blind Vault Git auth + GitHub PR creation
        pg.update_project(proj["project_id"], delivery_policy="remote_branch_pr")
        real_run_git, seen_push = sdlc_tools._run_git_cmd, []

        def _intercept_git(cwd: Path, git_args: List[str], env: Dict[str, str] | None = None, timeout: int = 30) -> Any:
            if git_args and git_args[0] == "push":
                seen_push.append({"args": git_args, "has_askpass": bool(env and env.get("GIT_ASKPASS"))})
                return 0, f"To https://github.com/acme/iter.git\n * [new branch] {br_first} -> {br_first}"
            return real_run_git(cwd, git_args, env=env, timeout=timeout)

        def _fake_open(_self: Any, req: Any, **_kw: Any) -> Any:
            m = getattr(req, "get_method", lambda: "GET")()
            body = b"[]" if m == "GET" else json.dumps({"number": 42, "html_url": "https://github.com/acme/iter/pull/42"}).encode("utf-8")
            cm = unittest.mock.MagicMock()
            cm.__enter__.return_value.read.return_value = body
            cm.__enter__.return_value.status = 200
            return cm

        with memory_store_context(self.store), unittest.mock.patch.object(sdlc_tools, "_run_git_cmd", side_effect=_intercept_git), unittest.mock.patch("urllib.request.OpenerDirector.open", _fake_open):
            pushed = push_worktree_branch_and_pr(card["item_id"], store=self.store)
        self.assertTrue(pushed["ok"], pushed)
        self.assertEqual(len(seen_push), 1)
        self.assertTrue(seen_push[0]["has_askpass"])
        self.assertEqual(pushed["pr_url"], "https://github.com/acme/iter/pull/42")
        self.assertEqual(pg.get_item(card["item_id"])["deliverable"]["pr_number"], 42)

        # Discard removes worktree & resets card to ready
        disc = discard_worktree(card["item_id"], store=self.store)
        self.assertTrue(disc["ok"])
        self.assertFalse(Path(wt_first).exists())
        self.assertEqual(pg.get_item(card["item_id"])["column_key"], "ready")

    def test_parallel_subagents_concurrent_fanout_sse_events_and_50ms_cancel(self) -> None:
        events: List[Dict[str, Any]] = []
        bar = threading.Barrier(3, timeout=3.0)

        def _sub_chat(messages: List[Dict[str, Any]], *_args: Any, **_kw: Any) -> Dict[str, Any]:
            bar.wait()
            time.sleep(0.05)
            user_txt = str(messages[-1].get("content") or "")
            return _msg_resp(f"Subagent finished: {user_txt[:40]}")

        with stream_event_context(events.append):
            res = run_parallel_subagents(
                tasks=[{"goal": "Audit module A"}, {"goal": "Audit module B"}, {"goal": "Audit module C"}],
                max_concurrency=3, max_iters=3, store=self.store, chat_fn=_sub_chat,
            )
        self.assertTrue(res["ok"])
        self.assertEqual(res["count"], 3)
        prog_events = [e for e in events if e.get("type") == "subagent_progress"]
        self.assertEqual(len([e for e in prog_events if e.get("status") == "running"]), 3)
        self.assertEqual(len([e for e in prog_events if e.get("status") == "done"]), 3)
        step_lbl = summarize_tool_step("workflow_manage", {"action": "parallel_subagents", "tasks": [{}, {}, {}]})
        self.assertEqual(step_lbl, "Ran 3 parallel subagents")

        # Parent CancelScope aborts all in-flight child subagents within <80ms
        parent_scope = CancelScope()
        started = threading.Event()

        def _slow_chat(_messages: List[Dict[str, Any]], *_args: Any, **_kw: Any) -> Dict[str, Any]:
            started.set()
            for _ in range(40):
                check_cancelled()
                time.sleep(0.01)
            return _msg_resp("late")

        err_box: List[str] = []

        def _runner() -> None:
            with cancel_scope_context(parent_scope):
                try:
                    run_parallel_subagents(tasks=[{"goal": "Slow A"}, {"goal": "Slow B"}], max_concurrency=2, store=self.store, chat_fn=_slow_chat)
                except InterruptedError:
                    err_box.append("cancelled")

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        self.assertTrue(started.wait(timeout=1.0))
        t0 = time.perf_counter()
        parent_scope.cancel()
        t.join(timeout=0.5)
        abort_ms = (time.perf_counter() - t0) * 1000.0
        self.assertEqual(err_box, ["cancelled"])
        self.assertLess(abort_ms, 80.0)


if __name__ == "__main__":
    unittest.main()
