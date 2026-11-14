"""
Wiki 3.1 Maintenance, Visual Diffs, Ontology Consolidation, Freshness & Asset Engine
(`companion/wiki_maintenance.py` — A-Frame Domain Layer).

Provides 100% Python 3.12 stdlib capabilities extending the Co-Owned Knowledge Warehouse:
1. `compute_wiki_diff`       — Unified + Side-by-Side (`SequenceMatcher`) diffs for Archive Lineage & Pending Approvals
2. `consolidate_ontology`    — Governed `alias -> canonical` synonym healing, near-duplicate detection & Never-Delete rewrites
3. `verify_note_freshness`   — 1-click hardware/runbook verification stamp (`last_verified_at`, `review_after_days`)
4. `list_stale_notes`        — Cadence-aware staleness audit across `02_knowledge/` (`90d` specs/SOPs, `180d` ADRs, `365d` concepts)
5. `ingest_wiki_asset`       — Magic-byte-verified image/diagram storage in `03_resources/assets/` (Obsidian + Web compatible)
6. `stage_chat_turn_to_inbox`— 1-click Forever Chat single-turn distillation into `01_inbox/`
"""

import base64
import difflib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.attachments import MAX_ATTACHMENT_BYTES, inspect_image_bytes
from companion.disciplines import walk_pruned_files
from companion.telemetry import redact_secrets
from companion.wiki_engine import WikiEngine, compute_note_staleness
from companion.wiki_templates import (
    BUILTIN_TEMPLATE_MAP, as_slug_list, canonicalize_ontology, default_review_days,
    hydrate_note_meta, load_tag_authority, parse_frontmatter, recommend_templates,
    render_frontmatter, scrub_conversational_chatter, sha16_bytes, slugify_wiki,
)

__all__ = [
    "compute_wiki_diff", "consolidate_ontology", "execute_wiki_maintenance_subaction",
    "ingest_wiki_asset", "list_stale_notes", "read_wiki_asset_raw",
    "stage_chat_turn_to_inbox", "verify_note_freshness",
]

_SAFE_ASSET_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIME_EXT_MAP = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _build_diff_file(title: str, old_label: str, new_label: str, old_text: str, new_text: str) -> Dict[str, Any]:
    old_lines = (old_text or "").replace("\r\n", "\n").splitlines()
    new_lines = (new_text or "").replace("\r\n", "\n").splitlines()
    uni = "\n".join(difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label, lineterm="", n=3))
    rows: List[Dict[str, Any]] = []
    add_cnt = del_cnt = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for group in matcher.get_grouped_opcodes(3):
        if not group:
            continue
        i_start, i_end = group[0][1] + 1, max(group[-1][2] - group[0][1], 0)
        j_start, j_end = group[0][3] + 1, max(group[-1][4] - group[0][3], 0)
        rows.append({"kind": "hunk", "old_ln": None, "new_ln": None, "old_text": f"@@ -{i_start},{i_end} +{j_start},{j_end} @@", "new_text": ""})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append({"kind": "context", "old_ln": i1 + k + 1, "new_ln": j1 + k + 1, "old_text": old_lines[i1 + k], "new_text": new_lines[j1 + k]})
            elif tag == "delete":
                del_cnt += i2 - i1
                for k in range(i1, i2):
                    rows.append({"kind": "del", "old_ln": k + 1, "new_ln": None, "old_text": old_lines[k], "new_text": ""})
            elif tag == "insert":
                add_cnt += j2 - j1
                for k in range(j1, j2):
                    rows.append({"kind": "add", "old_ln": None, "new_ln": k + 1, "old_text": "", "new_text": new_lines[k]})
            elif tag == "replace":
                del_cnt += i2 - i1
                add_cnt += j2 - j1
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    rows.append({"kind": "change", "old_ln": i1 + k + 1, "new_ln": j1 + k + 1, "old_text": old_lines[i1 + k], "new_text": new_lines[j1 + k]})
                for k in range(i1 + paired, i2):
                    rows.append({"kind": "del", "old_ln": k + 1, "new_ln": None, "old_text": old_lines[k], "new_text": ""})
                for k in range(j1 + paired, j2):
                    rows.append({"kind": "add", "old_ln": None, "new_ln": k + 1, "old_text": "", "new_text": new_lines[k]})
    return {
        "title": title, "old_label": old_label, "new_label": new_label,
        "additions": add_cnt, "deletions": del_cnt, "identical": add_cnt == 0 and del_cnt == 0,
        "unified_diff": uni, "rows": rows[:400],
    }


