"""
End-to-End Verification Suite for the 5-Phase Agent-Native Headless Core Refactoring:
- Phase 1: Unified `TriggerPolicy` & `Routines` Consolidation (`Background Autonomy` x `Self-Dev Mode`)
- Phase 2: Code-Controlled Deterministic Python `dag_runtime` & Capability Forge (`dags/*.py`)
- Phase 3: Universal Headless Project & Goal Graph + `personal-agile-coach` Skill Pack
- Phase 4: Cognitive Triage Gate (`companion/intake.py`)
- Phase 5: Approval-Staged MCP/Endpoint Self-Wiring, Harvester & Pluggable Domain Validators
"""

import contextlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.archive_and_approvals import get_approval_queue
from companion.config import settings
from companion.core_actions import execute_core_action
from companion.dag_runtime import (
    DAGNode,
    DAGRuntimeBindings,
    DAGSpec,
    dag_node,
    discover_dags,
    dry_run_dag,
    execute_dag_spec,
    get_dag_run,
    resume_dag_run,
    self_test_dag,
)
from companion.endpoints import get_endpoint_manager
from companion.intake import match_verified_dag, triage_turn
from companion.mcp_hub import get_mcp_hub, harvest_mcp_capability_pack
from companion.memory_store import MemoryStore, memory_store_context
from companion.os_tools import _dict_arg
from companion.project_graph import get_project_graph
from companion.routines import (
    IdleAutonomyWatcher,
    format_morning_briefing,
    get_routine_manager,
)
from companion.safety import lint_single_file_edit
from companion.skill_forge import run_skill_script, stage_and_promote_skill, validate_skill_markdown
from companion.tools import resolve
from companion.trigger_policy import (
    TriggerPolicy,
    evaluate_trigger_eligibility,
    is_within_allowed_window,
    matches_cron_5field,
    register_trigger_precheck,
)
from companion.workflow_dag import (
    VerificationSpec,
    evaluate_step_verification,
    register_domain_validator,
)


def _mock_chat_fn(_messages, _tools=None, _tool_choice="auto"):
    return {
        "choices": [{"message": {"role": "assistant", "content": "Completed autonomous work slice cleanly."}}],
        "usage": {"total_tokens": 18},
    }


