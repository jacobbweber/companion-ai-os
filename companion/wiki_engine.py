"""
Co-Owned Interactive Knowledge Wiki Engine 3.0 (`companion/wiki_engine.py` — A-Frame Domain Layer).

Implements the plain-Markdown 4-Folder Knowledge Warehouse (`01_inbox/`, `02_knowledge/`,
`03_resources/`, `04_archive/` + undoable `trash/`) strictly separated from personal profile memory:
1. Code-Enforced One-Door Inbox (`Option A`): Fast `inbox-raw` human/export staging in `01_inbox/`,
   paired with governed 11-template + rich YAML frontmatter enforcement in `02_knowledge/`.
2. Max-2-Level Taxonomy: `02_knowledge/<domain>[/<topic>]/<note>.md` prevents deep folder mazes.
3. Dual-Author Respect (`user` vs `companion`) + Never-Delete `04_archive/` lineage (`supersedes`/`superseded_by`).
4. Read Purity, Optimistic `content_hash` Locks, `[[wikilinks]]` + 2-hop `ontology` Cross-Domain Graph.
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.archive_and_approvals import get_approval_queue
from companion.config import settings
from companion.disciplines import walk_pruned_files
from companion.memory_store import MemoryStore, get_memory_store
from companion.semantic_memory import hybrid_rrf_rank
from companion.telemetry import redact_secrets
from companion.wiki_templates import (
    BUILTIN_TEMPLATE_MAP, as_slug_list, canonicalize_ontology, compute_body_metrics, default_review_days,
    extract_wikilinks, hydrate_note_meta, parse_frontmatter, recommend_templates, render_frontmatter,
    scrub_conversational_chatter, seed_templates_and_authority, sha16_bytes, slugify_wiki, validate_curated_note,
)

__all__ = ["WIKI_FOLDERS", "WikiEngine", "compute_note_staleness", "extract_wikilinks", "get_wiki_engine", "parse_frontmatter", "render_frontmatter"]

WIKI_FOLDERS = ("01_inbox", "02_knowledge", "03_resources", "04_archive")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_-]{1,39})\b")

WIKI_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS wiki_notes_index (rel_path TEXT PRIMARY KEY, stem TEXT NOT NULL, folder TEXT NOT NULL, domain TEXT DEFAULT '', topic TEXT DEFAULT '', title TEXT NOT NULL, summary TEXT DEFAULT '', author TEXT NOT NULL DEFAULT 'companion', status TEXT DEFAULT 'active', template TEXT DEFAULT '', document_type TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', ontology_json TEXT DEFAULT '[]', supersedes_json TEXT DEFAULT '[]', superseded_by TEXT DEFAULT '', uid TEXT DEFAULT '', word_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0, content_hash TEXT NOT NULL, updated_at TEXT DEFAULT '', last_verified_at TEXT DEFAULT '', review_after_days INTEGER DEFAULT 365, excerpt TEXT DEFAULT '', body_text TEXT DEFAULT '')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(rel_path UNINDEXED, title, tags, body_text)",
    "CREATE TABLE IF NOT EXISTS wiki_links_index (source_path TEXT NOT NULL, target_ref TEXT NOT NULL, resolved_path TEXT DEFAULT '', PRIMARY KEY (source_path, target_ref))",
    "CREATE INDEX IF NOT EXISTS idx_wiki_links_resolved ON wiki_links_index(resolved_path)",
    "CREATE INDEX IF NOT EXISTS idx_wiki_notes_stem ON wiki_notes_index(stem)",
    "CREATE TABLE IF NOT EXISTS wiki_read_telemetry (rel_path TEXT PRIMARY KEY, read_count INTEGER DEFAULT 0, last_read_at TEXT DEFAULT '')",
    "CREATE TABLE IF NOT EXISTS wiki_curation_sessions (curation_id TEXT PRIMARY KEY, inbox_paths_json TEXT NOT NULL, candidate_paths_json TEXT NOT NULL, required_reads_json TEXT NOT NULL, read_paths_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'open', validated_plan_json TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wiki_curation_history (tx_id TEXT PRIMARY KEY, curation_id TEXT DEFAULT '', strategy TEXT NOT NULL, actor TEXT NOT NULL, summary TEXT NOT NULL, inbox_archives_json TEXT NOT NULL, knowledge_archives_json TEXT NOT NULL, output_paths_json TEXT NOT NULL, repointed_links_json TEXT NOT NULL DEFAULT '[]', reverted INTEGER DEFAULT 0, created_at TEXT NOT NULL)",
)
_EXTRA_INDEX_COLS: Tuple[Tuple[str, str], ...] = (
    ("topic", "TEXT DEFAULT ''"), ("summary", "TEXT DEFAULT ''"), ("status", "TEXT DEFAULT 'active'"),
    ("template", "TEXT DEFAULT ''"), ("document_type", "TEXT DEFAULT ''"), ("ontology_json", "TEXT DEFAULT '[]'"),
    ("supersedes_json", "TEXT DEFAULT '[]'"), ("superseded_by", "TEXT DEFAULT ''"), ("uid", "TEXT DEFAULT ''"),
    ("word_count", "INTEGER DEFAULT 0"), ("context_tokens", "INTEGER DEFAULT 0"),
    ("last_verified_at", "TEXT DEFAULT ''"), ("review_after_days", "INTEGER DEFAULT 365"),
)
_SUMMARY_COLS = ["rel_path", "stem", "folder", "domain", "topic", "title", "summary", "author", "status", "template", "document_type", "tags_json", "ontology_json", "superseded_by", "content_hash", "updated_at", "last_verified_at", "review_after_days", "excerpt"]


def _is_system_wiki_file(rel: str) -> bool:
    norm = rel.replace("\\", "/").lstrip("/")
    return norm.startswith(("trash/", "03_resources/templates/", "03_resources/assets/")) or norm == "03_resources/tag-authority.md"


def compute_note_staleness(folder: str, tpl: str, verified_at: str, fallback_ts: str, review_days: Any, now_dt: Optional[datetime] = None) -> Tuple[int, int, bool]:
    ref_dt = now_dt or datetime.now(timezone.utc)
    eff_days = int(review_days) if str(review_days or "").isdigit() and int(review_days) > 0 else default_review_days(tpl)
    raw_ts = (verified_at or fallback_ts or "").strip()
    try:
        ver_dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")) if raw_ts else ref_dt
        if ver_dt.tzinfo is None:
            ver_dt = ver_dt.replace(tzinfo=timezone.utc)
        age_days = max(0, (ref_dt - ver_dt).days)
    except Exception:
        age_days = 0
    return age_days, eff_days, (folder == "02_knowledge" and age_days >= eff_days)


class WikiEngine:
    """Co-owned Markdown Knowledge Warehouse with Dual-Author provenance, 11 templates & Ontology graph."""

    def __init__(self, wiki_dir: Optional[Path] = None, store: Optional[MemoryStore] = None):
        self._wiki_dir = wiki_dir
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        with mem._lock:
            for ddl in WIKI_DDL:
                mem.conn.execute(ddl)
            existing = {str(r[1]) for r in mem.conn.execute("PRAGMA table_info(wiki_notes_index)").fetchall()}
            for col_name, col_def in _EXTRA_INDEX_COLS:
                if col_name not in existing:
                    mem.conn.execute(f"ALTER TABLE wiki_notes_index ADD COLUMN {col_name} {col_def}")
            mem.conn.commit()
        return mem

    @property
    def root(self) -> Path:
        if self._wiki_dir is not None:
            return Path(self._wiki_dir).resolve()
        active_store = self._store or get_memory_store()
        if active_store.db_path.resolve() != settings.db_path.resolve():
            return (active_store.db_path.parent / "wiki").resolve()
        return settings.wiki_dir.resolve()

    def ensure_warehouse(self) -> Path:
        base = self.root
        for sub in (*WIKI_FOLDERS, "03_resources/templates", "03_resources/assets", "04_archive/inbox_origins", "04_archive/revisions", "04_archive/reverted", "trash"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        seed_templates_and_authority(base)
        return base

    def _validate_rel_note_path(self, rel_path: str, allow_archive: bool = False) -> Tuple[Optional[Path], Optional[str]]:
        norm = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not norm:
            return None, "Note `path` is required."
        if not norm.lower().endswith(".md"):
            norm += ".md"
        parts = [p for p in norm.split("/") if p]
        if any(p in (".", "..") for p in parts):
            return None, f"Invalid traversal in Wiki path `{rel_path}`."
        allowed_roots = WIKI_FOLDERS if allow_archive else ("01_inbox", "02_knowledge", "03_resources")
        if parts[0] not in allowed_roots:
            return None, f"Wiki path `{norm}` must start with one of {list(allowed_roots)}."
        if parts[0] == "02_knowledge" and len(parts) > 4:
            return None, f"WIKI TAXONOMY GUARD: `{norm}` exceeds the max 2-level folder depth inside `02_knowledge/` (use `02_knowledge/<domain>[/<topic>]/<note>.md`)."
        full = (self.ensure_warehouse() / "/".join(parts)).resolve()
        return (full, None) if full.is_relative_to(self.root) else (None, f"Wiki path `{rel_path}` escapes Warehouse root.")

    def _archive_prior_version(self, full_path: Path, reason: str, superseded_by: str = "", tx_id: str = "", subdir: str = "") -> Optional[str]:
        if not full_path.is_file():
            return None
        rel = str(full_path.relative_to(self.root)).replace("\\", "/")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        prefix = f"04_archive/{subdir.strip('/')}" if subdir else "04_archive"
        arch_rel = f"{prefix}/{stamp}_{reason}_{rel.replace('/', '__').removesuffix('.md')}.md"
        arch_full = self.ensure_warehouse() / arch_rel
        arch_full.parent.mkdir(parents=True, exist_ok=True)
        if superseded_by or tx_id:
            meta, body = parse_frontmatter(full_path.read_text(encoding="utf-8", errors="replace"))
            meta.update({"status": "archived", "archive_reason": reason, "original_path": rel})
            if superseded_by:
                meta["superseded_by"] = superseded_by
            if tx_id:
                meta["curation_tx_id"] = tx_id
            arch_full.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
        else:
            shutil.copy2(full_path, arch_full)
        return arch_rel

    def _index_single_file(self, full_path: Path) -> None:
        if not full_path.is_file() or full_path.suffix.lower() != ".md":
            return
        rel = str(full_path.relative_to(self.root)).replace("\\", "/")
        if _is_system_wiki_file(rel):
            return
        raw_bytes = full_path.read_bytes()
        meta, body = parse_frontmatter(raw_bytes.decode("utf-8", errors="replace"))
        parts = rel.split("/")
        folder = parts[0]
        domain = str(meta.get("domain") or (parts[1] if folder == "02_knowledge" and len(parts) >= 3 else folder))
        topic = str(meta.get("topic") or (parts[2] if folder == "02_knowledge" and len(parts) == 4 else ""))
        title = str(meta.get("title") or next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), full_path.stem))[:140]
        author = "user" if str(meta.get("author") or "companion").strip().lower() in ("user", "human", "partner") else "companion"
        all_tags = sorted(set(as_slug_list(meta.get("tags")) + [t.lower() for t in _INLINE_TAG_RE.findall(body)]))
        all_ont = as_slug_list(meta.get("ontology"))
        sup_raw, sup_by = meta.get("supersedes"), meta.get("superseded_by")
        sup_list = list(sup_raw) if isinstance(sup_raw, list) else ([str(sup_raw)] if sup_raw else [])
        sup_by_str = ", ".join(str(x) for x in sup_by) if isinstance(sup_by, list) else str(sup_by or "")
        summary = str(meta.get("summary") or "")
        excerpt = (summary or re.sub(r"\s+", " ", body).strip())[:260]
        metrics = compute_body_metrics(body)
        upd = str(meta.get("updated_at") or datetime.fromtimestamp(full_path.stat().st_mtime, tz=timezone.utc).isoformat())
        ver_at = str(meta.get("last_verified_at") or meta.get("created_at") or upd)
        rev_days = int(meta.get("review_after_days")) if str(meta.get("review_after_days") or "").isdigit() else default_review_days(str(meta.get("template") or ""))
        rel_links = extract_wikilinks(body + "\n" + " ".join(str(x) for x in (meta.get("related") if isinstance(meta.get("related"), list) else [])))
        with self.store._lock:
            cur = self.store.conn.cursor()
            cur.execute("DELETE FROM wiki_fts WHERE rel_path = ?", (rel,))
            cur.execute(
                "INSERT OR REPLACE INTO wiki_notes_index (rel_path, stem, folder, domain, topic, title, summary, author, status, template, document_type, tags_json, ontology_json, supersedes_json, superseded_by, uid, word_count, context_tokens, content_hash, updated_at, last_verified_at, review_after_days, excerpt, body_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rel, full_path.stem.lower(), folder, domain, topic, title, summary, author, str(meta.get("status") or ("inbox" if folder == "01_inbox" else "active")), str(meta.get("template") or ""), str(meta.get("document_type") or ""), json.dumps(all_tags), json.dumps(all_ont), json.dumps(sup_list), sup_by_str, str(meta.get("uid") or ""), int(meta.get("word_count") or metrics["word_count"]), int(meta.get("context_tokens") or metrics["context_tokens"]), sha16_bytes(raw_bytes), upd, ver_at, rev_days, excerpt, body),
            )
            cur.execute("INSERT INTO wiki_fts (rel_path, title, tags, body_text) VALUES (?, ?, ?, ?)", (rel, title, f"{' '.join(all_tags)} {' '.join(all_ont)} {summary}", body))
            cur.execute("DELETE FROM wiki_links_index WHERE source_path = ?", (rel,))
            for ref in rel_links:
                cur.execute("INSERT OR REPLACE INTO wiki_links_index (source_path, target_ref, resolved_path) VALUES (?, ?, ?)", (rel, ref, self._resolve_wikilink_target(cur, ref)))
            cur.execute("UPDATE wiki_links_index SET resolved_path = ? WHERE resolved_path = '' AND (LOWER(target_ref) = ? OR LOWER(target_ref) = ?)", (rel, full_path.stem.lower(), rel.removesuffix(".md").lower()))
            self.store.conn.commit()

    def _resolve_wikilink_target(self, cur: Any, ref: str) -> str:
        clean = ref.strip().replace("\\", "/").removesuffix(".md")
        row = cur.execute("SELECT rel_path FROM wiki_notes_index WHERE LOWER(rel_path) = ? OR stem = ? ORDER BY folder ASC LIMIT 1", (f"{clean.lower()}.md", Path(clean).name.lower())).fetchone()
        return str(row[0]) if row else ""

    def rebuild_index(self) -> Dict[str, Any]:
        base = self.ensure_warehouse()
        md_files = [p for f in WIKI_FOLDERS if (base / f).is_dir() for p in walk_pruned_files(base / f) if p.suffix.lower() == ".md" and not _is_system_wiki_file(str(p.relative_to(base)))]
        with self.store._lock:
            for tbl in ("wiki_notes_index", "wiki_fts", "wiki_links_index"):
                self.store.conn.execute(f"DELETE FROM {tbl}")
            self.store.conn.commit()
        for f in md_files:
            self._index_single_file(f)
        with self.store._lock:
            cur = self.store.conn.cursor()
            for src_p, tgt_ref in cur.execute("SELECT source_path, target_ref FROM wiki_links_index WHERE resolved_path = ''").fetchall():
                if res := self._resolve_wikilink_target(cur, str(tgt_ref)):
                    cur.execute("UPDATE wiki_links_index SET resolved_path = ? WHERE source_path = ? AND target_ref = ?", (res, src_p, tgt_ref))
            link_cnt = int((cur.execute("SELECT COUNT(*) FROM wiki_links_index").fetchone() or (0,))[0])
            self.store.conn.commit()
        return {"ok": True, "indexed_notes": len(md_files), "indexed_links": link_cnt, "summary": f"Reindexed {len(md_files)} Wiki note(s) and {link_cnt} wikilink(s)."}

    def _record_sqlite_read(self, rel: str) -> int:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.store._lock:
            cur = self.store.conn.cursor()
            cur.execute("INSERT INTO wiki_read_telemetry (rel_path, read_count, last_read_at) VALUES (?, 1, ?) ON CONFLICT(rel_path) DO UPDATE SET read_count = read_count + 1, last_read_at = excluded.last_read_at", (rel, now_iso))
            cnt = int((cur.execute("SELECT read_count FROM wiki_read_telemetry WHERE rel_path = ?", (rel,)).fetchone() or (1,))[0])
            for cid, r_json in cur.execute("SELECT curation_id, read_paths_json FROM wiki_curation_sessions WHERE status IN ('open', 'validated')").fetchall():
                seen = json.loads(r_json or "[]")
                if rel not in seen:
                    cur.execute("UPDATE wiki_curation_sessions SET read_paths_json = ?, updated_at = ? WHERE curation_id = ?", (json.dumps([*seen, rel]), now_iso, cid))
            self.store.conn.commit()
        return cnt

    def read_note(self, path_or_stem: str) -> Dict[str, Any]:
        """Read a Wiki note with rich frontmatter, `content_hash`, links, and SQLite read telemetry — NEVER modifies disk."""
        q = (path_or_stem or "").strip().replace("\\", "/").lstrip("/")
        if not q:
            return {"ok": False, "error": "Note `path` or stem is required."}
        base = self.ensure_warehouse()
        cand = base / (q if q.lower().endswith(".md") else f"{q}.md")
        if not cand.is_file():
            rows = self.store._fetch_rows("SELECT rel_path FROM wiki_notes_index WHERE stem = ? OR LOWER(rel_path) = ? LIMIT 1", (Path(q).stem.lower(), (q if q.lower().endswith(".md") else f"{q}.md").lower()))
            if rows:
                cand = base / str(rows[0][0])
        if not cand.is_file() or not cand.resolve().is_relative_to(self.root):
            return {"ok": False, "error": f"Wiki note `{path_or_stem}` not found."}
        raw_bytes = cand.read_bytes()
        raw_text = raw_bytes.decode("utf-8", errors="replace")
        meta, body = parse_frontmatter(raw_text)
        rel = str(cand.resolve().relative_to(self.root)).replace("\\", "/")
        read_cnt, parts = self._record_sqlite_read(rel), rel.split("/")
        tpl_s = str(meta.get("template") or "")
        ver_s = str(meta.get("last_verified_at") or meta.get("created_at") or meta.get("updated_at") or "")
        age_d, rev_d, is_stale = compute_note_staleness(parts[0], tpl_s, ver_s, str(meta.get("updated_at") or ""), meta.get("review_after_days"))
        return {
            "ok": True, "path": rel, "title": str(meta.get("title") or cand.stem), "summary": str(meta.get("summary") or ""), "note_summary": str(meta.get("summary") or ""),
            "author": str(meta.get("author") or "companion"), "status": str(meta.get("status") or ("inbox" if parts[0] == "01_inbox" else "active")),
            "template": tpl_s, "document_type": str(meta.get("document_type") or ""),
            "domain": str(meta.get("domain") or (parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else parts[0])),
            "topic": str(meta.get("topic") or (parts[2] if parts[0] == "02_knowledge" and len(parts) == 4 else "")),
            "tags": as_slug_list(meta.get("tags")), "ontology": as_slug_list(meta.get("ontology")),
            "supersedes": meta.get("supersedes") if isinstance(meta.get("supersedes"), list) else [],
            "superseded_by": meta.get("superseded_by") or "", "frontmatter": meta, "body": body, "raw": raw_text,
            "last_verified_at": ver_s, "review_after_days": rev_d, "days_since_verified": age_d, "stale": is_stale,
            "content_hash": sha16_bytes(raw_bytes), "wikilinks": extract_wikilinks(body), "backlinks": self.get_backlinks(rel), "read_count": read_cnt,
        }

    def capture_inbox(self, title: str, content: str, tags: Optional[List[str]] = None, source: str = "companion", slug: str = "", actor: str = "companion", template: str = "", summary: str = "", ontology: Optional[List[str]] = None, domain_hint: str = "") -> Dict[str, Any]:
        clean_title = redact_secrets((title or "").strip())[:140]
        in_meta, clean_body = parse_frontmatter(scrub_conversational_chatter(redact_secrets((content or "").strip())))
        eff_title = clean_title or str(in_meta.get("title") or "")[:140]
        if not eff_title or not clean_body:
            return {"ok": False, "error": "Both `title` and `content` are required for `capture_inbox`."}
        base, stem = self.ensure_warehouse(), slugify_wiki(slug or eff_title, fallback="inbox-note")
        rel, full = f"01_inbox/{stem}.md", base / f"01_inbox/{stem}.md"
        eff_author = "user" if actor.strip().lower() in ("user", "cli", "web") else "companion"
        if full.is_file() and eff_author == "companion":
            existing_meta, _ = parse_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
            if str(existing_meta.get("author") or "").lower() in ("user", "human", "partner"):
                rel = f"01_inbox/{stem}-companion-{datetime.now(timezone.utc).strftime('%H%M%S')}.md"
                full = base / rel
            else:
                self._archive_prior_version(full, "inbox_update")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req_tpl = slugify_wiki(template or str(in_meta.get("template") or ""), "")
        chosen_tpl = req_tpl if req_tpl in BUILTIN_TEMPLATE_MAP else (recommend_templates(eff_title, clean_body, source)[0]["slug"] if eff_author == "companion" and source not in ("chat-export", "job-overflow") else "inbox-raw")
        body_out = clean_body if clean_body.startswith("#") else f"# {eff_title}\n\n{clean_body}"
        meta = hydrate_note_meta({**in_meta, "source": source}, eff_title, body_out, eff_author, "inbox", chosen_tpl, slugify_wiki(domain_hint or str(in_meta.get("domain") or "inbox"), "inbox"), "", as_slug_list(tags if tags is not None else in_meta.get("tags")) or ["inbox"], canonicalize_ontology(self.root, ontology if ontology is not None else in_meta.get("ontology")), now_iso, summary=summary)
        full.write_text(render_frontmatter(meta, body_out), encoding="utf-8", newline="\n")
        self._index_single_file(full)
        return {"ok": True, "path": rel, "author": eff_author, "template": chosen_tpl, "content_hash": sha16_bytes(full.read_bytes()), "summary": f"Captured note `{rel}` in `01_inbox/` (`author: {eff_author}`, `template: {chosen_tpl}`)."}

    def _count_other_domain_notes(self, domain: str, exclude_rel: str) -> int:
        mem = self.store
        with mem._lock:
            row = mem.conn.execute("SELECT COUNT(*) FROM wiki_notes_index WHERE folder = '02_knowledge' AND domain = ? AND rel_path != ?", (domain, exclude_rel)).fetchone()
        return int(row[0]) if row else 0

    def save_or_update_note(self, rel_path: str, content: str, title: str = "", tags: Optional[List[str]] = None, actor: str = "companion", expected_hash: str = "", confirmed: bool = False, summary: str = "", template: str = "", ontology: Optional[List[str]] = None, status: str = "") -> Dict[str, Any]:
        is_user = actor.strip().lower() in ("user", "cli", "web")
        norm_in = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not is_user and norm_in.startswith("02_knowledge/"):
            cand_check, _ = self._validate_rel_note_path(norm_in)
            if cand_check is not None and not cand_check.is_file():
                return {"ok": False, "error": "WIKI ONE-DOOR INBOX GUARD: New Companion notes must be captured in `01_inbox/` first (then curated via `inspect_graduation` -> `commit_curation` or `graduate_note`)."}
        full, path_err = self._validate_rel_note_path(norm_in)
        if path_err or full is None:
            return {"ok": False, "error": path_err or "Invalid path."}
        rel = str(full.relative_to(self.root)).replace("\\", "/")
        clean_body_raw = redact_secrets(content or "")
        in_meta, in_body = parse_frontmatter(clean_body_raw)
        existed, old_meta = full.is_file(), {}
        if existed:
            disk_bytes = full.read_bytes()
            disk_hash = sha16_bytes(disk_bytes)
            if expected_hash and expected_hash.strip() != disk_hash:
                return {"ok": False, "conflict": True, "current_hash": disk_hash, "error": f"CONCURRENCY CONFLICT on `{rel}`: expected hash `{expected_hash}`, found `{disk_hash}`. Re-read the note before saving."}
            old_meta, _ = parse_frontmatter(disk_bytes.decode("utf-8", errors="replace"))
            if not is_user and str(old_meta.get("author") or "companion").strip().lower() in ("user", "human", "partner"):
                allowed, apr = get_approval_queue(self.store).check_or_enqueue("wiki_edit", "edit_user_note", rel, f"Companion requests permission to update your user-authored Wiki note `{rel}`.", payload={"path": rel, "content": clean_body_raw, "title": title, "tags": tags, "summary": summary, "template": template, "ontology": ontology, "content_sha": sha16_bytes(clean_body_raw.encode("utf-8"))}, requester=actor, confirmed=confirmed, always_require_in_autonomous=True)
                if not allowed:
                    return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"WIKI AUTHOR GUARD REJECTED (`{apr['approval_id']}`): `{rel}` has `author: user` and awaits your approval before Companion can edit it."}
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eff_author = "user" if is_user else str(old_meta.get("author") or "companion")
        eff_title = title.strip() or str(in_meta.get("title") or old_meta.get("title") or full.stem)
        parts = rel.split("/")
        eff_domain = parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else parts[0]
        eff_topic = parts[2] if parts[0] == "02_knowledge" and len(parts) == 4 else str(in_meta.get("topic") or old_meta.get("topic") or "")
        eff_tpl = slugify_wiki(template or str(in_meta.get("template") or old_meta.get("template") or ("standard-note" if parts[0] == "02_knowledge" else "inbox-raw")), "standard-note")
        eff_status = status.strip() or str(in_meta.get("status") or old_meta.get("status") or ("inbox" if parts[0] == "01_inbox" else "active"))
        merged_meta = hydrate_note_meta({**old_meta, **in_meta}, eff_title, in_body, eff_author, eff_status, eff_tpl, eff_domain, eff_topic, as_slug_list(tags if tags is not None else (in_meta.get("tags") or old_meta.get("tags") or [])), canonicalize_ontology(self.root, ontology if ontology is not None else (in_meta.get("ontology") or old_meta.get("ontology") or [])), now_iso, summary=summary)
        if parts[0] == "02_knowledge" and (bool(template.strip()) or bool(in_meta.get("template"))):
            errs = validate_curated_note(self.root, merged_meta, in_body, existing_domain_notes=self._count_other_domain_notes(eff_domain, rel))
            if errs:
                return {"ok": False, "validation_errors": errs, "error": "WIKI SCHEMA GUARD REJECTED:\n- " + "\n- ".join(errs)}
        archived_to = self._archive_prior_version(full, "edit", superseded_by=rel) if existed else None
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(render_frontmatter(merged_meta, in_body), encoding="utf-8", newline="\n")
        self._index_single_file(full)
        return {"ok": True, "path": rel, "author": eff_author, "archived_prior": archived_to, "content_hash": sha16_bytes(full.read_bytes()), "summary": f"Saved `{rel}` (`author: {eff_author}`" + (f", archived prior -> `{archived_to}`" if archived_to else "") + ")."}

    def graduate_note(self, inbox_path: str, domain: str, topic: str = "", new_slug: str = "", actor: str = "companion", confirmed: bool = False, template: str = "", summary: str = "", ontology: Optional[List[str]] = None) -> Dict[str, Any]:
        src_rel = (inbox_path or "").strip().replace("\\", "/").lstrip("/")
        if not src_rel.startswith("01_inbox/"):
            src_rel = f"01_inbox/{Path(src_rel).name}"
        src_full, err = self._validate_rel_note_path(src_rel)
        if err or src_full is None or not src_full.is_file():
            return {"ok": False, "error": f"Inbox note `{src_rel}` not found."}
        dom_slug = slugify_wiki(domain, fallback="general")
        top_slug = slugify_wiki(topic, fallback="") if (topic or "").strip() else ""
        note_slug = slugify_wiki(new_slug or src_full.stem, fallback=src_full.stem)
        dst_rel = f"02_knowledge/{dom_slug}/{top_slug}/{note_slug}.md" if top_slug else f"02_knowledge/{dom_slug}/{note_slug}.md"
        dst_full, dst_err = self._validate_rel_note_path(dst_rel)
        if dst_err or dst_full is None:
            return {"ok": False, "error": dst_err or "Invalid destination path."}
        meta, body = parse_frontmatter(src_full.read_text(encoding="utf-8", errors="replace"))
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_tpl = slugify_wiki(template or str(meta.get("template") or ""), "")
        must_validate = bool(template.strip()) or raw_tpl in ("", "inbox-raw")
        cur_tpl = (raw_tpl or "inbox-raw") if must_validate else raw_tpl
        out_meta = hydrate_note_meta({**meta, "supersedes": [src_rel]}, str(meta.get("title") or note_slug), body, str(meta.get("author") or "companion"), "active", cur_tpl, dom_slug, top_slug, as_slug_list(meta.get("tags")) or [dom_slug], canonicalize_ontology(self.root, ontology if ontology is not None else meta.get("ontology")), now_iso, summary=summary)
        if must_validate:
            errs = validate_curated_note(self.root, out_meta, body, existing_domain_notes=self._count_other_domain_notes(dom_slug, dst_rel))
            if errs:
                return {"ok": False, "validation_errors": errs, "error": "WIKI SCHEMA GUARD REJECTED:\n- " + "\n- ".join(errs)}
        if actor.strip().lower() not in ("user", "cli", "web"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue("wiki_edit", "graduate_note", f"{src_rel} -> {dst_rel}", f"Graduate inbox note `{src_rel}` into curated knowledge `{dst_rel}`.", payload={"from": src_rel, "to": dst_rel, "template": template, "summary": summary, "ontology": ontology}, requester=actor, confirmed=confirmed)
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"Approval `{apr['approval_id']}` required to graduate `{src_rel}` -> `{dst_rel}`."}
        arch_src = self._archive_prior_version(src_full, "graduate", superseded_by=dst_rel)
        if dst_full.is_file():
            self._archive_prior_version(dst_full, "overwrite_graduate", superseded_by=dst_rel)
        out_meta["supersedes"] = [arch_src or src_rel]
        dst_full.parent.mkdir(parents=True, exist_ok=True)
        dst_full.write_text(render_frontmatter(out_meta, body), encoding="utf-8", newline="\n")
        src_full.unlink()
        self.rebuild_index()
        return {"ok": True, "from": src_rel, "path": dst_rel, "archived_source": arch_src, "template": cur_tpl, "summary": f"Graduated `{src_rel}` -> `{dst_rel}` (`template: {cur_tpl}`)."}

    def create_proxy_note(self, asset_path: str, title: str, executive_summary: str, highlights: str = "", mime_type: str = "", size_label: str = "", sha256: str = "", tags: Optional[List[str]] = None, ontology: Optional[List[str]] = None, related_links: Optional[List[str]] = None, actor: str = "companion") -> Dict[str, Any]:
        rel_str = ", ".join(f"[[{r.strip('[]')}]]" for r in (related_links or []) if r.strip()) or "(seed asset proxy)"
        body = f"# {title.strip() or Path(asset_path).name}\n\n## Asset Metadata & Location\n- **Source File / URI**: `{asset_path}`\n- **Format & Size**: `{mime_type or Path(asset_path).suffix or 'binary'}` • `{size_label or 'n/a'}`\n- **Checksum / Provenance**: `{sha256 or 'verified-local'}`\n\n## Executive Summary & Key Facts\n{executive_summary.strip() or 'Searchable proxy note for attached binary/external resource.'}\n\n## Extracted Highlights\n{(highlights or 'Inspect raw asset for full layout and diagrams.').strip()}\n\n- **Related Knowledge**: {rel_str}\n"
        return self.capture_inbox(title=title or f"Proxy: {Path(asset_path).name}", content=body, tags=list(dict.fromkeys(["asset-proxy", *(tags or [])])), source="asset-proxy", slug=f"proxy-{Path(asset_path).stem}", actor=actor, template="proxy-asset-note", summary=executive_summary[:240], ontology=list(dict.fromkeys(["asset-provenance", *(ontology or ["reference-knowledge"])])))

    def trash_note(self, rel_path: str, actor: str = "companion", confirmed: bool = False) -> Dict[str, Any]:
        full, err = self._validate_rel_note_path(rel_path, allow_archive=True)
        if err or full is None or not full.is_file():
            return {"ok": False, "error": err or f"Note `{rel_path}` not found."}
        rel = str(full.relative_to(self.root)).replace("\\", "/")
        if actor.strip().lower() not in ("user", "cli", "web"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue("wiki_edit", "trash_note", rel, f"Move Wiki note `{rel}` to undoable `trash/`.", payload={"path": rel}, requester=actor, confirmed=confirmed, always_require_in_autonomous=True)
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"Approval `{apr['approval_id']}` required before Companion can trash `{rel}`."}
        trash_dir = self.ensure_warehouse() / "trash"
        trash_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{rel.replace('/', '__')}"
        shutil.move(str(full), str(trash_dir / trash_id))
        mf_path = trash_dir / "trash_manifest.json"
        entries = json.loads(mf_path.read_text(encoding="utf-8")) if mf_path.is_file() else {}
        entries[trash_id] = {"original_path": rel, "trashed_at": datetime.now(timezone.utc).isoformat(), "by": actor}
        mf_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        self.rebuild_index()
        return {"ok": True, "trash_id": trash_id, "original_path": rel, "summary": f"Moved `{rel}` to undoable `trash/{trash_id}`."}

    def restore_note(self, trash_id_or_path: str) -> Dict[str, Any]:
        trash_dir = self.ensure_warehouse() / "trash"
        mf_path = trash_dir / "trash_manifest.json"
        entries: Dict[str, Any] = json.loads(mf_path.read_text(encoding="utf-8")) if mf_path.is_file() else {}
        key = (trash_id_or_path or "").strip()
        chosen = key if key in entries else next((tid for tid, info in reversed(list(entries.items())) if info.get("original_path") == key or key in tid), "")
        if not chosen or not (trash_dir / chosen).is_file():
            return {"ok": False, "error": f"Trashed note `{trash_id_or_path}` not found in `trash/`."}
        orig_rel = str(entries[chosen]["original_path"])
        dst = self.ensure_warehouse() / orig_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(trash_dir / chosen), str(dst))
        entries.pop(chosen, None)
        mf_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        self._index_single_file(dst)
        return {"ok": True, "path": orig_rel, "summary": f"Restored `{orig_rel}` from `trash/`."}

    def list_notes(self, folder: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        self.ensure_warehouse()
        if not self.store._fetch_rows("SELECT 1 FROM wiki_notes_index LIMIT 1"):
            self.rebuild_index()
        clean_f, sel = (folder or "").strip().replace("\\", "/").strip("/"), ", ".join(_SUMMARY_COLS)
        if clean_f and "/" in clean_f:
            rows = self.store._fetch_dicts(_SUMMARY_COLS, f"SELECT {sel} FROM wiki_notes_index WHERE rel_path LIKE ? ORDER BY updated_at DESC, rel_path ASC LIMIT ?", (f"{clean_f}/%", max(1, int(limit))))
        elif clean_f:
            rows = self.store._fetch_dicts(_SUMMARY_COLS, f"SELECT {sel} FROM wiki_notes_index WHERE folder = ? ORDER BY updated_at DESC, rel_path ASC LIMIT ?", (clean_f, max(1, int(limit))))
        else:
            rows = self.store._fetch_dicts(_SUMMARY_COLS, f"SELECT {sel} FROM wiki_notes_index WHERE folder != '04_archive' ORDER BY folder ASC, rel_path ASC LIMIT ?", (max(1, int(limit)),))
        for r in rows:
            r["tags"] = json.loads(r.pop("tags_json") or "[]")
            r["ontology"] = json.loads(r.pop("ontology_json") or "[]")
            age_d, rev_d, is_stale = compute_note_staleness(str(r.get("folder") or ""), str(r.get("template") or ""), str(r.get("last_verified_at") or ""), str(r.get("updated_at") or ""), r.get("review_after_days"))
            r["review_after_days"], r["days_since_verified"], r["stale"] = rev_d, age_d, is_stale
        return rows

    def find_ontology_bridges(self, seed_paths: List[str], seed_ontology: List[str], exclude_paths: List[str], limit: int = 6) -> List[Dict[str, Any]]:
        ont_set = set(canonicalize_ontology(self.root, seed_ontology))
        if not ont_set or limit <= 0:
            return []
        excl, via_label = set(exclude_paths), (seed_paths[0] if seed_paths else "query-ontology")
        rows = self.store._fetch_dicts(_SUMMARY_COLS, f"SELECT {', '.join(_SUMMARY_COLS)} FROM wiki_notes_index WHERE folder != '04_archive' ORDER BY updated_at DESC LIMIT 250")
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for r in rows:
            if str(r["rel_path"]) in excl:
                continue
            r_ont = canonicalize_ontology(self.root, json.loads(r.pop("ontology_json") or "[]"))
            if shared := sorted(ont_set.intersection(r_ont)):
                r.update({"tags": json.loads(r.pop("tags_json") or "[]"), "ontology": r_ont, "match_kind": "ontology_bridge", "shared_ontology": shared, "bridge_via": via_label})
                scored.append((len(shared), r))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def search_notes(self, query: str, limit: int = 12, include_archive: bool = False, include_ontology_bridges: bool = True) -> List[Dict[str, Any]]:
        self.ensure_warehouse()
        if not self.store._fetch_rows("SELECT 1 FROM wiki_notes_index LIMIT 1"):
            self.rebuild_index()
        clean_q = (query or "").strip()
        if not clean_q:
            return self.list_notes(limit=limit)
        cols, arch_sql = [*_SUMMARY_COLS, "body_text"], ("" if include_archive else "WHERE folder != '04_archive' ")
        by_path = {str(r["rel_path"]): r for r in self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM wiki_notes_index {arch_sql}ORDER BY updated_at DESC LIMIT 250")}
        tokens = [t for t in re.findall(r"[a-zA-Z0-9_-]+", clean_q) if t.lower() not in ("and", "or")]
        lex_paths: List[str] = []
        if tokens:
            with self.store._lock:
                for joiner in (" AND ", " OR "):
                    hits = self.store.conn.execute("SELECT rel_path FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY rank LIMIT ?", (joiner.join(f'"{t}"*' for t in tokens), max(limit * 2, 20))).fetchall()
                    if hits:
                        lex_paths = [str(h[0]) for h in hits if str(h[0]) in by_path]
                        break
        ranked = hybrid_rrf_rank(clean_q, [(p, f"{r['title']} {r['summary']} {r['tags_json']} {r['ontology_json']} {r['body_text'][:1200]}") for p, r in by_path.items()], lexical_ids=lex_paths, limit=limit)
        out: List[Dict[str, Any]] = []
        seed_ont: List[str] = []
        for p in ranked:
            if p in by_path:
                raw_r = by_path[p]
                ont = json.loads(raw_r.get("ontology_json") or "[]")
                seed_ont.extend(ont)
                item = {**raw_r, "tags": json.loads(raw_r.get("tags_json") or "[]"), "ontology": ont, "match_kind": "direct"}
                for k_drop in ("tags_json", "ontology_json", "body_text"):
                    item.pop(k_drop, None)
                out.append(item)
        if include_ontology_bridges and seed_ont and len(out) < limit:
            out.extend(self.find_ontology_bridges([x["rel_path"] for x in out[:3]], seed_ont, [x["rel_path"] for x in out], limit=limit - len(out)))
        return out

    def get_backlinks(self, path_or_stem: str) -> List[str]:
        target = (path_or_stem or "").strip().replace("\\", "/").lstrip("/")
        return [str(r[0]) for r in self.store._fetch_rows("SELECT DISTINCT source_path FROM wiki_links_index WHERE resolved_path = ? OR LOWER(target_ref) = ? ORDER BY source_path ASC", (target, Path(target).stem.lower()))]

    def export_mindmap_graph(self) -> Dict[str, Any]:
        notes = self.list_notes(limit=500)
        nodes = [{"id": n["rel_path"], "label": n["title"], "folder": n["folder"], "domain": n["domain"], "author": n["author"], "template": n.get("template", ""), "tags": n["tags"], "ontology": n.get("ontology", [])} for n in notes]
        node_ids = {n["id"] for n in nodes}
        raw_edges = self.store._fetch_rows("SELECT source_path, target_ref, resolved_path FROM wiki_links_index")
        edges = [{"source": str(s), "target": str(r), "ref": str(t)} for s, t, r in raw_edges if str(s) in node_ids and str(r) in node_ids]
        direct_pairs = {(e["source"], e["target"]) for e in edges} | {(e["target"], e["source"]) for e in edges}
        ontology_edges = [
            {"source": a["id"], "target": b["id"], "shared_ontology": shared}
            for idx, a in enumerate(nodes) if a.get("ontology")
            for b in nodes[idx + 1 :] if (a["id"], b["id"]) not in direct_pairs and (shared := sorted(set(a["ontology"]).intersection(b.get("ontology") or [])))
        ]
        connected = {e["source"] for e in edges} | {e["target"] for e in edges}
        domains = sorted({n["domain"] for n in nodes if n["domain"]})
        return {
            "ok": True, "nodes": nodes, "edges": edges, "ontology_edges": ontology_edges[:250], "domains": domains,
            "orphan_notes": sorted(node_ids - connected), "unresolved_links": [{"source": str(s), "missing_ref": str(t)} for s, t, r in raw_edges if str(s) in node_ids and not str(r)],
            "stats": {"note_count": len(nodes), "edge_count": len(edges), "ontology_edge_count": len(ontology_edges), "domain_count": len(domains)},
        }

    def export_chat_to_wiki(self, title: str, limit: int = 12, tags: Optional[List[str]] = None, actor: str = "companion") -> Dict[str, Any]:
        rows = [(r, c, ts) for r, c, ts in reversed(self.store.get_recent_messages(limit=max(2, min(int(limit), 60)))) if r in ("user", "assistant")]
        if not rows:
            return {"ok": False, "error": "No conversation turns found to export."}
        body_lines = [f"Exported {len(rows)} conversation turn(s) into the Knowledge Wiki Inbox.", ""]
        for role, content, ts in rows:
            body_lines.extend([f"### {'User' if role == 'user' else 'Companion'} ({ts})", content.strip(), ""])
        return self.capture_inbox(title=title or "Chat Session Export", content="\n".join(body_lines), tags=(tags or ["chat-export"]), source="chat-export", actor=actor)


def get_wiki_engine(wiki_dir: Optional[Path] = None, store: Optional[MemoryStore] = None) -> WikiEngine:
    return WikiEngine(wiki_dir=wiki_dir, store=store)
