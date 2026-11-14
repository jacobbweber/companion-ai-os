"""
Headless Education, Socratic Tutoring, Spaced-Repetition & Inline Flashcard Engine
(`companion/education_engine.py` — A-Frame Domain Layer).

Provides deterministic pedagogical state (`Priming -> Dual Coding -> Elaboration -> Retrieval -> Mastered`),
SM-2/Leitner spaced-repetition mastery (`education_mastery`), governed Wiki study-note curation, and
fenced `companion-flashcards` Markdown payloads rendered inline inside Forever Chat.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from companion.memory_store import MemoryStore, get_memory_store
from companion.wiki_engine import get_wiki_engine
from companion.wiki_templates import slugify_wiki

__all__ = [
    "EducationEngine", "ITEM_KINDS", "PEDAGOGY_STAGES", "SRS_INTERVAL_DAYS",
    "execute_education_action", "format_education_hud", "get_education_engine", "parse_education_slash",
]

PEDAGOGY_STAGES: Tuple[str, ...] = ("priming", "dual_coding", "elaboration", "retrieval", "mastered")
SRS_INTERVAL_DAYS: Tuple[int, ...] = (0, 1, 3, 7, 14, 30, 90)
ITEM_KINDS: Tuple[str, ...] = ("flashcard", "quiz")

_COURSE_COLS = [
    "course_id", "title", "domain", "goal_summary", "current_module_idx",
    "syllabus_json", "wiki_hub_path", "active", "created_at", "updated_at",
]
_ITEM_COLS = [
    "item_id", "course_id", "module_slug", "topic", "item_kind", "prompt", "answer",
    "choices_json", "explanation", "srs_box", "ease_factor", "reps", "lapses",
    "last_grade", "last_miss_reason", "due_at", "last_reviewed_at", "created_at",
]

EDUCATION_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS education_courses (course_id TEXT PRIMARY KEY, title TEXT NOT NULL, domain TEXT DEFAULT 'general', goal_summary TEXT DEFAULT '', current_module_idx INTEGER DEFAULT 0, syllabus_json TEXT NOT NULL, wiki_hub_path TEXT DEFAULT '', active INTEGER DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS education_mastery (item_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, module_slug TEXT DEFAULT '', topic TEXT NOT NULL, item_kind TEXT DEFAULT 'flashcard', prompt TEXT NOT NULL, answer TEXT NOT NULL, choices_json TEXT DEFAULT '[]', explanation TEXT DEFAULT '', srs_box INTEGER DEFAULT 0, ease_factor REAL DEFAULT 2.5, reps INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0, last_grade TEXT DEFAULT '', last_miss_reason TEXT DEFAULT '', due_at TEXT NOT NULL, last_reviewed_at TEXT DEFAULT '', created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_edu_mastery_course ON education_mastery(course_id, srs_box)",
    "CREATE INDEX IF NOT EXISTS idx_edu_mastery_due ON education_mastery(due_at)",
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(raw: str, prefix: str = "course") -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")[:56]
    return s or f"{prefix}-{os.urandom(3).hex()}"


def _make_item_id() -> str:
    return f"edu_{_now_utc().strftime('%Y%m%d')}_{os.urandom(3).hex()}"


def _normalize_modules(raw_modules: Any, existing: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    prev_by_slug = {str(m.get("slug") or ""): m for m in (existing or []) if isinstance(m, dict)}
    items = raw_modules if isinstance(raw_modules, list) else ([x.strip() for x in str(raw_modules or "").split(",") if x.strip()] if isinstance(raw_modules, str) else [])
    normalized: List[Dict[str, Any]] = []
    for idx, entry in enumerate(items):
        if isinstance(entry, dict):
            m_title = str(entry.get("title") or entry.get("name") or f"Module {idx + 1}").strip()
            m_slug = _slug(str(entry.get("slug") or m_title), f"mod-{idx + 1}")
            prev = prev_by_slug.get(m_slug, {})
            stage = str(entry.get("stage") or prev.get("stage") or "priming").strip().lower()
            normalized.append({
                "slug": m_slug,
                "title": m_title[:120],
                "objective": str(entry.get("objective") or prev.get("objective") or "").strip()[:280],
                "stage": stage if stage in PEDAGOGY_STAGES else "priming",
                "wiki_path": str(entry.get("wiki_path") or prev.get("wiki_path") or "").strip(),
            })
        elif str(entry).strip():
            m_title = str(entry).strip()[:120]
            m_slug = _slug(m_title, f"mod-{idx + 1}")
            prev = prev_by_slug.get(m_slug, {})
            normalized.append({
                "slug": m_slug,
                "title": m_title,
                "objective": str(prev.get("objective") or ""),
                "stage": str(prev.get("stage") or "priming"),
                "wiki_path": str(prev.get("wiki_path") or ""),
            })
    return normalized or [{"slug": "foundations", "title": "Core Foundations & Mental Models", "objective": "Build big-picture priming map and foundational vocabulary.", "stage": "priming", "wiki_path": ""}]


class EducationEngine:
    """Deterministic SQLite Syllabus, Pedagogical Stage, Spaced Repetition & Study Curation Engine."""

    def __init__(self, store: Optional[MemoryStore] = None):
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()
        with mem._lock:
            for ddl in EDUCATION_DDL:
                mem.conn.execute(ddl)
            mem.conn.commit()
        return mem

    def _hydrate_course(self, row: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
        cid = row["course_id"]
        mods = json.loads(row.pop("syllabus_json") or "[]")
        cur_idx = max(0, min(len(mods) - 1, int(row.get("current_module_idx") or 0))) if mods else 0
        items = self.store._fetch_rows("SELECT srs_box, due_at FROM education_mastery WHERE course_id = ?", (cid,))
        total_items = len(items)
        due_cnt = sum(1 for _, due in items if str(due) <= now_iso)
        box_score = sum(min(1.0, int(box) / 4.0) for box, _ in items) / total_items if total_items else 0.0
        stage_weights = {"priming": 0.1, "dual_coding": 0.35, "elaboration": 0.6, "retrieval": 0.85, "mastered": 1.0}
        mod_score = sum(stage_weights.get(str(m.get("stage") or "priming"), 0.1) for m in mods) / len(mods) if mods else 0.0
        mastery_pct = round(((box_score * 0.6 + mod_score * 0.4) if total_items else mod_score) * 100, 1)
        return {
            **row,
            "active": bool(row.get("active")),
            "current_module_idx": cur_idx,
            "current_module": mods[cur_idx] if mods else None,
            "modules": mods,
            "total_modules": len(mods),
            "mastered_modules": sum(1 for m in mods if m.get("stage") == "mastered"),
            "total_items": total_items,
            "due_count": due_cnt,
            "mastery_pct": mastery_pct,
        }

    def list_courses(self) -> List[Dict[str, Any]]:
        now_s = _fmt_iso()
        rows = self.store._fetch_dicts(_COURSE_COLS, f"SELECT {', '.join(_COURSE_COLS)} FROM education_courses ORDER BY active DESC, updated_at DESC")
        return [self._hydrate_course(r, now_s) for r in rows]

    def get_course(self, course_id: str = "") -> Optional[Dict[str, Any]]:
        courses = self.list_courses()
        if not courses:
            return None
        if course_id.strip():
            target = _slug(course_id)
            return next((c for c in courses if c["course_id"] in (course_id.strip(), target)), None)
        return next((c for c in courses if c["active"]), courses[0])

    def upsert_course(self, title: str, domain: str = "general", goal_summary: str = "", modules: Any = None, course_id: str = "", set_active: bool = True, wiki_hub_path: str = "") -> Dict[str, Any]:
        clean_title = (title or "").strip()
        if not clean_title:
            return {"ok": False, "error": "Course `title` is required."}
        cid = _slug(course_id or clean_title, "course")
        prev = self.get_course(cid)
        norm_mods = _normalize_modules(modules, existing=prev["modules"] if prev else None) if modules is not None or not prev else prev["modules"]
        cur_idx = min(len(norm_mods) - 1, int(prev["current_module_idx"]) if prev else 0)
        dom = slugify_wiki(domain or (prev or {}).get("domain") or "general", "general")
        goal = (goal_summary or (prev or {}).get("goal_summary") or "").strip()[:400]
        hub = (wiki_hub_path or (prev or {}).get("wiki_hub_path") or "").strip()
        now_s = _fmt_iso()
        with self.store._lock:
            if set_active:
                self.store.conn.execute("UPDATE education_courses SET active = 0 WHERE course_id != ?", (cid,))
            self.store.conn.execute(
                """INSERT INTO education_courses (course_id, title, domain, goal_summary, current_module_idx, syllabus_json, wiki_hub_path, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(course_id) DO UPDATE SET title=excluded.title, domain=excluded.domain, goal_summary=excluded.goal_summary,
                   current_module_idx=excluded.current_module_idx, syllabus_json=excluded.syllabus_json, wiki_hub_path=excluded.wiki_hub_path,
                   active=excluded.active, updated_at=excluded.updated_at""",
                (cid, clean_title[:120], dom, goal, cur_idx, json.dumps(norm_mods), hub, 1 if set_active else int((prev or {}).get("active", 1)), (prev or {}).get("created_at") or now_s, now_s),
            )
            self.store.conn.commit()
        course = self.get_course(cid)
        return {"ok": True, "course_id": cid, "course": course, "summary": f"Saved course `{clean_title}` (`{cid}`, {len(norm_mods)} module(s), domain=`{dom}`)."}

    def set_active_course(self, course_id: str) -> Dict[str, Any]:
        target = self.get_course(course_id)
        if not target:
            return {"ok": False, "error": f"Course `{course_id}` not found."}
        now_s = _fmt_iso()
        with self.store._lock:
            self.store.conn.execute("UPDATE education_courses SET active = CASE WHEN course_id = ? THEN 1 ELSE 0 END, updated_at = CASE WHEN course_id = ? THEN ? ELSE updated_at END", (target["course_id"], target["course_id"], now_s))
            self.store.conn.commit()
        return {"ok": True, "course": self.get_course(target["course_id"]), "summary": f"Active study course set to `{target['title']}` (`{target['course_id']}`)."}

    def advance_module_stage(self, course_id: str = "", module_slug: str = "", stage: str = "", next_module: bool = False, wiki_path: str = "") -> Dict[str, Any]:
        course = self.get_course(course_id)
        if not course:
            return {"ok": False, "error": "No matching course found."}
        mods = list(course["modules"])
        idx = next((i for i, m in enumerate(mods) if m["slug"] == _slug(module_slug)), int(course["current_module_idx"])) if module_slug else int(course["current_module_idx"])
        cur_stage = str(mods[idx].get("stage") or "priming")
        if stage:
            st = stage.strip().lower()
            if st not in PEDAGOGY_STAGES:
                return {"ok": False, "error": f"Invalid pedagogical `stage` '{stage}'. Valid: {', '.join(PEDAGOGY_STAGES)}."}
            new_stage = st
        else:
            pos = PEDAGOGY_STAGES.index(cur_stage) if cur_stage in PEDAGOGY_STAGES else 0
            new_stage = PEDAGOGY_STAGES[min(len(PEDAGOGY_STAGES) - 1, pos + 1)]
        mods[idx]["stage"] = new_stage
        if wiki_path.strip():
            mods[idx]["wiki_path"] = wiki_path.strip()
        new_idx = min(len(mods) - 1, idx + 1) if (next_module or (new_stage == "mastered" and idx + 1 < len(mods))) else idx
        now_s = _fmt_iso()
        with self.store._lock:
            self.store.conn.execute("UPDATE education_courses SET syllabus_json = ?, current_module_idx = ?, updated_at = ? WHERE course_id = ?", (json.dumps(mods), new_idx, now_s, course["course_id"]))
            self.store.conn.commit()
        updated = self.get_course(course["course_id"])
        return {"ok": True, "course": updated, "module": mods[idx], "summary": f"Module `{mods[idx]['title']}` -> stage `{new_stage}` (active module index={new_idx + 1}/{len(mods)})."}

    def _hydrate_item(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {**row, "choices": json.loads(row.pop("choices_json") or "[]")}

    def upsert_study_items(self, course_id: str, items: List[Dict[str, Any]], module_slug: str = "", default_topic: str = "") -> Dict[str, Any]:
        course = self.get_course(course_id)
        if not course:
            return {"ok": False, "error": f"Course `{course_id}` not found. Create the course first."}
        if not isinstance(items, list) or not items:
            return {"ok": False, "error": "Provide a non-empty `items` list of flashcard/quiz dicts (`prompt`, `answer`)."}
        cid = course["course_id"]
        eff_mod = _slug(module_slug) if module_slug else str((course.get("current_module") or {}).get("slug") or "")
        existing_by_prompt = {
            str(r[1]).strip().casefold(): str(r[0])
            for r in self.store._fetch_rows("SELECT item_id, prompt FROM education_mastery WHERE course_id = ?", (cid,))
        }
        now_s = _fmt_iso()
        saved_ids: List[str] = []
        with self.store._lock:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                prompt, answer = str(raw.get("prompt") or raw.get("front") or raw.get("question") or "").strip(), str(raw.get("answer") or raw.get("back") or "").strip()
                if not prompt or not answer:
                    continue
                kind = str(raw.get("item_kind") or raw.get("kind") or "flashcard").strip().lower()
                kind = kind if kind in ITEM_KINDS else "flashcard"
                iid = str(raw.get("item_id") or existing_by_prompt.get(prompt.casefold()) or _make_item_id()).strip()
                topic = str(raw.get("topic") or default_topic or eff_mod or course["title"]).strip()[:100]
                choices = [str(c).strip() for c in (raw.get("choices") or []) if str(c).strip()] if isinstance(raw.get("choices"), list) else []
                expl = str(raw.get("explanation") or "").strip()[:600]
                self.store.conn.execute(
                    """INSERT INTO education_mastery (item_id, course_id, module_slug, topic, item_kind, prompt, answer, choices_json, explanation, srs_box, ease_factor, reps, lapses, due_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 2.5, 0, 0, ?, ?)
                       ON CONFLICT(item_id) DO UPDATE SET module_slug=excluded.module_slug, topic=excluded.topic, item_kind=excluded.item_kind,
                       prompt=excluded.prompt, answer=excluded.answer, choices_json=excluded.choices_json, explanation=excluded.explanation""",
                    (iid, cid, eff_mod, topic, kind, prompt[:600], answer[:800], json.dumps(choices), expl, now_s, now_s),
                )
                saved_ids.append(iid)
            self.store.conn.commit()
        return {"ok": True, "course_id": cid, "item_ids": saved_ids, "count": len(saved_ids), "summary": f"Saved {len(saved_ids)} study item(s) into `{course['title']}`."}

    def grade_item(self, item_id: str, passed: bool, miss_reason: str = "", confidence: int = 4, now_dt: Optional[datetime] = None) -> Dict[str, Any]:
        rows = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM education_mastery WHERE item_id = ?", (item_id.strip(),))
        if not rows:
            return {"ok": False, "error": f"Study item `{item_id}` not found."}
        it = rows[0]
        ref = now_dt or _now_utc()
        old_box, old_ef = int(it["srs_box"] or 0), float(it["ease_factor"] or 2.5)
        reps, lapses = int(it["reps"] or 0), int(it["lapses"] or 0)
        if passed:
            jump = 2 if int(confidence) >= 5 and old_box == 0 else 1
            new_box = min(len(SRS_INTERVAL_DAYS) - 1, old_box + jump)
            new_ef = round(min(3.2, max(1.3, old_ef + (0.1 if int(confidence) >= 4 else 0.0))), 2)
            interval_d = max(1, int(round(SRS_INTERVAL_DAYS[new_box] * (new_ef / 2.5)))) if new_box > 1 else max(1, SRS_INTERVAL_DAYS[new_box])
            due_dt, reps, grade_lbl, reason = ref + timedelta(days=interval_d), reps + 1, "pass", ""
        else:
            new_box = 1 if old_box >= 3 else 0
            new_ef = round(max(1.3, old_ef - 0.2), 2)
            interval_d = 0
            due_dt, lapses, grade_lbl, reason = ref, lapses + 1, "miss", (miss_reason or "needs_review").strip()[:240]
        due_s, rev_s = _fmt_iso(due_dt), _fmt_iso(ref)
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE education_mastery SET srs_box=?, ease_factor=?, reps=?, lapses=?, last_grade=?, last_miss_reason=?, due_at=?, last_reviewed_at=? WHERE item_id=?",
                (new_box, new_ef, reps, lapses, grade_lbl, reason, due_s, rev_s, it["item_id"]),
            )
            self.store.conn.commit()
        updated = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM education_mastery WHERE item_id = ?", (it["item_id"],))[0]
        return {
            "ok": True, "item": self._hydrate_item(updated), "srs_box": new_box, "interval_days": interval_d, "due_at": due_s,
            "summary": f"Graded `{it['item_id']}` as `{grade_lbl}` -> SRS box {new_box} (next review: {'today' if interval_d == 0 else f'in {interval_d}d'}).",
        }

    def get_items(self, course_id: str = "", item_ids: Optional[List[str]] = None, limit: int = 50) -> List[Dict[str, Any]]:
        if item_ids:
            clean_ids = [str(x).strip() for x in item_ids if str(x).strip()][:100]
            if not clean_ids:
                return []
            q_marks = ", ".join("?" for _ in clean_ids)
            rows = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM education_mastery WHERE item_id IN ({q_marks})", tuple(clean_ids))
            return [self._hydrate_item(r) for r in rows]
        return self.list_due_items(course_id=course_id, limit=limit, include_upcoming=True)

    def list_due_items(self, course_id: str = "", limit: int = 20, include_upcoming: bool = False) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if course_id.strip():
            clauses.append("course_id = ?")
            params.append(_slug(course_id))
        if not include_upcoming:
            clauses.append("due_at <= ?")
            params.append(_fmt_iso())
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(max(1, int(limit)))
        rows = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM education_mastery {where}ORDER BY srs_box ASC, due_at ASC LIMIT ?", tuple(params))
        return [self._hydrate_item(r) for r in rows]

    def build_inline_flashcard_block(self, course_id: str = "", topic: str = "", item_ids: Optional[List[str]] = None, limit: int = 6) -> Dict[str, Any]:
        """Generate a fenced `companion-flashcards` Markdown block for inline interactive Chat rendering."""
        course = self.get_course(course_id)
        cid = course["course_id"] if course else _slug(course_id)
        if item_ids:
            q_marks = ", ".join("?" for _ in item_ids)
            raw = self.store._fetch_dicts(_ITEM_COLS, f"SELECT {', '.join(_ITEM_COLS)} FROM education_mastery WHERE item_id IN ({q_marks})", tuple(item_ids))
            cards = [self._hydrate_item(r) for r in raw]
        else:
            cards = self.list_due_items(course_id=cid, limit=limit, include_upcoming=False)
            if not cards:
                cards = self.list_due_items(course_id=cid, limit=limit, include_upcoming=True)
        if topic.strip():
            t_low = topic.strip().lower()
            filtered = [c for c in cards if t_low in c["topic"].lower() or t_low in c["prompt"].lower()]
            cards = filtered or cards
        cards = cards[: max(1, min(15, int(limit)))]
        if not cards:
            return {"ok": False, "error": "No flashcards found for this course/topic yet. Add study items first via `upsert_items`."}
        deck = {
            "course_id": cid,
            "deck_title": f"{course['title'] if course else cid} — Active Recall Deck ({len(cards)} card{'s' if len(cards) != 1 else ''})",
            "cards": [
                {
                    "item_id": c["item_id"], "topic": c["topic"], "prompt": c["prompt"],
                    "answer": c["answer"], "explanation": c["explanation"], "srs_box": c["srs_box"],
                }
                for c in cards
            ],
        }
        fenced = f"```companion-flashcards\n{json.dumps(deck, ensure_ascii=False)}\n```"
        return {"ok": True, "course_id": cid, "deck": deck, "markdown_block": fenced, "summary": f"Built inline flashcard deck ({len(cards)} card(s)) for `{cid}`."}

    def curate_study_note(
        self, course_id: str, module_title: str, core_idea: str, mechanism_markdown: str,
        mermaid_diagram: str = "", connections: Optional[List[str]] = None, module_slug: str = "", actor: str = "user",
    ) -> Dict[str, Any]:
        """Curate a Priming + Dual-Coding + Elaboration study note into `02_knowledge/education/<course>/` via WikiEngine."""
        course = self.get_course(course_id)
        if not course:
            return {"ok": False, "error": f"Course `{course_id}` not found."}
        wk = get_wiki_engine(store=self.store)
        c_slug = slugify_wiki(course["course_id"], "study-course")
        m_slug = slugify_wiki(module_slug or module_title, "study-module")
        hub_stem = f"{c_slug}-course-map"
        links = [f"[[{hub_stem}]]", *[f"[[{slugify_wiki(x)}]]" for x in (connections or []) if str(x).strip()]]
        diag_block = f"\n\n```mermaid\n{mermaid_diagram.strip()}\n```\n" if mermaid_diagram.strip() else ""
        body = (
            f"# {module_title.strip()}\n\n"
            f"## Core Idea\n{core_idea.strip()}\n\n"
            f"## Mechanism & Context\n{mechanism_markdown.strip()}{diag_block}\n\n"
            f"## Connections\n- Course & Concept Links: {', '.join(dict.fromkeys(links))}\n"
        )
        summary_txt = f"Curated study guide for {course['title']}: {core_idea.strip()[:140]}"
        if len(summary_txt) < 24:
            summary_txt = f"{summary_txt} — structured priming and dual-coding reference."
        inbox_res = wk.capture_inbox(
            title=module_title.strip(), content=body, tags=["education", c_slug, course["domain"]],
            source="education-engine", slug=f"{c_slug}-{m_slug}", actor=actor, template="zettelkasten-atomic",
            summary=summary_txt, ontology=["knowledge-synthesis"], domain_hint="education",
        )
        if not inbox_res.get("ok"):
            return inbox_res
        grad = wk.graduate_note(
            inbox_path=str(inbox_res["path"]), domain="education", topic=c_slug, new_slug=m_slug,
            actor="user", confirmed=True, template="zettelkasten-atomic", summary=summary_txt, ontology=["knowledge-synthesis"],
        )
        if not grad.get("ok"):
            return grad
        self.advance_module_stage(course["course_id"], module_slug=module_slug or m_slug, stage=str((course.get("current_module") or {}).get("stage") or "dual_coding"), wiki_path=str(grad["path"]))
        return {"ok": True, "path": grad["path"], "course_id": course["course_id"], "summary": f"Curated study note `{grad['path']}` and linked to `{course['title']}`."}

    def get_progress_summary(self) -> Dict[str, Any]:
        courses = self.list_courses()
        active = next((c for c in courses if c["active"]), courses[0] if courses else None)
        due_all = self.list_due_items(limit=100)
        misses = [
            {"item_id": str(r[0]), "topic": str(r[1]), "reason": str(r[2]), "lapses": int(r[3])}
            for r in self.store._fetch_rows("SELECT item_id, topic, last_miss_reason, lapses FROM education_mastery WHERE lapses > 0 ORDER BY lapses DESC, last_reviewed_at DESC LIMIT 5")
        ]
        return {
            "has_courses": bool(courses), "course_count": len(courses), "active_course": active,
            "due_total": len(due_all), "courses": courses, "recent_misses": misses,
        }


def get_education_engine(store: Optional[MemoryStore] = None) -> EducationEngine:
    return EducationEngine(store=store)


def format_education_hud(store: Optional[MemoryStore] = None) -> str:
    """Render a compact Headless Education & Due SRS summary for Companion's System Prompt HUD."""
    prog = get_education_engine(store=store).get_progress_summary()
    lines = ["[EDUCATION & MASTERY ENGINE (CONVERSATIONAL SOCRATIC TUTOR)]"]
    if prog["active_course"]:
        ac = prog["active_course"]
        cm = ac.get("current_module") or {}
        lines.append(
            f"- Active Course: `{ac['title']}` (`{ac['course_id']}`) | Mastery: {ac['mastery_pct']}% | "
            f"Current Module: `{cm.get('title', 'N/A')}` (stage=`{cm.get('stage', 'priming')}`) | Due SRS Reviews: {prog['due_total']}"
        )
        if prog["recent_misses"]:
            miss_s = ", ".join(f"`{m['topic']}` ({m['lapses']}x: {m['reason'] or 'review'})" for m in prog["recent_misses"][:3])
            lines.append(f"- Recent Struggle / Miss Areas: {miss_s}")
    else:
        lines.append("- Active Course: (None yet — use `education_manage` + `socratic-tutoring` skill when teaching or tracking study goals)")
    return "\n".join(lines)


def execute_education_action(p: Dict[str, Any], store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Unified CoreAction handler for `education` (`courses`, `syllabus`, `items`, `grade_item`, `flashcard_deck`, `curate_note`)."""
    eng = get_education_engine(store=store)
    sub = str(p.get("subaction") or p.get("action") or "progress").strip().lower()
    if sub in ("list_courses", "courses"):
        cs = eng.list_courses()
        return {"ok": True, "courses": cs, "summary": f"{len(cs)} course(s) tracked."}
    if sub in ("save_course", "create_course", "upsert_course"):
        return eng.upsert_course(str(p.get("title") or ""), str(p.get("domain") or "general"), str(p.get("goal_summary") or ""), p.get("modules"), str(p.get("course_id") or ""), bool(p.get("set_active", True)), str(p.get("wiki_hub_path") or ""))
    if sub in ("set_active", "active_course"):
        return eng.set_active_course(str(p.get("course_id") or ""))
    if sub in ("advance_stage", "update_module"):
        return eng.advance_module_stage(str(p.get("course_id") or ""), str(p.get("module_slug") or ""), str(p.get("stage") or ""), bool(p.get("next_module", False)), str(p.get("wiki_path") or ""))
    if sub in ("upsert_items", "add_items", "save_items"):
        items = p.get("items") if isinstance(p.get("items"), list) else ([{"prompt": p.get("prompt"), "answer": p.get("answer"), "topic": p.get("topic", ""), "item_kind": p.get("item_kind", "flashcard"), "explanation": p.get("explanation", "")}] if p.get("prompt") else [])
        return eng.upsert_study_items(str(p.get("course_id") or ""), items, str(p.get("module_slug") or ""), str(p.get("topic") or ""))
    if sub in ("grade_item", "grade"):
        passed = p.get("passed") if isinstance(p.get("passed"), bool) else str(p.get("grade") or p.get("passed") or "pass").strip().lower() in ("pass", "true", "1", "got_it", "correct")
        return eng.grade_item(str(p.get("item_id") or ""), passed=passed, miss_reason=str(p.get("miss_reason") or ""), confidence=int(p.get("confidence") or (4 if passed else 2)))
    if sub in ("get_items", "list_items", "items"):
        items = eng.get_items(str(p.get("course_id") or ""), p.get("item_ids") if isinstance(p.get("item_ids"), list) else None, int(p.get("limit") or 50))
        return {"ok": True, "items": items, "count": len(items), "summary": f"{len(items)} study item(s) loaded."}
    if sub in ("due", "list_due"):
        items = eng.list_due_items(str(p.get("course_id") or ""), int(p.get("limit") or 20), bool(p.get("include_upcoming", False)))
        return {"ok": True, "items": items, "count": len(items), "summary": f"{len(items)} study item(s) due for review."}
    if sub in ("flashcard_deck", "flashcards", "deck"):
        res = eng.build_inline_flashcard_block(str(p.get("course_id") or ""), str(p.get("topic") or ""), p.get("item_ids") if isinstance(p.get("item_ids"), list) else None, int(p.get("limit") or 6))
        if res.get("ok") and res.get("markdown_block") and bool(p.get("persist_to_chat", False)):
            chat_text = f"**{res['deck']['deck_title']}**\n\n{res['markdown_block']}"
            msg_id = eng.store.log_message("assistant", chat_text, session_tag="study_deck")
            res["chat_message"] = {"id": msg_id, "role": "assistant", "content": chat_text, "timestamp": _fmt_iso(), "session_tag": "study_deck"}
        return res
    if sub in ("curate_note", "save_study_note"):
        return eng.curate_study_note(str(p.get("course_id") or ""), str(p.get("module_title") or p.get("title") or ""), str(p.get("core_idea") or ""), str(p.get("mechanism_markdown") or p.get("content") or ""), str(p.get("mermaid_diagram") or ""), p.get("connections") if isinstance(p.get("connections"), list) else None, str(p.get("module_slug") or ""), str(p.get("actor") or "user"))
    prog = eng.get_progress_summary()
    ac = prog["active_course"]
    return {
        "ok": True, **prog,
        "summary": f"Active course: `{ac['title']}` ({ac['mastery_pct']}% mastery, {prog['due_total']} due)" if ac else f"No active courses yet ({prog['due_total']} due items).",
    }


def parse_education_slash(parts: List[str]) -> Dict[str, Any]:
    """Parse `/study` CLI/REPL slash arguments into `execute_education_action` params."""
    sub, rest = (parts[0].lower() if parts else "progress"), parts[1:]
    if sub in ("courses", "list"):
        return {"subaction": "list_courses"}
    if sub == "due":
        return {"subaction": "due", "course_id": rest[0] if rest else ""}
    if sub in ("flashcards", "deck"):
        return {"subaction": "flashcard_deck", "course_id": rest[0] if rest else ""}
    if sub == "active" and rest:
        return {"subaction": "set_active", "course_id": " ".join(rest)}
    if sub == "new" and rest:
        return {"subaction": "save_course", "title": " ".join(rest)}
    return {"subaction": "progress"}