class TestPhase1UnifiedTriggerPolicyAndRoutines(unittest.TestCase):
    """Phase 1: Unified TriggerPolicy, overnight windows, prechecks, and Background Autonomy."""

    def test_allowed_window_overnight_and_weekday_filters(self) -> None:
        dt_2330 = datetime(2026, 11, 16, 23, 30)  # Monday 23:30
        dt_0215 = datetime(2026, 11, 17, 2, 15)   # Tuesday 02:15
        dt_1400 = datetime(2026, 11, 16, 14, 0)   # Monday 14:00
        dt_sat = datetime(2026, 11, 21, 23, 30)   # Saturday 23:30

        self.assertTrue(is_within_allowed_window("22:00-06:00", dt_2330))
        self.assertTrue(is_within_allowed_window("22:00-06:00", dt_0215))
        self.assertFalse(is_within_allowed_window("22:00-06:00", dt_1400))

        self.assertTrue(is_within_allowed_window("Mon-Fri 22:00-06:00", dt_2330))
        self.assertFalse(is_within_allowed_window("Mon-Fri 22:00-06:00", dt_sat))
        self.assertTrue(matches_cron_5field("30 23 * * 1", dt_2330))
        self.assertFalse(matches_cron_5field("30 23 * * 2", dt_2330))

    def test_trigger_eligibility_precheck_cooldown_and_window_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "p1.db")) as store:
            calls = {"count": 0}

            def _fast_precheck(_mem: MemoryStore):
                calls["count"] += 1
                return True, "work_available"

            register_trigger_precheck("unit_test_precheck", _fast_precheck)
            policy = TriggerPolicy(
                mode="on_idle",
                idle_min_seconds=300,
                allowed_window="22:00-06:00",
                cooldown_seconds=1800,
                precheck_name="unit_test_precheck",
            )
            now_utc = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
            now_inside = datetime(2026, 11, 16, 23, 30)
            now_outside = datetime(2026, 11, 16, 14, 0)

            ok, reason = evaluate_trigger_eligibility(
                policy, idle_seconds=600, background_autonomy_enabled=False,
                now_utc=now_utc, now_local=now_inside, store=store,
            )
            self.assertFalse(ok)
            self.assertIn("background_autonomy", reason)

            ok, reason = evaluate_trigger_eligibility(
                policy, idle_seconds=120, background_autonomy_enabled=True,
                now_utc=now_utc, now_local=now_inside, store=store,
            )
            self.assertFalse(ok)
            self.assertIn("idle", reason)

            ok, reason = evaluate_trigger_eligibility(
                policy, idle_seconds=600, background_autonomy_enabled=True,
                now_utc=now_utc, now_local=now_outside, store=store,
            )
            self.assertFalse(ok)
            self.assertIn("window", reason)

            t0 = time.perf_counter()
            ok, reason = evaluate_trigger_eligibility(
                policy, idle_seconds=600, background_autonomy_enabled=True,
                now_utc=now_utc, now_local=now_inside, store=store,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertTrue(ok)
            self.assertEqual(reason, "eligible_on_idle")
            self.assertEqual(calls["count"], 1)
            self.assertLess(elapsed_ms, 25.0)

            recent_iso = (now_utc - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
            ok_cd, reason_cd = evaluate_trigger_eligibility(
                policy, idle_seconds=600, last_run_at=recent_iso,
                background_autonomy_enabled=True,
                now_utc=now_utc, now_local=now_inside, store=store,
            )
            self.assertFalse(ok_cd)
            self.assertIn("cooldown", reason_cd)

    def test_builtin_idle_routines_preemption_and_morning_briefing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "p1_routines.db")) as store:
            mgr = get_routine_manager(store=store)
            routines = {r["routine_id"]: r for r in mgr.list_routines()}
            for rid in ("wiki_gardener", "memory_curator", "trace_reviewer"):
                self.assertIn(rid, routines)
            self.assertNotIn("code_hygiene", routines)

            store.set_background_autonomy(False)
            eligible_off = [r["routine_id"] for r in mgr.select_eligible_idle_routines(idle_seconds=3600)]
            self.assertEqual(eligible_off, [])
            store.set_background_autonomy(True)

            watcher_busy = IdleAutonomyWatcher(is_busy_fn=lambda: True, store=store)
            res_busy = watcher_busy.tick_once(idle_seconds=3600)
            self.assertEqual(res_busy["action"], "busy_skip")

            store.log_autonomy_run("wiki_gardener", "completed", "Consolidated 2 duplicate entity pages.")
            brief = format_morning_briefing(store=store, mark_seen=True)
            self.assertIn("wiki_gardener", brief)
            self.assertEqual(len(store.get_autonomy_journal(unseen_only=True)), 0)


