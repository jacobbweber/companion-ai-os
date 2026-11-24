"""
Live 12-Scenario Real-Model Calibration Suite (`skills/devops-ops/scripts/live_model_calibration.py`).

Executes all 12 zero-mock live LAN inference scenarios against isolated temporary `COMPANION_DATA_DIR`
workspaces (vLLM `nemotron-3.5-lightning` + Ollama `qwen3.8:latest` & `qwen3-coder:latest`) plus `--self-test`.
"""

import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VLLM_URL = os.environ.get("COMPANION_CAL_VLLM_URL", os.environ.get("COMPANION_BASE_URL", "http://127.0.0.1:8006"))
VLLM_MODEL = os.environ.get("COMPANION_CAL_VLLM_MODEL", os.environ.get("COMPANION_MODEL", "nemotron-3.5-lightning"))
OLLAMA_URL = os.environ.get("COMPANION_CAL_OLLAMA_URL", "http://127.0.0.1:11434")
REASONING_MODEL = os.environ.get("COMPANION_CAL_REASONING_MODEL", "qwen3.8:latest")
CODER_MODEL = os.environ.get("COMPANION_CAL_CODER_MODEL", "qwen3-coder:latest")


def _git_init_commit(repo_dir: Path, files: Dict[str, str], branch: str = "main") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", branch], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Companion Calibration"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "calibration@local"], cwd=str(repo_dir), check=True, capture_output=True)
    merged = {".gitignore": "__pycache__/\n*.py[cod]\n.pytest_cache/\n", **files}
    for rel, content in merged.items():
        fp = repo_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial fixture"], cwd=str(repo_dir), check=True, capture_output=True)


class LiveCallRecorder:
    """Thread-safe wrapper around `companion.llm.chat` recording real wire timings & token usage."""

    def __init__(self, real_chat_fn: Callable[..., Dict[str, Any]]) -> None:
        self._real_chat = real_chat_fn
        self._lock = threading.Lock()
        self.calls: List[Dict[str, Any]] = []
        self._active = 0
        self.peak_concurrency = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        from companion.config import settings
        from companion.llm import current_tier_name

        t_start = time.perf_counter()
        with self._lock:
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)
        eff_tier = str(kwargs.get("tier") or current_tier_name() or "fast")
        t_cfg = settings.resolve_tier(eff_tier)
        try:
            resp = self._real_chat(*args, **kwargs)
            t_end = time.perf_counter()
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            usage = resp.get("usage") or {}
            rec = {
                "tier": eff_tier, "model": kwargs.get("model") or t_cfg.model, "base_url": kwargs.get("base_url") or t_cfg.base_url,
                "start_mono": round(t_start, 3), "end_mono": round(t_end, 3), "duration_ms": round((t_end - t_start) * 1000.0, 1),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0), "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0), "content_preview": str(msg.get("content") or "")[:200],
                "thinking_chars": len(str(msg.get("thinking") or msg.get("reasoning_content") or msg.get("reasoning") or "")),
                "tool_calls": [{"name": (tc.get("function") or {}).get("name"), "arguments": (tc.get("function") or {}).get("arguments")} for tc in (msg.get("tool_calls") or [])],
                "ok": True,
            }
            with self._lock:
                self.calls.append(rec)
            return resp
        except Exception as exc:
            t_end = time.perf_counter()
            with self._lock:
                self.calls.append({
                    "tier": eff_tier, "model": kwargs.get("model") or t_cfg.model, "base_url": kwargs.get("base_url") or t_cfg.base_url,
                    "start_mono": round(t_start, 3), "end_mono": round(t_end, 3), "duration_ms": round((t_end - t_start) * 1000.0, 1),
                    "error": f"{type(exc).__name__}: {exc}", "ok": False,
                })
            raise
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)


