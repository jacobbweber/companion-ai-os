"""
Live Local-Model Flight Evaluation Harness (`companion/eval_harness.py` — A-Frame Application Layer).

Stress-tests Companion's cognitive harness against real configured local models (`fast` + `reasoning`)
inside a disposable `WorkspaceContext` (`ADR-007`) so Companion core (`companion-self`) is never mutated:
1. Flight 1 (`ast_symbol_grounding`)      : Symbol-sliced `read_file(..., symbol=...)` accuracy & anti-hallucination
2. Flight 2 (`inline_linter_compliance`)  : Clean Python `edit_file` passing `<5ms` AST & unused-import micro-linter
3. Flight 3 (`blueprint_plan_approve_e2e`): Full `Plan -> Pause (Read-Only Lock) -> Approve -> Implement & Test` loop
4. Flight 4 (`domain_neutral_goal_reflect`): Non-Python multi-artifact workspace goal + `REFLECT` fact distillation
"""

import ast
import json
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from companion.autonomy import run_autonomous_goal, scan_placeholder_violations
from companion.blueprint import approve_blueprint_and_build, create_or_revise_blueprint
from companion.config import TierConfig, settings, workspace_context
from companion.hygiene import find_unused_imports_in_source
from companion.llm import chat, probe_endpoint
from companion.memory_store import MemoryStore
from companion.safety import CheckpointManager, run_unit_tests, validate_python_syntax
from companion.workflow_graph import WorkflowEngine


@dataclass(frozen=True)
class FlightResult:
    """Result record for a single behavioral flight evaluation scenario."""

    flight_id: str
    title: str
    passed: bool
    duration_s: float
    detail: str


def _run_flight_1_symbol_grounding(
    chat_fn: Callable[..., Dict[str, Any]],
    verbose: bool,
) -> FlightResult:
    """Verify the model uses `read_file(..., symbol=...)` to inspect a target method accurately."""
    from companion.agent import run_agent_turn

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp) / "flight1_ws"
        ws_root.mkdir(parents=True, exist_ok=True)
        (ws_root / "billing.py").write_text(
            '"""Billing fixture."""\n\n'
            "class InvoiceEngine:\n"
            "    def legacy_tax(self) -> int:\n"
            "        return 7\n\n"
            "    def compute_fee(self) -> str:\n"
            '        return "FEE_CODE_9417"\n',
            encoding="utf-8",
        )
        store = MemoryStore(db_path=Path(tmp) / "f1.db")
        try:
            with workspace_context(ws_root, "eval-flight-1"):
                msgs: List[Dict[str, Any]] = [
                    {
                        "role": "user",
                        "content": (
                            "Use `read_file` with `symbol='InvoiceEngine.compute_fee'` on `billing.py` and tell me "
                            "the exact string code it returns. Do not modify any files."
                        ),
                    }
                ]
                reply = run_agent_turn(msgs, max_iters=4, chat_fn=chat_fn, store=store, session_tag="eval:flight1", verbose=verbose, auto_promote=False)
                used_read = any(m.get("role") == "tool" and "FEE_CODE_9417" in str(m.get("content", "")) for m in msgs)
                ok = used_read and ("FEE_CODE_9417" in reply)
                detail = "Model sliced `InvoiceEngine.compute_fee` via AST and grounded `FEE_CODE_9417`." if ok else f"Expected tool read + `FEE_CODE_9417`, got: {reply[:180]}"
        finally:
            store.close()
    return FlightResult("ast_symbol_grounding", "AST Symbol-Sliced Read & Grounding", ok, round(time.perf_counter() - t0, 2), detail)