class TestPhase2CodeControlledDAGRuntimeAndForge(unittest.TestCase):
    """Phase 2: Python DAGSpec, foreach fan-out/fan-in, R3 pause/resume, and Skill Forge DAGs."""

    def test_dag_spec_cycle_and_schema_validation(self) -> None:
        cyclic = DAGSpec(
            name="cyclic-dag",
            description="Cycle rejection check with valid description length",
            nodes=[
                DAGNode("a", depends_on=["b"], fn=lambda _c: {}),
                DAGNode("b", depends_on=["a"], fn=lambda _c: {}),
            ],
        )
        dry = dry_run_dag(cyclic)
        self.assertFalse(dry["ok"])
        self.assertIn("cycle", dry["error"].lower())

    def test_foreach_fanout_retries_and_r3_approval_pause_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "p2_dag.db")) as store:
            counts = {"seed": 0, "flaky": 0, "deploy": 0}

            @dag_node("seed_items", output_schema={"items": "list"})
            def seed_items(_ctx):
                counts["seed"] += 1
                return {"items": [2, 3, 5]}

            @dag_node("square_each", depends_on=["seed_items"], foreach="seed_items.items", output_schema={"sq": "int"})
            def square_each(ctx):
                val = int(ctx["item"])
                return {"sq": val * val}

            @dag_node("flaky_sum", depends_on=["square_each"], retries=2, output_schema={"total": "int"})
            def flaky_sum(ctx):
                counts["flaky"] += 1
                if counts["flaky"] == 1:
                    raise RuntimeError("transient glitch")
                vals = [int(x["sq"]) for x in ctx["nodes"]["square_each"]["items"]]
                return {"total": sum(vals)}

            @dag_node("prod_cutover", depends_on=["flaky_sum"], risk_tier="R3", output_schema={"deployed_total": "int"})
            def prod_cutover(ctx):
                counts["deploy"] += 1
                return {"deployed_total": int(ctx["nodes"]["flaky_sum"]["total"])}

            spec = DAGSpec(
                name="fanout-r3-dag",
                description="Test foreach fanout, retry, and R3 checkpoint pause/resume",
                nodes=[seed_items, square_each, flaky_sum, prod_cutover],
            )

            paused = execute_dag_spec(spec, store=store)
            self.assertEqual(paused["status"], "waiting_approval", f"Unexpected DAG status: {paused}")
            self.assertEqual(paused["paused_node_id"], "prod_cutover")
            self.assertEqual(counts["seed"], 1)
            self.assertEqual(counts["flaky"], 2)
            self.assertEqual(counts["deploy"], 0)

            snap = get_dag_run(paused["run_id"], store=store)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["status"], "waiting_approval")

            queue = get_approval_queue(store=store)
            waiting = queue.list_requests(status="waiting")
            self.assertEqual(len(waiting), 1)
            queue.resolve(waiting[0]["approval_id"], decision="approve", actor="user")

            resumed = resume_dag_run(
                paused["run_id"], approved_node_id="prod_cutover", store=store, dag_spec=spec
            )
            self.assertEqual(resumed["status"], "completed", f"Resume failed: {resumed}")
            self.assertEqual(counts["seed"], 1)  # Upstream node NOT re-executed!
            self.assertEqual(counts["deploy"], 1)
            self.assertEqual(resumed["nodes"]["prod_cutover"]["deployed_total"], 38)

    def test_all_builtin_skill_dags_pass_self_test_and_forge_promotes_dags(self) -> None:
        discovered = discover_dags(root=settings.root)
        names = {d["name"] for d in discovered}
        expected = {"pre-commit-verify", "publish-pr"}
        self.assertTrue(expected.issubset(names), f"Missing built-in DAGs: {expected - names}")
        self.assertFalse({"full-audit", "release-check", "safe-deploy"} & names)
        for d in discovered:
            if d["name"] in expected:
                st = self_test_dag(Path(d["path"]))
                self.assertTrue(st["ok"], f"DAG self-test failed for {d['name']}: {st}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir(parents=True)
            skill_md = (
                "---\nname: sample-forged-pack\n"
                "description: Use when testing forged code-controlled Python DAG promotion.\n"
                "required_tools: [workflow_manage]\n---\n"
                "# Sample Forged Pack\n\n"
                "## When to Use This Skill\nUse during DAG forge tests.\n\n"
                "## Step-by-Step Procedure\n1. Run `dags/sample_pipeline.py`.\n"
            )
            dag_code = (
                "from companion.dag_runtime import DAGNode, DAGSpec, execute_dag_spec\n\n"
                "DAG = DAGSpec(\n"
                "    name='sample-pipeline',\n"
                "    description='Sample deterministic forged pipeline',\n"
                "    nodes=[DAGNode('step1', output_schema={'ok': 'bool'}, fn=lambda _c: {'ok': True})],\n"
                "    self_test_fn=lambda: execute_dag_spec(DAG)['status'] == 'completed',\n"
                ")\n"
            )
            res = stage_and_promote_skill(
                name="sample-forged-pack",
                skill_md=skill_md,
                dags={"sample_pipeline.py": dag_code},
                root=root,
            )
            self.assertTrue(res["promoted"], f"Forge failed: {res}")
            self.assertTrue((root / "skills" / "sample-forged-pack" / "dags" / "sample_pipeline.py").is_file())


class TestPhase3UniversalProjectGraphAndAgileCoach(unittest.TestCase):
    """Phase 3: Universal Project & Goal Graph, human->companion unblocking, and coach skill."""

    def test_human_blocker_unblocks_companion_item_and_idle_slice_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "p3_graph.db")) as store:
            store.set_background_autonomy(True)
            pg = get_project_graph(store=store)

            proj = pg.create_project(
                title="Home Network Observability",
                goal_summary="Monitor router uptime and build dashboard",
                kind="goal",
                trigger_policy={
                    "mode": "on_idle",
                    "idle_min_seconds": 300,
                    "allowed_window": "22:00-06:00",
                    "cooldown_seconds": 60,
                    "precheck_name": "active_goal_has_ready_items",
                },
            )
            pid = proj["project_id"]
            ms_map = pg.seed_dikw_goal_milestones(pid)
            self.assertEqual(len(ms_map), 4)

            human_item = pg.create_item(
                pid, "Generate read-only router API token", owner="human",
                stage_kind="OBSERVE_DATA", milestone_id=ms_map["OBSERVE_DATA"],
            )
            comp_item = pg.create_item(
                pid, "Ingest router metrics and verify schema", owner="companion",
                stage_kind="DELIVER_AND_VERIFY", milestone_id=ms_map["DELIVER_AND_VERIFY"],
                depends_on=[human_item["item_id"]],
            )

            self.assertEqual(pg.get_item(comp_item["item_id"])["status"], "blocked")
            self.assertEqual(pg.next_ready_companion_items(pid), [])

            pg.update_item(human_item["item_id"], status="done")
            ready_now = pg.next_ready_companion_items(pid)
            self.assertEqual(len(ready_now), 1)
            self.assertEqual(ready_now[0]["item_id"], comp_item["item_id"])

            rmgr = get_routine_manager(store=store)
            for r in rmgr.list_routines():
                rmgr.set_routine_enabled(r["routine_id"], False)

            watcher = IdleAutonomyWatcher(
                is_busy_fn=lambda: False,
                store=store,
                chat_fn=_mock_chat_fn,
            )
            now_utc = datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc)
            outside_res = watcher.tick_once(
                idle_seconds=900, now_utc=now_utc, now_local=datetime(2026, 11, 16, 14, 0)
            )
            self.assertEqual(outside_res["action"], "idle_noop")

            inside_res = watcher.tick_once(
                idle_seconds=900, now_utc=now_utc, now_local=datetime(2026, 11, 16, 23, 30)
            )
            self.assertTrue(inside_res["ok"], f"Idle goal slice failed: {inside_res}")
            self.assertEqual(inside_res["action"], "idle_goal_slice")
            self.assertEqual(pg.get_item(comp_item["item_id"])["column_key"], "done")

    def test_personal_agile_coach_skill_self_test(self) -> None:
        out = run_skill_script("personal-agile-coach", "coach_helper.py", ["--self-test"])
        self.assertIn('"ok": true', out.lower())