def _diff_pending_approval(engine: WikiEngine, approval_id: str) -> Dict[str, Any]:
    req = get_approval_queue(engine.store).get_request((approval_id or "").strip())
    if not req:
        return {"ok": False, "error": f"Approval request `{approval_id}` not found."}
    act = str(req.get("action_name") or "")
    payload = json.loads(req.get("payload_json") or "{}")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files: List[Dict[str, Any]] = []

    if act == "edit_user_note" and payload.get("path"):
        rel = str(payload["path"]).replace("\\", "/").lstrip("/")
        full = engine.root / rel
        old_raw = full.read_text(encoding="utf-8", errors="replace") if full.is_file() else ""
        old_meta, _ = parse_frontmatter(old_raw)
        in_meta, in_body = parse_frontmatter(str(payload.get("content") or ""))
        parts = rel.split("/")
        dom = parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else parts[0]
        top = parts[2] if parts[0] == "02_knowledge" and len(parts) == 4 else ""
        tpl = slugify_wiki(str(payload.get("template") or in_meta.get("template") or old_meta.get("template") or "standard-note"))
        merged = hydrate_note_meta(
            {**old_meta, **in_meta}, str(payload.get("title") or in_meta.get("title") or old_meta.get("title") or full.stem),
            in_body, str(old_meta.get("author") or "user"), "active", tpl, dom, top,
            as_slug_list(payload.get("tags") or in_meta.get("tags") or old_meta.get("tags")),
            canonicalize_ontology(engine.root, payload.get("ontology") or in_meta.get("ontology") or old_meta.get("ontology")),
            now_iso, summary=str(payload.get("summary") or ""),
        )
        files.append(_build_diff_file(rel, f"{rel} (current)", f"{rel} (proposed edit)", old_raw, render_frontmatter(merged, in_body)))
    elif act == "commit_curation" and payload.get("curation_id"):
        rows = engine.store._fetch_rows("SELECT inbox_paths_json, validated_plan_json FROM wiki_curation_sessions WHERE curation_id = ?", (str(payload["curation_id"]),))
        if not rows or not rows[0][1]:
            return {"ok": False, "error": f"Validated curation plan for `{payload.get('curation_id')}` not found."}
        in_paths, plan = json.loads(rows[0][0] or "[]"), json.loads(rows[0][1])
        in_raw = (engine.root / in_paths[0]).read_text(encoding="utf-8", errors="replace") if in_paths and (engine.root / in_paths[0]).is_file() else ""
        for out in plan.get("outputs", []):
            dst_rel = str(out["path"])
            dst_full = engine.root / dst_rel
            old_raw = dst_full.read_text(encoding="utf-8", errors="replace") if dst_full.is_file() else in_raw
            old_lbl = f"{dst_rel} (existing)" if dst_full.is_file() else f"{in_paths[0] if in_paths else '01_inbox'} (staged)"
            files.append(_build_diff_file(dst_rel, old_lbl, f"{dst_rel} (curated)", old_raw, render_frontmatter(out["meta"], str(out["body"]))))
    elif act == "graduate_note" and payload.get("from") and payload.get("to"):
        src_rel, dst_rel = str(payload["from"]), str(payload["to"])
        src_raw = (engine.root / src_rel).read_text(encoding="utf-8", errors="replace") if (engine.root / src_rel).is_file() else ""
        meta, body = parse_frontmatter(src_raw)
        to_parts = [x for x in dst_rel.split("/") if x]
        dom = to_parts[1] if len(to_parts) >= 3 else "general"
        top = to_parts[2] if len(to_parts) >= 4 else ""
        tpl = slugify_wiki(str(payload.get("template") or meta.get("template") or "standard-note"))
        out_m = hydrate_note_meta({**meta, "supersedes": [src_rel]}, str(meta.get("title") or Path(dst_rel).stem), body, str(meta.get("author") or "companion"), "active", tpl, dom, top, as_slug_list(meta.get("tags")) or [dom], canonicalize_ontology(engine.root, payload.get("ontology") or meta.get("ontology")), now_iso, summary=str(payload.get("summary") or ""))
        old_raw = (engine.root / dst_rel).read_text(encoding="utf-8", errors="replace") if (engine.root / dst_rel).is_file() else src_raw
        files.append(_build_diff_file(dst_rel, src_rel, dst_rel, old_raw, render_frontmatter(out_m, body)))
    else:
        return {"ok": False, "error": f"Approval `{approval_id}` (`{act}`) does not contain a diffable Wiki payload."}

    tot_add, tot_del = sum(f["additions"] for f in files), sum(f["deletions"] for f in files)
    uni_all = "\n\n".join(f["unified_diff"] for f in files if f["unified_diff"])
    return {
        "ok": True, "mode": "approval_preview", "approval_id": approval_id, "action_name": act,
        "files": files, "total_additions": tot_add, "total_deletions": tot_del, "unified_diff": uni_all,
        "summary": f"Pending Approval `{approval_id}` (`{act}`): +{tot_add} / -{tot_del} lines across {len(files)} file(s).",
    }