class IsolatedEnvContext:
    """Ephemeral `COMPANION_DATA_DIR` bound to live LAN inference providers with zero production side effects."""

    def __init__(self, coding_provider: str = "vllm-spark", coding_model: str = VLLM_MODEL, mode: str = "auto") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="comp_live_cal_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.coding_provider, self.coding_model, self.mode = coding_provider, coding_model, mode
        self.store: Any = None
        self._prev_data_dir: Optional[Path] = None
        self._prev_env_data: Optional[str] = None
        self._prev_store_inst: Any = None

    def __enter__(self) -> "IsolatedEnvContext":
        import companion.memory_store as ms
        from companion.config import settings
        from companion.llm import get_endpoint_gate, model_gate
        from companion.model_registry import bump_model_registry_cache, execute_model_registry_action
        from companion.wiki_engine import _INDEXED_WIKI_ROOTS

        self._prev_env_data, self._prev_data_dir, self._prev_store_inst = os.environ.get("COMPANION_DATA_DIR"), settings.data_dir, ms._store_instance
        os.environ["COMPANION_DATA_DIR"] = str(self.data_dir)
        settings.data_dir, settings.base_url, settings.model = self.data_dir, VLLM_URL, VLLM_MODEL
        settings._load_tiers()
        settings.reset_workspace()
        self.store = ms.MemoryStore(db_path=self.data_dir / "conversations.db")
        ms._store_instance = self.store
        _INDEXED_WIKI_ROOTS.clear()
        bump_model_registry_cache()
        execute_model_registry_action({"subaction": "save_provider", "provider_id": "vllm-spark", "label": "Spark vLLM", "preset": "vllm", "dialect": "openai_compat", "base_url": VLLM_URL, "max_concurrency": 4}, actor="user", store=self.store)
        execute_model_registry_action({"subaction": "save_provider", "provider_id": "ollama-lan", "label": "Ollama LAN", "preset": "ollama", "dialect": "openai_compat", "base_url": OLLAMA_URL, "max_concurrency": 1}, actor="user", store=self.store)
        for t_name, pid, mod, temp in (("default", "vllm-spark", VLLM_MODEL, 0.2), ("fast", "vllm-spark", VLLM_MODEL, 0.15), ("coding", self.coding_provider, self.coding_model, 0.15), ("reasoning", "ollama-lan", REASONING_MODEL, 0.6)):
            execute_model_registry_action({"subaction": "save_tier", "tier": t_name, "provider_id": pid, "model": mod, "temperature": temp, "context_window_tokens": 262144}, actor="user", store=self.store)
        execute_model_registry_action({"subaction": "set_mode", "mode": self.mode}, actor="user", store=self.store)
        model_gate.set_max_concurrency(4)
        get_endpoint_gate(VLLM_URL).set_max_concurrency(4)
        get_endpoint_gate(OLLAMA_URL).set_max_concurrency(1)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        import companion.memory_store as ms
        from companion.config import settings
        from companion.model_registry import bump_model_registry_cache
        from companion.sdlc_tools import preview_manager

        preview_manager.stop_all()
        if self.store is not None:
            try:
                self.store.close()
            except Exception:
                pass
        ms._store_instance = self._prev_store_inst
        if self._prev_data_dir is not None:
            settings.data_dir = self._prev_data_dir
        settings.reset_workspace()
        if self._prev_env_data is None:
            os.environ.pop("COMPANION_DATA_DIR", None)
        else:
            os.environ["COMPANION_DATA_DIR"] = self._prev_env_data
        bump_model_registry_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)


def _extract_tool_msgs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn, tc_id = tc.get("function") or {}, tc.get("id")
                res_txt = next((str(tm.get("content") or "") for tm in messages[i + 1 :] if tm.get("role") == "tool" and tm.get("tool_call_id") == tc_id), "")
                out.append({"name": fn.get("name"), "args": fn.get("arguments"), "result_head": res_txt[:220]})
    return out


def _has_tag_leak(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in ("<think>", "</think>", "<tool_call>", "</tool_call>", "<function=", "</function>"))


