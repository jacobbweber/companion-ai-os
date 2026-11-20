"""Parallel Multi-Agent Subagents & Isolated Git Worktree Card Orchestrator (`companion/worktree_runner.py`)."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
import base64, contextlib, contextvars, importlib.util, json, os, re, shutil, time  # noqa: E401
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.attachments import ingest_attachment
from companion.background_jobs import BackgroundJobManager, get_job_manager
from companion.config import is_in_agent_tool_turn, settings, workspace_context
from companion.llm import chat
from companion.memory_store import MemoryStore, get_memory_store, memory_store_context
from companion.project_graph import get_project_graph
from companion.sdlc_tools import _current_branch, _run_git_cmd, check_git_stage_safety, discover_repo_sdlc_context, preview_manager, tool_git_manage
from companion.streaming import CancelScope, cancel_scope_context, check_cancelled, get_active_cancel_scope, get_active_stream_sink
from companion.tool_schemas import int_arg

__all__ = [
    "WORKTREE_CODE_TOOLS", "collect_worktree_deliverable_receipt", "discard_worktree", "dispatch_parallel_ready_items",
    "dispatch_work_item_agent", "execute_agile_worktree_subaction", "format_parallel_subagents_tool", "merge_worktree_into_base",
    "parse_unified_git_diff", "probe_repo_workspace", "push_worktree_branch_and_pr", "request_worktree_changes", "run_parallel_subagents",
]

WORKTREE_CODE_TOOLS: Tuple[str, ...] = ("read_file", "write_file", "edit_file", "list_dir", "search", "run_python", "run_shell", "git_manage", "dev_preview", "workflow_manage", "memory_manage", "wiki_manage")
_PERSONAL_WORKER_TOOLS: Tuple[str, ...] = ("read_file", "write_file", "edit_file", "list_dir", "search", "web_fetch", "web_search", "wiki_manage", "memory_manage", "agile_manage", "calendar_manage")
_DIFF_CAP_CHARS = 40_000
_ATT_ID_RE = re.compile(r"\batt_[A-Za-z0-9_]+\b")
_IMG_PATH_RE = re.compile(r"([A-Za-z0-9_./:\\-]+?\.(?:png|jpg|jpeg|webp))", re.I)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify_part(raw: str, fallback: str = "slice", max_len: int = 36) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")[:max_len].strip("-") or fallback


def _scrub_remote_url(raw_url: str) -> str:
    clean = (raw_url or "").strip()
    return re.sub(r"://[^/@\s]+@", "://", clean) if "://" in clean else clean


def _detect_stack_and_rules(root: Path) -> Tuple[str, List[str]]:
    stacks: List[str] = []
    if any((root / m).exists() for m in ("pyproject.toml", "requirements.txt", "harness.py", "tests")):
        stacks.append("Python")
    if (root / "package.json").is_file() or (root / "frontend" / "package.json").is_file():
        stacks.append("Frontend" if (root / "frontend" / "package.json").is_file() else "Node")
    if (root / "Cargo.toml").is_file():
        stacks.append("Rust")
    if (root / "go.mod").is_file():
        stacks.append("Go")
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")):
        stacks.append(".NET")
    rules = [f".agents/rules/{f.name}" for f in sorted((root / ".agents" / "rules").glob("*.md"))[:8]] if (root / ".agents" / "rules").is_dir() else []
    rules.extend(doc for doc in ("AGENTS.md", "CLAUDE.md") if (root / doc).is_file())
    return (" + ".join(stacks) if stacks else "General"), rules


def probe_repo_workspace(path_str: str) -> Dict[str, Any]:
    """Inspect a candidate repository path without mutating anything (powers UI `Check Repo`)."""
    raw = (path_str or "").strip()
    if not raw:
        return {"ok": False, "exists": False, "is_git_repo": False, "error": "Repository path is required."}
    target = Path(raw).expanduser().resolve()
    base_res = {"ok": True, "repo_root": str(target), "branch": "", "clean": True, "changed_count": 0, "remote_url": ""}
    if not target.exists() or not target.is_dir():
        return {**base_res, "exists": False, "is_git_repo": False, "stack_summary": "", "rules_files": [], "summary": f"Directory `{target}` does not exist."}
    stack_sum, rules_files = _detect_stack_and_rules(target)
    if not (target / ".git").exists():
        return {**base_res, "exists": True, "is_git_repo": False, "stack_summary": stack_sum, "rules_files": rules_files, "summary": f"Folder exists (not a Git repository) · {stack_sum}"}
    branch = _current_branch(target)
    if branch == "HEAD":
        rc_sym, sym_out = _run_git_cmd(target, ["symbolic-ref", "--short", "HEAD"])
        branch = sym_out.strip() if rc_sym == 0 and sym_out.strip() else "main"
    rc_st, st_out = _run_git_cmd(target, ["status", "--porcelain=v1"])
    changed = [ln for ln in (st_out.splitlines() if rc_st == 0 else []) if ln.strip()]
    rc_rem, rem_out = _run_git_cmd(target, ["remote", "get-url", "origin"])
    remote_url = _scrub_remote_url(rem_out.splitlines()[0]) if rc_rem == 0 and rem_out.strip() else ""
    rules_note = f" · {len(rules_files)} rules file{'s' if len(rules_files) != 1 else ''}" if rules_files else ""
    clean_lbl = "Clean" if not changed else f"{len(changed)} changed"
    return {
        "ok": True, "exists": True, "is_git_repo": True, "repo_root": str(target),
        "branch": branch, "clean": not changed, "changed_count": len(changed), "remote_url": remote_url,
        "stack_summary": stack_sum, "rules_files": rules_files,
        "summary": f"Git repo on {branch} · {clean_lbl} · {stack_sum}{rules_note}",
    }


def _detect_base_branch(repo_root: Path) -> str:
    cur = _current_branch(repo_root)
    if cur not in ("HEAD", "") and not cur.startswith(("feat/", "fix/", "wt/")):
        return cur
    for cand in ("main", "master"):
        if _run_git_cmd(repo_root, ["show-ref", "--verify", f"refs/heads/{cand}"])[0] == 0:
            return cand
    return cur if cur not in ("HEAD", "") else "main"


def parse_unified_git_diff(unified_diff: str, base_label: str = "main", head_label: str = "HEAD") -> Dict[str, Any]:
    """Convert unified git diff text into `WikiDiffPayload` format for `<DiffView>` rendering."""
    files: List[Dict[str, Any]] = []
    cur_file: Optional[Dict[str, Any]] = None
    old_ln, new_ln, total_add, total_del = 1, 1, 0, 0
    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")
    for raw_ln in (unified_diff or "").splitlines():
        if raw_ln.startswith("diff --git "):
            if cur_file is not None:
                files.append(cur_file)
            parts = raw_ln.split()
            rel = parts[-1].removeprefix("b/") if len(parts) >= 4 else "file"
            cur_file = {"title": rel, "old_label": f"{base_label}:{rel}", "new_label": f"{head_label}:{rel}", "additions": 0, "deletions": 0, "identical": False, "unified_diff": "", "rows": []}
        elif cur_file is None or raw_ln.startswith(("index ", "--- ", "+++ ", "new file ", "deleted file ")):
            continue
        elif hm := hunk_re.match(raw_ln):
            old_ln, new_ln = int(hm.group(1)), int(hm.group(2))
            cur_file["rows"].append({"kind": "hunk", "old_ln": None, "new_ln": None, "old_text": raw_ln, "new_text": raw_ln})
        elif raw_ln.startswith("+"):
            cur_file["additions"] += 1
            total_add += 1
            cur_file["rows"].append({"kind": "add", "old_ln": None, "new_ln": new_ln, "old_text": "", "new_text": raw_ln[1:]})
            new_ln += 1
        elif raw_ln.startswith("-"):
            cur_file["deletions"] += 1
            total_del += 1
            cur_file["rows"].append({"kind": "del", "old_ln": old_ln, "new_ln": None, "old_text": raw_ln[1:], "new_text": ""})
            old_ln += 1
        elif raw_ln.startswith(" "):
            cur_file["rows"].append({"kind": "context", "old_ln": old_ln, "new_ln": new_ln, "old_text": raw_ln[1:], "new_text": raw_ln[1:]})
            old_ln, new_ln = old_ln + 1, new_ln + 1
    if cur_file is not None:
        files.append(cur_file)
    return {"ok": True, "mode": "worktree_diff", "files": files, "total_additions": total_add, "total_deletions": total_del, "unified_diff": unified_diff, "summary": f"{len(files)} file(s) changed (+{total_add} / -{total_del})"}


def _extract_test_and_screenshots(raw_text: str, worktree_path: Optional[Path], store: MemoryStore) -> Tuple[str, List[str]]:
    test_lines = [ln.strip() for ln in (raw_text or "").splitlines() if any(k in ln for k in ("Ran ", "PASSED", "FAILED", "OK", "Test Files", "Tests ", "exit=0", "HEALTHY (PASS)"))]
    seen_ids = list(dict.fromkeys(_ATT_ID_RE.findall(raw_text or "")))
    for raw_img in _IMG_PATH_RE.findall(raw_text or "")[:4]:
        cand = Path(raw_img) if Path(raw_img).is_absolute() or worktree_path is None else (worktree_path / raw_img).resolve()
        if cand.is_file() and cand.stat().st_size <= 10_000_000:
            b64 = base64.b64encode(cand.read_bytes()).decode("ascii")
            aid = str((ingest_attachment(cand.name, b64, store=store).get("attachment") or {}).get("id") or "")
            if aid and aid not in seen_ids:
                seen_ids.append(aid)
    return ("\n".join(test_lines[-8:])[:800] if test_lines else ""), seen_ids[:6]


def _autocommit_clean_worktree_edits(worktree_path: Path, project_id: str, item_id: str, title: str, store: MemoryStore) -> None:
    rc_st, st_out = _run_git_cmd(worktree_path, ["status", "--porcelain=v1"])
    cand_rels = [
        entry for ln in (st_out.splitlines() if rc_st == 0 else [])
        if (m := re.match(r"^[MADRCU?! ]{1,2}\s+(.+)$", ln))
        and (entry := m.group(1).split(" -> ")[-1].strip().strip('"'))
        and check_git_stage_safety(worktree_path, [entry]) is None
    ]
    if cand_rels:
        with workspace_context(worktree_path, project_id), memory_store_context(store):
            tool_git_manage({"action": "commit", "repo_path": str(worktree_path), "paths": cand_rels, "message": f"feat({item_id}): {title[:64]}"})


def collect_worktree_deliverable_receipt(
    base_repo: Path, worktree_path: Path, branch_name: str,
    item: Dict[str, Any], job_row: Dict[str, Any], store: MemoryStore,
) -> Dict[str, Any]:
    """Auto-commit clean edits on `branch_name` and build the `deliverable_json` diff & screenshot receipt."""
    for p in preview_manager.list_instances():
        with contextlib.suppress(Exception):
            if Path(str(p.get("cwd") or "")).resolve() == worktree_path.resolve():
                preview_manager.stop(str(p["preview_id"]))
    base_branch = _detect_base_branch(base_repo)
    _autocommit_clean_worktree_edits(worktree_path, str(item.get("project_id") or ""), item["item_id"], item["title"], store)
    _, sha_out = _run_git_cmd(worktree_path, ["rev-parse", "--short", "HEAD"])
    _, names_out = _run_git_cmd(worktree_path, ["diff", "--name-only", f"{base_branch}...HEAD"])
    _, stat_out = _run_git_cmd(worktree_path, ["diff", "--stat", f"{base_branch}...HEAD"])
    _, diff_out = _run_git_cmd(worktree_path, ["diff", f"{base_branch}...HEAD"])
    rc_rem, rem_out = _run_git_cmd(base_repo, ["remote", "get-url", "origin"])
    remote_url = _scrub_remote_url(rem_out.splitlines()[0]) if rc_rem == 0 and rem_out.strip() else ""
    changed_files = [ln.strip() for ln in names_out.splitlines() if ln.strip()]
    capped_diff = (diff_out or "")[:_DIFF_CAP_CHARS]
    worker_txt = str(job_row.get("full_output") or job_row.get("envelope_summary") or job_row.get("partial_output") or "").strip()
    test_sum, shot_ids = _extract_test_and_screenshots(f"{worker_txt}\n{job_row.get('partial_output') or ''}", worktree_path, store)
    prev_deliv = dict(item.get("deliverable") or {})
    return {
        **prev_deliv, "kind": "code_worktree", "worker_status": str(job_row.get("status") or "done"),
        "branch_name": branch_name, "base_branch": base_branch, "worktree_path": str(worktree_path),
        "commit_sha": sha_out.strip(), "changed_files": changed_files, "diff_stat": stat_out.strip(),
        "unified_diff": capped_diff, "diff_payload": parse_unified_git_diff(capped_diff, base_label=base_branch, head_label=branch_name),
        "summary": worker_txt[:3000] or str(job_row.get("error") or ""), "test_summary": test_sum,
        "screenshot_ids": shot_ids or list(prev_deliv.get("screenshot_ids") or []),
        "job_id": str(job_row.get("job_id") or ""), "has_remote": bool(remote_url), "remote_url": remote_url,
        "error": str(job_row.get("error") or ""), "updated_at": _now_iso(),
    }


def _build_card_worker_goal(
    proj: Dict[str, Any], item: Dict[str, Any], is_code_repo: bool,
    worktree_path: Optional[Path], branch_name: str, feedback: str,
) -> str:
    fb_blk = f"\n\n[HUMAN REVIEW REVISION FEEDBACK]\n{feedback.strip()}\nAddress every point above on this existing branch and re-verify." if feedback.strip() else ""
    crit_blk = f"\nAcceptance Criteria: {item['acceptance_criteria']}" if item.get("acceptance_criteria") else ""
    desc_blk = f"\nDetails:\n{item['description']}" if item.get("description") else ""
    if not is_code_repo or worktree_path is None:
        return f"Execute Board Card `{item['item_id']}` in Project `{proj['title']}`:\nTitle: {item['title']}{desc_blk}{crit_blk}{fb_blk}\n\nProduce a clear, actionable Markdown deliverable summary."
    repo_ctx = discover_repo_sdlc_context(worktree_path, proj)
    return (
        f"[ISOLATED GIT WORKTREE CARD EXECUTION]\nProject: `{proj['title']}` (`delivery_policy={proj.get('delivery_policy', 'local_only')}`)\n"
        f"Card `{item['item_id']}`: {item['title']}{desc_blk}{crit_blk}\nIsolated Worktree Root: `{worktree_path}` (branch `{branch_name}`)\n{repo_ctx}{fb_blk}\n\n"
        "Execution Rules:\n1. Work strictly inside your isolated worktree root (`read_file`, `edit_file`, `write_file`, `run_python`, `run_shell`).\n"
        "2. Never switch to `main`/`master` or push to remotes unless explicitly instructed.\n"
        "3. Run relevant unit tests (and `dev_preview` + `browser-dogfood` if changing Web UI) before finishing.\n"
        "4. Stage and commit your verified changes cleanly onto this feature branch via `git_manage(action='commit', ...)` and summarize test results."
    )


def _prepare_item_worktree(proj: Dict[str, Any], item: Dict[str, Any], base_repo: Path) -> Tuple[Optional[Path], str, str]:
    if _run_git_cmd(base_repo, ["rev-parse", "--verify", "HEAD"])[0] != 0:
        return None, "", f"Repository `{base_repo}` has no initial commit yet — commit at least once on `main` first."
    slug = _slugify_part(item["title"], fallback="card", max_len=28)
    short_id = re.sub(r"[^a-z0-9]+", "", item["item_id"].lower())[-6:] or "wi"
    existing_wt, existing_br = str(item.get("worktree_path") or "").strip(), str(item.get("branch_name") or "").strip()
    branch_name = existing_br or f"feat/{slug}-{short_id}"
    wt_dir = (
        Path(existing_wt).resolve()
        if existing_wt and Path(existing_wt).resolve() != settings.root.resolve()
        else (base_repo.parent / ".worktrees" / _slugify_part(proj["project_id"], "proj", 32) / f"{short_id}-{slug}").resolve()
    )
    if (wt_dir / ".git").exists():
        return wt_dir, _current_branch(wt_dir) or branch_name, ""
    with workspace_context(base_repo, proj["project_id"]):
        wt_out = tool_git_manage({"action": "worktree_add", "repo_path": str(base_repo), "branch": branch_name, "worktree_path": str(wt_dir)})
    return (None, "", wt_out) if wt_out.startswith(("error:", "SAFETY GUARD REJECTED")) else (wt_dir, branch_name, "")


def dispatch_work_item_agent(
    item_id: str, project_id: str = "", feedback: str = "", run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Start (or iterate on) a Board `WorkItem` inside an isolated git worktree or Personal OS worker."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(project_id or item["project_id"])
    jm = job_mgr or BackgroundJobManager(store=mem, chat_fn=chat_fn or chat, tool_fn=tool_fn)
    if item.get("active_job_id"):
        cur_job = jm.get_job(str(item["active_job_id"]))
        if cur_job and cur_job.get("status") in ("queued", "running"):
            return {"ok": False, "error": f"Card `{item['item_id']}` already has an active worker (`{cur_job['job_id']}`)."}

    ws_str = str(proj.get("workspace_path") or "").strip()
    base_repo = Path(ws_str).resolve() if ws_str else None
    is_code_repo = bool(base_repo and base_repo.is_dir() and (base_repo / ".git").exists())
    wt_dir, branch_name = (None, "")
    if is_code_repo and base_repo is not None:
        wt_dir, branch_name, wt_err = _prepare_item_worktree(proj, item, base_repo)
        if wt_err or wt_dir is None:
            return {"ok": False, "error": wt_err or "Failed to prepare git worktree."}

    job_id = f"bgj_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
    prev_deliv = dict(item.get("deliverable") or {})
    if feedback.strip():
        prev_deliv["revision_history"] = [*list(prev_deliv.get("revision_history") or []), {"feedback": feedback.strip(), "requested_at": _now_iso()}]
    pg.update_item(item["item_id"], status="in_progress", active_job_id=job_id, worktree_path=str(wt_dir) if wt_dir else "", branch_name=branch_name, deliverable=prev_deliv)
    goal_txt = _build_card_worker_goal(proj, item, is_code_repo, wt_dir, branch_name, feedback)

    def _on_done(done_job: Dict[str, Any]) -> None:
        st_j = str(done_job.get("status") or "done")
        fresh_item = pg.get_item(item["item_id"])
        if st_j == "cancelled":
            pg.update_item(item["item_id"], status="verify" if (fresh_item.get("deliverable") or {}).get("unified_diff") else "ready", active_job_id="")
            return
        try:
            if is_code_repo and base_repo is not None and wt_dir is not None:
                receipt = collect_worktree_deliverable_receipt(base_repo, wt_dir, branch_name, fresh_item, done_job, mem)
            else:
                out_s = str(done_job.get("full_output") or done_job.get("envelope_summary") or done_job.get("error") or "").strip()
                receipt = {**dict(fresh_item.get("deliverable") or {}), "kind": "personal_deliverable", "worker_status": st_j, "summary": out_s[:4000], "job_id": job_id, "error": str(done_job.get("error") or ""), "updated_at": _now_iso()}
        except Exception as exc:
            receipt = {**dict(fresh_item.get("deliverable") or {}), "worker_status": "failed", "summary": str(done_job.get("full_output") or ""), "error": f"{type(exc).__name__}: {exc}", "updated_at": _now_iso()}
        next_st = "verify" if st_j == "done" or receipt.get("changed_files") else "ready"
        pg.update_item(item["item_id"], status=next_st, active_job_id="", verification_result=str(receipt.get("summary") or "")[:500], deliverable=receipt)

    exec_root = wt_dir if (is_code_repo and wt_dir is not None) else settings.scratch_dir
    exec_root.mkdir(parents=True, exist_ok=True)
    with workspace_context(exec_root, proj["project_id"]), memory_store_context(mem):
        j_res = jm.dispatch_job(
            goal=goal_txt, title=f"[{branch_name or 'Card'}] {item['title'][:72]}", kind="worker", starter="worktree",
            session_id="board_studio", tier="coding" if is_code_repo else "fast",
            allowed_tools=list(WORKTREE_CODE_TOOLS if is_code_repo else _PERSONAL_WORKER_TOOLS),
            max_iters=20 if is_code_repo else 10, timeout_sec=900,
            run_in_background=run_in_background, on_complete=_on_done, job_id_override=job_id,
        )
    updated_item = pg.get_item(item["item_id"])
    return {
        "ok": bool(j_res.get("ok", True)), "job_id": job_id, "branch_name": branch_name,
        "worktree_path": str(wt_dir) if wt_dir else "", "item": updated_item, "card": updated_item,
        "summary": f"Started `{item['title']}`" + (f" in worktree `{branch_name}` (`{job_id}`)." if branch_name else f" (`{job_id}`)."),
    }


def dispatch_parallel_ready_items(
    project_id: str = "", max_workers: int = 0, run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Fan out up to `project.max_parallel_workers` unblocked `Ready` Companion cards in isolated worktrees."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    proj = pg.get_project(project_id)
    jm = job_mgr or BackgroundJobManager(store=mem, chat_fn=chat_fn or chat, tool_fn=tool_fn)
    cap = max(1, min(4, int(max_workers or proj.get("max_parallel_workers") or 2)))
    active_running = sum(
        1 for it in pg.list_items(project_id=proj["project_id"])
        if it["status"] == "in_progress" and it.get("active_job_id")
        and (jm.get_job(str(it["active_job_id"])) or {}).get("status") in ("queued", "running")
    )
    slots = max(0, cap - active_running)
    if slots == 0:
        return {"ok": False, "dispatched": [], "error": f"Already running {active_running}/{cap} parallel agents on `{proj['title']}`."}
    ready = pg.next_ready_companion_items(proj["project_id"], limit=slots)
    if not ready:
        return {"ok": False, "dispatched": [], "error": f"No unblocked Ready Companion items in `{proj['title']}`."}

    if run_in_background:
        results = [dispatch_work_item_agent(it["item_id"], project_id=proj["project_id"], run_in_background=True, store=mem, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=jm) for it in ready]
    else:
        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
            futs = [pool.submit(lambda t=it: contextvars.Context().run(lambda: dispatch_work_item_agent(t["item_id"], project_id=proj["project_id"], run_in_background=False, store=mem, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=jm))) for it in ready]
            results = [f.result() for f in futs]
    ok_runs = [r for r in results if r.get("ok")]
    return {
        "ok": len(ok_runs) > 0, "count": len(ok_runs), "dispatched": results,
        "items": [r["item"] for r in ok_runs if "item" in r],
        "summary": f"Dispatched {len(ok_runs)} parallel agent(s) across isolated worktrees on `{proj['title']}`.",
    }


def merge_worktree_into_base(item_id: str, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Merge a verified worktree branch cleanly into local base (`main`/`master`), clean up worktree, and unblock dependents."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    ws_str = str(proj.get("workspace_path") or "").strip()
    branch = str(item.get("branch_name") or (item.get("deliverable") or {}).get("branch_name") or "").strip()
    if not ws_str or not branch:
        pre_blk = {it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["column_key"] == "blocked"}
        done_item = pg.update_item(item["item_id"], status="done", active_job_id="")
        unblocked = [it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["item_id"] in pre_blk and it["column_key"] == "ready"]
        return {"ok": True, "item": done_item, "card": done_item, "unblocked_ids": unblocked, "summary": f"Completed `{item['title']}`."}

    base_repo = Path(ws_str).resolve()
    if not (base_repo / ".git").exists() or base_repo == settings.root.resolve():
        return {"ok": False, "error": f"Base repository `{base_repo}` is invalid or immutable."}
    wt_str = str(item.get("worktree_path") or "").strip()
    if wt_str and (Path(wt_str) / ".git").exists():
        _autocommit_clean_worktree_edits(Path(wt_str).resolve(), proj["project_id"], item["item_id"], item["title"], mem)

    rc_st, st_out = _run_git_cmd(base_repo, ["status", "--porcelain=v1"])
    if rc_st != 0 or any(ln.strip() for ln in st_out.splitlines()):
        return {"ok": False, "error": f"Base repository `{base_repo}` has uncommitted changes; commit or stash them before merging `{branch}`."}
    base_branch = str((item.get("deliverable") or {}).get("base_branch") or _detect_base_branch(base_repo))
    if _current_branch(base_repo) != base_branch:
        rc_co, out_co = _run_git_cmd(base_repo, ["checkout", base_branch])
        if rc_co != 0:
            return {"ok": False, "error": f"Could not checkout `{base_branch}` in `{base_repo}`: {out_co}"}

    merge_msg = f"Merge {branch}: {item['title'][:64]}"
    rc_m, out_m = _run_git_cmd(base_repo, ["merge", "--no-ff", "-m", merge_msg, branch])
    if rc_m != 0 and any(k in out_m for k in ("user.email", "user.name", "Please tell me who you are")):
        _run_git_cmd(base_repo, ["config", "user.name", "Companion AI OS"])
        _run_git_cmd(base_repo, ["config", "user.email", "companion@local"])
        rc_m, out_m = _run_git_cmd(base_repo, ["merge", "--no-ff", "-m", merge_msg, branch])
    if rc_m != 0:
        _run_git_cmd(base_repo, ["merge", "--abort"])
        return {"ok": False, "error": f"Merge conflict merging `{branch}` into `{base_branch}`: {out_m[:320]}"}

    _, merged_sha = _run_git_cmd(base_repo, ["rev-parse", "--short", "HEAD"])
    if wt_str and Path(wt_str).resolve() not in (base_repo, settings.root.resolve()):
        _run_git_cmd(base_repo, ["worktree", "remove", "--force", str(Path(wt_str).resolve())])
        _run_git_cmd(base_repo, ["worktree", "prune"])
        shutil.rmtree(Path(wt_str).resolve(), ignore_errors=True)
    _run_git_cmd(base_repo, ["branch", "-d", branch])

    deliv = {**dict(item.get("deliverable") or {}), "merged": True, "merged_commit": merged_sha.strip(), "merged_into": base_branch, "merged_at": _now_iso()}
    pre_blocked = {it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["column_key"] == "blocked"}
    done_item = pg.update_item(item["item_id"], status="done", active_job_id="", worktree_path="", deliverable=deliv)
    now_ready = [it["item_id"] for it in pg.list_items(project_id=proj["project_id"]) if it["item_id"] in pre_blocked and it["column_key"] == "ready"]
    unblk_note = f" (unblocked {len(now_ready)} dependent card(s))" if now_ready else ""
    return {
        "ok": True, "merged_commit": merged_sha.strip(), "base_branch": base_branch,
        "unblocked_ids": now_ready, "item": done_item, "card": done_item,
        "summary": f"Merged `{branch}` into `{base_branch}` (`{merged_sha.strip()}`) and completed `{item['title']}`{unblk_note}.",
    }


def _parse_github_owner_repo(remote_url: str) -> str:
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", (remote_url or "").strip())
    return m.group(1) if m else ""


def push_worktree_branch_and_pr(
    item_id: str, store: Optional[MemoryStore] = None, api_base: str = "https://api.github.com",
) -> Dict[str, Any]:
    """Push verified worktree branch using Blind Vault auth and open/update a GitHub PR if applicable."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    if str(proj.get("delivery_policy") or "local_only") == "local_only":
        return {"ok": False, "error": "Delivery Policy is `Local Only` — switch to `Ask Before Push` or `Remote Branch + PR` in Project Settings first."}
    ws_str, branch = str(proj.get("workspace_path") or "").strip(), str(item.get("branch_name") or "").strip()
    if not ws_str or not branch:
        return {"ok": False, "error": "Card has no bound repository or feature branch to push."}
    base_repo = Path(ws_str).resolve()
    wt_str = str(item.get("worktree_path") or "").strip()
    git_dir = Path(wt_str).resolve() if wt_str and (Path(wt_str) / ".git").exists() else base_repo
    cred_ref = str(proj.get("git_credential_ref") or "").strip()
    with workspace_context(base_repo, proj["project_id"]), memory_store_context(mem):
        push_out = tool_git_manage({"action": "push", "repo_path": str(git_dir), "branch": branch, "credential_ref": cred_ref, "confirmed": not is_in_agent_tool_turn()})
    if push_out.startswith(("error:", "SAFETY GUARD REJECTED", "AWAITING_USER_APPROVAL")):
        return {"ok": False, "error": push_out}
    deliv = {**dict(item.get("deliverable") or {}), "pushed": True}
    rc_rem, rem_out = _run_git_cmd(base_repo, ["remote", "get-url", "origin"])
    gh_repo = _parse_github_owner_repo(rem_out.splitlines()[0] if rc_rem == 0 and rem_out.strip() else "")
    pr_note = ""
    gh_script = settings.skills_dir / "github-workflow" / "scripts" / "github_pr_ci.py"
    if gh_repo and cred_ref and gh_script.is_file():
        spec = importlib.util.spec_from_file_location("companion_gh_pr_ci", gh_script)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            base_br = str(deliv.get("base_branch") or _detect_base_branch(base_repo))
            pr_res = mod.upsert_pull_request(gh_repo, branch, base_br, item["title"], str(deliv.get("summary") or item["description"]), cred_ref, api_base=api_base)
            if pr_res.get("ok"):
                deliv["pr_url"], deliv["pr_number"] = str(pr_res.get("html_url") or ""), pr_res.get("number")
                pr_note = f" and {pr_res.get('action', 'opened')} PR #{pr_res.get('number')} ({deliv['pr_url']})"
    upd_item = pg.update_item(item["item_id"], deliverable=deliv)
    return {"ok": True, "item": upd_item, "card": upd_item, "pr_url": deliv.get("pr_url", ""), "summary": f"Pushed `{branch}` to `origin`{pr_note}."}