def compute_wiki_diff(
    engine: WikiEngine, path: str = "", compare_to: str = "", approval_id: str = "", proposed_content: str = "",
) -> Dict[str, Any]:
    """Compute structured Unified + Side-by-Side Markdown diffs for lineage revisions or pending approvals."""
    engine.ensure_warehouse()
    if (approval_id or "").strip():
        return _diff_pending_approval(engine, approval_id)
    cur_note = engine.read_note(path)
    if not cur_note.get("ok"):
        return {"ok": False, "error": cur_note.get("error") or f"Wiki note `{path}` not found."}
    cur_rel, cur_raw, cur_meta = str(cur_note["path"]), str(cur_note["raw"]), dict(cur_note["frontmatter"])

    if proposed_content:
        df = _build_diff_file(cur_rel, f"{cur_rel} (on disk)", f"{cur_rel} (unsaved draft)", cur_raw, proposed_content)
        return {
            "ok": True, "mode": "draft_preview", "path": cur_rel, "files": [df],
            "total_additions": df["additions"], "total_deletions": df["deletions"], "unified_diff": df["unified_diff"],
            "summary": f"Draft diff for `{cur_rel}`: +{df['additions']} / -{df['deletions']} lines.",
        }

    other_rel = (compare_to or "").strip().replace("\\", "/").lstrip("/")
    direction = "prior_to_current"
    if not other_rel:
        sups = cur_meta.get("supersedes") if isinstance(cur_meta.get("supersedes"), list) else []
        if sups:
            other_rel = str(sups[-1])
        elif cur_meta.get("superseded_by"):
            other_rel = str(cur_meta["superseded_by"]).split(",")[0].strip()
            direction = "archive_to_successor"
        else:
            stem_tag = f"_{cur_rel.replace('/', '__').removesuffix('.md')}.md"
            arch_dir = engine.root / "04_archive"
            arch_hits = sorted(p for p in walk_pruned_files(arch_dir) if p.name.endswith(stem_tag)) if arch_dir.is_dir() else []
            if arch_hits:
                other_rel = str(arch_hits[-1].relative_to(engine.root)).replace("\\", "/")
    if not other_rel:
        return {"ok": False, "error": f"Note `{cur_rel}` has no archived prior revision or `supersedes` lineage yet."}
    other_note = engine.read_note(other_rel)
    if not other_note.get("ok"):
        return {"ok": False, "error": f"Comparison revision `{other_rel}` not found."}
    old_lbl, new_lbl = (cur_rel, str(other_note["path"])) if direction == "archive_to_successor" else (str(other_note["path"]), cur_rel)
    old_txt, new_txt = (cur_raw, str(other_note["raw"])) if direction == "archive_to_successor" else (str(other_note["raw"]), cur_raw)
    df = _build_diff_file(new_lbl, old_lbl, new_lbl, old_txt, new_txt)
    return {
        "ok": True, "mode": "lineage_diff", "path": cur_rel, "compare_to": str(other_note["path"]), "files": [df],
        "total_additions": df["additions"], "total_deletions": df["deletions"], "unified_diff": df["unified_diff"],
        "summary": f"Lineage diff `{old_lbl}` -> `{new_lbl}`: +{df['additions']} / -{df['deletions']} lines.",
    }