def scenario_01_spark_wiki_save() -> Dict[str, Any]:
    from companion.llm import chat
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="auto") as ctx:
        rec, events = LiveCallRecorder(chat), []
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        prompt = (
            "Save this troubleshooting runbook to my Knowledge Wiki at `02_knowledge/homelab/spark-gdm-rdp-recovery.md`:\n\n"
            "# Spark Black-Screen RDP & GDM Recovery\n"
            "When Ubuntu desktop sharing shows a black screen after a driver reset, restart GDM over interactive SSH:\n"
            '```bash\nssh -t Spark "sudo systemctl restart gdm"\n```\n'
            "Verify `systemctl is-active gdm` returns `active` before reconnecting."
        )
        t0 = time.perf_counter()
        with stream_event_context(events.append):
            code, payload = sess.execute_chat(prompt, mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        streamed_txt = "".join(str(e.get("delta") or "") for e in events if e.get("type") == "text_delta")
        reply = str(payload.get("response") or "")
        wiki_root = ctx.data_dir / "wiki" / "02_knowledge" / "homelab"
        saved_files = list(wiki_root.glob("*.md")) if wiki_root.is_dir() else []
        saved_content = saved_files[0].read_text(encoding="utf-8") if saved_files else ""
        t_steps = _extract_tool_msgs(sess.messages)
        no_bp = not bool(payload.get("awaiting_approval")) and ctx.store.get_active_workflow() is None
        no_leak = not _has_tag_leak(reply) and not _has_tag_leak(streamed_txt)
        ok = code == 200 and bool(saved_files) and "sudo systemctl restart gdm" in saved_content and 1 <= len(t_steps) <= 2 and dur_s < 20.0 and no_bp and no_leak
        return {"id": 1, "name": "Spark Incident Replay (`sudo`/`ssh` Wiki Save)", "passed": ok, "duration_s": dur_s, "tool_calls_count": len(t_steps), "tool_steps": t_steps, "llm_calls": rec.calls, "no_blueprint_hijack": no_bp, "no_think_leak": no_leak, "reply_preview": reply[:220]}


def scenario_02_question_plus_board_write() -> Dict[str, Any]:
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="auto") as ctx:
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        prompt = (
            "Please add a high-priority task card titled 'Audit NVMe SMART wear on Spark' to my Life & Home board right now, "
            "and could you also briefly explain why NVMe percentage-used metrics matter for ZFS mirror longevity?"
        )
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat(prompt, mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        reply = str(payload.get("response") or "")
        created = [it for it in get_project_graph(ctx.store).list_items() if "nvme" in it["title"].lower() or "smart" in it["title"].lower()]
        t_steps = _extract_tool_msgs(sess.messages)
        used_agile = any(s["name"] == "agile_manage" for s in t_steps)
        used_fast = bool(rec.calls) and all(c["model"] == VLLM_MODEL for c in rec.calls)
        explained = any(k in reply.lower() for k in ("zfs", "wear", "endurance", "mirror", "nand", "ssd"))
        ok = code == 200 and used_agile and bool(created) and explained and not _has_tag_leak(reply)
        return {"id": 2, "name": "`?` Question + Immediate Board Write", "passed": ok, "duration_s": dur_s, "used_fast_tier": used_fast, "tool_calls_count": len(t_steps), "tool_steps": t_steps, "llm_calls": rec.calls, "reply_preview": reply[:220]}


def scenario_03_goal_board_dependency_graph() -> Dict[str, Any]:
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="fast") as ctx:
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        prompt = (
            "On the Board, create a new software project titled 'Telemetry Collector v2', create a milestone inside it titled "
            "'M1 Core Pipeline', and add 3 work items linked to that milestone in dependency order: "
            "1) 'Define protobuf schema' (no dependencies), "
            "2) 'Implement batch ingester' (depends on item 1), and "
            "3) 'Add retention compaction' (depends on item 2)."
        )
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat(prompt, mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        pg = get_project_graph(ctx.store)
        projs = [p for p in pg.list_projects() if "telemetry collector" in p["title"].lower()]
        pid = projs[0]["project_id"] if projs else ""
        milestones = pg.list_milestones(pid) if pid else []
        items = pg.list_items(project_id=pid) if pid else []
        by_kw = {kw: hit for kw in ("protobuf", "batch", "retention") if (hit := next((it for it in items if kw in it["title"].lower()), None))}
        dep_ok = len(by_kw) == 3 and by_kw["protobuf"]["item_id"] in by_kw["batch"]["depends_on"] and by_kw["batch"]["item_id"] in by_kw["retention"]["depends_on"]
        t_steps = _extract_tool_msgs(sess.messages)
        err_steps = [s for s in t_steps if str(s["result_head"]).startswith("error:")]
        ok = code == 200 and bool(projs) and bool(milestones) and dep_ok and len(err_steps) == 0
        return {"id": 3, "name": "Structured Goal Board & Dependency Graph", "passed": ok, "duration_s": dur_s, "project_found": bool(projs), "milestone_found": bool(milestones), "dep_chain_ok": dep_ok, "error_retries": len(err_steps), "tool_calls_count": len(t_steps), "tool_steps": t_steps, "llm_calls": rec.calls, "reply_preview": str(payload.get("response") or "")[:220]}


def scenario_04_calendar_event_and_today() -> Dict[str, Any]:
    from companion.calendar_engine import get_calendar_manager
    from companion.llm import chat
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="fast") as ctx:
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        today_ymd = datetime.now().astimezone().strftime("%Y-%m-%d")
        prompt = (
            f"Schedule an appointment on my Calendar for today ({today_ymd}) at 15:30 titled "
            "'GPU Rack Thermal Inspection' with notes 'Check front intake dust filters', "
            "then read back today's calendar agenda and confirm the scheduled time and title."
        )
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat(prompt, mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        reply = str(payload.get("response") or "")
        matched = [e for e in get_calendar_manager(ctx.store).list_events(today_ymd, today_ymd) if "thermal" in e["title"].lower() and "15:30" in e["start_at"]]
        t_steps = _extract_tool_msgs(sess.messages)
        read_called = any(s["name"] == "calendar_manage" and any(k in str(s["args"]) for k in ("today", "window", "list_events", "week")) for s in t_steps)
        ok = code == 200 and bool(matched) and read_called and "15:30" in reply and "thermal" in reply.lower()
        return {"id": 4, "name": "Calendar Event + Local Today Agenda", "passed": ok, "duration_s": dur_s, "events_found": matched, "read_called": read_called, "tool_steps": t_steps, "llm_calls": rec.calls, "reply_preview": reply[:220]}


def scenario_05_plan_first_approve_execute() -> Dict[str, Any]:
    from companion.config import settings
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.safety import run_unit_tests
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="auto") as ctx:
        repo_dir = ctx.tmp / "plan_repo"
        _git_init_commit(repo_dir, {
            "README.md": "# Calculator Repo\n",
            "calculator.py": '"""Simple math helpers."""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n',
            "tests/test_calculator.py": "import unittest\nfrom calculator import add\n\n\nclass TestCalc(unittest.TestCase):\n    def test_add(self) -> None:\n        self.assertEqual(add(2, 3), 5)\n",
        })
        pg = get_project_graph(ctx.store)
        proj = pg.create_project(title="Calc App", kind="software", workspace_path=str(repo_dir), delivery_policy="local_only")
        pg.set_focus_project(proj["project_id"])
        settings.set_workspace(repo_dir, proj["project_id"])
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code1, plan_res = sess.execute_chat("Add `multiply(a: int, b: int) -> int` to `calculator.py` and add unit tests for `multiply` (including zero and negative numbers) in `tests/test_calculator.py`.", mode="plan", focus_project_id=proj["project_id"])
        plan_s = round(time.perf_counter() - t0, 2)
        paused_ok = code1 == 200 and bool(plan_res.get("awaiting_approval")) and "def multiply" not in (repo_dir / "calculator.py").read_text(encoding="utf-8")
        t1 = time.perf_counter()
        exec_code, exec_res = (sess.execute_chat("approve", mode="chat", focus_project_id=proj["project_id"]) if paused_ok else (0, {}))
        exec_s = round(time.perf_counter() - t1, 2)
        ut = run_unit_tests(root=repo_dir, timeout=20)
        ok = paused_ok and exec_code == 200 and "def multiply" in (repo_dir / "calculator.py").read_text(encoding="utf-8") and bool(ut.get("passed")) and int(ut.get("ran", 0)) >= 2 and not _has_tag_leak(str(plan_res.get("response") or "")) and not _has_tag_leak(str(exec_res.get("response") or ""))
        return {"id": 5, "name": "`Plan First` (`mode='plan'`) -> Approve -> Execute", "passed": ok, "duration_s": round(plan_s + exec_s, 2), "plan_s": plan_s, "exec_s": exec_s, "paused_readonly_ok": paused_ok, "tests_ran": ut.get("ran", 0), "tests_passed": ut.get("passed", False), "llm_calls": rec.calls}