def request_worktree_changes(
    item_id: str, feedback: str, run_in_background: bool = True,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    tool_fn: Optional[Callable[[str, Dict[str, Any]], str]] = None, job_mgr: Optional[BackgroundJobManager] = None,
) -> Dict[str, Any]:
    """Keep the existing worktree & feature branch intact and dispatch a revision pass with human feedback."""
    clean_fb = (feedback or "").strip()
    if not clean_fb:
        return {"ok": False, "error": "Please enter revision notes before sending the card back to Companion."}
    return dispatch_work_item_agent(item_id=item_id, feedback=clean_fb, run_in_background=run_in_background, store=store, chat_fn=chat_fn, tool_fn=tool_fn, job_mgr=job_mgr)


def discard_worktree(item_id: str, store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Cancel any running worker, remove the isolated git worktree, and reset the card to `ready`."""
    mem = store or get_memory_store()
    pg = get_project_graph(mem)
    item = pg.get_item(item_id)
    proj = pg.get_project(item["project_id"])
    if item.get("active_job_id"):
        get_job_manager(store=mem).cancel_job(str(item["active_job_id"]))
    ws_str, wt_str = str(proj.get("workspace_path") or "").strip(), str(item.get("worktree_path") or "").strip()
    if ws_str and wt_str:
        base_repo, wt_res = Path(ws_str).resolve(), Path(wt_str).resolve()
        if (base_repo / ".git").exists() and wt_res not in (base_repo, settings.root.resolve()):
            _run_git_cmd(base_repo, ["worktree", "remove", "--force", str(wt_res)])
            _run_git_cmd(base_repo, ["worktree", "prune"])
            shutil.rmtree(wt_res, ignore_errors=True)
    upd = pg.update_item(item["item_id"], status="ready", active_job_id="", worktree_path="", branch_name="")
    return {"ok": True, "item": upd, "card": upd, "summary": f"Discarded worktree and reset `{item['title']}` to Ready."}


def _run_one_subagent_slot(
    idx: int, total: int, spec: Dict[str, Any], max_iters: int, mem: MemoryStore,
    chat_fn: Optional[Callable[..., Dict[str, Any]]], parent_scope: Optional[CancelScope], parent_sink: Any, ws: Any,
) -> Dict[str, Any]:
    from companion.agent import run_subagent

    check_cancelled()
    goal_s = str(spec["goal"]).strip()
    skill_s = str(spec.get("skill") or spec.get("skill_name") or "").strip() or None
    tier_s = str(spec.get("tier") or "fast").strip() or "fast"
    wt_branch = str(spec.get("worktree_branch") or "").strip()
    child_scope = parent_scope.spawn_child() if parent_scope is not None else CancelScope()
    wt_created, t0 = "", time.perf_counter()
    if parent_sink is not None:
        with contextlib.suppress(Exception):
            parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "running", "goal": goal_s[:90], "tier": tier_s})
    try:
        with cancel_scope_context(child_scope), memory_store_context(mem):
            target_root = ws.project_root
            if wt_branch and (ws.project_root / ".git").exists() and ws.project_root.resolve() != settings.root.resolve():
                wt_dir = (ws.project_root.parent / ".worktrees" / ws.project_id / _slugify_part(wt_branch, f"sub-{idx}")).resolve()
                wt_out = tool_git_manage({"action": "worktree_add", "repo_path": str(ws.project_root), "branch": wt_branch, "worktree_path": str(wt_dir)})
                if not wt_out.startswith(("error:", "SAFETY GUARD")):
                    target_root, wt_created = wt_dir, str(wt_dir)
            with workspace_context(target_root, ws.project_id):
                out = run_subagent(goal=goal_s, skill_name=skill_s, max_iters=int(spec.get("max_iters") or max_iters or 8), chat_fn=chat_fn or chat, store=mem, verbose=False, tier=tier_s)
        if parent_sink is not None:
            with contextlib.suppress(Exception):
                parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "done", "goal": goal_s[:90], "tier": tier_s})
        return {"index": idx, "ok": True, "goal": goal_s, "skill": skill_s or "worker", "tier": tier_s, "worktree_branch": wt_branch, "worktree_path": wt_created, "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1), "output": out}
    except InterruptedError:
        if parent_sink is not None:
            with contextlib.suppress(Exception):
                parent_sink({"type": "subagent_progress", "index": idx, "total": total, "status": "cancelled", "goal": goal_s[:90], "tier": tier_s})
        raise
    except Exception as exc:
        return {"index": idx, "ok": False, "goal": goal_s, "skill": skill_s or "worker", "tier": tier_s, "error": f"{type(exc).__name__}: {exc}", "output": ""}
    finally:
        if parent_scope is not None:
            parent_scope.unregister_child(child_scope)


def run_parallel_subagents(
    tasks: List[Dict[str, Any]], max_concurrency: int = 3, max_iters: int = 8,
    store: Optional[MemoryStore] = None, chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fan out `1..4` concurrent subagents with parent-linked child `CancelScope`s (`<50ms` abort) and live SSE progress."""
    valid = [dict(t) for t in (tasks or []) if isinstance(t, dict) and str(t.get("goal") or "").strip()][:4]
    if not valid:
        return {"ok": False, "results": [], "error": "Provide 1-4 `{goal, skill?, tier?, worktree_branch?}` task dicts for `parallel_subagents`."}
    mem = store or get_memory_store()
    eff_conc = max(1, min(4, int(max_concurrency or 3), len(valid)))
    parent_scope, parent_sink, ws = get_active_cancel_scope(), get_active_stream_sink(), settings.get_workspace()
    results: List[Dict[str, Any]] = []
    pool = ThreadPoolExecutor(max_workers=eff_conc)
    futs = []
    try:
        for i, sp in enumerate(valid, 1):
            futs.append(pool.submit(contextvars.copy_context().run, _run_one_subagent_slot, i, len(valid), sp, max_iters, mem, chat_fn, parent_scope, parent_sink, ws))
        for f in futs:
            while True:
                check_cancelled()
                try:
                    results.append(f.result(timeout=0.02))
                    break
                except FuturesTimeout:
                    continue
    except InterruptedError:
        for f in futs:
            f.cancel()
        if parent_scope is not None:
            parent_scope.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)

    blocks = [
        f"### Subagent {r['index']}/{len(results)} (`{r['skill']}` | tier=`{r['tier']}`"
        + (f" | wt=`{r['worktree_branch']}`" if r.get("worktree_branch") else "")
        + f")\nGoal: {r['goal']}\n" + (r["output"] if r.get("ok") else f"ERROR: {r.get('error')}")
        for r in results
    ]
    return {
        "ok": all(r.get("ok") for r in results), "count": len(results), "results": results,
        "summary": f"=== Parallel Subagents Synthesis ({len(results)} workers, max_concurrency={eff_conc}) ===\n\n" + "\n\n".join(blocks),
    }


