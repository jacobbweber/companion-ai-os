"""
Universal Headless Project & Goal Graph (`companion/project_graph.py` — Domain Layer).

Unifies tasks, backlog, `/plan`, `/goal`, and Kanban/Roadmap projections into one
domain-blind execution graph (`Projects/Goals -> Milestones -> Work Items`):
- Supports `owner` (`companion` | `human` | `collaborative`)
- Supports prerequisite `stage_kind` (`OBSERVE_DATA`, `CURATE_DIKW`, `FORGE_CAPABILITY`, `DELIVER_AND_VERIFY`)
- Enforces topological `depends_on` readiness (completing an `owner=human` blocker automatically
  unblocks dependent `owner=companion` items)
- Attaches `TriggerPolicy` to Goals/Projects for multi-session `on_idle` windowed execution
"""

import contextlib
import json
import os
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from companion.config import SELF_PROJECT_ID, _WORKSPACE_OVERRIDE, settings, workspace_context
from companion.llm import chat, normalize_assistant_message, tier_context
from companion.memory_store import MemoryStore, get_memory_store
from companion.routines import TriggerPolicy, evaluate_trigger_eligibility, register_trigger_precheck
from companion.vault import normalize_secret_name

VALID_ITEM_OWNERS = ("companion", "human", "collaborative")
VALID_DELIVERY_POLICIES = ("local_only", "remote_branch_pr", "ask_me")
VALID_STAGE_KINDS = ("OBSERVE_DATA", "CURATE_DIKW", "FORGE_CAPABILITY", "DELIVER_AND_VERIFY")
STAGE_ORDER: Dict[str, int] = {name: idx for idx, name in enumerate(VALID_STAGE_KINDS)}
DONE_STATUSES = ("done", "completed")
READY_STATUSES = ("ready", "pending", "sprint_todo")
BOARD_COLUMNS = (
    ("backlog", "Backlog"),
    ("ready", "Ready"),
    ("in_progress", "In Progress"),
    ("blocked", "Blocked / Waiting"),
    ("verify", "Verify"),
    ("done", "Done"),
)

_ITEM_COLS = [
    "id", "item_id", "project_id", "milestone_id", "timebox_id", "workflow_id", "item_kind",
    "title", "description", "category", "status", "owner", "stage_kind", "priority",
    "depends_on_json", "verification_json", "dag_name", "checkpoint_id",
    "acceptance_criteria", "verification_result", "active_job_id", "worktree_path",
    "branch_name", "deliverable_json", "estimate_minutes",
    "planned_date", "due_date", "checklist_json", "tags_json", "position",
    "created_at", "updated_at", "completed_at",
]


def _slugify(text: str, fallback: str = "project") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:48] or fallback


def _is_project_placeholder(val: Any) -> bool:
    s = str(val or "").strip().lower()
    return s in ("...", "…", "1. ...", "step 1", "1-2 sentences") or s.endswith("...")


def _extract_project_json(raw_text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1].strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(cleaned):
        idx = cleaned.find("{", pos)
        if idx == -1:
            break
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        pos = idx + 1
    return {}


def _norm_column_key(status: str, item_kind: str = "task") -> str:
    st = (status or "ready").strip().lower()
    if st in DONE_STATUSES:
        return "done"
    if st == "backlog" or (item_kind == "backlog" and st in READY_STATUSES):
        return "backlog"
    if st in ("blocked", "waiting_approval"):
        return "blocked"
    if st == "verify":
        return "verify"
    if st == "in_progress":
        return "in_progress"
    return "ready"


def _hydrate_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(raw)
    for jcol, key, default in (
        ("depends_on_json", "depends_on", []), ("verification_json", "verification", {}),
        ("deliverable_json", "deliverable", {}), ("checklist_json", "checklist", []), ("tags_json", "tags", []),
    ):
        try:
            out[key] = json.loads(out.get(jcol) or ("[]" if isinstance(default, list) else "{}"))
        except Exception:
            out[key] = list(default) if isinstance(default, list) else dict(default)
    out["active_job_id"] = str(out.get("active_job_id") or "")
    out["worktree_path"] = str(out.get("worktree_path") or "")
    out["branch_name"] = str(out.get("branch_name") or "")
    out["column_key"] = _norm_column_key(str(out.get("status") or ""), str(out.get("item_kind") or "task"))
    out["card_id"] = out["item_id"]
    out["epic_id"] = out.get("milestone_id") or ""
    out["sprint_id"] = out.get("timebox_id") or ""
    return out


def _clean_delivery_policy(raw: Optional[str]) -> str:
    dp = (raw or "local_only").strip().lower()
    if dp not in VALID_DELIVERY_POLICIES:
        raise ValueError(f"delivery_policy must be one of {VALID_DELIVERY_POLICIES}, got '{raw}'.")
    return dp


def _clean_git_cred_ref(raw: Optional[str]) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    if "-----BEGIN " in val or val.startswith(("ghp_", "github_pat_", "glpat-", "sk-")):
        raise ValueError("VAULT GUARD REJECTED: Pass a symbolic Blind Vault slug in `git_credential_ref`, never a raw token or key.")
    return normalize_secret_name(val)