def _run_scenario_06_candidate(provider_id: str, model_name: str) -> Dict[str, Any]:
    from companion.config import settings
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.safety import run_unit_tests
    from companion.sdlc_tools import _current_branch, _run_git_cmd
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(coding_provider=provider_id, coding_model=model_name, mode="coding") as ctx:
        repo_dir = ctx.tmp / "discount_repo"
        _git_init_commit(repo_dir, {
            ".agents/rules/repo-policy.md": "# Repository SDLC Rules\n1. Never commit directly onto `main` — create a `feat/*` or `fix/*` branch via `git_manage`.\n2. Delivery policy is `local_only` (never `git push`).\n3. Ensure `python -m unittest discover -s tests` passes with `OK` before committing.\n",
            "discount.py": '"""Order discount helper."""\n\n\ndef apply_percentage_discount(subtotal_cents: int, pct: float) -> int:\n    """Return discounted subtotal in cents, clamped to [0, subtotal_cents]."""\n    if subtotal_cents < 0:\n        raise ValueError("subtotal_cents must be non-negative")\n    # BUG: adds discount instead of subtracting and ignores clamping pct to [0, 100]\n    discount = round(subtotal_cents * (pct / 100.0))\n    return subtotal_cents + discount\n',
            "tests/test_discount.py": "import unittest\nfrom discount import apply_percentage_discount\n\n\nclass TestDiscount(unittest.TestCase):\n    def test_twenty_percent_off(self) -> None:\n        self.assertEqual(apply_percentage_discount(1000, 20.0), 800)\n\n    def test_over_hundred_clamps_to_zero(self) -> None:\n        self.assertEqual(apply_percentage_discount(1000, 150.0), 0)\n\n    def test_negative_pct_clamps_to_subtotal(self) -> None:\n        self.assertEqual(apply_percentage_discount(1000, -15.0), 1000)\n",
        })
        _, main_sha_before = _run_git_cmd(repo_dir, ["rev-parse", "main"])
        pg = get_project_graph(ctx.store)
        proj = pg.create_project(title="Discount Service", kind="software", workspace_path=str(repo_dir), delivery_policy="local_only")
        pg.set_focus_project(proj["project_id"])
        settings.set_workspace(repo_dir, proj["project_id"])
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        prompt = "Follow `sdlc-engineering`: inspect `.agents/rules/repo-policy.md`, switch to a new `feat/fix-discount` branch (never commit on `main`), fix the bug in `discount.py`, run `unittest` until all tests pass, and commit your changes cleanly."
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat(prompt, mode="chat", focus_project_id=proj["project_id"])
        dur_s = round(time.perf_counter() - t0, 2)
        wt_parent = repo_dir.parent / ".worktrees" / repo_dir.name
        wt_cands = [p for p in wt_parent.iterdir() if (p / ".git").exists()] if wt_parent.is_dir() else []
        target_dir = wt_cands[0] if (_current_branch(repo_dir) in ("main", "master") and wt_cands) else repo_dir
        cur_branch = _current_branch(target_dir)
        _, main_sha_after = _run_git_cmd(repo_dir, ["rev-parse", "main"])
        _, head_sha = _run_git_cmd(target_dir, ["rev-parse", "HEAD"])
        _, porc_target = _run_git_cmd(target_dir, ["status", "--porcelain=v1"])
        _, porc_main = _run_git_cmd(repo_dir, ["status", "--porcelain=v1"])
        porc = f"{porc_target}\n{porc_main}".strip() if target_dir != repo_dir else porc_target
        ut = run_unit_tests(root=target_dir, timeout=20)
        t_steps = _extract_tool_msgs(sess.messages)
        read_rules = any("repo-policy.md" in str(s["args"]) for s in t_steps if s["name"] == "read_file")
        err_steps = [s for s in t_steps if str(s["result_head"]).startswith(("error:", "SAFETY GUARD", "SYNTAX GUARD"))]
        comp_tokens = sum(c.get("completion_tokens", 0) for c in rec.calls)
        llm_time_s = sum(c.get("duration_ms", 0.0) for c in rec.calls) / 1000.0
        tok_s = round(comp_tokens / llm_time_s, 1) if llm_time_s > 0 else 0.0
        reply = str(payload.get("response") or "")
        ok = code == 200 and read_rules and cur_branch not in ("main", "master", "HEAD") and main_sha_before.strip() == main_sha_after.strip() and head_sha.strip() != main_sha_before.strip() and not porc.strip() and bool(ut.get("passed")) and int(ut.get("ran", 0)) >= 3 and not _has_tag_leak(reply)
        return {"model": model_name, "provider": provider_id, "passed": ok, "duration_s": dur_s, "llm_calls_count": len(rec.calls), "tool_calls_count": len(t_steps), "tool_error_steps": len(err_steps), "completion_tokens": comp_tokens, "tokens_per_sec": tok_s, "branch": cur_branch, "clean_tree": not bool(porc.strip()), "tests_passed": ut.get("passed", False), "tool_steps": t_steps, "llm_calls": rec.calls}