class TestPhase4CognitiveTriageGate(unittest.TestCase):
    """Phase 4: Cognitive Triage routing across DIRECT_TURN, EXECUTE_VERIFIED_DAG, GOAL_ALIGN_PLAN, ADVANCE_ACTIVE_GOAL."""

    def test_cognitive_triage_routes_and_llm_structured_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "p4_triage.db")) as store:
            pg = get_project_graph(store=store)
            proj = pg.create_project("Active Refactor Goal", kind="goal")
            pg.create_item(proj["project_id"], "Write migration unit test", owner="companion", status="ready")

            d_chat = triage_turn("Hey, how are you doing today?", store=store, root=settings.root)
            self.assertEqual(d_chat.route, "DIRECT_TURN")

            d_dag = triage_turn("Please run the `pre-commit-verify` DAG on the repository now", store=store, root=settings.root)
            self.assertEqual(d_dag.route, "EXECUTE_VERIFIED_DAG")
            self.assertEqual(d_dag.matched_dag, "pre-commit-verify")

            d_adv = triage_turn("Continue advancing our active goal slice", store=store, root=settings.root)
            self.assertEqual(d_adv.route, "ADVANCE_ACTIVE_GOAL")

            d_r3 = triage_turn(
                "Architect and plan a multi-phase production database drop and migration",
                store=store, root=settings.root,
            )
            self.assertEqual(d_r3.route, "GOAL_ALIGN_PLAN")
            self.assertTrue(d_r3.needs_blueprint)

            d_llm = triage_turn(
                "Help me organize my garage renovation into milestones and weekly sprints",
                store=store,
                root=settings.root,
                triage_llm_fn=lambda _txt, _ctx: {
                    "route": "GOAL_ALIGN_PLAN",
                    "tier": "reasoning",
                    "needs_blueprint": True,
                    "matched_dag": "",
                    "reason": "Multi-milestone planning goal",
                },
            )
            self.assertEqual(d_llm.route, "GOAL_ALIGN_PLAN")
            self.assertEqual(d_llm.suggested_tier, "reasoning")
            self.assertTrue(d_llm.needs_blueprint)


