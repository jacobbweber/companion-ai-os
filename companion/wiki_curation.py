"""
Deliberate Multi-Turn Wiki Curation, Overlap Synthesis & Reversible Lineage Engine
(`companion/wiki_curation.py` — A-Frame Domain Layer).

Enforces quality-over-speed absorption from `01_inbox/` into `02_knowledge/`:
1. `inspect_graduation`: Hybrid FTS5 + Semantic RRF + Title/Tag/Ontology overlap scan,
   2-hop cross-domain ontology bridges, vault domain/hub map, and template recommendations.
2. Mandatory Full-Read Gate: High-overlap candidates and merge targets MUST be read in full
   via `read_note` before `validate_curation_plan` passes.
3. Four Explicit Strategies (`CREATE_NEW`, `MERGE_AND_REFINE`, `SPLIT_MULTI`, `KEEP_SEPARATE`)
   with Anti-Lazy-Dump & Anti-Summary-Loss guards.
4. Never-Delete `04_archive/` lineage (`supersedes` / `superseded_by`) + 1-click `revert_curation`.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.semantic_memory import semantic_similarity
from companion.telemetry import redact_secrets
from companion.wiki_engine import WikiEngine
from companion.wiki_templates import (
    as_slug_list, canonicalize_ontology, compute_body_metrics, extract_wikilinks, hydrate_note_meta,
    list_templates, load_tag_authority, parse_frontmatter, recommend_templates,
    register_authority_terms, render_frontmatter, scrub_conversational_chatter,
    slugify_wiki, validate_curated_note,
)

__all__ = [
    "CURATION_STRATEGIES", "commit_curation_plan", "inspect_graduation",
    "list_curation_history", "pending_curation_nudge", "revert_curation", "validate_curation_plan",
]

CURATION_STRATEGIES = ("CREATE_NEW", "MERGE_AND_REFINE", "SPLIT_MULTI", "KEEP_SEPARATE")
_MID_CURATION_INTENT_RE = re.compile(
    r"\b(?:i'll|i will|let's|let me|now i|next i|draft|validate_curation|validate the curation|commit_curation|proceed to)\b",
    re.IGNORECASE,
)
_LAZY_DUMP_HEADING_RE = re.compile(
    r"^#{1,4}\s*(?:appended\s+update|new\s+notes?\s+dump|inbox\s+dump|raw\s+append|merged\s+appendix)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_inbox_list(raw: Any) -> List[str]:
    items = raw if isinstance(raw, (list, tuple)) else [x.strip() for x in str(raw or "").split(",") if x.strip()]
    out: List[str] = []
    for p in items:
        clean = p.replace("\\", "/").strip("/")
        if clean.lower() in ("", "01_inbox", "all", "*"):
            continue
        if not clean.startswith("01_inbox/"):
            clean = f"01_inbox/{Path(clean).name}"
        if not clean.lower().endswith(".md"):
            clean += ".md"
        if clean not in out:
            out.append(clean)
    return out


def _score_candidate_overlap(inbox_title: str, inbox_stem: str, inbox_tags: List[str], inbox_ont: List[str], inbox_body: str, cand: Dict[str, Any]) -> float:
    c_title, c_stem = str(cand.get("title") or ""), str(cand.get("stem") or "")
    c_tags = set(cand.get("tags") or [])
    c_ont = set(cand.get("ontology") or [])
    c_text = f"{c_title} {cand.get('summary', '')} {' '.join(c_tags)} {cand.get('body_text', '')[:1800]}"
    sem = semantic_similarity(f"{inbox_title}\n{inbox_body[:1800]}", c_text)
    title_sim = semantic_similarity(inbox_title, c_title)
    tag_sim = (len(set(inbox_tags) & c_tags) / max(1, len(set(inbox_tags) | c_tags))) if (inbox_tags or c_tags) else 0.0
    ont_sim = (len(set(inbox_ont) & c_ont) / max(1, len(set(inbox_ont) | c_ont))) if (inbox_ont or c_ont) else 0.0
    exact_bonus = 0.35 if (inbox_stem == c_stem or slugify_wiki(inbox_title) == slugify_wiki(c_title)) else 0.0
    return round(min(1.0, 0.45 * sem + 0.25 * title_sim + 0.18 * tag_sim + 0.12 * ont_sim + exact_bonus), 3)


def inspect_graduation(engine: WikiEngine, inbox_paths: Any) -> Dict[str, Any]:
    """Audit `01_inbox/` note(s) against `02_knowledge/` for overlaps, ontology bridges, domain hubs, and templates."""
    engine.ensure_warehouse()
    paths = _normalize_inbox_list(inbox_paths)
    if not paths:
        paths = [str(n["rel_path"]) for n in engine.list_notes(folder="01_inbox", limit=50)]
    if not paths:
        return {"ok": False, "error": "No staged notes exist in `01_inbox/` yet. Call `wiki_manage(action='capture_inbox', title=..., content=...)` first before `inspect_graduation`."}

    staged_notes: List[Dict[str, Any]] = []
    combined_title: List[str] = []
    combined_body: List[str] = []
    combined_tags: List[str] = []
    combined_ont: List[str] = []
    for p in paths:
        rd = engine.read_note(p)
        if not rd.get("ok"):
            return {"ok": False, "error": f"Inbox note `{p}` not found in `01_inbox/`."}
        staged_notes.append({"path": rd["path"], "title": rd["title"], "template": rd["template"], "tags": rd["tags"], "ontology": rd["ontology"], "word_count": compute_body_metrics(rd["body"])["word_count"]})
        combined_title.append(rd["title"])
        combined_body.append(rd["body"])
        combined_tags.extend(rd["tags"])
        combined_ont.extend(rd["ontology"])

    cols = ["rel_path", "stem", "domain", "topic", "title", "summary", "author", "template", "tags_json", "ontology_json", "body_text"]
    kb_rows = engine.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM wiki_notes_index WHERE folder = '02_knowledge' ORDER BY updated_at DESC LIMIT 300")
    candidates: List[Dict[str, Any]] = []
    domain_stats: Dict[str, Dict[str, Any]] = {}
    q_title, q_body = " ".join(combined_title), "\n\n".join(combined_body)
    primary_stem = Path(paths[0]).stem.lower()

    for r in kb_rows:
        r["tags"] = json.loads(r.pop("tags_json") or "[]")
        r["ontology"] = json.loads(r.pop("ontology_json") or "[]")
        dom = str(r.get("domain") or "general")
        d_entry = domain_stats.setdefault(dom, {"note_count": 0, "has_hub": False, "sample_notes": []})
        d_entry["note_count"] += 1
        if r.get("template") == "concept-map-system-hub":
            d_entry["has_hub"] = True
        if len(d_entry["sample_notes"]) < 6:
            d_entry["sample_notes"].append(r["rel_path"])

        score = _score_candidate_overlap(q_title, primary_stem, combined_tags, combined_ont, q_body, r)
        if score >= 0.16:
            must_read = score >= 0.34
            candidates.append({
                "path": r["rel_path"], "title": r["title"], "domain": dom, "author": r["author"],
                "template": r["template"], "overlap_score": score, "must_read": must_read,
                "tags": r["tags"], "ontology": r["ontology"], "summary": r["summary"][:180],
            })

    candidates.sort(key=lambda c: c["overlap_score"], reverse=True)
    top_candidates = candidates[:8]
    required_reads = [c["path"] for c in top_candidates if c["must_read"]]
    ont_bridges = engine.find_ontology_bridges(paths, combined_ont, [*paths, *(c["path"] for c in top_candidates)], limit=6)
    rec_templates = recommend_templates(q_title, q_body)
    all_tpls = [{"slug": t["slug"], "document_type": t["document_type"], "required_sections": t["required_sections"], "when_to_use": t["when_to_use"]} for t in list_templates(engine.root)]
    authority = load_tag_authority(engine.root)
    hub_nudges = [f"Domain `{d}` has {info['note_count']} notes without a `concept-map-system-hub` MOC." for d, info in domain_stats.items() if info["note_count"] >= 3 and not info["has_hub"]]

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cid = f"cur_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
    with engine.store._lock:
        engine.store.conn.execute(
            """INSERT INTO wiki_curation_sessions
               (curation_id, inbox_paths_json, candidate_paths_json, required_reads_json, read_paths_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
            (cid, json.dumps(paths), json.dumps([c["path"] for c in top_candidates]), json.dumps(required_reads), json.dumps(paths), now_iso, now_iso),
        )
        engine.store.conn.commit()

    staged_lbl = ", ".join(f"`{s['path']}` ({s['word_count']}w, tpl={s['template']})" for s in staged_notes)
    lines = [
        f"=== Deliberate Curation Audit (`{cid}`) ===",
        f"Staged Inbox : {staged_lbl}",
        f"Must-Read Overlaps ({len(required_reads)}): " + (", ".join(f"`{c['path']}` (score={c['overlap_score']})" for c in top_candidates if c["must_read"]) or "none (safe to `CREATE_NEW` or `SPLIT_MULTI`)"),
        f"Ontology Bridges ({len(ont_bridges)}): " + (", ".join(f"`{b['rel_path']}` via {b['shared_ontology']}" for b in ont_bridges) or "none yet"),
        "Recommended Templates: " + "; ".join(f"`{t['slug']}` (sections: {t['required_sections']})" for t in rec_templates),
        f"Vault Domains: { {k: v['note_count'] for k, v in domain_stats.items()} }",
        "Next Steps: (1) Read every Must-Read candidate via `wiki_manage(action='read', path=...)`; (2) Choose `CREATE_NEW`, `MERGE_AND_REFINE`, `SPLIT_MULTI`, or `KEEP_SEPARATE`; (3) Run `validate_curation`; (4) Run `commit_curation`.",
    ]
    if hub_nudges:
        lines.append("Hub Opportunity: " + " ".join(hub_nudges))
    return {
        "ok": True, "curation_id": cid, "staged_notes": staged_notes, "overlap_candidates": top_candidates,
        "required_reads": required_reads, "ontology_bridges": ont_bridges, "recommended_templates": rec_templates,
        "available_templates": all_tpls, "domains": domain_stats, "hub_recommendations": hub_nudges,
        "approved_domains": authority["domains"], "approved_ontology": authority["ontology"],
        "strategies": list(CURATION_STRATEGIES), "summary": "\n".join(lines),
    }