def scenario_06_tdd_bugfix_comparison() -> Dict[str, Any]:
    res_nemotron = _run_scenario_06_candidate("vllm-spark", VLLM_MODEL)
    res_qwen_coder = _run_scenario_06_candidate("ollama-lan", CODER_MODEL)
    return {"id": 6, "name": "Single-Repo TDD Bugfix (`sdlc-engineering`: Nemotron vs Qwen3-Coder)", "passed": bool(res_nemotron["passed"] and res_qwen_coder["passed"]), "duration_s": res_nemotron["duration_s"], "nemotron": res_nemotron, "qwen3_coder": res_qwen_coder}


def scenario_07_parallel_vllm_worktrees() -> Dict[str, Any]:
    from companion.background_jobs import BackgroundJobManager
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.safety import run_unit_tests
    from companion.worktree_runner import dispatch_parallel_ready_items, merge_worktree_into_base

    with IsolatedEnvContext(coding_provider="vllm-spark", coding_model=VLLM_MODEL, mode="coding") as ctx:
        repo_dir = ctx.tmp / "parallel_repo"
        _git_init_commit(repo_dir, {
            "README.md": "# Parallel Worktree Sandbox\n",
            "math_utils.py": '"""Math utilities."""\n\n\ndef identity_int(x: int) -> int:\n    return x\n',
            "text_utils.py": '"""Text utilities."""\n\n\ndef raw_trim(s: str) -> str:\n    return s.strip()\n',
            "tests/__init__.py": "",
        })
        pg = get_project_graph(ctx.store)
        proj = pg.create_project(title="Parallel Toolkit", kind="software", workspace_path=str(repo_dir), delivery_policy="local_only", max_parallel_workers=2)
        pid = proj["project_id"]
        c1 = pg.create_item(project_id=pid, title="Add clamp_int to math_utils", description="Edit ONLY `math_utils.py` to add `clamp_int(val: int, lo: int, hi: int) -> int` and create `tests/test_math_utils.py` using `unittest`.", owner="companion", status="ready", priority=1)
        c2 = pg.create_item(project_id=pid, title="Add slugify_title to text_utils", description="Edit ONLY `text_utils.py` to add `slugify_title(text: str) -> str` (lowercase alphanumeric words joined by `-`) and create `tests/test_text_utils.py` using `unittest`.", owner="companion", status="ready", priority=1)
        c3 = pg.create_item(project_id=pid, title="Build combined summary CLI", description="Depends on both utility cards.", owner="companion", status="ready", depends_on=[c1["item_id"], c2["item_id"]], priority=2)
        rec = LiveCallRecorder(chat)
        jm = BackgroundJobManager(store=ctx.store, chat_fn=rec)
        t0 = time.perf_counter()
        fan_res = dispatch_parallel_ready_items(project_id=pid, max_workers=2, run_in_background=False, store=ctx.store, chat_fn=rec, job_mgr=jm)
        dur_s = round(time.perf_counter() - t0, 2)
        it1, it2 = pg.get_item(c1["item_id"]), pg.get_item(c2["item_id"])
        deliv1, deliv2 = it1.get("deliverable") or {}, it2.get("deliverable") or {}
        files1, files2 = set(deliv1.get("changed_files") or []), set(deliv2.get("changed_files") or [])
        zero_collision = bool(files1) and bool(files2) and files1.isdisjoint(files2)
        overlapped = rec.peak_concurrency >= 2
        m1 = merge_worktree_into_base(c1["item_id"], store=ctx.store) if it1["status"] == "verify" else {"ok": False}
        m2 = merge_worktree_into_base(c2["item_id"], store=ctx.store) if it2["status"] == "verify" else {"ok": False}
        it3_after = pg.get_item(c3["item_id"])
        ut_main = run_unit_tests(root=repo_dir, timeout=20)
        ok = bool(fan_res.get("ok")) and overlapped and zero_collision and bool(deliv1.get("unified_diff")) and bool(deliv2.get("unified_diff")) and bool(m1.get("ok")) and bool(m2.get("ok")) and it3_after["column_key"] == "ready" and bool(ut_main.get("passed"))
        return {"id": 7, "name": "Parallel vLLM Worktree Execution (`2` Cards at Once)", "passed": ok, "duration_s": dur_s, "peak_gpu_concurrency": rec.peak_concurrency, "zero_file_collision": zero_collision, "card3_unblocked_status": it3_after["column_key"], "main_tests_passed": ut_main.get("passed", False), "llm_calls_count": len(rec.calls)}