def _parse_alias_rules(raw_aliases: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(raw_aliases, dict):
        for k, v in raw_aliases.items():
            ks, vs = slugify_wiki(str(k), ""), slugify_wiki(str(v), "")
            if ks and vs and ks != vs:
                out[ks] = vs
    elif isinstance(raw_aliases, str) and raw_aliases.strip():
        for chunk in re.split(r"[,\n;]+", raw_aliases):
            sep = "->" if "->" in chunk else ("=" if "=" in chunk else "")
            if sep:
                lhs, _, rhs = chunk.partition(sep)
                ks, vs = slugify_wiki(lhs, ""), slugify_wiki(rhs, "")
                if ks and vs and ks != vs:
                    out[ks] = vs
    return out


def consolidate_ontology(engine: WikiEngine, aliases: Any = None, dry_run: bool = False, actor: str = "user") -> Dict[str, Any]:
    """Canonicalize ontology synonyms across active notes with Never-Delete `04_archive/` snapshots & near-duplicate scan."""
    engine.ensure_warehouse()
    is_user = actor.strip().lower() in ("user", "cli", "web")
    custom_map = _parse_alias_rules(aliases)
    auth = load_tag_authority(engine.root)
    merged_aliases = {**auth["aliases"], **custom_map}
    added_rules = {k: v for k, v in custom_map.items() if auth["aliases"].get(k) != v}
    if added_rules and not dry_run:
        raw_auth = str(auth["raw"]).rstrip() + "\n"
        rule_lines = "\n".join(f"- `{k}` -> `{v}`" for k, v in sorted(added_rules.items()))
        if "## Ontology Synonym Aliases" not in raw_auth:
            raw_auth += f"\n## Ontology Synonym Aliases (alias -> canonical)\n{rule_lines}\n"
        else:
            raw_auth += f"{rule_lines}\n"
        (engine.root / "03_resources" / "tag-authority.md").write_text(raw_auth, encoding="utf-8", newline="\n")

    canon_set = set(auth["ontology"]) | set(merged_aliases.values())
    updated_notes: List[Dict[str, Any]] = []
    skipped_user_notes: List[str] = []
    seen_unknown: set[str] = set()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in engine.list_notes(limit=500):
        rel = str(row["rel_path"])
        full = engine.root / rel
        if not full.is_file() or row["folder"] not in ("01_inbox", "02_knowledge"):
            continue
        meta, body = parse_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
        old_ont = as_slug_list(meta.get("ontology"))
        new_ont: List[str] = []
        for s in old_ont:
            mapped = merged_aliases.get(s, s[:-1] if s.endswith("s") and s[:-1] in canon_set else s)
            if mapped and mapped not in new_ont:
                new_ont.append(mapped)
            if mapped not in canon_set:
                seen_unknown.add(mapped)
        if new_ont != old_ont:
            if not is_user and str(meta.get("author") or "").lower() in ("user", "human", "partner"):
                skipped_user_notes.append(rel)
                continue
            arch_p = ""
            if not dry_run:
                arch_p = engine._archive_prior_version(full, "ontology_consolidate", superseded_by=rel, subdir="revisions") or ""
                meta["ontology"] = new_ont
                meta["updated_at"] = now_iso
                full.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
            updated_notes.append({"path": rel, "before": old_ont, "after": new_ont, "archived_prior": arch_p})

    if updated_notes and not dry_run:
        engine.rebuild_index()

    suggestions: List[Dict[str, Any]] = []
    for unk in sorted(seen_unknown):
        best_c, best_score = "", 0.0
        unk_toks = set(unk.split("-"))
        for can in canon_set:
            ratio = difflib.SequenceMatcher(None, unk, can).ratio()
            tok_overlap = len(unk_toks & set(can.split("-"))) / max(1, len(unk_toks | set(can.split("-"))))
            score = round(max(ratio, 0.55 * ratio + 0.45 * tok_overlap), 2)
            if score > best_score:
                best_c, best_score = can, score
        if best_c and best_score >= 0.68:
            suggestions.append({"alias": unk, "suggested_canonical": best_c, "similarity": best_score})

    mode_lbl = "DRY-RUN" if dry_run else "APPLIED"
    return {
        "ok": True, "dry_run": dry_run, "added_aliases": added_rules, "updated_notes": updated_notes,
        "skipped_user_notes": skipped_user_notes, "near_duplicate_suggestions": suggestions,
        "summary": f"Ontology Consolidation ({mode_lbl}): {len(updated_notes)} note(s) canonicalized, {len(added_rules)} alias rule(s) added, {len(suggestions)} near-duplicate suggestion(s).",
    }


def verify_note_freshness(
    engine: WikiEngine, path: str, review_after_days: int = 0, actor: str = "user", verified_at_override: str = "",
) -> Dict[str, Any]:
    """Stamp `last_verified_at` (and optional `review_after_days`) on a Wiki note to reset its staleness timer."""
    rd = engine.read_note(path)
    if not rd.get("ok"):
        return {"ok": False, "error": rd.get("error") or f"Wiki note `{path}` not found."}
    rel = str(rd["path"])
    full = engine.root / rel
    meta, body = parse_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
    is_user = actor.strip().lower() in ("user", "cli", "web")
    if not is_user and str(meta.get("author") or "").lower() in ("user", "human", "partner"):
        return {"ok": False, "error": f"WIKI AUTHOR GUARD REJECTED: `{rel}` is user-authored; only the user can stamp verification."}
    stamp = (verified_at_override or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")).strip()
    eff_days = int(review_after_days) if int(review_after_days or 0) > 0 else int(meta.get("review_after_days") or default_review_days(str(meta.get("template") or "")))
    meta["last_verified_at"] = stamp
    meta["review_after_days"] = eff_days
    full.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
    engine._index_single_file(full)
    age_d, _, is_stale = compute_note_staleness(rel.split("/")[0], str(meta.get("template") or ""), stamp, stamp, eff_days)
    return {
        "ok": True, "path": rel, "last_verified_at": stamp, "review_after_days": eff_days,
        "days_since_verified": age_d, "stale": is_stale, "content_hash": sha16_bytes(full.read_bytes()),
        "summary": f"Marked `{rel}` verified at `{stamp}` (next review in {eff_days} days).",
    }


def list_stale_notes(engine: WikiEngine, as_of_iso: str = "", include_due_soon_days: int = 14) -> Dict[str, Any]:
    """List curated `02_knowledge/` notes past their template verification cadence (`90d`/`180d`/`365d`)."""
    ref_dt = datetime.now(timezone.utc)
    if (as_of_iso or "").strip():
        ref_dt = datetime.fromisoformat(as_of_iso.strip().replace("Z", "+00:00"))
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
    curated = engine.list_notes(folder="02_knowledge", limit=400)
    stale_items: List[Dict[str, Any]] = []
    due_soon: List[Dict[str, Any]] = []
    for n in curated:
        age_d, rev_d, is_stale = compute_note_staleness("02_knowledge", str(n.get("template") or ""), str(n.get("last_verified_at") or ""), str(n.get("updated_at") or ""), n.get("review_after_days"), now_dt=ref_dt)
        entry = {**n, "days_since_verified": age_d, "review_after_days": rev_d, "overdue_days": age_d - rev_d, "stale": is_stale}
        if is_stale:
            stale_items.append(entry)
        elif rev_d - age_d <= max(0, int(include_due_soon_days)):
            due_soon.append(entry)
    stale_items.sort(key=lambda x: x["overdue_days"], reverse=True)
    lines = [f"=== Wiki Freshness & Staleness Audit ({len(stale_items)} stale / {len(curated)} curated) ==="]
    lines.extend([f"  * [STALE +{s['overdue_days']}d] `{s['rel_path']}` (tpl={s['template']}, verified={s.get('last_verified_at') or 'n/a'}, cadence={s['review_after_days']}d)" for s in stale_items] or ["  (All curated `02_knowledge/` notes are within their verification cadence.)"])
    return {"ok": True, "stale_notes": stale_items, "due_soon": due_soon, "total_curated": len(curated), "summary": "\n".join(lines)}


def ingest_wiki_asset(engine: WikiEngine, filename: str, data_base64: str, alt_text: str = "") -> Dict[str, Any]:
    """Validate and save an inline diagram/screenshot inside `03_resources/assets/` for Obsidian + Web rendering."""
    b64_clean = (data_base64 or "").strip()
    if "," in b64_clean[:256] and b64_clean.lower().startswith("data:"):
        b64_clean = b64_clean.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64_clean, validate=False)
    except Exception as exc:
        return {"ok": False, "error": f"Invalid base64 image payload: {exc}"}
    if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
        return {"ok": False, "error": "Wiki image payload must be non-empty and <= 20 MB."}
    img_info = inspect_image_bytes(raw)
    if img_info is None:
        return {"ok": False, "error": "Unsupported image bytes — only valid PNG, JPEG, WEBP, or GIF diagrams/images are allowed."}
    mime_type, width, height = img_info
    ext = _MIME_EXT_MAP.get(mime_type, ".png")
    stem = slugify_wiki(Path((filename or "diagram").strip()).stem, fallback="diagram")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    asset_name = f"{stamp}_{os.urandom(2).hex()}_{stem}{ext}"
    assets_dir = engine.ensure_warehouse() / "03_resources" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / asset_name).write_bytes(raw)
    rel_asset = f"03_resources/assets/{asset_name}"
    clean_alt = re.sub(r"[\[\]\r\n]+", " ", (alt_text or stem.replace("-", " ").title())).strip() or "Diagram"
    return {
        "ok": True, "asset_name": asset_name, "asset_rel_path": rel_asset, "raw_url": f"/api/wiki/assets/{asset_name}",
        "mime_type": mime_type, "width": width, "height": height, "size_bytes": len(raw),
        "markdown": f"![{clean_alt}]({rel_asset})",
        "summary": f"Saved Wiki image `{rel_asset}` ({width}x{height} {mime_type}).",
    }