def _run_flight_2_inline_linter_edit(
    chat_fn: Callable[..., Dict[str, Any]],
    verbose: bool,
) -> FlightResult:
    """Verify the model edits a Python module cleanly under `lint_single_file_edit` with zero unused imports."""
    from companion.agent import run_agent_turn

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp) / "flight2_ws"
        ws_root.mkdir(parents=True, exist_ok=True)
        mod_path = ws_root / "math_ops.py"
        mod_path.write_text(
            '"""Math operations."""\n\n'
            "def add_pair(a: int, b: int) -> int:\n"
            "    return a + b\n",
            encoding="utf-8",
        )
        store = MemoryStore(db_path=Path(tmp) / "f2.db")
        try:
            with workspace_context(ws_root, "eval-flight-2"):
                msgs: List[Dict[str, Any]] = [
                    {
                        "role": "user",
                        "content": (
                            "Read `math_ops.py` and use `edit_file` to add a function `clamp_score(val: int, lo: int = 0, hi: int = 100) -> int` "
                            "that clamps `val` between `lo` and `hi`. Keep zero unused imports."
                        ),
                    }
                ]
                run_agent_turn(msgs, max_iters=6, chat_fn=chat_fn, store=store, session_tag="eval:flight2", verbose=verbose, auto_promote=False)
                src = mod_path.read_text(encoding="utf-8")
                syn_ok, _ = validate_python_syntax(src, filename="math_ops.py")
                unused = find_unused_imports_in_source(ast.parse(src, filename="math_ops.py"), "math_ops.py") if syn_ok else ["syntax_error"]
                ns: Dict[str, Any] = {}
                if syn_ok:
                    exec(compile(src, "math_ops.py", "exec"), ns)
                clamp_fn = ns.get("clamp_score")
                behav_ok = callable(clamp_fn) and clamp_fn(-5) == 0 and clamp_fn(42) == 42 and clamp_fn(250) == 100
                ok = syn_ok and not unused and behav_ok
                detail = (
                    "`clamp_score` edited cleanly (valid AST, 0 unused imports, edge values verified)."
                    if ok
                    else f"syn_ok={syn_ok}, unused={unused}, callable={callable(clamp_fn)}"
                )
        finally:
            store.close()
    return FlightResult("inline_linter_compliance", "Surgical Edit & Inline Micro-Linter Hygiene", ok, round(time.perf_counter() - t0, 2), detail)


def _run_flight_3_blueprint_approve_build(
    chat_fn: Callable[..., Dict[str, Any]],
    verbose: bool,
) -> FlightResult:
    """Verify full `Plan (Read-Only Lock) -> Pause -> Approve -> Implement & Unit-Test` workflow."""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp) / "flight3_ws"
        ws_root.mkdir(parents=True, exist_ok=True)
        (ws_root / "README.md").write_text("# Stats Sandbox\nEmpty workspace ready for `stats_box.py`.\n", encoding="utf-8")
        store = MemoryStore(db_path=Path(tmp) / "f3.db")
        cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups", store=store)
        try:
            with workspace_context(ws_root, "eval-flight-3"):
                bp = create_or_revise_blueprint(
                    goal_or_feedback=(
                        "Create `stats_box.py` with `summarize_numbers(values: list[float]) -> dict` returning "
                        "`{'count': ..., 'min': ..., 'max': ..., 'mean': ...}` (raising `ValueError` on empty list), "
                        "and create `tests/test_stats_box.py` using Python `unittest` covering normal and empty-list cases."
                    ),
                    max_phase_iters=6,
                    chat_fn=chat_fn,
                    store=store,
                    cp_mgr=cp_mgr,
                    root=ws_root,
                    verbose=verbose,
                )
                paused_cleanly = bool(bp.get("awaiting_approval")) and not (ws_root / "stats_box.py").exists()
                if not paused_cleanly:
                    return FlightResult(
                        "blueprint_plan_approve_e2e",
                        "Blueprint Plan -> Pause -> Approve -> Verified Build",
                        False,
                        round(time.perf_counter() - t0, 2),
                        f"Blueprint did not pause cleanly with read-only lock: status={bp.get('status')}",
                    )

                build_res = approve_blueprint_and_build(
                    max_phase_iters=8,
                    max_recoveries=2,
                    chat_fn=chat_fn,
                    store=store,
                    cp_mgr=cp_mgr,
                    root=ws_root,
                    run_doctor_tests=True,
                    verbose=verbose,
                )
                mod_exists = (ws_root / "stats_box.py").is_file()
                test_files = list((ws_root / "tests").glob("test_*.py")) if (ws_root / "tests").is_dir() else list(ws_root.glob("test_*.py"))
                t_res = run_unit_tests(root=ws_root, timeout=30)
                stubs = scan_placeholder_violations([ws_root / "stats_box.py", *test_files], root=ws_root)
                ok = bool(build_res.get("passed")) and mod_exists and bool(test_files) and bool(t_res.get("passed")) and int(t_res.get("ran", 0)) >= 1 and not stubs
                detail = (
                    f"Paused read-only at `PLAN`, then built `stats_box.py` + {len(test_files)} test file(s) (`ran={t_res.get('ran', 0)}` tests, 0 stubs)."
                    if ok
                    else f"build_passed={build_res.get('passed')}, mod={mod_exists}, tests={len(test_files)}, ran={t_res.get('ran', 0)}, stubs={stubs}"
                )
        finally:
            store.close()
    return FlightResult("blueprint_plan_approve_e2e", "Blueprint Plan -> Pause -> Approve -> Verified Build", ok, round(time.perf_counter() - t0, 2), detail)