def scenario_08_parallel_subagents_fanout() -> Dict[str, Any]:
    import companion.agent as agent_mod
    from companion.config import settings
    from companion.llm import chat
    from companion.streaming import stream_event_context
    import companion.worktree_runner as wr_mod
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="fast") as ctx:
        ws_dir = ctx.tmp / "subagent_ws"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "auth_service.py").write_text("TOKEN_TTL_SECONDS = 945\ndef verify_jwt() -> bool:\n    return True\n", encoding="utf-8")
        (ws_dir / "cache_layer.py").write_text("EVICT_WINDOW_SECONDS = 1820\ndef purge_lru() -> int:\n    return 0\n", encoding="utf-8")
        (ws_dir / "rate_limiter.py").write_text("BURST_CAP_REQUESTS = 375\ndef allow_request() -> bool:\n    return True\n", encoding="utf-8")
        settings.set_workspace(ws_dir, "subagent-fanout-ws")
        rec = LiveCallRecorder(chat)
        orig_a, orig_w = agent_mod.chat, wr_mod.chat
        agent_mod.chat = wr_mod.chat = rec
        try:
            sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
            prompt = "Call `workflow_manage` with `action='parallel_subagents'` across 3 concurrent tasks to inspect `auth_service.py`, `cache_layer.py`, and `rate_limiter.py`, then synthesize a concise summary reporting the exact numeric constant defined in each of the 3 files."
            t0 = time.perf_counter()
            with stream_event_context([].append):
                code, payload = sess.execute_chat(prompt, mode="chat")
            dur_s = round(time.perf_counter() - t0, 2)
        finally:
            agent_mod.chat, wr_mod.chat = orig_a, orig_w
        reply = str(payload.get("response") or "")
        t_steps = _extract_tool_msgs(sess.messages)
        used_parallel = any(s["name"] == "workflow_manage" and "parallel_subagents" in str(s["args"]) for s in t_steps)
        has_all_vals = all(v in reply for v in ("945", "1820", "375"))
        ok = code == 200 and used_parallel and has_all_vals and not _has_tag_leak(reply)
        return {"id": 8, "name": "Parallel Subagents Fan-Out (`parallel_subagents`)", "passed": ok, "duration_s": dur_s, "peak_gpu_concurrency": rec.peak_concurrency, "used_parallel_subagents": used_parallel, "grounded_all_constants": has_all_vals, "llm_calls_count": len(rec.calls), "tool_steps": t_steps}


def scenario_09_dev_preview_browser_dogfood() -> Dict[str, Any]:
    from companion.config import settings
    from companion.llm import chat
    from companion.project_graph import get_project_graph
    from companion.sdlc_tools import preview_manager
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(coding_provider="vllm-spark", coding_model=VLLM_MODEL, mode="coding") as ctx:
        repo_dir = ctx.tmp / "preview_app"
        app_py = (
            "import argparse, http.server, json, socketserver\n"
            'HTML = b"<!doctype html><html><head><meta charset=utf-8><title>Edge Ops</title></head><body><main data-testid=ops-dashboard><h1>Edge Node Healthy</h1></main></body></html>"\n'
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({'ok': True}).encode() if self.path == '/api/health' else HTML\n"
            "        self.send_response(200); self.send_header('Content-Type', 'application/json' if self.path == '/api/health' else 'text/html'); self.end_headers(); self.wfile.write(body)\n"
            "    def log_message(self, *a): return\n"
            "if __name__ == '__main__':\n"
            "    ap = argparse.ArgumentParser(); ap.add_argument('--port', type=int, default=8091); a = ap.parse_args()\n"
            "    with socketserver.ThreadingTCPServer(('127.0.0.1', a.port), H) as s: s.serve_forever()\n"
        )
        _git_init_commit(repo_dir, {"app_server.py": app_py, "README.md": "# Preview App\n"})
        pg = get_project_graph(ctx.store)
        proj = pg.create_project(title="Preview App", kind="software", workspace_path=str(repo_dir), delivery_policy="local_only")
        pg.set_focus_project(proj["project_id"])
        settings.set_workspace(repo_dir, proj["project_id"])
        out_shots = repo_dir / "dogfood_out"
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        prompt = f"Boot `app_server.py` using `dev_preview(action='start', command='{sys.executable} app_server.py --port {{port}}', health_path='/api/health')`, then run `workflow_manage(action='run_script', skill='browser-dogfood', script='browser_dogfood.py', script_args=['--url', '<PREVIEW_URL>/', '--out-dir', '{out_shots.as_posix()}', '--expect-selector', \"[data-testid='ops-dashboard']\", '--expect-text', 'Edge Node Healthy'])`, then call `dev_preview(action='stop')`."
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat(prompt, mode="chat", focus_project_id=proj["project_id"])
        dur_s = round(time.perf_counter() - t0, 2)
        t_steps = _extract_tool_msgs(sess.messages)
        started = any(s["name"] == "dev_preview" and "start" in str(s["args"]) for s in t_steps)
        dogfood_ran = any(m.get("role") == "tool" and '"console_errors": []' in str(m.get("content") or "") and '"ok": true' in str(m.get("content") or "").lower() for m in sess.messages)
        desktop_ok, mobile_ok = (out_shots / "desktop_1440x900.png").is_file(), (out_shots / "mobile_390x844.png").is_file()
        zero_orphans = len(preview_manager.list_instances()) == 0
        ok = code == 200 and started and dogfood_ran and desktop_ok and mobile_ok and zero_orphans and not _has_tag_leak(str(payload.get("response") or ""))
        return {"id": 9, "name": "Live `dev_preview` + `browser-dogfood` Loop", "passed": ok, "duration_s": dur_s, "dogfood_ok": dogfood_ran, "desktop_png": desktop_ok, "mobile_png": mobile_ok, "zero_orphans": zero_orphans, "tool_steps": t_steps}