def read_wiki_asset_raw(engine: WikiEngine, asset_name: str) -> Optional[Tuple[str, bytes, str]]:
    """Safely serve raw image bytes from `03_resources/assets/<asset_name>` with traversal & magic-byte checks."""
    clean = _SAFE_ASSET_RE.sub("", (asset_name or "").strip())
    if not clean or clean != asset_name.strip() or ".." in clean:
        return None
    assets_dir = (engine.ensure_warehouse() / "03_resources" / "assets").resolve()
    cand = (assets_dir / clean).resolve()
    if not cand.is_file() or not cand.is_relative_to(assets_dir):
        return None
    raw = cand.read_bytes()
    img_info = inspect_image_bytes(raw)
    if img_info is None:
        return None
    return img_info[0], raw, clean


def stage_chat_turn_to_inbox(
    engine: WikiEngine, assistant_content: str, user_prompt: str = "", title: str = "", actor: str = "user",
) -> Dict[str, Any]:
    """Distill a single Forever Chat assistant response (+ user prompt context) cleanly into `01_inbox/`."""
    body = scrub_conversational_chatter(redact_secrets((assistant_content or "").strip()))
    if not body:
        return {"ok": False, "error": "Assistant message `content` is required to stage a chat turn."}
    clean_prompt = redact_secrets((user_prompt or "").strip())
    heading_m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    eff_title = (title or "").strip() or (heading_m.group(1).strip() if heading_m else "")
    if not eff_title:
        seed_line = clean_prompt.splitlines()[0] if clean_prompt else body.splitlines()[0]
        eff_title = re.sub(r"[#*`>\[\]]+", "", seed_line).strip()[:84] or "Staged Chat Insight"
    rec = recommend_templates(eff_title, f"{clean_prompt}\n{body}", source="chat-turn")
    chosen_tpl = rec[0]["slug"] if rec else "zettelkasten-atomic"
    tpl_spec = BUILTIN_TEMPLATE_MAP.get(chosen_tpl)
    ont = canonicalize_ontology(engine.root, list(tpl_spec.default_ontology) if tpl_spec else ["knowledge-synthesis"])
    ctx_header = f"> **Origin Prompt**: {clean_prompt[:280].replace(chr(10), ' ')}\n\n" if clean_prompt else ""
    staged_body = f"# {eff_title}\n\n{ctx_header}{body}" if not body.lstrip().startswith("#") else f"{body.splitlines()[0]}\n\n{ctx_header}" + "\n".join(body.splitlines()[1:]).lstrip("\n")
    return engine.capture_inbox(title=eff_title, content=staged_body, tags=["chat-turn", chosen_tpl], source="chat-turn", actor=actor, template=chosen_tpl, ontology=ont)