def _hydrate_project(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(raw)
    try:
        tp_raw = json.loads(out.get("trigger_policy_json") or "{}")
    except Exception:
        tp_raw = {}
    try:
        meta = json.loads(out.get("metadata_json") or "{}")
    except Exception:
        meta = {}
    dp = str(out.get("delivery_policy") or meta.get("delivery_policy") or "local_only").strip().lower()
    out["delivery_policy"] = dp if dp in VALID_DELIVERY_POLICIES else "local_only"
    out["git_credential_ref"] = str(out.get("git_credential_ref") or meta.get("git_credential_ref") or "").strip()
    out["max_parallel_workers"] = max(1, min(4, int(out.get("max_parallel_workers") or meta.get("max_parallel_workers") or 2)))
    out["test_command"] = str(meta.get("test_command") or "").strip()
    out["preview_command"] = str(meta.get("preview_command") or "").strip()
    out["trigger_policy"] = TriggerPolicy.from_dict(tp_raw).to_dict() if tp_raw else TriggerPolicy(mode="manual").to_dict()
    out["metadata"] = meta
    out["board_id"] = out["project_id"]
    return out


class ProjectGraphManager:
    """Domain-blind manager for Goals/Projects, Milestones, Timeboxes, and Work Items."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self.store = store or get_memory_store()
        self._ensure_default_projects()

    def _ensure_default_projects(self) -> None:
        ws = settings.get_workspace()
        with self.store._lock:
            self.store.conn.execute(
                "INSERT OR IGNORE INTO projects (project_id, slug, title, kind, workspace_id) VALUES (?, ?, ?, 'project', ?)",
                (SELF_PROJECT_ID, _slugify(SELF_PROJECT_ID), "Companion Self & Core", SELF_PROJECT_ID),
            )
            alt_ws_id = ws.project_id.replace("-", "_")
            has_ws = self.store.conn.execute(
                "SELECT 1 FROM projects WHERE project_id IN (?, ?) OR workspace_path = ? LIMIT 1",
                (ws.project_id, alt_ws_id, str(ws.project_root.resolve())),
            ).fetchone()
            if not has_ws and ws.project_id:
                self.store.conn.execute(
                    "INSERT OR IGNORE INTO projects (project_id, slug, title, kind, workspace_id) VALUES (?, ?, ?, 'project', ?)",
                    (ws.project_id, _slugify(ws.project_id), ws.project_id.replace("-", " ").title(), ws.project_id),
                )
            self.store.conn.commit()

    def resolve_project_id(self, project_id: Optional[str] = None) -> str:
        raw = (project_id or "").strip()
        if not raw:
            focused = self.store.get_user_profile().get("active_goal_project_id", "").strip()
            if focused:
                f_row = self.store._fetch_rows("SELECT project_id FROM projects WHERE project_id IN (?, ?) OR slug = ? LIMIT 1", (focused, focused.replace("-", "_"), focused))
                if f_row:
                    return str(f_row[0][0])
            ws = settings.get_workspace()
            w_row = self.store._fetch_rows("SELECT project_id FROM projects WHERE project_id IN (?, ?) OR workspace_path = ? LIMIT 1", (ws.project_id, ws.project_id.replace("-", "_"), str(ws.project_root.resolve())))
            return str(w_row[0][0]) if w_row else (ws.project_id or SELF_PROJECT_ID)
        row = self.store._fetch_rows("SELECT project_id FROM projects WHERE project_id IN (?, ?) OR slug = ? LIMIT 1", (raw, raw.replace("-", "_"), raw))
        return str(row[0][0]) if row else raw

    def set_focus_project(self, project_id: str) -> Dict[str, Any]:
        """Set the user's active Goal/Project focus across UI and Chat tool turns (syncing repo `workspace_path` if bound)."""
        pid = self.resolve_project_id(project_id)
        proj = self.get_project(pid)
        self.store.set_user_profile("active_goal_project_id", proj["project_id"])
        ws_path = str(proj.get("workspace_path") or "").strip()
        if self.store.db_path.resolve() == settings.db_path.resolve():
            if proj["project_id"] == SELF_PROJECT_ID:
                settings.reset_workspace()
            elif ws_path and Path(ws_path).is_dir():
                settings.set_workspace(Path(ws_path).resolve(), proj["project_id"])
        return proj

    @contextlib.contextmanager
    def project_workspace_scope(self, project_id: Optional[str] = None) -> Iterator[None]:
        """Bind `workspace_context(Path(workspace_path), project_id)` when a focused/target project has a bound repo directory."""
        raw_pid = "" if _WORKSPACE_OVERRIDE.get() is not None else (project_id or self.store.get_user_profile().get("active_goal_project_id") or "").strip()
        if not raw_pid:
            yield
            return
        proj = self.get_project(raw_pid)
        ws_path = str(proj.get("workspace_path") or "").strip()
        if ws_path and Path(ws_path).is_dir():
            with workspace_context(Path(ws_path).resolve(), proj["project_id"]):
                yield
        else:
            yield

    def create_project(
        self, title: str, goal_summary: str = "", slug: Optional[str] = None, kind: str = "goal",
        workspace_id: Optional[str] = None, workspace_path: str = "", delivery_policy: str = "local_only",
        git_credential_ref: str = "", max_parallel_workers: int = 2, test_command: str = "", preview_command: str = "",
        wiki_hub_path: str = "", risk_ceiling: str = "R2", trigger_policy: Optional[Union[TriggerPolicy, Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None, project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Project/Goal title is required.")
        base_slug = _slugify(slug or clean_title, "goal")
        existing_p = None if (project_id or slug) else self.store._fetch_rows("SELECT project_id FROM projects WHERE LOWER(title) = LOWER(?) AND status = 'active' LIMIT 1", (clean_title,))
        pid = (project_id or (str(existing_p[0][0]) if existing_p else f"proj_{base_slug}_{uuid.uuid4().hex[:6]}")).strip()
        tp = trigger_policy if isinstance(trigger_policy, TriggerPolicy) else TriggerPolicy.from_dict(trigger_policy)
        ws_id = (workspace_id or settings.get_workspace().project_id or SELF_PROJECT_ID).strip()
        ws_p = str(Path(workspace_path).resolve()) if (workspace_path or "").strip() else ""
        dp_clean = _clean_delivery_policy(delivery_policy)
        cred_clean = _clean_git_cred_ref(git_credential_ref)
        mpw = max(1, min(4, int(max_parallel_workers or 2)))
        eff_meta = dict(metadata or {})
        if test_command.strip():
            eff_meta["test_command"] = test_command.strip()
        if preview_command.strip():
            eff_meta["preview_command"] = preview_command.strip()
        with self.store._lock:
            final_slug = base_slug
            if self.store.conn.execute("SELECT 1 FROM projects WHERE slug = ? AND project_id != ?", (final_slug, pid)).fetchone():
                final_slug = f"{base_slug}-{uuid.uuid4().hex[:4]}"
            self.store.conn.execute(
                "INSERT INTO projects (project_id, slug, title, goal_summary, kind, status, workspace_id, workspace_path, delivery_policy, git_credential_ref, max_parallel_workers, wiki_hub_path, risk_ceiling, trigger_policy_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET title = excluded.title, goal_summary = excluded.goal_summary, kind = excluded.kind, "
                "workspace_path = CASE WHEN excluded.workspace_path != '' THEN excluded.workspace_path ELSE projects.workspace_path END, "
                "delivery_policy = excluded.delivery_policy, max_parallel_workers = excluded.max_parallel_workers, "
                "git_credential_ref = CASE WHEN excluded.git_credential_ref != '' THEN excluded.git_credential_ref ELSE projects.git_credential_ref END, "
                "wiki_hub_path = excluded.wiki_hub_path, risk_ceiling = excluded.risk_ceiling, trigger_policy_json = excluded.trigger_policy_json, metadata_json = excluded.metadata_json, updated_at = CURRENT_TIMESTAMP",
                (pid, final_slug, clean_title, goal_summary.strip(), kind, ws_id, ws_p, dp_clean, cred_clean, mpw, wiki_hub_path.strip(), risk_ceiling, json.dumps(tp.to_dict()), json.dumps(eff_meta)),
            )
            self.store.conn.commit()
        return self.get_project(pid)

    def get_project(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        pid = self.resolve_project_id(project_id)
        cols = ["project_id", "slug", "title", "goal_summary", "kind", "status", "workspace_id", "workspace_path", "delivery_policy", "git_credential_ref", "max_parallel_workers", "wiki_hub_path", "risk_ceiling", "trigger_policy_json", "metadata_json", "created_at", "updated_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM projects WHERE project_id = ?", (pid,))
        if not rows:
            with self.store._lock:
                self.store._ensure_project_record(pid)
                self.store.conn.commit()
            rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM projects WHERE project_id = ?", (pid,))
        return _hydrate_project(rows[0])

    def list_projects(self, status: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        cols = ["project_id", "slug", "title", "goal_summary", "kind", "status", "workspace_id", "workspace_path", "delivery_policy", "git_credential_ref", "max_parallel_workers", "wiki_hub_path", "risk_ceiling", "trigger_policy_json", "metadata_json", "created_at", "updated_at"]
        clauses: List[Tuple[str, Any]] = ([("status = ?", status)] if status else []) + ([("kind = ?", kind)] if kind else [])
        where = (" WHERE " + " AND ".join(c for c, _ in clauses)) if clauses else ""
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM projects{where} ORDER BY updated_at DESC, created_at ASC", tuple(v for _, v in clauses))
        return [_hydrate_project(r) for r in rows]

    def update_project(self, project_id: str, **updates: Any) -> Dict[str, Any]:
        proj = self.get_project(project_id)
        pid = proj["project_id"]
        tp_dict = dict(proj["trigger_policy"])
        if "trigger_policy" in updates and updates["trigger_policy"] is not None:
            raw_tp = updates["trigger_policy"]
            tp_dict = raw_tp.to_dict() if isinstance(raw_tp, TriggerPolicy) else TriggerPolicy.from_dict({**tp_dict, **dict(raw_tp)}).to_dict()
        meta = {**dict(proj["metadata"]), **dict(updates.get("metadata") or {})}
        for cmd_k in ("test_command", "preview_command"):
            if cmd_k in updates and updates[cmd_k] is not None:
                meta[cmd_k] = str(updates[cmd_k]).strip()
        title = str(updates.get("title") or proj["title"]).strip()
        goal_s = str(updates.get("goal_summary") if updates.get("goal_summary") is not None else proj["goal_summary"]).strip()
        status = str(updates.get("status") or proj["status"]).strip()
        kind = str(updates.get("kind") or proj["kind"]).strip()
        wiki_p = str(updates.get("wiki_hub_path") if updates.get("wiki_hub_path") is not None else proj["wiki_hub_path"]).strip()
        raw_ws_p = updates.get("workspace_path")
        ws_p = (str(Path(str(raw_ws_p).strip()).resolve()) if str(raw_ws_p).strip() else "") if raw_ws_p is not None else str(proj.get("workspace_path") or "")
        dp_clean = _clean_delivery_policy(str(updates["delivery_policy"])) if updates.get("delivery_policy") is not None else str(proj.get("delivery_policy") or "local_only")
        cred_clean = _clean_git_cred_ref(str(updates["git_credential_ref"])) if updates.get("git_credential_ref") is not None else str(proj.get("git_credential_ref") or "")
        mpw = max(1, min(4, int(updates["max_parallel_workers"]))) if updates.get("max_parallel_workers") not in (None, "") else int(proj.get("max_parallel_workers") or 2)
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE projects SET title = ?, goal_summary = ?, kind = ?, status = ?, workspace_path = ?, delivery_policy = ?, git_credential_ref = ?, max_parallel_workers = ?, wiki_hub_path = ?, trigger_policy_json = ?, metadata_json = ?, updated_at = CURRENT_TIMESTAMP WHERE project_id = ?",
                (title, goal_s, kind, status, ws_p, dp_clean, cred_clean, mpw, wiki_p, json.dumps(tp_dict), json.dumps(meta), pid),
            )
            self.store.conn.commit()
        return self.get_project(pid)

    def create_milestone(self, project_id: str, title: str, description: str = "", stage_kind: str = "DELIVER_AND_VERIFY", target_date: str = "", sort_order: Optional[int] = None, milestone_id: str = "") -> Dict[str, Any]:
        pid = self.resolve_project_id(project_id)
        sk = stage_kind if stage_kind in VALID_STAGE_KINDS else "DELIVER_AND_VERIFY"
        mid = (milestone_id or "").strip() or f"ms_{uuid.uuid4().hex[:8]}"
        order_val = int(sort_order) if sort_order is not None else STAGE_ORDER.get(sk, 3) * 10
        with self.store._lock:
            self.store._ensure_project_record(pid)
            self.store.conn.execute(
                "INSERT INTO milestones (milestone_id, project_id, title, description, stage_kind, status, sort_order, target_date) VALUES (?, ?, ?, ?, ?, 'active', ?, ?) "
                "ON CONFLICT(milestone_id) DO UPDATE SET title = CASE WHEN excluded.title != '' THEN excluded.title ELSE milestones.title END, "
                "description = CASE WHEN excluded.description != '' THEN excluded.description ELSE milestones.description END, "
                "stage_kind = excluded.stage_kind, target_date = CASE WHEN excluded.target_date != '' THEN excluded.target_date ELSE milestones.target_date END",
                (mid, pid, title.strip(), description.strip(), sk, order_val, target_date.strip()),
            )
            self.store.conn.commit()
        rows = self.store._fetch_dicts(["milestone_id", "project_id", "title", "description", "stage_kind", "status", "sort_order", "target_date"], "SELECT milestone_id, project_id, title, description, stage_kind, status, sort_order, target_date FROM milestones WHERE milestone_id = ?", (mid,))
        out = rows[0] if rows else {"milestone_id": mid, "project_id": pid, "title": title.strip(), "description": description.strip(), "stage_kind": sk, "status": "active", "sort_order": order_val, "target_date": target_date.strip()}
        out["epic_id"] = out["milestone_id"]
        return out

    def delete_milestone(self, milestone_id: str) -> bool:
        clean_id = (milestone_id or "").strip()
        if not clean_id:
            return False
        with self.store._lock:
            self.store.conn.execute("UPDATE work_items SET milestone_id = '' WHERE milestone_id = ?", (clean_id,))
            cur = self.store.conn.execute("DELETE FROM milestones WHERE milestone_id = ?", (clean_id,))
            self.store.conn.commit()
        return bool(cur.rowcount)

    def slice_unblocker(
        self,
        item_ref: Union[str, int],
        title: str = "",
        description: str = "",
        owner: str = "",
        chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Create a concrete 15-minute `ready` unblocker step via LLM decomposition (when `title` omitted) + deterministic SQLite link."""
        parent = self.get_item(item_ref)
        proj = self.get_project(parent["project_id"])
        step_title = (title or "").strip()
        step_desc = (description or "").strip()
        step_owner = (owner or "").strip().lower()
        step_checklist: List[str] = []

        should_ai = (
            not step_title
            and (chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1"))
        )
        if should_ai:
            llm_call = chat_fn or chat
            sys_msg = (
                "You are Companion's Personal Agile Unblocker Coach.\n"
                "Think very briefly (at most 3 short sentences), then decompose a stuck/large task into the single smallest concrete <=15-minute physical or cognitive first action that breaks inertia.\n"
                "Make it domain-authentic (household, DIY, cooking, finances, or coding — NEVER generic '15m Unblocker: ...' repetition).\n"
                "Respond ONLY with a JSON object containing keys `title`, `description`, `owner` (`human` or `companion`), and `checklist` (array of 3 short steps)."
            )
            usr_msg = (
                f"Project: {proj['title']} ({proj.get('goal_summary') or proj.get('kind')})\n"
                f"Blocked/Target Card: {parent['title']}\nDetails: {parent.get('description') or '(none)'}\n"
                f"Current Owner: {parent['owner']}"
            )
            try:
                with tier_context("fast"):
                    resp = llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none", max_tokens=4096) if chat_fn is None else llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none")
                txt, _thk, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
                parsed = _extract_project_json(txt)
                t_cand = str(parsed.get("title") or "").strip()
                if t_cand and not _is_project_placeholder(t_cand):
                    step_title = t_cand[:120]
                d_cand = str(parsed.get("description") or "").strip()
                if not step_desc and d_cand and not _is_project_placeholder(d_cand):
                    step_desc = d_cand[:400]
                cand_own = str(parsed.get("owner") or "").strip().lower()
                if not step_owner and cand_own in VALID_ITEM_OWNERS:
                    step_owner = cand_own
                if isinstance(parsed.get("checklist"), list):
                    step_checklist = [str(c).strip() for c in parsed["checklist"] if str(c).strip() and not _is_project_placeholder(c)][:5]
            except Exception:
                pass

        eff_title = step_title or f"15m Unblocker: {parent['title'][:72]}"
        eff_desc = step_desc or f"First 15-minute unblocking slice for `{parent['item_id']}` ({parent['title']})."
        eff_owner = step_owner if step_owner in VALID_ITEM_OWNERS else (parent["owner"] or "human")
        unblocker = self.create_item(
            project_id=parent["project_id"],
            title=eff_title,
            description=eff_desc,
            owner=eff_owner,
            stage_kind=parent["stage_kind"],
            status="ready",
            priority=max(1, int(parent["priority"]) - 1),
            milestone_id=parent["milestone_id"],
            timebox_id=parent["timebox_id"],
            estimate_minutes=15,
            checklist=step_checklist or None,
            tags=["unblocker", parent["item_id"]],
        )
        new_deps = list(dict.fromkeys([unblocker["item_id"], *parent["depends_on"]]))
        self.update_item(parent["item_id"], depends_on=new_deps)
        return unblocker

    def seed_dikw_goal_milestones(
        self,
        project_id: str,
        chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> Dict[str, str]:
        """Generate 4 domain-authentic DIKW milestones (+ starter items when live LLM is available) for `project_id`."""
        pid = self.resolve_project_id(project_id)
        proj = self.get_project(pid)
        existing = {m["stage_kind"]: m["milestone_id"] for m in self.list_milestones(pid)}
        titles = {
            "OBSERVE_DATA": "1. Observe & Gather Grounding Data",
            "CURATE_DIKW": "2. Synthesize & Curate DIKW Wiki Context",
            "FORGE_CAPABILITY": "3. Prepare Tools, Materials & Capabilities",
            "DELIVER_AND_VERIFY": "4. Deliver, Verify & Complete Outcomes",
        }
        descriptions: Dict[str, str] = {}
        starter_tasks: Dict[str, Dict[str, Any]] = {}

        missing_stages = [sk for sk in VALID_STAGE_KINDS if sk not in existing]
        should_ai = bool(missing_stages) and (
            chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1")
        )
        if should_ai:
            llm_call = chat_fn or chat
            sys_msg = (
                "You are Companion's Goal & Milestone Planner.\n"
                "Think very briefly (at most 3 short sentences), then tailor 4 progressive milestones (`OBSERVE_DATA`, `CURATE_DIKW`, `FORGE_CAPABILITY`, `DELIVER_AND_VERIFY`) "
                "specifically to the user's goal domain (e.g. DIY patio refresh, cooking, fitness, household budget, or software — NEVER use software/DAG/DIKW jargon for non-software goals!).\n"
                "For each of the 4 keys inside `stages`, provide a domain-authentic `title` (numbered `1.`..`4.`), `description`, and `starter_item` (`title` and `owner`: `human` or `companion`).\n"
                "Respond ONLY with a JSON object containing key `stages` mapping `OBSERVE_DATA`, `CURATE_DIKW`, `FORGE_CAPABILITY`, and `DELIVER_AND_VERIFY`."
            )
            usr_msg = f"Project Title: {proj['title']}\nGoal Summary: {proj.get('goal_summary') or '(general goal)'}\nKind: {proj.get('kind')}"
            try:
                with tier_context("fast"):
                    resp = llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none", max_tokens=4096) if chat_fn is None else llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none")
                txt, _thk, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
                parsed = _extract_project_json(txt)
                stages_obj = parsed.get("stages") if isinstance(parsed.get("stages"), dict) else parsed
                if isinstance(stages_obj, dict):
                    for sk in VALID_STAGE_KINDS:
                        entry = stages_obj.get(sk)
                        if isinstance(entry, dict) and str(entry.get("title") or "").strip() and not _is_project_placeholder(entry.get("title")):
                            titles[sk] = str(entry["title"]).strip()[:120]
                            descriptions[sk] = str(entry.get("description") or "").strip()[:260]
                            st_it = entry.get("starter_item")
                            if isinstance(st_it, dict) and str(st_it.get("title") or "").strip() and not _is_project_placeholder(st_it.get("title")):
                                starter_tasks[sk] = st_it
            except Exception:
                pass

        prev_item_id = ""
        for idx, sk in enumerate(VALID_STAGE_KINDS):
            if sk not in existing:
                ms = self.create_milestone(pid, titles[sk], description=descriptions.get(sk, ""), stage_kind=sk, sort_order=idx * 10)
                existing[sk] = ms["milestone_id"]
                if sk in starter_tasks:
                    st_spec = starter_tasks[sk]
                    st_own = str(st_spec.get("owner") or "companion").strip().lower()
                    created_it = self.create_item(
                        project_id=pid,
                        title=str(st_spec["title"]).strip()[:120],
                        description=str(st_spec.get("description") or descriptions.get(sk, "")).strip()[:280],
                        owner=st_own if st_own in VALID_ITEM_OWNERS else "companion",
                        stage_kind=sk,
                        milestone_id=ms["milestone_id"],
                        depends_on=[prev_item_id] if prev_item_id else None,
                    )
                    prev_item_id = created_it["item_id"]
        return existing

    def list_milestones(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        pid = self.resolve_project_id(project_id)
        cols = ["milestone_id", "project_id", "title", "description", "stage_kind", "status", "sort_order", "target_date", "created_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM milestones WHERE project_id = ? ORDER BY sort_order ASC, created_at ASC", (pid,))
        for r in rows:
            r["epic_id"] = r["milestone_id"]
        return rows

    def _resolve_dep_ids(self, pid: str, raw_deps: Optional[List[Any]], exclude_id: str = "") -> List[str]:
        if not raw_deps:
            return []
        existing = self.list_items(project_id=pid)
        known_ids, out = {it["item_id"] for it in existing}, []
        for raw in raw_deps:
            d = str(raw or "").strip()
            if not d or d == exclude_id:
                continue
            if d not in known_ids:
                low_d = d.lower()
                hit = next((it for it in existing if str(it["id"]) == d or it["title"].lower() == low_d or (d.startswith("wi_") and len(d) >= 9 and it["item_id"][:9] == d[:9]) or (not d.startswith("wi_") and len(low_d) >= 4 and low_d in it["title"].lower())), None)
                d = hit["item_id"] if hit else d
            if d != exclude_id and d not in out:
                out.append(d)
        return out

    def create_item(
        self, project_id: Optional[str], title: str, description: str = "", owner: str = "companion",
        stage_kind: str = "DELIVER_AND_VERIFY", status: str = "ready", priority: int = 3,
        depends_on: Optional[List[str]] = None, milestone_id: str = "", timebox_id: str = "",
        workflow_id: Optional[int] = None, verification: Optional[Dict[str, Any]] = None, dag_name: str = "",
        acceptance_criteria: str = "", estimate_minutes: int = 30, planned_date: str = "", due_date: str = "",
        checklist: Optional[List[Any]] = None, tags: Optional[List[str]] = None, category: str = "general", item_kind: str = "task",
    ) -> Dict[str, Any]:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValueError("Work item title is required.")
        pid = self.resolve_project_id(project_id)
        own = owner.strip().lower() if owner and owner.strip().lower() in VALID_ITEM_OWNERS else "companion"
        sk = stage_kind if stage_kind in VALID_STAGE_KINDS else "DELIVER_AND_VERIFY"
        deps = self._resolve_dep_ids(pid, depends_on)
        dup_rows = self.store._fetch_rows("SELECT item_id FROM work_items WHERE project_id = ? AND milestone_id = ? AND LOWER(title) = LOWER(?) AND status NOT IN ('done', 'completed', 'cancelled', 'archived') LIMIT 1", (pid, milestone_id, clean_title))
        if dup_rows:
            return self.update_item(str(dup_rows[0][0]), **({"depends_on": deps} if deps else {}), **({"description": description.strip()} if description.strip() else {}))
        iid = f"wi_{uuid.uuid4().hex[:10]}"
        with self.store._lock:
            self.store._ensure_project_record(pid)
            self.store.conn.execute(
                "INSERT INTO work_items (item_id, project_id, milestone_id, timebox_id, workflow_id, item_kind, title, description, category, status, owner, stage_kind, priority, depends_on_json, verification_json, dag_name, acceptance_criteria, estimate_minutes, planned_date, due_date, checklist_json, tags_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (iid, pid, milestone_id, timebox_id, workflow_id, item_kind, clean_title, description.strip(), category, status, own, sk, max(1, min(5, int(priority))), json.dumps(deps), json.dumps(verification or {}), dag_name.strip(), acceptance_criteria.strip(), max(5, int(estimate_minutes)), planned_date.strip(), due_date.strip(), json.dumps(checklist or []), json.dumps(tags or [])),
            )
            self.store.conn.commit()
        self.recompute_readiness(pid)
        return self.get_item(iid)

    def get_item(self, item_ref: Union[str, int]) -> Dict[str, Any]:
        ref_str = str(item_ref).strip()
        where = "id = ?" if ref_str.isdigit() else "item_id = ?"
        arg: Any = int(ref_str) if ref_str.isdigit() else ref_str
        rows = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM work_items WHERE {where} LIMIT 1", (arg,))
        if not rows:
            raise ValueError(f"Work item `{item_ref}` not found.")
        return _hydrate_item(rows[0])

    def list_items(self, project_id: Optional[str] = None, owner: Optional[str] = None, status: Optional[str] = None, timebox_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses: List[Tuple[str, Any]] = []
        if project_id:
            clauses.append(("project_id = ?", self.resolve_project_id(project_id)))
        if owner:
            clauses.append(("owner = ?", owner.strip().lower()))
        if status:
            clauses.append(("status = ?", status.strip().lower()))
        if timebox_id:
            clauses.append(("timebox_id = ?", timebox_id.strip()))
        where = (" WHERE " + " AND ".join(c for c, _ in clauses)) if clauses else ""
        rows = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM work_items{where} ORDER BY priority ASC, position ASC, id ASC", tuple(v for _, v in clauses))
        return [_hydrate_item(r) for r in rows]

    def update_item(self, item_ref: Union[str, int], **updates: Any) -> Dict[str, Any]:
        cur = self.get_item(item_ref)
        iid, pid = cur["item_id"], cur["project_id"]
        new_status = str(updates.get("status") or updates.get("column_key") or cur["status"]).strip().lower()
        comp_at = cur.get("completed_at")
        if new_status in DONE_STATUSES and not comp_at:
            comp_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        deps = self._resolve_dep_ids(pid, updates["depends_on"], exclude_id=iid) if "depends_on" in updates and updates["depends_on"] is not None else cur["depends_on"]
        chk = updates["checklist"] if "checklist" in updates and updates["checklist"] is not None else cur["checklist"]
        tags = updates["tags"] if "tags" in updates and updates["tags"] is not None else cur["tags"]
        ver = updates["verification"] if "verification" in updates and updates["verification"] is not None else cur["verification"]
        deliv = updates["deliverable"] if "deliverable" in updates and updates["deliverable"] is not None else cur.get("deliverable", {})
        if isinstance(deliv, str):
            deliv = json.loads(deliv) if deliv.strip().startswith("{") else {}
        act_jid = str(updates["active_job_id"]) if "active_job_id" in updates and updates["active_job_id"] is not None else str(cur.get("active_job_id") or "")
        wt_p = str(updates["worktree_path"]) if "worktree_path" in updates and updates["worktree_path"] is not None else str(cur.get("worktree_path") or "")
        br_n = str(updates["branch_name"]) if "branch_name" in updates and updates["branch_name"] is not None else str(cur.get("branch_name") or "")
        own = str(updates.get("owner") or cur["owner"]).strip().lower()
        sk = str(updates.get("stage_kind") or cur["stage_kind"]).strip()
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE work_items SET title = ?, description = ?, status = ?, owner = ?, stage_kind = ?, priority = ?, milestone_id = ?, timebox_id = ?, "
                "depends_on_json = ?, verification_json = ?, dag_name = ?, checkpoint_id = ?, verification_result = ?, active_job_id = ?, worktree_path = ?, branch_name = ?, deliverable_json = ?, estimate_minutes = ?, planned_date = ?, due_date = ?, "
                "checklist_json = ?, tags_json = ?, completed_at = ?, updated_at = CURRENT_TIMESTAMP WHERE item_id = ?",
                (
                    str(updates.get("title") or cur["title"]).strip(),
                    str(updates["description"] if "description" in updates and updates["description"] is not None else cur["description"]),
                    new_status, own if own in VALID_ITEM_OWNERS else cur["owner"], sk if sk in VALID_STAGE_KINDS else cur["stage_kind"],
                    max(1, min(5, int(updates.get("priority", cur["priority"])))),
                    str(updates.get("milestone_id", updates.get("epic_id", cur["milestone_id"])) or ""),
                    str(updates.get("timebox_id", updates.get("sprint_id", cur["timebox_id"])) or ""),
                    json.dumps(list(deps)), json.dumps(dict(ver)), str(updates.get("dag_name", cur["dag_name"]) or ""),
                    str(updates.get("checkpoint_id", cur["checkpoint_id"]) or ""),
                    str(updates.get("verification_result", cur["verification_result"]) or ""),
                    act_jid, wt_p, br_n, json.dumps(dict(deliv)),
                    int(updates.get("estimate_minutes", cur["estimate_minutes"])),
                    str(updates.get("planned_date", cur["planned_date"]) or ""), str(updates.get("due_date", cur["due_date"]) or ""),
                    json.dumps(list(chk)), json.dumps(list(tags)), comp_at, iid,
                ),
            )
            self.store.conn.commit()
        self.recompute_readiness(pid)
        return self.get_item(iid)

    def delete_item(self, item_ref: Union[str, int]) -> bool:
        cur = self.get_item(item_ref)
        with self.store._lock:
            res = self.store.conn.execute("DELETE FROM work_items WHERE item_id = ?", (cur["item_id"],))
            self.store.conn.commit()
        self.recompute_readiness(cur["project_id"])
        return res.rowcount > 0

    def recompute_readiness(self, project_id: str) -> List[str]:
        """Promote `blocked -> ready` when all `depends_on` items are done; demote `ready -> blocked` when dependencies remain."""
        pid = self.resolve_project_id(project_id)
        items = self.list_items(project_id=pid)
        by_key: Dict[str, Dict[str, Any]] = {}
        for it in items:
            by_key[it["item_id"]] = it
            by_key[str(it["id"])] = it
        unblocked_ids: List[str] = []
        with self.store._lock:
            for it in items:
                deps = [str(d).strip() for d in it["depends_on"] if str(d).strip()]
                if not deps:
                    continue
                all_done = all((by_key.get(d) or {}).get("status") in DONE_STATUSES for d in deps)
                st = str(it["status"]).lower()
                if not all_done and st in (*READY_STATUSES, "backlog"):
                    self.store.conn.execute("UPDATE work_items SET status = 'blocked', updated_at = CURRENT_TIMESTAMP WHERE item_id = ?", (it["item_id"],))
                elif all_done and st == "blocked":
                    self.store.conn.execute("UPDATE work_items SET status = 'ready', updated_at = CURRENT_TIMESTAMP WHERE item_id = ?", (it["item_id"],))
                    unblocked_ids.append(it["item_id"])
            self.store.conn.commit()
        return unblocked_ids

    def next_ready_companion_items(self, project_id: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        """Return unblocked `companion`/`collaborative` items ordered by DIKW stage, priority, and creation."""
        pid = self.resolve_project_id(project_id) if project_id else None
        if pid:
            self.recompute_readiness(pid)
        items = self.list_items(project_id=pid)
        ready = [
            it for it in items
            if it["owner"] in ("companion", "collaborative")
            and str(it["status"]).lower() in READY_STATUSES
            and it["item_kind"] != "backlog"
        ]
        ready.sort(key=lambda x: (STAGE_ORDER.get(x["stage_kind"], 3), int(x["priority"]), int(x["id"])))
        return ready[: max(1, int(limit))]

    def eligible_idle_goal_projects(
        self,
        idle_seconds: float,
        now_utc: Optional[datetime] = None,
        now_local: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Return active `on_idle` Goals/Projects whose `TriggerPolicy` window/cooldown is open and have ready Companion items."""
        bg_on = self.store.is_background_autonomy_enabled()
        eligible: List[Dict[str, Any]] = []
        for proj in self.list_projects(status="active"):
            tp = TriggerPolicy.from_dict(proj.get("trigger_policy"))
            if tp.mode != "on_idle":
                continue
            last_slice_at = str(proj.get("metadata", {}).get("last_slice_at") or "")
            ok, _reason = evaluate_trigger_eligibility(
                tp, idle_seconds=idle_seconds, last_run_at=last_slice_at,
                background_autonomy_enabled=bg_on, now_utc=now_utc, now_local=now_local, store=self.store,
            )
            if ok and self.next_ready_companion_items(project_id=proj["project_id"], limit=1):
                eligible.append(proj)
        return eligible

    def create_timebox(self, project_id: Optional[str], title: str, goal: str = "", start_date: str = "", end_date: str = "", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pid = self.resolve_project_id(project_id)
        s_dt = start_date.strip() or date.today().isoformat()
        e_dt = end_date.strip() or (date.fromisoformat(s_dt) + timedelta(days=6)).isoformat()
        tid = f"tb_{uuid.uuid4().hex[:8]}"
        with self.store._lock:
            self.store._ensure_project_record(pid)
            self.store.conn.execute(
                "INSERT INTO project_timeboxes (timebox_id, project_id, title, goal, start_date, end_date, status, metadata_json) VALUES (?, ?, ?, ?, ?, ?, 'planning', ?)",
                (tid, pid, title.strip(), goal.strip(), s_dt, e_dt, json.dumps(metadata or {})),
            )
            self.store.conn.commit()
        return self.get_timebox(tid)

    def get_timebox(self, timebox_id: str) -> Dict[str, Any]:
        cols = ["timebox_id", "project_id", "title", "goal", "start_date", "end_date", "status", "metadata_json", "retro_notes", "created_at"]
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM project_timeboxes WHERE timebox_id = ?", (timebox_id.strip(),))
        if not rows:
            raise ValueError(f"Timebox `{timebox_id}` not found.")
        out = dict(rows[0])
        out["sprint_id"] = out["timebox_id"]
        try:
            out["metadata"] = json.loads(out.get("metadata_json") or "{}")
        except Exception:
            out["metadata"] = {}
        return out

    def update_timebox_status(self, timebox_id: str, status: str, retro_notes: str = "") -> Dict[str, Any]:
        tb = self.get_timebox(timebox_id)
        with self.store._lock:
            if status == "active":
                self.store.conn.execute("UPDATE project_timeboxes SET status = 'completed' WHERE project_id = ? AND status = 'active'", (tb["project_id"],))
            self.store.conn.execute("UPDATE project_timeboxes SET status = ?, retro_notes = COALESCE(NULLIF(?, ''), retro_notes) WHERE timebox_id = ?", (status, retro_notes.strip(), tb["timebox_id"]))
            self.store.conn.commit()
        return self.get_timebox(tb["timebox_id"])

    def get_active_timebox(self, project_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        cols = ["timebox_id", "project_id", "title", "goal", "start_date", "end_date", "status", "metadata_json", "retro_notes", "created_at"]
        where = "WHERE status = 'active'" + (" AND project_id = ?" if project_id else "")
        params: Tuple[Any, ...] = (self.resolve_project_id(project_id),) if project_id else ()
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM project_timeboxes {where} ORDER BY start_date DESC LIMIT 1", params)
        if not rows:
            return None
        out = dict(rows[0])
        out["sprint_id"] = out["timebox_id"]
        return out

    def list_timeboxes(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        cols = ["timebox_id", "project_id", "title", "goal", "start_date", "end_date", "status", "metadata_json", "retro_notes", "created_at"]
        where = "WHERE project_id = ?" if project_id else ""
        params: Tuple[Any, ...] = (self.resolve_project_id(project_id),) if project_id else ()
        rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM project_timeboxes {where} ORDER BY created_at DESC", params)
        for r in rows:
            r["sprint_id"] = r["timebox_id"]
        return rows

    def list_dated_items(self, start_date: str, end_date: str, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        items = self.list_items(project_id=project_id)
        return [
            it for it in items
            if (start_date <= str(it.get("planned_date") or "") <= end_date)
            or (start_date <= str(it.get("due_date") or "") <= end_date)
        ]

    def get_board_projection(self, project_id: Optional[str] = None, timebox_id: Optional[str] = None, owner_filter: Optional[str] = None) -> Dict[str, Any]:
        """Project the Universal Project & Goal Graph into a clean Kanban board view."""
        proj = self.get_project(project_id)
        pid = proj["project_id"]
        self.recompute_readiness(pid)
        items = self.list_items(project_id=pid, owner=owner_filter, timebox_id=timebox_id)
        columns = [{"col_key": k, "title": lbl, "cards": [it for it in items if it["column_key"] == k]} for k, lbl in BOARD_COLUMNS]
        milestones = self.list_milestones(pid)
        active_tb = self.get_active_timebox(pid)
        return {
            "board": proj,
            "project": proj,
            "columns": columns,
            "cards": items,
            "epics": milestones,
            "milestones": milestones,
            "active_sprint": active_tb,
            "sprints": self.list_timeboxes(pid),
            "counts": {
                "total": len(items),
                "companion_ready": sum(1 for it in items if it["owner"] in ("companion", "collaborative") and it["column_key"] == "ready"),
                "human_pending": sum(1 for it in items if it["owner"] == "human" and it["column_key"] != "done"),
                "blocked": sum(1 for it in items if it["column_key"] == "blocked"),
                "done": sum(1 for it in items if it["column_key"] == "done"),
            },
        }

    def get_roadmap_projection(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        proj = self.get_project(project_id)
        pid = proj["project_id"]
        items = self.list_items(project_id=pid)
        milestones = []
        for ms in self.list_milestones(pid):
            ms_items = [it for it in items if it["milestone_id"] == ms["milestone_id"]]
            done_n = sum(1 for it in ms_items if it["column_key"] == "done")
            pct = round((done_n / len(ms_items)) * 100.0, 1) if ms_items else 0.0
            milestones.append({**ms, "item_count": len(ms_items), "done_count": done_n, "progress_pct": pct, "items": ms_items})
        return {"project": proj, "milestones": milestones, "epics": milestones, "unassigned_count": sum(1 for it in items if not it["milestone_id"])}


def get_project_graph(store: Optional[MemoryStore] = None) -> ProjectGraphManager:
    """Return a `ProjectGraphManager` bound to the active `MemoryStore`."""
    return ProjectGraphManager(store=store)


def sync_workflow_to_project_graph(store: MemoryStore, workflow_id: int, goal: str, state: str) -> None:
    """Keep active state-machine workflow runs reflected on the Universal Project & Goal Graph."""
    mgr = get_project_graph(store)
    pid = mgr.resolve_project_id()
    col_state = "done" if state == "COMPLETE" else ("verify" if state == "VERIFY" else ("in_progress" if state == "IMPLEMENT" else "ready"))
    existing = [it for it in mgr.list_items(project_id=pid) if it.get("workflow_id") == workflow_id and "workflow-tracker" in it.get("tags", [])]
    if existing:
        mgr.update_item(existing[0]["item_id"], status=col_state, title=f"[WF #{workflow_id}] {goal[:120]}")
    else:
        mgr.create_item(pid, f"[WF #{workflow_id}] {goal[:120]}", owner="companion", status=col_state, workflow_id=workflow_id, tags=["workflow-tracker"], item_kind="workflow_tracker")


def format_project_graph_hud(store: Optional[MemoryStore] = None) -> str:
    """Render concise Universal Project & Goal Graph summary block for the System Prompt."""
    mgr = get_project_graph(store)
    proj = mgr.get_project()
    view = mgr.get_board_projection(proj["project_id"])
    cnt = view["counts"]
    tb = view["active_sprint"]
    tb_txt = f"`{tb['title']}` ({tb['start_date']}..{tb['end_date']})" if tb else "(none)"
    tp = proj["trigger_policy"]
    win = f" [{tp['allowed_window']}]" if tp.get("allowed_window") else ""
    open_rows = [
        f"    - `{it['item_id']}` [{it['owner']} | {it['column_key']}]: {it['title']}"
        for it in view.get("cards", [])
        if it.get("column_key") != "done"
    ][:5]
    dp_seg = f" | delivery=`{proj.get('delivery_policy', 'local_only')}`"
    ws_seg = f" | repo=`{proj['workspace_path']}`" if proj.get("workspace_path") else ""
    cred_seg = f" | git_auth=`vault://{proj['git_credential_ref']}`" if proj.get("git_credential_ref") else ""
    goal_line = f"\n  Goal : {proj['goal_summary']}" if proj.get("goal_summary") else ""
    items_block = ("\n" + "\n".join(open_rows)) if open_rows else ""
    return (
        f"[PROJECT & GOAL GRAPH (`{proj['title']}` | project_id=`{proj['project_id']}` | slug=`{proj['slug']}` | trigger=`{tp['mode']}{win}`{dp_seg}{ws_seg}{cred_seg} | timebox={tb_txt})]{goal_line}\n"
        f"  Items: total={cnt['total']} | companion_ready={cnt['companion_ready']} | human_pending={cnt['human_pending']} | blocked={cnt['blocked']} | done={cnt['done']}{items_block}"
    )


def _precheck_active_goal_ready(store: MemoryStore) -> Tuple[bool, str]:
    mgr = get_project_graph(store)
    for proj in mgr.list_projects(status="active"):
        if mgr.next_ready_companion_items(proj["project_id"], limit=1):
            return True, f"ready_items_in:{proj['slug']}"
    return False, "no_ready_goal_items"


register_trigger_precheck("active_goal_has_ready_items", _precheck_active_goal_ready)