def scenario_10_graceful_awaiting_approval_stop() -> Dict[str, Any]:
    from companion.archive_and_approvals import get_approval_queue
    from companion.core_actions import execute_core_action
    from companion.llm import chat
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="fast") as ctx:
        execute_core_action("wiki", {"subaction": "save", "path": "02_knowledge/homelab/core-firewall-policy.md", "title": "Core Firewall Policy", "summary": "Canonical LAN firewall port allowlist.", "content": "# Core Firewall Policy\n\n## Overview\nAllow SSH `22/tcp` from management VLAN.\n\n## Key Details\nDefault deny inbound.\n", "tags": ["firewall", "security"]}, actor="user", store=ctx.store)
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code, payload = sess.execute_chat("Update the existing Wiki note `02_knowledge/homelab/core-firewall-policy.md` to add port `8006/tcp` for the LAN vLLM server.", mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        reply = str(payload.get("response") or "")
        tool_msgs = [m for m in sess.messages if m.get("role") == "tool"]
        first_apr_idx = next((i for i, m in enumerate(tool_msgs) if "AWAITING_USER_APPROVAL" in str(m.get("content") or "")), -1)
        trailing = len(tool_msgs) - (first_apr_idx + 1) if first_apr_idx != -1 else 999
        waiting = get_approval_queue(ctx.store).list_requests(status="waiting")
        ok = code == 200 and first_apr_idx != -1 and trailing == 0 and len(waiting) == 1 and any(w in reply.lower() for w in ("approval", "approve", "pending", "awaiting", "permission")) and not _has_tag_leak(reply)
        return {"id": 10, "name": "Graceful `AWAITING_USER_APPROVAL` Stop", "passed": ok, "duration_s": dur_s, "trailing_tool_calls_after_approval": trailing, "waiting_approvals": len(waiting), "tool_steps": _extract_tool_msgs(sess.messages)}


def scenario_11_deep_reasoning_cot_hygiene() -> Dict[str, Any]:
    from companion.config import settings
    from companion.llm import chat
    from companion.streaming import stream_event_context
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="reasoning") as ctx:
        ws_dir = ctx.tmp / "ledger_ws"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "ledger_audit.py").write_text(
            "def compute_reported_balance(opening_cents: int, tx_list: list[dict]) -> int:\n"
            "    bal = opening_cents\n"
            "    for tx in tx_list:\n"
            "        amt = int(tx['amount_cents'])\n"
            "        if tx['kind'] == 'credit': bal += amt\n"
            "        elif tx['kind'] == 'debit': bal -= (amt + int(tx.get('fee_cents', 0)))\n"
            "        elif tx['kind'] == 'wire_out': bal -= amt\n"
            "    return bal\n",
            encoding="utf-8",
        )
        (ws_dir / "transactions.json").write_text(json.dumps({"account_id": "ACC-402", "opening_cents": 50000, "reported_ending_cents": 41200, "transactions": [{"id": "t1", "kind": "credit", "amount_cents": 2500, "fee_cents": 0}, {"id": "t2", "kind": "debit", "amount_cents": 4250, "fee_cents": 50}, {"id": "t3", "kind": "wire_out", "amount_cents": 7000, "fee_cents": 17}]}), encoding="utf-8")
        settings.set_workspace(ws_dir, "ledger-audit-ws")
        rec, events = LiveCallRecorder(chat), []
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        t0 = time.perf_counter()
        with stream_event_context(events.append):
            code, payload = sess.execute_chat("Read `ledger_audit.py` and `transactions.json` using tools, determine why account `ACC-402` has a 17-cent discrepancy, and state the exact true ending balance in cents once `wire_out` fees are properly deducted. Do not modify any files.", mode="chat")
        dur_s = round(time.perf_counter() - t0, 2)
        reply = str(payload.get("response") or "")
        streamed_txt = "".join(str(e.get("delta") or "") for e in events if e.get("type") == "text_delta")
        streamed_think = "".join(str(e.get("delta") or "") for e in events if e.get("type") == "thinking_delta")
        t_steps = _extract_tool_msgs(sess.messages)
        http_400s = [c for c in rec.calls if not c.get("ok") and "400" in str(c.get("error") or "")]
        no_leak = not _has_tag_leak(reply) and not _has_tag_leak(streamed_txt)
        grounded = "41183" in reply.replace(",", "") and ("wire_out" in reply or "fee" in reply.lower())
        ok = code == 200 and len(t_steps) >= 1 and len(http_400s) == 0 and len(streamed_think) > 0 and no_leak and grounded
        return {"id": 11, "name": "Deep Reasoning & CoT Hygiene (`qwen3.8:latest`)", "passed": ok, "duration_s": dur_s, "thinking_stream_chars": len(streamed_think), "http_400_count": len(http_400s), "no_think_leak": no_leak, "grounded_41183": grounded}