def execute_wiki_maintenance_subaction(wk: WikiEngine, sub: str, p: Dict[str, Any], actor: str) -> Optional[Dict[str, Any]]:
    """Dispatch Wiki 3.1 maintenance subactions (`diff`, `consolidate_ontology`, `verify_note`, `list_stale`, `upload_asset`, `stage_turn`)."""
    if sub in ("diff", "diff_note"):
        return compute_wiki_diff(wk, path=str(p.get("path") or ""), compare_to=str(p.get("compare_to") or ""), approval_id=str(p.get("approval_id") or ""), proposed_content=str(p.get("proposed_content") or ""))
    if sub == "consolidate_ontology":
        raw_dr = p.get("dry_run")
        dry = (raw_dr.strip().lower() in ("true", "1", "yes")) if isinstance(raw_dr, str) else bool(raw_dr)
        return consolidate_ontology(wk, aliases=p.get("aliases"), dry_run=dry, actor=actor)
    if sub == "verify_note":
        return verify_note_freshness(wk, path=str(p.get("path") or ""), review_after_days=int(p.get("review_after_days") or 0), actor=actor, verified_at_override=str(p.get("verified_at") or ""))
    if sub in ("list_stale", "stale"):
        return list_stale_notes(wk, as_of_iso=str(p.get("as_of") or ""), include_due_soon_days=int(p.get("due_soon_days") or 14))
    if sub == "upload_asset":
        return ingest_wiki_asset(wk, filename=str(p.get("filename") or "diagram.png"), data_base64=str(p.get("data_base64") or ""), alt_text=str(p.get("alt_text") or ""))
    if sub == "stage_turn":
        return stage_chat_turn_to_inbox(wk, assistant_content=str(p.get("content") or ""), user_prompt=str(p.get("user_prompt") or ""), title=str(p.get("title") or ""), actor=actor)
    return None