def _run_flight_4_domain_neutral_goal_and_reflect(
    chat_fn: Callable[..., Dict[str, Any]],
    verbose: bool,
) -> FlightResult:
    """Verify domain-neutral structured config/artifact DAG verification + post-`VERIFY` `REFLECT` fact compounding."""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = Path(tmp) / "flight4_ws"
        ws_root.mkdir(parents=True, exist_ok=True)
        (ws_root / "README.md").write_text("# Structured Artifact Sandbox\n", encoding="utf-8")
        store = MemoryStore(db_path=Path(tmp) / "f4.db")
        cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups", store=store)
        try:
            with workspace_context(ws_root, "eval-flight-4"):
                res = run_autonomous_goal(
                    goal=(
                        "Create `service_manifest.toml` with `[service]` table setting `name = 'edge-gateway'` and `port = 8095`, "
                        "and create `rollout_policy.json` setting `{\"strategy\": \"canary\", \"max_surge\": 1}`."
                    ),
                    max_phase_iters=6,
                    max_recoveries=1,
                    chat_fn=chat_fn,
                    store=store,
                    cp_mgr=cp_mgr,
                    root=ws_root,
                    run_doctor_tests=True,
                    verbose=verbose,
                )
                toml_p, json_p = ws_root / "service_manifest.toml", ws_root / "rollout_policy.json"
                toml_ok = toml_p.is_file() and tomllib.loads(toml_p.read_text(encoding="utf-8")).get("service", {}).get("port") == 8095
                json_ok = json_p.is_file() and json.loads(json_p.read_text(encoding="utf-8")).get("strategy") == "canary"
                facts = store.query_knowledge_facts("deliverables", project_id="eval-flight-4")
                reflect_ok = any("service_manifest.toml" in f["value"] for f in facts)
                ok = bool(res.get("passed")) and toml_ok and json_ok and reflect_ok
                detail = (
                    "Verified non-Python TOML + JSON artifacts via `GoalPlan` DAG and recorded `REFLECT` deliverables in `KnowledgePort`."
                    if ok
                    else f"passed={res.get('passed')}, toml_ok={toml_ok}, json_ok={json_ok}, reflect_ok={reflect_ok}"
                )
        finally:
            store.close()
    return FlightResult("domain_neutral_dag_reflect", "Domain-Neutral Artifact DAG & REFLECT Distillation", ok, round(time.perf_counter() - t0, 2), detail)


def format_flight_eval_report(
    flights: List[FlightResult],
    tier_health: Dict[str, str],
    total_s: float,
) -> str:
    """Format a plain-English Flight Readiness Scorecard."""
    passed_cnt = sum(1 for f in flights if f.passed)
    badge = "ALL FLIGHTS PASSED" if (flights and passed_cnt == len(flights)) else "ISSUES DETECTED"
    lines = [
        f"=== Companion Live Model Flight Eval: {badge} ({passed_cnt}/{len(flights)} in {total_s:.1f}s) ===",
        "Model Endpoints:",
        *[f"  * `{tier}`: {status}" for tier, status in tier_health.items()],
        "",
        "Flight Scenarios:",
    ]
    for idx, f in enumerate(flights, 1):
        tag = "PASS" if f.passed else "FAIL"
        lines.append(f"  [{tag}] Flight {idx} (`{f.flight_id}` | {f.duration_s}s) — {f.title}")
        lines.append(f"         {f.detail}")
    return "\n".join(lines)