def scenario_12_turn2_attachment_recall() -> Dict[str, Any]:
    from companion.attachments import ingest_attachment
    from companion.llm import chat
    from companion.streaming import stream_event_context
    from surfaces.api_extensions import dispatch_chat_or_job_with_attachments
    from surfaces.web_server import CompanionWebSession

    with IsolatedEnvContext(mode="fast") as ctx:
        spec_md = b"# Homelab Rack UPS (`APS-3000RT`)\n- Nominal Output: `208V / 2700W`\n- Float Charge Voltage: `54.4 VDC`\n- SNMP Trap OID: `1.3.6.1.4.1.318.1.1.1.2.2.3.0`\n- Maintenance Bypass Breaker ID: `MBP-208V-30A`\n"
        att_id = ingest_attachment("rack_ups_spec.md", base64.b64encode(spec_md).decode("ascii"), store=ctx.store)["attachment"]["id"]
        rec = LiveCallRecorder(chat)
        sess = CompanionWebSession(store=ctx.store, chat_fn=rec)
        t0 = time.perf_counter()
        with stream_event_context([].append):
            code1, _ = dispatch_chat_or_job_with_attachments(sess, {"message": "Summarize this UPS specification in 3 bullet points.", "attachment_ids": [att_id]}, is_async=False)
            code2, res2 = dispatch_chat_or_job_with_attachments(sess, {"message": "Without me re-uploading the file, what are the exact SNMP Trap OID, Float Charge Voltage, and Maintenance Bypass Breaker ID from that UPS spec?"}, is_async=False)
        dur_s = round(time.perf_counter() - t0, 2)
        reply2 = str(res2.get("response") or "")
        recalled = "1.3.6.1.4.1.318.1.1.1.2.2.3.0" in reply2 and "54.4" in reply2 and "MBP-208V-30A" in reply2
        ok = code1 == 200 and code2 == 200 and recalled and not _has_tag_leak(reply2)
        return {"id": 12, "name": "Turn-2 Document Attachment Recall", "passed": ok, "duration_s": dur_s, "recalled_all_3_facts": recalled}


ALL_SCENARIOS: Dict[int, Callable[[], Dict[str, Any]]] = {
    1: scenario_01_spark_wiki_save, 2: scenario_02_question_plus_board_write, 3: scenario_03_goal_board_dependency_graph,
    4: scenario_04_calendar_event_and_today, 5: scenario_05_plan_first_approve_execute, 6: scenario_06_tdd_bugfix_comparison,
    7: scenario_07_parallel_vllm_worktrees, 8: scenario_08_parallel_subagents_fanout, 9: scenario_09_dev_preview_browser_dogfood,
    10: scenario_10_graceful_awaiting_approval_stop, 11: scenario_11_deep_reasoning_cot_hygiene, 12: scenario_12_turn2_attachment_recall,
}


def run_self_test() -> int:
    assert len(ALL_SCENARIOS) == 12
    assert _has_tag_leak("ok </think>") and _has_tag_leak("<function=read_file>") and not _has_tag_leak("Clean output")
    print(json.dumps({"ok": True, "scenarios": len(ALL_SCENARIOS)}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Live 12-Scenario Real-Model Calibration Suite")
    ap.add_argument("--self-test", action="store_true", help="Run fast deterministic self-test without LAN calls")
    ap.add_argument("--only", type=str, default="", help="Comma-separated scenario IDs (e.g. 1,2,6)")
    ap.add_argument("--out", type=str, default="", help="Optional JSON report path")
    args = ap.parse_args()
    if args.self_test:
        return run_self_test()
    selected = [int(x.strip()) for x in args.only.split(",") if x.strip()] if args.only else list(ALL_SCENARIOS.keys())
    results: List[Dict[str, Any]] = []
    suite_t0 = time.perf_counter()
    for sid in selected:
        fn = ALL_SCENARIOS[sid]
        print(f"[RUNNING] Scenario {sid:02d}...", flush=True)
        try:
            res = fn()
        except Exception as exc:
            res = {"id": sid, "name": fn.__name__, "passed": False, "duration_s": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        print(f"  -> [{'PASS' if res.get('passed') else 'FAIL'}] Scenario {sid:02d}: {res.get('name')} ({res.get('duration_s')}s)", flush=True)
        results.append(res)
    total_s = round(time.perf_counter() - suite_t0, 2)
    passed_n = sum(1 for r in results if r.get("passed"))
    summary = {"passed_count": passed_n, "total_count": len(results), "total_duration_s": total_s, "results": results}
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== SUMMARY: {passed_n}/{len(results)} PASSED in {total_s}s ===", flush=True)
    return 0 if passed_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