class TestPhase5SelfWiringHarvesterAndDomainValidators(unittest.TestCase):
    """Phase 5: Companion propose_endpoint/propose_server -> 1-click approve -> MCP Harvester -> Domain Validator."""

    def test_propose_approve_and_harvest_mcp_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            (tmp_root / "skills").mkdir(parents=True)
            (tmp_root / "data" / "skills").mkdir(parents=True)
            with contextlib.closing(MemoryStore(db_path=tmp_root / "data" / "p5.db")) as store, memory_store_context(store):
                ep_mgr = get_endpoint_manager(store=store)
                prop_ep = ep_mgr.propose_endpoint(
                    endpoint_id="home-assistant", protocol="http_api", host="192.168.1.50",
                    port=8123, credential_ref="ha_api_token", actor="companion",
                )
                self.assertTrue(prop_ep["awaiting_approval"])
                self.assertIsNone(ep_mgr.get_endpoint("home-assistant"))

                dec_ep = execute_core_action(
                    "approvals", {"subaction": "approve", "approval_id": prop_ep["approval_id"]}, actor="user", store=store
                )
                self.assertTrue(dec_ep["ok"], f"Endpoint approval failed: {dec_ep}")
                self.assertIsNotNone(ep_mgr.get_endpoint("home-assistant"))

                def _mock_transport(_srv, req):
                    if req.get("method") == "tools/list":
                        return {"result": {"tools": [{"name": "list_sensors", "description": "List smart home sensors", "inputSchema": {"type": "object"}}]}}
                    return {"result": {"content": [{"type": "text", "text": '{"sensors": ["temp_living"]}'}]}}

                hub = get_mcp_hub(store=store, transport_fn=_mock_transport)
                prop_mcp = hub.propose_server(
                    server_id="ha-mcp", transport="http", url="http://127.0.0.1:8910/mcp",
                    credential_ref="ha_mcp_token", actor="companion",
                )
                self.assertTrue(prop_mcp["awaiting_approval"])

                dec_mcp = execute_core_action(
                    "approvals", {"subaction": "approve", "approval_id": prop_mcp["approval_id"]}, actor="user", store=store
                )
                self.assertTrue(dec_mcp["ok"], f"MCP approval failed: {dec_mcp}")
                self.assertTrue(hub.discover_server("ha-mcp", actor="user")["ok"])
                hub.approve_server("ha-mcp", actor="user")

                harvest = harvest_mcp_capability_pack("ha-mcp", pack_name="ha-smart-home", store=store, root=tmp_root)
                self.assertTrue(harvest["ok"], f"Harvest failed: {harvest}")
                pack_dir = Path(harvest["path"])
                self.assertTrue((pack_dir / "SKILL.md").is_file())
                self.assertTrue((pack_dir / "references" / "mcp-tools.md").is_file())
                self.assertTrue((pack_dir / "scripts" / "mcp_client_helper.py").is_file())
                dags = list((pack_dir / "dags").glob("*.py"))
                self.assertEqual(len(dags), 1)
                self.assertTrue(self_test_dag(dags[0])["ok"])

                mcp_dag = DAGSpec(
                    name="mcp-bound-check", description="Verify MCP step binding end to end",
                    nodes=[DAGNode("fetch", kind="mcp_step", server_id="ha-mcp", tool_name="list_sensors")],
                )
                run_mcp = execute_dag_spec(
                    mcp_dag, store=store, bindings=DAGRuntimeBindings(mcp_fn=lambda sid, t, a: hub.call_mcp_tool(sid, t, a))
                )
                self.assertEqual(run_mcp["status"], "completed")

    def test_pluggable_domain_validator_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            register_domain_validator(
                "sensor_budget_validator",
                lambda target_or_out, params, _root: (
                    int(params.get("min_count", 1)) <= target_or_out.count("temp_"),
                    "sensor_budget_verified",
                ),
            )
            vspec = VerificationSpec(kind="domain_validator", target="sensor_budget_validator", params={"min_count": 1})
            passed, detail = evaluate_step_verification(vspec, tmp_root, step_output='{"sensors": ["temp_living"]}')
            self.assertTrue(passed)
            self.assertIn("sensor_budget_verified", detail)