def format_parallel_subagents_tool(args: Dict[str, Any]) -> str:
    raw_tasks = args.get("tasks")
    if isinstance(raw_tasks, str) and raw_tasks.strip().startswith("["):
        with contextlib.suppress(Exception):
            raw_tasks = json.loads(raw_tasks)
    if not isinstance(raw_tasks, list):
        return "error: `tasks` array of `{goal, skill?, tier?, worktree_branch?}` objects is required for `parallel_subagents`"
    res = run_parallel_subagents(tasks=raw_tasks, max_concurrency=int_arg(args, "max_concurrency", 3), max_iters=int_arg(args, "max_iters", 8))
    return str(res["summary"]) if res.get("results") else f"error: {res.get('error')}"


def execute_agile_worktree_subaction(
    sub: str, pid: str, p: Dict[str, Any], _actor: str, mem: MemoryStore,
    job_mgr_fn: Callable[..., BackgroundJobManager] = get_job_manager,
) -> Optional[Dict[str, Any]]:
    """Handle Studio repo probe, parallel worktree dispatch, and 1-click deliverable subactions for `agile`."""
    if sub == "probe_repo":
        return probe_repo_workspace(str(p.get("workspace_path") or p.get("path") or ""))
    ref = str(p.get("item_id") or p.get("card_id") or "").strip()
    bg = p.get("run_in_background")
    run_bg = True if bg is None else (bg if isinstance(bg, bool) else str(bg).strip().lower() in ("1", "true", "yes", "on"))
    if sub in ("start_item", "start_card", "dispatch_item"):
        return dispatch_work_item_agent(ref, project_id=pid, feedback=str(p.get("feedback") or ""), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub in ("run_parallel_ready", "run_ready"):
        return dispatch_parallel_ready_items(project_id=pid, max_workers=int(p.get("max_workers") or 0), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub in ("merge_item", "merge_local"):
        return merge_worktree_into_base(ref, store=mem)
    if sub in ("push_item_pr", "push_and_pr"):
        return push_worktree_branch_and_pr(ref, store=mem)
    if sub == "request_changes":
        return request_worktree_changes(ref, feedback=str(p.get("feedback") or p.get("note") or ""), run_in_background=run_bg, store=mem, job_mgr=job_mgr_fn(store=mem))
    if sub == "discard_worktree":
        return discard_worktree(ref, store=mem)
    return None