def _parse_outputs_arg(outputs_raw: Any, fallback: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if isinstance(outputs_raw, str) and outputs_raw.strip():
        try:
            parsed = json.loads(outputs_raw)
            outputs_raw = parsed if isinstance(parsed, list) else [parsed]
        except Exception as exc:
            return [], f"Invalid JSON in `outputs_json`: {exc}"
    if isinstance(outputs_raw, list) and outputs_raw:
        return [dict(x) for x in outputs_raw if isinstance(x, dict)], None
    if fallback.get("target_path") or fallback.get("content"):
        dom = slugify_wiki(str(fallback.get("domain") or "general"), "general")
        top = slugify_wiki(str(fallback.get("topic") or ""), "") if fallback.get("topic") else ""
        slug = slugify_wiki(str(fallback.get("new_slug") or fallback.get("title") or "curated-note"))
        default_p = f"02_knowledge/{dom}/{top}/{slug}.md" if top else f"02_knowledge/{dom}/{slug}.md"
        return [{
            "path": str(fallback.get("target_path") or default_p),
            "title": str(fallback.get("title") or ""), "summary": str(fallback.get("summary") or ""),
            "template": str(fallback.get("template") or ""), "domain": dom, "topic": top,
            "tags": fallback.get("tags"), "ontology": fallback.get("ontology"),
            "content": str(fallback.get("content") or ""),
        }], None
    return [], "Provide `outputs_json` (list of curated output note specs) or target note fields (`target_path`/`domain`, `template`, `summary`, `content`)."


def _load_curation_session(engine: WikiEngine, curation_id: str) -> Optional[Dict[str, Any]]:
    cols = ["curation_id", "inbox_paths_json", "candidate_paths_json", "required_reads_json", "read_paths_json", "status", "validated_plan_json"]
    rows = engine.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM wiki_curation_sessions WHERE curation_id = ?", ((curation_id or "").strip(),))
    if not rows:
        return None
    r = rows[0]
    return {
        "curation_id": str(r["curation_id"]), "status": str(r["status"]),
        "inbox_paths": json.loads(r["inbox_paths_json"] or "[]"),
        "candidate_paths": json.loads(r["candidate_paths_json"] or "[]"),
        "required_reads": json.loads(r["required_reads_json"] or "[]"),
        "read_paths": json.loads(r["read_paths_json"] or "[]"),
        "validated_plan": json.loads(r["validated_plan_json"]) if r["validated_plan_json"] else None,
    }


def _validate_single_output(
    engine: WikiEngine, item: Dict[str, Any], idx: int, read_set: set[str],
    allow_condense: bool, split_count: int, now_iso: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    errs: List[str] = []
    raw_p = str(item.get("path") or "").strip().replace("\\", "/").lstrip("/")
    if not raw_p.startswith("02_knowledge/"):
        dom_f = slugify_wiki(str(item.get("domain") or "general"), "general")
        top_f = slugify_wiki(str(item.get("topic") or ""), "") if item.get("topic") else ""
        stem_f = slugify_wiki(Path(raw_p).stem if raw_p else str(item.get("title") or f"note-{idx + 1}"))
        raw_p = f"02_knowledge/{dom_f}/{top_f}/{stem_f}.md" if top_f else f"02_knowledge/{dom_f}/{stem_f}.md"
    full_p, p_err = engine._validate_rel_note_path(raw_p)
    if p_err or full_p is None:
        return None, [p_err or f"Invalid output path `{raw_p}`."]
    rel_p = str(full_p.relative_to(engine.root)).replace("\\", "/")
    parts = rel_p.split("/")
    dom, top = (parts[1] if len(parts) >= 3 else "general"), (parts[2] if len(parts) == 4 else "")

    scrubbed = scrub_conversational_chatter(redact_secrets(str(item.get("content") or item.get("body") or "")))
    in_meta, body = parse_frontmatter(scrubbed)
    if _LAZY_DUMP_HEADING_RE.search(body):
        errs.append(f"`{rel_p}`: Lazy bottom-dump heading detected. Synthesize facts cleanly into template sections.")

    old_meta: Dict[str, Any] = {}
    if full_p.is_file():
        if rel_p not in read_set:
            errs.append(f"MANDATORY FULL-READ GATE: Target merge note `{rel_p}` MUST be read via `wiki_manage(action='read', path='{rel_p}')` before merging.")
        old_meta, old_body = parse_frontmatter(full_p.read_text(encoding="utf-8", errors="replace"))
        old_m, new_m = compute_body_metrics(old_body), compute_body_metrics(body)
        if not allow_condense and (new_m["word_count"] < int(old_m["word_count"] * 0.75) or new_m["code_blocks"] < old_m["code_blocks"]):
            errs.append(f"ANTI-LOSS GUARD on `{rel_p}`: Merged note shrinks existing content ({old_m['word_count']}w/{old_m['code_blocks']} code blocks -> {new_m['word_count']}w/{new_m['code_blocks']} code blocks). Preserve technical detail or pass `allow_condense=true`.")

    title = str(item.get("title") or in_meta.get("title") or old_meta.get("title") or full_p.stem).strip()
    if body and not body.lstrip().startswith("#"):
        body = f"# {title}\n\n{body}"
    tpl = slugify_wiki(str(item.get("template") or in_meta.get("template") or old_meta.get("template") or ""), "")
    tags = as_slug_list(item.get("tags") if item.get("tags") is not None else (in_meta.get("tags") or old_meta.get("tags")))
    ont = canonicalize_ontology(engine.root, item.get("ontology") if item.get("ontology") is not None else (in_meta.get("ontology") or old_meta.get("ontology")))
    meta = hydrate_note_meta(
        {**old_meta, **in_meta, "confidence": item.get("confidence", in_meta.get("confidence", 0.95)), "sensitivity": str(item.get("sensitivity") or in_meta.get("sensitivity") or "internal")},
        title, body, str(old_meta.get("author") or in_meta.get("author") or "companion"),
        str(item.get("status") or in_meta.get("status") or "active"), tpl, dom, top, tags, ont, now_iso,
        summary=str(item.get("summary") or in_meta.get("summary") or ""),
    )
    dom_cnt_rows = engine.store._fetch_rows("SELECT COUNT(*) FROM wiki_notes_index WHERE folder = '02_knowledge' AND domain = ? AND rel_path != ?", (dom, rel_p))
    existing_dom_cnt = int((dom_cnt_rows[0][0] if dom_cnt_rows else 0) or 0)
    errs.extend(f"`{rel_p}`: {e}" for e in validate_curated_note(engine.root, meta, body, existing_domain_notes=existing_dom_cnt, split_sibling_count=split_count))
    return {"path": rel_p, "meta": meta, "body": body, "existed": full_p.is_file(), "old_author": str(old_meta.get("author") or "")}, errs


def validate_curation_plan(
    engine: WikiEngine, curation_id: str, strategy: str, outputs: Any = None,
    rationale: str = "", allow_condense: bool = False, fallback_fields: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministically enforce full-read overlap gates, template sections, rich YAML, anti-orphan, and anti-loss rules."""
    sess = _load_curation_session(engine, curation_id)
    if sess is None:
        return {"ok": False, "error": f"Curation session `{curation_id}` not found. Call `wiki_manage(action='inspect_graduation', path='01_inbox/...')` first."}
    strat = (strategy or "CREATE_NEW").strip().upper()
    if strat not in CURATION_STRATEGIES:
        return {"ok": False, "error": f"Invalid curation `strategy='{strategy}'` (must be one of {list(CURATION_STRATEGIES)})."}

    parsed_outputs, parse_err = _parse_outputs_arg(outputs, fallback_fields or {})
    if parse_err or not parsed_outputs:
        return {"ok": False, "error": parse_err or "At least one curated output note is required."}
    if strat == "SPLIT_MULTI" and len(parsed_outputs) < 2:
        return {"ok": False, "error": "Strategy `SPLIT_MULTI` requires at least 2 cross-linked output notes in `outputs_json`."}
    if strat == "KEEP_SEPARATE" and len((rationale or "").strip()) < 15:
        return {"ok": False, "error": "Strategy `KEEP_SEPARATE` requires a >=15-char `rationale` explaining why the note remains separate from overlapping notes."}

    read_set = set(sess["read_paths"])
    unread_req = [p for p in sess["required_reads"] if p not in read_set]
    if unread_req:
        return {"ok": False, "unread_paths": unread_req, "error": f"MANDATORY FULL-READ GATE: Read high-overlap candidate(s) {unread_req} via `wiki_manage(action='read', path=...)` before validating your curation plan."}

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    normalized_outputs: List[Dict[str, Any]] = []
    all_errors: List[str] = []
    for idx, item in enumerate(parsed_outputs):
        norm_item, item_errs = _validate_single_output(engine, item, idx, read_set, allow_condense, len(parsed_outputs), now_iso)
        all_errors.extend(item_errs)
        if norm_item is not None:
            normalized_outputs.append(norm_item)

    if strat == "MERGE_AND_REFINE" and not any(o["existed"] for o in normalized_outputs):
        all_errors.append("Strategy `MERGE_AND_REFINE` requires at least one output `path` to target an existing `02_knowledge/...` note.")
    if strat == "SPLIT_MULTI" and len(normalized_outputs) >= 2:
        out_stems = {Path(o["path"]).stem.lower() for o in normalized_outputs}
        for o in normalized_outputs:
            links = {Path(ref).name.lower() for ref in extract_wikilinks(o["body"])}
            if not links.intersection(out_stems - {Path(o["path"]).stem.lower()}):
                all_errors.append(f"`{o['path']}`: `SPLIT_MULTI` notes must cross-link to at least one sibling split note via `[[wikilink]]`.")

    if all_errors:
        return {"ok": False, "curation_id": sess["curation_id"], "validation_errors": all_errors, "error": "CURATION VALIDATION FAILED:\n- " + "\n- ".join(all_errors)}

    validated_plan = {"curation_id": sess["curation_id"], "strategy": strat, "rationale": rationale.strip(), "inbox_paths": sess["inbox_paths"], "outputs": normalized_outputs}
    with engine.store._lock:
        engine.store.conn.execute("UPDATE wiki_curation_sessions SET status = 'validated', validated_plan_json = ?, updated_at = ? WHERE curation_id = ?", (json.dumps(validated_plan), now_iso, sess["curation_id"]))
        engine.store.conn.commit()
    return {
        "ok": True, "curation_id": sess["curation_id"], "strategy": strat,
        "output_paths": [o["path"] for o in normalized_outputs],
        "summary": f"Curation plan `{sess['curation_id']}` (`{strat}`) VALIDATED cleanly across {len(normalized_outputs)} note(s): {[o['path'] for o in normalized_outputs]}. Ready for `commit_curation`.",
    }


def _repoint_vault_wikilinks(engine: WikiEngine, stem_map: Dict[str, str], tx_id: str) -> List[str]:
    if not stem_map:
        return []
    repointed: List[str] = []
    for row in engine.list_notes(folder="02_knowledge", limit=400):
        full_p = engine.root / str(row["rel_path"])
        if not full_p.is_file():
            continue
        raw = full_p.read_text(encoding="utf-8", errors="replace")
        updated = raw
        for old_stem, new_stem in stem_map.items():
            if old_stem != new_stem:
                updated = re.sub(rf"\[\[{re.escape(old_stem)}(?=[\]|#])", f"[[{new_stem}", updated, flags=re.IGNORECASE)
        if updated != raw:
            engine._archive_prior_version(full_p, "link_repoint", superseded_by=str(row["rel_path"]), tx_id=tx_id, subdir="revisions")
            full_p.write_text(updated, encoding="utf-8", newline="\n")
            repointed.append(str(row["rel_path"]))
    return repointed


def commit_curation_plan(
    engine: WikiEngine, curation_id: str, actor: str = "companion",
    confirmed: bool = False, plan_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a validated Curation Plan with Trust-Mode gating, `04_archive/` lineage, and link repointing."""
    if plan_override:
        v_res = validate_curation_plan(engine, curation_id, str(plan_override.get("strategy") or ""), plan_override.get("outputs"), str(plan_override.get("rationale") or ""), bool(plan_override.get("allow_condense")), plan_override)
        if not v_res.get("ok"):
            return v_res
    sess = _load_curation_session(engine, curation_id)
    if sess is None or not sess.get("validated_plan"):
        return {"ok": False, "error": f"Curation session `{curation_id}` has no validated plan. Run `validate_curation` first."}

    plan = sess["validated_plan"]
    out_paths = [str(o["path"]) for o in plan["outputs"]]
    inbox_paths = [str(p) for p in plan["inbox_paths"]]
    touches_user = any(str(o.get("old_author") or "").lower() in ("user", "human", "partner") for o in plan["outputs"])

    if actor.strip().lower() not in ("user", "cli", "web"):
        allowed, apr = get_approval_queue(engine.store).check_or_enqueue(
            category="wiki_edit", action_name="commit_curation",
            target=f"{', '.join(inbox_paths)} -> {', '.join(out_paths)}",
            summary=f"Curate `{', '.join(inbox_paths)}` via `{plan['strategy']}` into `{', '.join(out_paths)}` (all originals preserved in `04_archive/`).",
            payload={"curation_id": curation_id}, requester=actor, confirmed=confirmed,
            always_require_in_autonomous=touches_user,
        )
        if not allowed:
            now_wait = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with engine.store._lock:
                engine.store.conn.execute("UPDATE wiki_curation_sessions SET status = 'awaiting_approval', updated_at = ? WHERE curation_id = ?", (now_wait, curation_id))
                engine.store.conn.commit()
            return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "curation_id": curation_id, "error": f"Approval `{apr['approval_id']}` required to commit curation `{curation_id}`."}

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tx_id = f"ctx_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}"
    sup_by_label = ", ".join(out_paths)
    inbox_archives: List[Dict[str, str]] = []
    kb_archives: List[Dict[str, str]] = []
    stem_repoint: Dict[str, str] = {}
    primary_new_stem = Path(out_paths[0]).stem

    for in_rel in inbox_paths:
        in_full = engine.root / in_rel
        if in_full.is_file():
            arch_p = engine._archive_prior_version(in_full, "inbox_curated", superseded_by=sup_by_label, tx_id=tx_id, subdir="inbox_origins")
            inbox_archives.append({"original_path": in_rel, "archive_path": arch_p or ""})
            stem_repoint[in_full.stem] = primary_new_stem
            in_full.unlink()

    for out in plan["outputs"]:
        dst_rel = str(out["path"])
        dst_full = engine.root / dst_rel
        prior_arch = ""
        if dst_full.is_file():
            prior_arch = engine._archive_prior_version(dst_full, "curation_merge", superseded_by=dst_rel, tx_id=tx_id, subdir="revisions") or ""
            kb_archives.append({"original_path": dst_rel, "archive_path": prior_arch})
        supersedes = [x["archive_path"] for x in inbox_archives if x["archive_path"]] + ([prior_arch] if prior_arch else [])
        meta = {**out["meta"], "supersedes": supersedes, "curation_tx_id": tx_id, "updated_at": now_iso}
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        dst_full.write_text(render_frontmatter(meta, str(out["body"])), encoding="utf-8", newline="\n")
        register_authority_terms(engine.root, domain=str(meta.get("domain") or ""), tags=meta.get("tags") or [], ontology=meta.get("ontology") or [])

    repointed = _repoint_vault_wikilinks(engine, stem_repoint, tx_id)
    engine.rebuild_index()
    summary = f"Committed curation `{tx_id}` (`{plan['strategy']}`): {', '.join(inbox_paths)} -> {', '.join(out_paths)} (archived {len(inbox_archives)} inbox + {len(kb_archives)} prior revision(s))."
    with engine.store._lock:
        engine.store.conn.execute("UPDATE wiki_curation_sessions SET status = 'committed', updated_at = ? WHERE curation_id = ?", (now_iso, curation_id))
        engine.store.conn.execute(
            """INSERT INTO wiki_curation_history
               (tx_id, curation_id, strategy, actor, summary, inbox_archives_json, knowledge_archives_json, output_paths_json, repointed_links_json, reverted, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (tx_id, curation_id, plan["strategy"], actor, summary, json.dumps(inbox_archives), json.dumps(kb_archives), json.dumps(out_paths), json.dumps(repointed), now_iso),
        )
        engine.store.conn.commit()
    return {"ok": True, "tx_id": tx_id, "curation_id": curation_id, "strategy": plan["strategy"], "output_paths": out_paths, "inbox_archives": inbox_archives, "knowledge_archives": kb_archives, "repointed_links": repointed, "summary": summary}


def list_curation_history(engine: WikiEngine, limit: int = 25) -> Dict[str, Any]:
    engine.ensure_warehouse()
    cols = ["tx_id", "curation_id", "strategy", "actor", "summary", "inbox_archives_json", "knowledge_archives_json", "output_paths_json", "repointed_links_json", "reverted", "created_at"]
    rows = engine.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM wiki_curation_history ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),))
    for r in rows:
        r["inbox_archives"] = json.loads(r.pop("inbox_archives_json") or "[]")
        r["knowledge_archives"] = json.loads(r.pop("knowledge_archives_json") or "[]")
        r["output_paths"] = json.loads(r.pop("output_paths_json") or "[]")
        r["repointed_links"] = json.loads(r.pop("repointed_links_json") or "[]")
        r["reverted"] = bool(r["reverted"])
    lines = [f"=== Wiki Curation Lineage ({len(rows)}) ==="] + ([f"  * `{r['tx_id']}` [{'REVERTED' if r['reverted'] else r['strategy']}] {r['summary']}" for r in rows] or ["  (No curations recorded yet.)"])
    return {"ok": True, "history": rows, "summary": "\n".join(lines)}


def revert_curation(engine: WikiEngine, tx_id_or_path: str, actor: str = "user", confirmed: bool = False) -> Dict[str, Any]:
    """1-click Never-Delete Curation Rollback: restores prior `02_knowledge/` notes and re-stages `01_inbox/` originals."""
    q = (tx_id_or_path or "").strip().replace("\\", "/").lstrip("/")
    hist = list_curation_history(engine, limit=100)["history"]
    target = next((h for h in hist if not h["reverted"] and (h["tx_id"] == q or q in h["output_paths"] or any(q in (a.get("archive_path"), a.get("original_path")) for a in (*h["inbox_archives"], *h["knowledge_archives"])))), None)
    if target is None:
        return {"ok": False, "error": f"Active curation transaction matching `{tx_id_or_path}` not found."}

    tx_id = str(target["tx_id"])
    if actor.strip().lower() not in ("user", "cli", "web"):
        allowed, apr = get_approval_queue(engine.store).check_or_enqueue(
            category="wiki_edit", action_name="revert_curation", target=tx_id,
            summary=f"Revert curation `{tx_id}` and re-stage original notes into `01_inbox/`.",
            payload={"tx_id": tx_id}, requester=actor, confirmed=confirmed,
        )
        if not allowed:
            return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"Approval `{apr['approval_id']}` required to revert curation `{tx_id}`."}

    prior_kb_map = {a["original_path"]: a["archive_path"] for a in target["knowledge_archives"] if a.get("archive_path")}
    for out_rel in target["output_paths"]:
        out_full = engine.root / out_rel
        if out_full.is_file():
            engine._archive_prior_version(out_full, "reverted_curation", tx_id=tx_id, subdir="reverted")
            if out_rel not in prior_kb_map:
                out_full.unlink()

    restored_kb: List[str] = []
    for orig_rel, arch_rel in prior_kb_map.items():
        arch_full = engine.root / arch_rel
        if arch_full.is_file():
            meta, body = parse_frontmatter(arch_full.read_text(encoding="utf-8", errors="replace"))
            for k_drop in ("archive_reason", "superseded_by", "original_path", "curation_tx_id"):
                meta.pop(k_drop, None)
            meta["status"] = "active"
            dst = engine.root / orig_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
            restored_kb.append(orig_rel)

    restored_inbox: List[str] = []
    for item in target["inbox_archives"]:
        orig_in, arch_in = str(item.get("original_path") or ""), str(item.get("archive_path") or "")
        arch_full = engine.root / arch_in
        if orig_in and arch_full.is_file():
            meta, body = parse_frontmatter(arch_full.read_text(encoding="utf-8", errors="replace"))
            for k_drop in ("archive_reason", "superseded_by", "original_path", "curation_tx_id"):
                meta.pop(k_drop, None)
            meta["status"] = "inbox"
            dst = engine.root / orig_in
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
            restored_inbox.append(orig_in)

    repointed_set = {str(p) for p in (target.get("repointed_links") or []) if p}
    if repointed_set:
        rev_dir = engine.root / "04_archive" / "revisions"
        if rev_dir.is_dir():
            for rev_file in sorted(rev_dir.glob("*_link_repoint_*.md")):
                r_meta, r_body = parse_frontmatter(rev_file.read_text(encoding="utf-8", errors="replace"))
                orig_p = str(r_meta.get("original_path") or "")
                if r_meta.get("curation_tx_id") == tx_id and orig_p in repointed_set and orig_p not in prior_kb_map:
                    for k_drop in ("archive_reason", "superseded_by", "original_path", "curation_tx_id"):
                        r_meta.pop(k_drop, None)
                    r_meta["status"] = "active"
                    dst_r = engine.root / orig_p
                    dst_r.parent.mkdir(parents=True, exist_ok=True)
                    dst_r.write_text(render_frontmatter(r_meta, r_body), encoding="utf-8", newline="\n")

    with engine.store._lock:
        engine.store.conn.execute("UPDATE wiki_curation_history SET reverted = 1 WHERE tx_id = ?", (tx_id,))
        engine.store.conn.commit()
    engine.rebuild_index()
    return {
        "ok": True, "tx_id": tx_id, "restored_inbox": restored_inbox, "restored_knowledge": restored_kb,
        "summary": f"Reverted curation `{tx_id}`: re-staged {restored_inbox} into `01_inbox/` and restored {len(restored_kb)} prior `02_knowledge/` note(s).",
    }


def pending_curation_nudge(
    store: Any, since_iso: str, assistant_text: str = "", require_intent_phrase: bool = True,
) -> Optional[str]:
    """Return a continuation prompt if an active turn started/validated a Wiki curation without committing it."""
    try:
        rows = store._fetch_rows(
            "SELECT curation_id, status, required_reads_json, read_paths_json FROM wiki_curation_sessions WHERE updated_at >= ? AND status IN ('open', 'validated') ORDER BY updated_at DESC LIMIT 1",
            (since_iso,),
        )
    except Exception:
        return None
    if not rows:
        return None
    cid, status, req_json, read_json = str(rows[0][0]), str(rows[0][1]), str(rows[0][2] or "[]"), str(rows[0][3] or "[]")
    if status == "validated":
        return f"[CURATION CONTINUATION GATE]: Curation session `{cid}` is validated but NOT YET COMMITTED. Invoke `wiki_manage(action='commit_curation', curation_id='{cid}')` now to finish graduating into `02_knowledge/`."
    txt = (assistant_text or "").strip()
    if require_intent_phrase and txt and not _MID_CURATION_INTENT_RE.search(txt):
        return None
    unread = [p for p in json.loads(req_json) if p not in set(json.loads(read_json))]
    if unread:
        return f"[CURATION CONTINUATION GATE]: Curation session `{cid}` is still `open`. Read remaining overlap candidate(s) {unread} via `wiki_manage(action='read', path=...)`, then call `validate_curation` and `commit_curation`."
    return f"[CURATION CONTINUATION GATE]: Curation session `{cid}` is still `open` (not yet validated or committed). Do NOT stop with narration — invoke `wiki_manage(action='validate_curation', curation_id='{cid}', strategy=..., outputs_json=...)` now, followed by `commit_curation`."