def _run_single_coding_candidate(
    label: str,
    cand_tier: TierConfig,
    chat_fn: Callable[..., Dict[str, Any]],
    verbose: bool,
) -> Dict[str, Any]:
    """Benchmark one `coding` tier model on an identical 3-task `IMPLEMENT + VERIFY + Git` race."""
    calls = [0]

    def _counted_chat(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        calls[0] += 1
        return chat_fn(*args, **kwargs)

    t0 = time.perf_counter()
    with settings.tier_override_context("coding", cand_tier):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = Path(tmp) / "bench_ws"
            ws_root.mkdir(parents=True, exist_ok=True)
            (ws_root / "README.md").write_text("# Interval Merge Sandbox\n", encoding="utf-8")
            store = MemoryStore(db_path=Path(tmp) / "bench.db")
            cp_mgr = CheckpointManager(root=ws_root, backup_dir=ws_root / ".companion_backups", store=store)
            wf = WorkflowEngine(store=store, cp_manager=cp_mgr, root=ws_root)
            try:
                with workspace_context(ws_root, f"bench-{label}"):
                    st = wf.start_workflow("Build `intervals.py` with `merge_intervals` and unit tests", "engineering_feature", "All interval tests pass.")
                    for target_st in ("DISCOVER", "DESIGN", "PLAN"):
                        wf.transition(target_st, f"Seeded {target_st} for controlled coding benchmark", run_tests_on_verify=False)
                    if st.workflow_id is not None:
                        store.update_workflow_metadata(
                            st.workflow_id,
                            {
                                "discovery_handoff": "Empty workspace `bench_ws` with `README.md`.",
                                "design_handoff": "Pure stdlib `intervals.py` exporting `merge_intervals(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]` + `tests/test_intervals.py`.",
                                "plan_handoff": "Task 1: Build `intervals.py` & `tests/test_intervals.py`. Task 2: Check negative/single-point edge cases in `tests/test_intervals.py`. Task 3: Run full verification.",
                            },
                        )
                    store.add_task(
                        "Create `intervals.py` with `merge_intervals(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]` "
                        "(sorting and merging overlapping or touching closed intervals, e.g. `[(1,3),(2,6),(8,10)] -> [(1,6),(8,10)]`) "
                        "and `tests/test_intervals.py` using `unittest`.",
                        priority=1,
                        workflow_id=st.workflow_id,
                    )
                    store.add_task(
                        "Ensure `tests/test_intervals.py` covers negative coordinates and single-point intervals.",
                        priority=2,
                        workflow_id=st.workflow_id,
                    )
                    store.add_task("Run full verification suite via unittest discover.", priority=3, workflow_id=st.workflow_id)
                    res = run_autonomous_goal(
                        "approve", max_phase_iters=7, max_recoveries=1, chat_fn=_counted_chat,
                        store=store, cp_mgr=cp_mgr, root=ws_root, run_doctor_tests=True,
                        verbose=verbose, resume=True, auto_git_commit=True,
                    )
                    dur = round(time.perf_counter() - t0, 2)
                    mod_p = ws_root / "intervals.py"
                    src = mod_p.read_text(encoding="utf-8") if mod_p.is_file() else ""
                    syn_ok, _ = validate_python_syntax(src, filename="intervals.py") if src else (False, "missing")
                    unused = find_unused_imports_in_source(ast.parse(src, filename="intervals.py"), "intervals.py") if syn_ok else ["missing"]
                    ns: Dict[str, Any] = {}
                    if syn_ok:
                        exec(compile(src, "intervals.py", "exec"), ns)
                    fn = ns.get("merge_intervals")
                    behav = (
                        callable(fn)
                        and [tuple(x) for x in fn([(8, 10), (1, 3), (2, 6)])] == [(1, 6), (8, 10)]
                        and list(fn([])) == []
                        and [tuple(x) for x in fn([(1, 2), (2, 4)])] == [(1, 4)]
                    )
                    t_res = run_unit_tests(root=ws_root, timeout=20)
                    auto_skips = sum(1 for t in store.get_all_tasks() if "Auto-verified" in str(t.get("verification_result", "")))
                    ok = bool(res.get("passed")) and syn_ok and not unused and behav and bool(t_res.get("passed")) and int(t_res.get("ran", 0)) >= 1
                    return {
                        "label": label, "model": cand_tier.model, "base_url": cand_tier.base_url,
                        "passed": ok, "duration_s": dur, "llm_calls": calls[0],
                        "tests_ran": int(t_res.get("ran", 0)), "unused_imports": len(unused),
                        "auto_skipped_tasks": auto_skips, "git_commit": str(res.get("git_commit", "")),
                    }
            finally:
                store.close()


def run_coding_model_comparison(
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    check_endpoints: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run a head-to-head `IMPLEMENT` coding race (`fast` vs dedicated/discovered `coding` model)."""
    fast_cfg = settings.resolve_tier("fast")
    code_cfg = settings.resolve_tier("coding")
    reas_cfg = settings.resolve_tier("reasoning")
    cand_a = TierConfig("coding", fast_cfg.base_url, fast_cfg.model, fast_cfg.api_key, fast_cfg.context_window_tokens, 0.15, True)
    cand_b = (
        TierConfig("coding", code_cfg.base_url, code_cfg.model, code_cfg.api_key, code_cfg.context_window_tokens, 0.15, True)
        if code_cfg.is_dedicated
        else TierConfig("coding", reas_cfg.base_url, "qwen3-coder:latest", reas_cfg.api_key, reas_cfg.context_window_tokens, 0.15, True)
    )

    candidates: List[Tuple[str, TierConfig]] = [("fast-default", cand_a)]
    if (cand_b.base_url, cand_b.model) != (cand_a.base_url, cand_a.model):
        candidates.append(("specialist-coder", cand_b))

    online_cands: List[Tuple[str, TierConfig]] = []
    if check_endpoints and chat_fn is chat:
        for lbl, tc in candidates:
            ok, info = probe_endpoint(tc.base_url, tc.api_key, timeout=3.5)
            if ok:
                online_cands.append((lbl, tc))
            elif lbl == "fast-default":
                return {"passed": False, "status": "OFFLINE", "candidates": [], "summary": f"=== Coding Model Benchmark: OFFLINE ===\n`{tc.base_url}` unreachable: {info}"}
    else:
        online_cands = candidates

    results = [_run_single_coding_candidate(lbl, tc, chat_fn, verbose) for lbl, tc in online_cands]
    passing = [r for r in results if r["passed"]]
    winner = min(passing, key=lambda r: (r["duration_s"], r["llm_calls"]))["model"] if passing else "none"
    lines = [
        f"=== Companion Head-to-Head Coding Tier Benchmark (Winner: `{winner}`) ===",
        "Scenario: 3-Task Controlled `IMPLEMENT + VERIFY + Auto-Git` Race (`intervals.py` + `unittest` + Carry-Forward)",
        "",
    ]
    for r in results:
        badge = "PASS" if r["passed"] else "FAIL"
        git_tag = f"`{r['git_commit']}`" if r["git_commit"] else "skipped"
        lines.append(
            f"  [{badge}] `{r['model']}` @ {r['base_url']} — {r['duration_s']}s | "
            f"LLM calls={r['llm_calls']} | tests={r['tests_ran']} | auto-skipped={r['auto_skipped_tasks']} | git={git_tag}"
        )
    return {
        "passed": bool(passing) and len(passing) == len(results),
        "status": "PASS" if (passing and len(passing) == len(results)) else "FAIL",
        "winner": winner,
        "candidates": results,
        "summary": "\n".join(lines),
    }


def run_live_flight_eval(
    suite: str = "full",
    chat_fn: Callable[..., Dict[str, Any]] = chat,
    check_endpoints: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run the 4-flight behavioral evaluation suite (or `compare` coding benchmark) in isolated workspaces."""
    clean_suite = (suite or "full").strip().lower()
    if clean_suite in ("compare", "coding", "benchmark"):
        return run_coding_model_comparison(chat_fn=chat_fn, check_endpoints=check_endpoints, verbose=verbose)
    tier_health: Dict[str, str] = {}
    if check_endpoints and chat_fn is chat:
        for t_name in ("fast", "reasoning"):
            cfg = settings.resolve_tier(t_name)
            ok, info = probe_endpoint(cfg.base_url, cfg.api_key, timeout=3.5)
            tier_health[t_name] = f"{cfg.model} @ {cfg.base_url} -> {info}"
            if not ok and t_name == "fast":
                summary = (
                    "=== Companion Live Model Flight Eval: SKIPPED (FAST ENDPOINT OFFLINE) ===\n"
                    f"Primary `fast` model endpoint `{cfg.base_url}` (`{cfg.model}`) is unreachable: {info}\n"
                    "Ensure your local Ollama / OpenAI-compatible server is running and reachable on the LAN."
                )
                return {"passed": False, "status": "OFFLINE", "flights": [], "summary": summary}
    else:
        for t_name in ("fast", "reasoning"):
            cfg = settings.resolve_tier(t_name)
            tier_health[t_name] = f"{cfg.model} (injected test driver)"

    t0 = time.perf_counter()
    runners: List[Tuple[str, Callable[[], FlightResult]]] = [
        ("quick", lambda: _run_flight_1_symbol_grounding(chat_fn, verbose)),
        ("quick", lambda: _run_flight_2_inline_linter_edit(chat_fn, verbose)),
        ("full", lambda: _run_flight_3_blueprint_approve_build(chat_fn, verbose)),
        ("general", lambda: _run_flight_4_domain_neutral_goal_and_reflect(chat_fn, verbose)),
    ]
    flights: List[FlightResult] = []
    for min_level, fn in runners:
        if clean_suite == "quick" and min_level != "quick":
            continue
        if clean_suite == "full" and min_level == "general":
            continue
        if clean_suite == "general" and min_level != "general":
            continue
        flights.append(fn())

    total_s = round(time.perf_counter() - t0, 2)
    all_ok = bool(flights) and all(f.passed for f in flights)
    summary = format_flight_eval_report(flights, tier_health, total_s)
    return {
        "passed": all_ok,
        "status": "PASS" if all_ok else "FAIL",
        "flights": [f.__dict__ for f in flights],
        "summary": summary,
    }
