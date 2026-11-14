"""
Autonomous "Agent Hobbies", Two-Lever Operating Architecture & Idle-Loop Engine (`ADR-006`, `ADR-019`).

Implements Companion's Independent Two-Lever Operating Architecture (`Hobby Mode` x `Self-Dev Mode`)
and 2-Track Hobby Split (`personal` vs `self_dev`):
  a) Hobbies OFF + Self-Dev OFF: Quiet stable daily driver, Companion core mechanically READ-ONLY.
  b) Hobbies ON  + Self-Dev OFF: Stable daily driver running `personal` track hobbies only; core READ-ONLY.
  c) Hobbies OFF + Self-Dev ON : Deliberate interactive self-dev mode (edits core only when asked).
  d) Hobbies ON  + Self-Dev ON : Full autonomous Lab/VM mode (`personal` + `self_dev` tracks eligible).
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from companion.config import settings
from companion.hygiene import analyze_workspace_hygiene
from companion.llm import get_endpoint_gate, tier_context
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.safety import CheckpointManager, get_active_checkpoint_manager, run_system_doctor
from companion.sdd import sync_decisions_md, verify_sdd
from companion.telemetry import telemetry


@dataclass(frozen=True)
class HobbySpec:
    """Definition of a bounded autonomous hobby routine classified into `personal` or `self_dev` track."""

    name: str
    title: str
    track: str
    discipline: str
    description: str
    instructions: str
    preferred_tier: str = "fast"


HOBBY_ROTATION: List[HobbySpec] = [
    HobbySpec(
        name="wiki_gardener",
        title="Knowledge Wiki Curator & Ontology Gardener",
        track="personal",
        discipline="Deliberate Knowledge Synthesis",
        description="Deliberately absorbs staged `01_inbox/` notes into `02_knowledge/`, enriches `ontology` bridges, and maintains Domain Hub MOCs in `data/wiki/`.",
        instructions=(
            "Execute your 'Knowledge Wiki Curator & Ontology Gardener' personal hobby (follow `skills/knowledge-wiki/SKILL.md`):\n"
            "1. Call `wiki_manage(action='list', path='01_inbox')`, `wiki_manage(action='list_stale')`, and `wiki_manage(action='consolidate_ontology')`.\n"
            "2. If `01_inbox/` has staged notes, pick one and run the deliberate curation workflow (`inspect_graduation` -> `read` overlap candidates -> `validate_curation` -> `commit_curation`). Never overwrite `author: user` notes.\n"
            "3. If `01_inbox/` is empty, inspect `wiki_manage(action='graph')` and `list_stale` for domains needing a `concept-map-system-hub` MOC or stale Companion notes, and stage/update one improvement inside `data/wiki/`.\n"
            "4. Summarize your Wiki curation, freshness audit, and ontology gardening work without modifying Companion source code."
        ),
        preferred_tier="reasoning",
    ),
    HobbySpec(
        name="memory_curator",
        title="Memory & Durable Lessons Curator",
        track="personal",
        discipline="Personal & Technical Memory Curation",
        description="Consolidates recent conversation insights into durable notes/Wiki inbox items and synchronizes `MEMORY.md` without modifying code.",
        instructions=(
            "Execute your 'Memory & Durable Lessons Curator' personal hobby:\n"
            "1. Call `memory_manage(action='recent_history', limit=15)` and `memory_manage(action='get_notes', limit=10)`.\n"
            "2. Distill any new durable user preferences, project facts, or lessons into `01_inbox/` via `wiki_manage(action='capture_inbox', ...)` or `memory_manage(action='add_note', ...)`. Never overwrite `author: user` notes.\n"
            "3. Call `memory_manage(action='sync')` to refresh `MEMORY.md` cleanly.\n"
            "4. Return a concise summary of curated memories and Wiki inbox captures without touching repository code."
        ),
        preferred_tier="reasoning",
    ),
    HobbySpec(
        name="trace_reviewer",
        title="Telemetry & Slow-Span Trace Reviewer",
        track="personal",
        discipline="Observability & Performance",
        description="Inspects Waterfall traces, KPIs, and structured WARN/ERROR logs to log actionable performance or reliability notes without editing code.",
        instructions=(
            "Execute your 'Telemetry & Slow-Span Trace Reviewer' personal hobby:\n"
            "1. Call `telemetry_manage(action='kpis')`, `telemetry_manage(action='recent_traces', limit=10)`, and `telemetry_manage(action='logs', level='WARN', limit=20)`.\n"
            "2. Inspect any slow spans (`>1000ms`), error traces, or safety/syntax guard triggers via `telemetry_manage(action='waterfall', trace_id=...)`.\n"
            "3. Record actionable performance or reliability observations via `memory_manage(action='add_note', category='telemetry_insight', ...)` or queue a backlog suggestion via `task_manage(action='add_backlog', ...)`.\n"
            "4. Summarize your trace telemetry findings clearly without modifying any files."
        ),
        preferred_tier="reasoning",
    ),
    HobbySpec(
        name="life_study_organizer",
        title="Life, Study & Horizon Readiness Organizer",
        track="personal",
        discipline="Personal Readiness & Study Ops",
        description="Reviews due Academy FSRS items, upcoming Calendar events, and stalled/active Agile Sprint cards to log a clean `/morning` readiness note without modifying code.",
        instructions=(
            "Execute your 'Life, Study & Horizon Readiness Organizer' personal hobby:\n"
            "1. Call `education_manage(action='progress')` to check active courses and due FSRS spaced-repetition cards.\n"
            "2. Call `calendar_manage(action='window')` to inspect upcoming appointments, deadlines, and scheduled routines.\n"
            "3. Call `agile_manage(action='board_view')` to check active Sprint capacity slots and any stalled or in-progress Kanban cards.\n"
            "4. Record a concise, warm readiness brief via `memory_manage(action='add_note', category='morning_readiness', ...)` highlighting due study reviews, upcoming schedule items, and suggested 15-minute unblocker priorities — do NOT modify any code files.\n"
            "5. Return the clean `/morning` readiness summary."
        ),
        preferred_tier="fast",
    ),
    HobbySpec(
        name="code_simplifier",
        title="Codebase Simplifier & Dead-Code Pruner",
        track="self_dev",
        discipline="Hygiene & Simplicity",
        description="Scans for dead code, unused imports, spec drift, or overly complex functions and simplifies them (`Self-Dev Mode = ON` required).",
        instructions=(
            "Execute your 'Codebase Simplifier & Dead-Code Pruner' self-dev hobby (follow `skills/sdd-compliance/SKILL.md`):\n"
            "1. Call `sdd_manage(action='verify')` and `system_doctor(run_tests=True)` to inspect hygiene, 3-Tier `skills/` integrity, and spec alignment.\n"
            "2. If any unused imports, dead definitions, oversized functions, skill linting issues, or spec drift are reported, clean them up with `edit_file`.\n"
            "3. If everything is already 100% clean, inspect one module to see if docstrings or clarity can be improved without adding bloat.\n"
            "4. Run `system_doctor(run_tests=True)` to confirm all checks pass, then return a concise summary."
        ),
        preferred_tier="fast",
    ),
    HobbySpec(
        name="backlog_builder",
        title="Spec-Driven Backlog Craftsman",
        track="self_dev",
        discipline="Spec-Driven Engineering",
        description="Picks the top pending item from the Engineering Backlog and implements it via SDD and the State Machine (`Self-Dev Mode = ON` required).",
        instructions=(
            "Execute your 'Spec-Driven Backlog Craftsman' self-dev hobby (follow `skills/engineering-workflow/SKILL.md`):\n"
            "1. Call `task_manage(action='list_all')` to inspect pending tasks and backlog items.\n"
            "2. Pick one pending item. If its capability is already implemented in `companion/`, verify its test coverage in a small test file.\n"
            "3. Prefer extending smaller files well under the 550-line limit or authoring a 3-Tier `skills/<name>/` runbook. Include needed imports and usage in a single edit.\n"
            "4. Run `system_doctor(run_tests=True)` to verify all 7 gates pass, and mark the backlog/task item `completed` via `task_manage`."
        ),
        preferred_tier="fast",
    ),
    HobbySpec(
        name="code_gardener",
        title="Quality Assurance & Test Gardener",
        track="self_dev",
        discipline="Verification & Resilience",
        description="Audits subsystem contracts in `specs/CONTRACTS.md` against unit tests in `tests/` and strengthens coverage (`Self-Dev Mode = ON` required).",
        instructions=(
            "Execute your 'Quality Assurance & Test Gardener' self-dev hobby:\n"
            "1. Read `specs/CONTRACTS.md` and inspect a compact test file such as `tests/test_memory.py` or `tests/test_safety_and_recovery.py`.\n"
            "2. Identify one contract invariant or edge case that could benefit from an additional `def test_...(self):` method in that `unittest.TestCase` class.\n"
            "3. Add one clean, fast `def test_...(self):` method using `edit_file` and verify with `system_doctor(run_tests=True)`.\n"
            "4. Summarize the coverage improvement."
        ),
        preferred_tier="fast",
    ),
]

HOBBY_MAP: Dict[str, HobbySpec] = {h.name: h for h in HOBBY_ROTATION}


def describe_operating_quadrant(hobby_enabled: bool, self_dev_enabled: bool) -> Dict[str, Any]:
    """Describe the active Two-Lever Operating Quadrant (`Hobby Mode` x `Self-Dev Mode`)."""
    quads = {
        (False, False): ("quiet_daily_driver", "Q1", "Quiet Stable Daily Driver", "Hobbies OFF + Self-Dev OFF — Companion core is mechanically locked READ-ONLY; `data/` & external `/project` workspaces remain writable.", []),
        (True, False): ("personal_daily_driver", "Q2", "Stable Daily Driver + Personal/Ops Hobbies", "Hobbies ON + Self-Dev OFF — Only `personal` track hobbies (`wiki_gardener`, `memory_curator`, `trace_reviewer`, `life_study_organizer`) run on idle; Companion core stays READ-ONLY.", ["personal"]),
        (False, True): ("deliberate_self_dev", "Q3", "Deliberate Interactive Self-Dev", "Hobbies OFF + Self-Dev ON — Companion can edit its own repository when explicitly asked; background idle hobbies are OFF.", []),
        (True, True): ("autonomous_lab", "Q4", "Full Autonomous Lab / VM Mode", "Hobbies ON + Self-Dev ON — Both `personal` and `self_dev` hobby tracks (`backlog_builder`, `code_simplifier`, `code_gardener`) are eligible during idle windows.", ["personal", "self_dev"]),
    }
    qid, code, title, summary, tracks = quads[(bool(hobby_enabled), bool(self_dev_enabled))]
    return {
        "id": qid, "code": code, "title": title, "summary": summary,
        "hobby_enabled": bool(hobby_enabled), "self_dev_enabled": bool(self_dev_enabled),
        "core_read_only": not self_dev_enabled, "eligible_tracks": tracks,
    }


class HobbyEngine:
    """Manages Two-Lever mode status, 2-track hobby selection, sandboxed execution, and Morning Briefings."""

    def __init__(self, store: Optional[MemoryStore] = None, cp_manager: Optional[CheckpointManager] = None, root: Optional[Path] = None):
        self._store = store
        self._cp_manager = cp_manager
        self._root = root.resolve() if root else None
        self._runner_fn: Optional[Callable[[List[Dict[str, Any]], int, MemoryStore, str], str]] = None

    @property
    def store(self) -> MemoryStore:
        return self._store or get_memory_store()

    @property
    def cp_manager(self) -> CheckpointManager:
        return self._cp_manager or get_active_checkpoint_manager()

    @property
    def root(self) -> Path:
        return self._root or self.cp_manager.root

    def bind_runner(self, runner_fn: Callable[[List[Dict[str, Any]], int, MemoryStore, str], str]) -> None:
        self._runner_fn = runner_fn

    def is_enabled(self) -> bool:
        return self.store.is_hobby_mode_enabled()

    def is_self_dev_enabled(self) -> bool:
        return self.store.is_self_dev_mode_enabled()

    def eligible_hobbies(self) -> List[HobbySpec]:
        self_dev = self.is_self_dev_enabled()
        return [h for h in HOBBY_ROTATION if self_dev or h.track == "personal"]

    def set_enabled(self, enabled: bool) -> str:
        self.store.set_hobby_mode(enabled)
        self.store.sync_memory_md()
        quad = describe_operating_quadrant(enabled, self.is_self_dev_enabled())
        state_str = "ON (Active after idle timeout)" if enabled else "OFF (Manual / Reactive only)"
        return (
            f"Autonomous Hobby Mode is now **{state_str}** (`{quad['code']}: {quad['title']}`).\n"
            f"Self-Dev Mode: **{'ON' if self.is_self_dev_enabled() else 'OFF (Core Read-Only)'}** | "
            f"Idle Trigger: {settings.idle_minutes} min | Cooldown: {settings.hobby_cooldown_minutes} min | Max Iters: {settings.hobby_max_iters}"
        )

    def set_self_dev_enabled(self, enabled: bool, actor: str = "user") -> str:
        self.store.set_self_dev_mode(enabled, actor=actor)
        self.store.sync_memory_md()
        quad = describe_operating_quadrant(self.is_enabled(), enabled)
        state_str = "ON (Companion core unlocked for self-evolution)" if enabled else "OFF (Companion core mechanically locked READ-ONLY)"
        return f"Self-Dev Mode is now **{state_str}**.\nActive Operating Quadrant: `{quad['code']} — {quad['title']}`\n{quad['summary']}"

    def select_next_hobby(self, requested_name: Optional[str] = None, fast_preview: bool = False) -> HobbySpec:
        """Select a specific hobby or rotate deterministically through eligible track(s)."""
        if requested_name and requested_name.strip().lower() in HOBBY_MAP:
            return HOBBY_MAP[requested_name.strip().lower()]

        self_dev = self.is_self_dev_enabled()
        if self_dev and not fast_preview:
            hygiene_rep = analyze_workspace_hygiene(self.root)
            sdd_rep = verify_sdd(self.root, store=self.store)
            if not hygiene_rep.clean or not sdd_rep.valid:
                return HOBBY_MAP["code_simplifier"]

        eligible = self.eligible_hobbies()
        recent = self.store.get_hobby_journal(limit=1)
        if not recent:
            return eligible[0]
        last_name = recent[0].get("hobby_name", "")
        names = [h.name for h in eligible]
        if last_name in names:
            return eligible[(names.index(last_name) + 1) % len(eligible)]
        return eligible[0]

    def _cleanup_hobby_workflow(self, pre_wf: Optional[Dict[str, Any]], completed: bool) -> None:
        curr = self.store.get_active_workflow()
        if not curr or (pre_wf and curr.get("id") == pre_wf.get("id")):
            return
        wf_id, from_st = int(curr["id"]), str(curr.get("current_state", "IMPLEMENT"))
        if completed:
            self.store.update_workflow_state(wf_id, from_st, "COMPLETE", "Auto-completed at end of passed hobby cycle", status="completed")
        else:
            self.store.update_workflow_state(wf_id, from_st, "ABORTED", "Auto-aborted due to hobby rollback", status="aborted", verification_passed=False)

    def _finish_cycle(
        self, hobby: HobbySpec, cp_id: str, pre_wf: Optional[Dict[str, Any]], passed: bool, summary: str, rollback_reason: str = "",
    ) -> Dict[str, Any]:
        if not passed:
            self.cp_manager.rollback_checkpoint(cp_id, reason=rollback_reason or summary)
        self._cleanup_hobby_workflow(pre_wf, completed=passed)
        self.store.sync_memory_md()
        if self.is_self_dev_enabled():
            sync_decisions_md(store=self.store, root=self.root)
        status = "completed" if passed else "rolled_back"
        self.store.log_hobby_run(hobby.name, status, summary, checkpoint_id=cp_id, doctor_passed=passed)
        return {"ok": passed, "hobby": hobby.name, "track": hobby.track, "status": status, "doctor_passed": passed, "checkpoint_id": cp_id, "summary": summary}

    def run_hobby_cycle(
        self,
        hobby_name: Optional[str] = None,
        runner_fn: Optional[Callable[[List[Dict[str, Any]], int], str]] = None,
        abort_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        """Run one bounded autonomous hobby cycle inside a checkpointed safety sandbox with 2-track gating."""
        hobby = self.select_next_hobby(hobby_name)
        if hobby.track == "self_dev" and not self.is_self_dev_enabled():
            msg = (
                f"SAFETY GUARD REJECTED hobby `{hobby.name}`: `self_dev` track hobbies require `Self-Dev Mode = ON` "
                "(`/selfdev on` or Web UI `System -> Autonomy & Hobbies`). Personal track hobbies "
                "(`wiki_gardener`, `memory_curator`, `trace_reviewer`, `life_study_organizer`) remain available while `Self-Dev Mode` is OFF."
            )
            return {"ok": False, "hobby": hobby.name, "track": hobby.track, "status": "refused_self_dev_off", "doctor_passed": True, "checkpoint_id": "", "summary": msg, "error": msg}

        pre_wf = self.store.get_active_workflow()
        cp = self.cp_manager.create_checkpoint(label=f"hobby_{hobby.name}")
        cp_id = cp["checkpoint_id"]
        is_live_agent = runner_fn is None
        if runner_fn is None:
            bound = self._runner_fn
            if bound is None:
                from companion.agent import run_agent_turn

                bound = lambda m, lim, st, tag: run_agent_turn(m, max_iters=lim, store=st, session_tag=tag)
            runner_fn = lambda msgs, limit: bound(msgs, limit, self.store, f"hobby:{hobby.name}")

        lessons = self.store.get_system_notes(category="rollback_lesson", limit=3)
        lessons_block = ("\n\n[RECENT ROLLBACK LESSONS — DO NOT REPEAT]\n" + "\n".join(f"- {n}" for n, _, _ in lessons)) if lessons else ""
        h_tier = hobby.preferred_tier
        gate = get_endpoint_gate(settings.resolve_tier(h_tier).base_url)
        prompt = (
            f"[AUTONOMOUS HOBBY CYCLE: {hobby.title} ({hobby.name}) | track={hobby.track} | tier={h_tier}]\n"
            f"Discipline: {hobby.discipline} | Self-Dev Mode: {'ON' if self.is_self_dev_enabled() else 'OFF (Core Read-Only)'}\n"
            f"Pre-Hobby Safety Checkpoint: `{cp_id}`\n\n{hobby.instructions}{lessons_block}"
        )
        messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]

        try:
            with memory_store_context(self.store), tier_context(h_tier), gate.background_context(abort_check=abort_check):
                agent_summary = runner_fn(messages, settings.hobby_max_iters)
        except InterruptedError as exc:
            preempt_msg = f"Hobby `{hobby.name}` preempted by foreground user activity ({exc}); restored `{cp_id}`."
            res = self._finish_cycle(hobby, cp_id, pre_wf, False, preempt_msg, rollback_reason=preempt_msg)
            res["status"] = "preempted"
            return res
        except Exception as exc:
            fail_msg = f"Hobby `{hobby.name}` raised {type(exc).__name__}: {exc}. Rolled back to `{cp_id}`."
            return self._finish_cycle(hobby, cp_id, pre_wf, False, fail_msg, rollback_reason=fail_msg)

        doc_report = run_system_doctor(root=self.root, run_tests=True)
        if not doc_report["healthy"] and is_live_agent and self.is_self_dev_enabled():
            repair_prompt = (
                "[POST-HOBBY SYSTEM DOCTOR FAILED — SELF-REPAIR PASS]\n"
                "Fix the exact issues reported below within 5 tool steps and run `system_doctor(run_tests=True)`, "
                f"or the hobby cycle will be rolled back:\n\n{doc_report['summary']}"
            )
            messages.append({"role": "user", "content": repair_prompt})
            try:
                with memory_store_context(self.store), tier_context(h_tier), gate.background_context(abort_check=abort_check):
                    agent_summary = runner_fn(messages, min(5, settings.hobby_max_iters))
            except Exception:
                pass
            doc_report = run_system_doctor(root=self.root, run_tests=True)

        if not doc_report["healthy"]:
            failed_checks = "; ".join(f"{c['check']}: {str(c['details'])[:160]}" for c in doc_report.get("checks", []) if not c.get("passed"))
            rb_summary = f"Post-hobby System Doctor check failed after `{hobby.name}`; automatically rolled back workspace to `{cp_id}`.\nDoctor Output:\n{doc_report['summary']}"
            return self._finish_cycle(hobby, cp_id, pre_wf, False, rb_summary, rollback_reason=f"Hobby `{hobby.name}` rolled back: {failed_checks or 'Doctor failed'}")

        clean_summary = agent_summary.strip() or f"Completed {hobby.title} and passed all System Doctor gates."
        return self._finish_cycle(hobby, cp_id, pre_wf, True, clean_summary)

    def get_snapshot(self, journal_limit: int = 12) -> Dict[str, Any]:
        """Return structured Two-Lever operating mode state, 2-track catalog, and journal for CLI & Web UI."""
        hobby_on, self_dev_on = self.is_enabled(), self.is_self_dev_enabled()
        next_h = self.select_next_hobby(fast_preview=True)
        journal = self.store.get_hobby_journal(limit=journal_limit)
        unseen = self.store.get_hobby_journal(unseen_only=True, mark_seen=False, limit=20)
        catalog = [
            {
                "name": h.name, "title": h.title, "track": h.track, "discipline": h.discipline,
                "description": h.description, "preferred_tier": h.preferred_tier,
                "eligible": bool(self_dev_on or h.track == "personal"),
                "locked_reason": "" if (self_dev_on or h.track == "personal") else "Requires Self-Dev Mode = ON",
                "is_next": h.name == next_h.name,
            }
            for h in HOBBY_ROTATION
        ]
        return {
            "hobby_enabled": hobby_on, "hobby_mode": "on" if hobby_on else "off",
            "self_dev_enabled": self_dev_on, "self_dev_mode": "on" if self_dev_on else "off",
            "self_dev_locked": settings.is_self_dev_locked_by_env(), "self_dev_lock_reason": settings.self_dev_lock_reason(),
            "immutable_core": settings.immutable_core, "quadrant": describe_operating_quadrant(hobby_on, self_dev_on),
            "idle_minutes": settings.idle_minutes, "cooldown_minutes": settings.hobby_cooldown_minutes,
            "max_iters": settings.hobby_max_iters, "next_hobby": next_h.name,
            "catalog": catalog, "journal": journal, "unseen_count": len(unseen),
            "morning_briefing": self.get_morning_briefing(mark_seen=False),
        }

    def format_status(self) -> str:
        snap = self.get_snapshot(journal_limit=5)
        quad = snap["quadrant"]
        lock_note = f" [{snap['self_dev_lock_reason']}]" if snap["self_dev_locked"] else " (Toggle with `/selfdev on|off`)"
        lines = [
            "=== Companion Two-Lever Autonomy & Hobby Engine ===",
            f"Active Quadrant : {quad['code']} — {quad['title']}",
            f"Hobby Mode      : {'ON (Autonomous Idle Loop Active)' if snap['hobby_enabled'] else 'OFF (Toggle with `/hobby on`)'}",
            f"Self-Dev Mode   : {'ON (Core Self-Evolution Unlocked)' if snap['self_dev_enabled'] else 'OFF (Core Mechanically READ-ONLY)'}{lock_note}",
            f"Idle Trigger    : {snap['idle_minutes']} min inactivity | Cooldown: {snap['cooldown_minutes']} min | Budget: {snap['max_iters']} steps",
            f"Next Eligible   : `{snap['next_hobby']}`",
            "",
            "Personal / Ops Track (Always safe — runs even when Self-Dev Mode is OFF):",
            *[f"  - `{c['name']}` [tier={c['preferred_tier']} | ACTIVE]: {c['description']}" for c in snap["catalog"] if c["track"] == "personal"],
            "",
            f"Self-Dev Engineering Track ({'UNLOCKED' if snap['self_dev_enabled'] else 'LOCKED — requires Self-Dev Mode = ON'}):",
            *[
                f"  - `{c['name']}` [tier={c['preferred_tier']} | {'ACTIVE' if c['eligible'] else 'LOCKED'}]: {c['description']}"
                for c in snap["catalog"] if c["track"] == "self_dev"
            ],
            "\nRecent Hobby Journal:",
        ]
        if snap["journal"]:
            for j in snap["journal"]:
                lines.append(f"  - [#{j['id']} {j['created_at']} | {j['hobby_name']} | {j['status']}] {j['summary'].splitlines()[0][:140]}")
        else:
            lines.append("  - (No autonomous hobby runs recorded yet. Run `/hobby run` to test one now!)")
        return "\n".join(lines)

    def get_morning_briefing(self, mark_seen: bool = True) -> str:
        """Format a Morning Briefing of unseen (or recent) autonomous hobby runs."""
        unseen = self.store.get_hobby_journal(unseen_only=True, mark_seen=mark_seen, limit=20)
        entries = unseen if unseen else self.store.get_hobby_journal(unseen_only=False, mark_seen=False, limit=5)
        if not entries:
            return "=== Morning Briefing ===\nNo autonomous hobby sessions have run yet. Enable with `/hobby on` or run one with `/hobby run`."
        header = (
            f"=== Morning Briefing ({len(unseen)} new autonomous session(s) while you were away) ==="
            if unseen
            else f"=== Hobby Journal Summary (Last {len(entries)} session(s)) ==="
        )
        lines = [header]
        for e in reversed(entries):
            badge = "PASS" if e["doctor_passed"] else "ROLLED BACK"
            lines.append(f"\n* [{e['created_at']}] **{e['hobby_name']}** ({e['status'].upper()} | Doctor: {badge} | Checkpoint: `{e['checkpoint_id']}`)\n  {e['summary'][:500]}")
        return "\n".join(lines)


hobby_engine = HobbyEngine()


def parse_selfdev_slash(parts: List[str]) -> Dict[str, Any]:
    """Parse `/selfdev [on|off|toggle|status]` CLI slash arguments into a `hobbies` CoreAction payload."""
    sub = parts[0].strip().lower() if parts else "status"
    if sub in ("on", "enable", "1", "true"):
        return {"subaction": "set_self_dev", "enabled": True}
    if sub in ("off", "disable", "0", "false"):
        return {"subaction": "set_self_dev", "enabled": False}
    if sub == "toggle":
        return {"subaction": "set_self_dev", "mode": "toggle"}
    return {"subaction": "status"}


def execute_hobby_mode_action(p: Dict[str, Any], actor: str, mem: MemoryStore) -> Dict[str, Any]:
    """Shared `CORE_ACTIONS['hobbies']` handler for Web UI `System` Studio and CLI (`/selfdev`)."""
    eng = HobbyEngine(store=mem)
    sub = str(p.get("subaction") or "status").strip().lower()
    if sub in ("set_hobby", "hobby"):
        mode = str(p.get("mode") or "").strip().lower()
        target = (not eng.is_enabled()) if mode == "toggle" else (
            mode in ("on", "true", "1", "enable") if mode else bool(p.get("enabled"))
        )
        msg = eng.set_enabled(target)
        return {"ok": True, **eng.get_snapshot(), "summary": msg}
    if sub in ("set_self_dev", "selfdev"):
        mode = str(p.get("mode") or "").strip().lower()
        target = (not eng.is_self_dev_enabled()) if mode == "toggle" else (
            mode in ("on", "true", "1", "enable") if mode else bool(p.get("enabled"))
        )
        try:
            msg = eng.set_self_dev_enabled(target, actor=actor)
        except PermissionError as exc:
            return {"ok": False, "error": str(exc), **eng.get_snapshot()}
        return {"ok": True, **eng.get_snapshot(), "summary": msg}
    if sub in ("run", "run_hobby"):
        req_name = str(p.get("hobby_name") or p.get("name") or "").strip() or None
        res = eng.run_hobby_cycle(hobby_name=req_name)
        ok = bool(res.get("status") != "refused_self_dev_off" and res.get("ok", True))
        return {**eng.get_snapshot(), **res, "ok": ok}
    if sub in ("morning", "mark_seen"):
        brief = eng.get_morning_briefing(mark_seen=bool(p.get("mark_seen", True)))
        return {"ok": True, **eng.get_snapshot(), "morning_briefing": brief, "summary": brief}
    return {"ok": True, **eng.get_snapshot(), "summary": eng.format_status()}


class WebIdleHobbyWatcher:
    """Respectful background idle-hobby scheduler for the LAN Web Server (`Hobby Mode` parity with CLI)."""

    def __init__(
        self,
        is_busy_fn: Callable[[], bool],
        engine: Optional[HobbyEngine] = None,
        poll_interval_sec: float = 15.0,
    ):
        self._is_busy_fn = is_busy_fn
        self._engine = engine or hobby_engine
        self._poll_sec = max(1.0, float(poll_interval_sec))
        self._lock = threading.Lock()
        self.last_human_activity_mono = time.monotonic()
        self.last_hobby_run_mono = 0.0
        self._running_cycle = False
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def record_human_activity(self) -> None:
        with self._lock:
            self.last_human_activity_mono = time.monotonic()

    def tick_once(self, now_mono: Optional[float] = None, idle_sec_override: Optional[float] = None, cooldown_sec_override: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Evaluate idle & cooldown gates once and run one eligible hobby cycle if idle and uncontended."""
        now = time.monotonic() if now_mono is None else float(now_mono)
        if not self._engine.is_enabled() or self._is_busy_fn():
            return None
        idle_req = float(settings.idle_minutes * 60) if idle_sec_override is None else float(idle_sec_override)
        cool_req = float(settings.hobby_cooldown_minutes * 60) if cooldown_sec_override is None else float(cooldown_sec_override)
        with self._lock:
            if self._running_cycle or (now - self.last_human_activity_mono) < idle_req:
                return None
            if self.last_hobby_run_mono > 0.0 and (now - self.last_hobby_run_mono) < cool_req:
                return None
            self._running_cycle = True
        try:
            res = self._engine.run_hobby_cycle(abort_check=self._is_busy_fn)
            telemetry.log("INFO", "hobbies", f"Web idle hobby `{res.get('hobby')}` finished ({res.get('status')})")
            return res
        finally:
            with self._lock:
                self.last_hobby_run_mono = time.monotonic() if now_mono is None else float(now_mono)
                self._running_cycle = False

    def _loop(self) -> None:
        while not self._stop_evt.wait(self._poll_sec):
            try:
                self.tick_once()
            except Exception as exc:
                telemetry.log("WARN", "hobbies", f"WebIdleHobbyWatcher tick error: {exc}")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="companion-web-idle-hobbies", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