class TestLiveDogfoodHardeningRegressions(unittest.TestCase):
    """Regression suite covering real-world gaps uncovered during live LLM dogfooding."""

    def test_triage_ignores_companion_ids_slugs_and_rightmost_dag_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "df_triage.db")) as store:
            prompt = (
                "Create an `on_idle` routine `overnight-backup` -> `snapshot_backup` and mark "
                "`wi_0ed8c2722f` done; digest `INFO` / `WARN` / `ERROR` lines via `DAGSpec`."
            )
            td = triage_turn(prompt, store=store, root=settings.root)
            self.assertEqual(td.readiness, "READY")
            self.assertEqual(td.route, "DIRECT_TURN")

            picked = match_verified_dag(
                "Run the `publish-pr` check? Wait: run the `pre-commit-verify` DAG.",
                root=settings.root,
            )
            self.assertEqual(picked, "pre-commit-verify")

    def test_dag_spec_dict_coercion_kwarg_fn_and_summary_output(self) -> None:
        dag = DAGSpec(
            name="log-digest-check",
            description="Count ERROR and WARN log lines deterministically.",
            input_schema={"lines": list},
            nodes=[
                {
                    "name": "count_levels",
                    "type": "python",
                    "output_schema": {"errors": int, "warns": int},
                    "func": lambda lines: {
                        "errors": sum(1 for ln in lines if "ERROR" in ln),
                        "warns": sum(1 for ln in lines if "WARN" in ln),
                    },
                    "self_test_fn": lambda: True,
                }
            ],
        )
        self.assertIsNone(dag.validate())
        res = execute_dag_spec(dag, params={"lines": ["INFO ok", "WARN disk", "ERROR p1", "ERROR p2"]})
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["final_output"], {"errors": 2, "warns": 1})
        self.assertIn('"errors": 2', res["summary"])
        self.assertEqual(_dict_arg({"dags": '{"a.py": "x\ny"'}, "dags"), {"a.py": "x\ny"})
        folded_md = (
            "---\nname: log-digest\ndescription: >\n  Digest log lines deterministically.\n"
            "required_tools:\n  - workflow_manage\n---\n"
            "## When to Use This Skill\nWhen digesting logs.\n\n## Step-by-Step Procedure\n1. Run DAG.\n"
        )
        self.assertIsNone(validate_skill_markdown(folded_md, expected_name="log-digest"))

    def test_custom_data_dir_resolution_dag_discovery_and_skill_linting(self) -> None:
        orig_data = settings.data_dir
        try:
            with tempfile.TemporaryDirectory() as tmp:
                custom_data = (Path(tmp) / "ext_data").resolve()
                (custom_data / "notes").mkdir(parents=True)
                settings.data_dir = custom_data
                self.assertEqual(resolve("data/notes/test.md").resolve(), (custom_data / "notes" / "test.md").resolve())
                (custom_data / "notes" / "test.md").write_text("12ft shelving", encoding="utf-8")
                ok, detail = evaluate_step_verification(
                    VerificationSpec(kind="evidence_file", target="data/notes/test.md", contains="12ft"),
                    settings.root,
                )
                self.assertTrue(ok, detail)

                dag_src = (
                    "from companion.dag_runtime import DAGNode, DAGSpec\n"
                    "DAG = DAGSpec(name='ext-data-dag', description='External data dir DAG test', "
                    "nodes=[DAGNode('n1', fn=lambda _c: {'ok': True})], self_test_fn=lambda: True)\n"
                )
                err, _warn = lint_single_file_edit(
                    settings.root / "data" / "skills" / "ext-pack" / "dags" / "ext_dag.py",
                    dag_src,
                )
                self.assertIsNone(err)
                dag_dir = custom_data / "skills" / "ext-pack" / "dags"
                dag_dir.mkdir(parents=True)
                (dag_dir / "ext_dag.py").write_text(dag_src, encoding="utf-8")
                names = [d["name"] for d in discover_dags(root=settings.root)]
                self.assertIn("ext-data-dag", names)
        finally:
            settings.data_dir = orig_data

    def test_backlog_dependency_blocking_and_pending_decide_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "df_graph.db")) as store:
            pg = get_project_graph(store=store)
            proj = pg.create_project("Alias Check", kind="project")
            up = pg.create_item(proj["project_id"], "Upstream step", owner="human", status="ready")
            down = pg.create_item(proj["project_id"], "Downstream step", owner="companion", status="backlog", depends_on=[up["item_id"]])
            self.assertEqual(down["status"], "blocked")

            ep_prop = get_endpoint_manager(store=store).propose_endpoint("metrics-ep", "http_api", "http://127.0.0.1:9099", actor="companion")
            pending = get_approval_queue(store=store).list_requests(status="pending")
            self.assertEqual(len(pending), 1)
            dec = execute_core_action("approvals", {"subaction": "decide", "approval_id": ep_prop["approval_id"], "decision": "approved"}, actor="web", store=store)
            self.assertTrue(dec["ok"])

    def test_project_focus_bridge_personal_triage_and_flashcard_fence_guard(self) -> None:
        from companion.agent import _extract_turn_widget_fence
        from companion.intake import assess_turn_readiness, triage_turn
        from companion.project_graph import format_project_graph_hud

        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(MemoryStore(db_path=Path(tmp) / "focus.db")) as store:
            pg = get_project_graph(store=store)
            proj = pg.create_project("Kitchen Remodel", goal_summary="New cabinets and lighting", kind="project")
            pg.set_focus_project(proj["project_id"])
            self.assertEqual(pg.resolve_project_id(), proj["project_id"])
            item = pg.create_item(None, "Measure north wall", owner="human", status="ready")
            self.assertEqual(item["project_id"], proj["project_id"])
            hud = format_project_graph_hud(store=store)
            self.assertIn("Kitchen Remodel", hud)
            self.assertIn("Measure north wall", hud)

            r_personal = assess_turn_readiness(
                "Create a short study course on Proxmox ZFS Basics with 2 flashcards",
                store=store,
                root=settings.root,
            )
            self.assertEqual(r_personal.status, "READY")
            t_personal = triage_turn(
                "Create a short study course on Proxmox ZFS Basics with 2 flashcards",
                store=store,
                root=settings.root,
            )
            self.assertEqual(t_personal.suggested_tier, "fast")
            self.assertFalse(t_personal.needs_blueprint)

            fence = _extract_turn_widget_fence(
                [
                    {"role": "user", "content": "quiz me"},
                    {"role": "tool", "content": "Built deck:\n```companion-flashcards\n{\"course_id\":\"zfs\",\"deck_title\":\"ZFS\",\"cards\":[]}\n```"},
                ],
                0,
            )
            self.assertTrue(fence.startswith("```companion-flashcards"))


if __name__ == "__main__":
    unittest.main()
