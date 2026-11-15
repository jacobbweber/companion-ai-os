"""
Epistemic Grounding, External `KnowledgePort` Adapter & `ExecutionTarget` Sandbox Seam
(`companion/grounding.py` — A-Frame Domain Layer).

Provides:
1. `KnowledgePort` protocol + `LocalKnowledgeAdapter` (backed by SQLite `knowledge_facts`,
   system lessons, known workspaces, and read-only reference docs; ready for external Wiki adapters).
2. `<10ms` deterministic `build_epistemic_grounding` surfacing `[KNOWN FACTS]` vs `[UNVERIFIED GAPS]`
   and Top-K procedural skill ranking without prompt bloat.
3. Domain-neutral `build_workspace_radar` covering both Python AST headroom and non-Python artifacts.
4. `ExecutionTarget` protocol + `LocalWorkspaceTarget` isolating external `PYTHONPATH` and subprocesses.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

from companion.checkpoints import CheckpointManager, get_active_checkpoint_manager, get_self_checkpoint_manager
from companion.config import SELF_PROJECT_ID, SKIP_DIRS, settings
from companion.disciplines import GENERAL_TRACKED_EXTENSIONS, analyze_python_source
from companion.hygiene import build_repo_headroom_map
from companion.memory_store import MemoryStore, get_memory_store
from companion.semantic_memory import expand_semantic_terms, semantic_similarity
from companion.wiki_engine import get_wiki_engine

__all__ = [
    "EpistemicGrounding",
    "ExecutionTarget",
    "KnowledgeEntry",
    "KnowledgePort",
    "LocalKnowledgeAdapter",
    "LocalWorkspaceTarget",
    "build_epistemic_grounding",
    "build_workspace_radar",
    "discover_skills",
    "get_execution_target",
    "get_knowledge_port",
    "rank_relevant_skills",
    "register_execution_target",
    "register_knowledge_adapter",
]

_STOP_WORDS = frozenset({
    "a", "about", "add", "after", "all", "also", "an", "and", "any", "are", "as", "at", "be", "before",
    "between", "build", "by", "can", "change", "check", "clean", "code", "companion", "complete", "create",
    "current", "do", "does", "doing", "edit", "end", "ensure", "evaluate", "explain", "fast", "feature",
    "file", "files", "fix", "folder", "for", "from", "full", "function", "get", "go", "goal", "good",
    "have", "help", "hey", "how", "i", "if", "implement", "in", "inspect", "into", "is", "it", "its",
    "just", "keep", "let", "list", "make", "me", "method", "mode", "module", "more", "my", "need", "new",
    "no", "not", "now", "of", "on", "one", "only", "open", "options", "or", "our", "out", "over", "patch",
    "plan", "please", "project", "python", "quick", "read", "refactor", "rename", "review", "run", "same",
    "see", "set", "should", "show", "so", "some", "start", "status", "step", "steps", "summary", "system",
    "task", "tasks", "tell", "test", "tests", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "to", "today", "tool", "tools", "tradeoffs", "turn", "unit", "up", "update", "upgrade",
    "use", "user", "using", "verify", "want", "we", "what", "when", "where", "which", "while", "with",
    "work", "workflow", "workspace", "write", "you", "your",
})


@dataclass(frozen=True)
class KnowledgeEntry:
    """Structured fact or reference record returned by a `KnowledgePort` implementation."""

    key: str
    title: str
    summary: str
    source: str = "local"
    confidence: float = 1.0
    content: str = ""


class KnowledgePort(Protocol):
    """Domain-layer port decoupling Companion's reasoning from any external Wiki or knowledge backend."""

    def search(self, query: str, project_id: Optional[str] = None, limit: int = 6) -> List[KnowledgeEntry]: ...
    def read_entry(self, key: str, project_id: Optional[str] = None) -> Optional[KnowledgeEntry]: ...
    def list_index(self, project_id: Optional[str] = None, limit: int = 20) -> List[KnowledgeEntry]: ...
    def propose_update(self, key: str, value: str, project_id: Optional[str] = None, source: str = "agent", confidence: float = 1.0) -> str: ...


def _doc_heading(txt: str, fallback: str) -> str:
    """Extract the first meaningful title line from markdown, skipping YAML frontmatter."""
    body = txt.replace("\r\n", "\n")
    if body.startswith("---\n"):
        end_idx = body.find("\n---\n", 4)
        if end_idx != -1:
            body = body[end_idx + 5 :]
    return next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("---")), fallback)


class LocalKnowledgeAdapter:
    """Default zero-dependency `KnowledgePort` backed by SQLite `knowledge_facts`, lessons, and local docs."""

    def __init__(self, store: Optional[MemoryStore] = None, docs_dir: Optional[Path] = None):
        self._store = store
        self._docs_dir = docs_dir

    @property
    def store(self) -> MemoryStore:
        return self._store or get_memory_store()

    def _ref_dir(self) -> Path:
        return self._docs_dir or (settings.root / "skills" / "knowledge-wiki" / "references")

    def _search_notes_and_projects(self, tokens: List[str], limit_left: int) -> List[KnowledgeEntry]:
        if not tokens or limit_left <= 0:
            return []
        extra: List[KnowledgeEntry] = []
        for idx, (note, cat, _ts) in enumerate(self.store.get_system_notes(limit=25), 1):
            hay = f"{cat} {note}".lower()
            if any(tok in hay for tok in tokens):
                extra.append(KnowledgeEntry(f"note:{idx}", f"note:{cat}", note[:240], "system-note", 0.9, note))
                if len(extra) >= limit_left:
                    return extra
        raw_kp = self.store.get_user_profile().get("known_projects_json", "{}")
        try:
            kp_map = json.loads(raw_kp) if isinstance(raw_kp, str) else {}
        except Exception:
            kp_map = {}
        if isinstance(kp_map, dict):
            for pid, ppath in sorted(kp_map.items()):
                hay = f"{pid} {ppath}".lower()
                if any(tok in hay for tok in tokens):
                    extra.append(KnowledgeEntry(f"project:{pid}", str(pid), str(ppath)[:240], "known-project", 0.95, str(ppath)))
                    if len(extra) >= limit_left:
                        return extra
        return extra

    def search(self, query: str, project_id: Optional[str] = None, limit: int = 6) -> List[KnowledgeEntry]:
        q_clean = (query or "").strip()
        tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_.-]{3,}", q_clean) if t.lower() not in _STOP_WORDS]
        expanded = set(tokens) | (expand_semantic_terms(q_clean) if tokens else set())
        entries: List[KnowledgeEntry] = []
        seen: set[str] = set()
        facts = self.store.query_knowledge_facts(query="", project_id=project_id, limit=max(limit * 6, 40))
        scored_facts: List[Tuple[float, Dict[str, Any]]] = []
        for f in facts:
            hay = f"{f['key']} {f['value']}".lower()
            lex_hit = (not tokens) or any(tok in hay for tok in tokens)
            sem_sim = semantic_similarity(q_clean, hay) if tokens else 0.0
            if lex_hit or sem_sim >= 0.14 or any(et in hay for et in expanded):
                score = (1.0 if lex_hit else 0.0) + sem_sim + float(f["confidence"]) * 0.1
                scored_facts.append((score, f))
        scored_facts.sort(key=lambda item: item[0], reverse=True)
        for _sc, f in scored_facts:
            if str(f["key"]) not in seen:
                seen.add(str(f["key"]))
                entries.append(KnowledgeEntry(str(f["key"]), str(f["key"]), str(f["value"])[:260], str(f["source"]), float(f["confidence"]), str(f["value"])))
                if len(entries) >= limit:
                    return entries
        for item in self._search_notes_and_projects(list(expanded), limit - len(entries)):
            if item.key not in seen:
                seen.add(item.key)
                entries.append(item)
                if len(entries) >= limit:
                    return entries
        if tokens and len(entries) < limit:
            for wn in get_wiki_engine(store=self.store).search_notes(q_clean, limit=limit - len(entries)):
                wkey = f"wiki:{wn['rel_path']}"
                if wkey not in seen:
                    seen.add(wkey)
                    entries.append(KnowledgeEntry(wkey, wn["title"][:80], wn["excerpt"][:220], f"wiki:{wn['author']}", 0.92, wn["excerpt"]))
        ref_dir = self._ref_dir()
        if ref_dir.is_dir() and tokens and len(entries) < limit:
            for md_file in sorted(ref_dir.glob("*.md")):
                if md_file.name == "INDEX.md":
                    continue
                txt = md_file.read_text(encoding="utf-8", errors="replace")
                if any(tok in txt.lower() or tok in md_file.stem.lower() for tok in expanded):
                    hd = _doc_heading(txt, md_file.stem)
                    entries.append(KnowledgeEntry(f"doc:{md_file.stem}", hd[:80], hd[:220], "reference-doc", 0.85, txt[:2000]))
                    if len(entries) >= limit:
                        break
        return entries

    def read_entry(self, key: str, project_id: Optional[str] = None) -> Optional[KnowledgeEntry]:
        k = (key or "").strip()
        if not k:
            return None
        if k.startswith("wiki:"):
            wn = get_wiki_engine(store=self.store).read_note(k[5:])
            return KnowledgeEntry(f"wiki:{wn['path']}", wn["title"], wn["body"][:240], f"wiki:{wn['author']}", 0.92, wn["raw"]) if wn.get("ok") else None
        if k.startswith("doc:"):
            raw_stem = k[4:].strip()
            ref_dir = self._ref_dir().resolve()
            p = (ref_dir / f"{Path(raw_stem).name}.md").resolve()
            if raw_stem and Path(raw_stem).name == raw_stem and p.parent == ref_dir and p.is_file():
                txt = p.read_text(encoding="utf-8", errors="replace")
                hd = _doc_heading(txt, p.stem)
                return KnowledgeEntry(k, p.stem, hd[:180], "reference-doc", 0.85, txt)
            return None
        if k.startswith("note:") and k[5:].isdigit():
            idx = int(k[5:]) - 1
            notes = self.store.get_system_notes(limit=max(25, idx + 1))
            if 0 <= idx < len(notes):
                note, cat, _ts = notes[idx]
                return KnowledgeEntry(k, f"note:{cat}", note[:240], "system-note", 0.9, note)
            return None
        if k.startswith("project:"):
            pid_target = k[8:].strip()
            raw_kp = self.store.get_user_profile().get("known_projects_json", "{}")
            kp_map = json.loads(raw_kp) if isinstance(raw_kp, str) and raw_kp.strip().startswith("{") else {}
            if isinstance(kp_map, dict) and pid_target in kp_map:
                ppath = str(kp_map[pid_target])
                return KnowledgeEntry(k, pid_target, ppath[:240], "known-project", 0.95, ppath)
            return None
        for f in self.store.query_knowledge_facts(query=k, project_id=project_id, limit=10):
            if str(f["key"]).lower() == k.lower():
                return KnowledgeEntry(str(f["key"]), str(f["key"]), str(f["value"])[:260], str(f["source"]), float(f["confidence"]), str(f["value"]))
        return None

    def list_index(self, project_id: Optional[str] = None, limit: int = 20) -> List[KnowledgeEntry]:
        rows = self.store.query_knowledge_facts(query="", project_id=project_id, limit=limit)
        return [KnowledgeEntry(str(r["key"]), str(r["key"]), str(r["value"])[:220], str(r["source"]), float(r["confidence"]), str(r["value"])) for r in rows]

    def propose_update(self, key: str, value: str, project_id: Optional[str] = None, source: str = "agent", confidence: float = 1.0) -> str:
        if not (key or "").strip() or not (value or "").strip():
            return "error: key and value are required for knowledge fact update"
        self.store.upsert_knowledge_fact(key=key.strip(), value=value.strip(), project_id=project_id, source=source, confidence=confidence)
        return f"Recorded grounded fact `{key.strip()}` (source={source}, confidence={confidence:.2f})."


_CUSTOM_KNOWLEDGE_ADAPTER: Optional[KnowledgePort] = None


def register_knowledge_adapter(adapter: Optional[KnowledgePort]) -> None:
    """Register or clear an external `KnowledgePort` adapter (e.g. external Wiki bridge)."""
    global _CUSTOM_KNOWLEDGE_ADAPTER
    _CUSTOM_KNOWLEDGE_ADAPTER = adapter


def get_knowledge_port(store: Optional[MemoryStore] = None) -> KnowledgePort:
    """Return the registered external `KnowledgePort` adapter or a `LocalKnowledgeAdapter`."""
    if _CUSTOM_KNOWLEDGE_ADAPTER is not None:
        return _CUSTOM_KNOWLEDGE_ADAPTER
    return LocalKnowledgeAdapter(store=store)


_SKILLS_CACHE: Dict[Tuple[str, int, int, int], List[Dict[str, str]]] = {}


def invalidate_skills_cache() -> None:
    """Clear the cached `discover_skills` metadata table after skill promotion or deletion."""
    _SKILLS_CACHE.clear()


def discover_skills(root: Optional[Path] = None) -> List[Dict[str, str]]:
    """Scan built-in `skills/*/SKILL.md` plus user `data/skills/*/SKILL.md` and return mtime-cached metadata."""
    target_root = (root or settings.root).resolve()
    dirs = [target_root / "skills", *([settings.user_skills_dir.resolve()] if root is None or target_root == settings.root.resolve() else [])]
    skill_map: Dict[str, Path] = {}
    for d in dirs:
        if d.is_dir():
            for smd in sorted(d.glob("*/SKILL.md")):
                skill_map[smd.parent.name] = smd
    if not skill_map:
        return []
    skill_mds = [skill_map[k] for k in sorted(skill_map)]
    tier3_assets = [a for smd in skill_mds for a in (*smd.parent.glob("scripts/*.py"), *smd.parent.glob("dags/*.py"), *smd.parent.glob("references/*.md"))]
    max_ns = max((int(p.stat().st_mtime_ns) for p in (*skill_mds, *tier3_assets)), default=0)
    ck = (f"{target_root}::{dirs[-1]}", len(skill_mds), len(tier3_assets), max_ns)
    if ck in _SKILLS_CACHE:
        return [dict(item) for item in _SKILLS_CACHE[ck]]
    skills: List[Dict[str, str]] = []
    for skill_md in skill_mds:
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        except Exception:
            continue
        sdir = skill_md.parent
        refs = [p.name for p in sorted((sdir / "references").glob("*.md"))] if (sdir / "references").is_dir() else []
        dags = [p.name for p in sorted((sdir / "dags").glob("*.py"))] if (sdir / "dags").is_dir() else []
        scripts = [p.name for p in sorted((sdir / "scripts").glob("*.py"))] if (sdir / "scripts").is_dir() else []
        rel_p = str(skill_md.relative_to(target_root)).replace("\\", "/") if skill_md.is_relative_to(target_root) else str(skill_md)
        scope = "core" if sdir.parent.resolve() == (target_root / "skills").resolve() else "user"
        meta: Dict[str, str] = {
            "name": sdir.name, "description": "Operational runbook skill.", "required_tools": "",
            "path": rel_p, "scope": scope, "references": ", ".join(refs),
            "dags": ", ".join(dags), "scripts": ", ".join(scripts),
        }
        if text.startswith("---\n"):
            end_idx = text.find("\n---\n", 4)
            if end_idx != -1:
                cur_k = ""
                for line in text[4:end_idx].splitlines():
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if (line.startswith(" ") or line.startswith("\t")) and cur_k in ("name", "description", "required_tools"):
                        item = s[1:].strip().strip("'\"") if s.startswith("-") else s.strip("'\"")
                        meta[cur_k] = f"{meta[cur_k]}, {item}" if (meta[cur_k] and s.startswith("-")) else f"{meta[cur_k]} {item}".strip()
                    elif ":" in line:
                        k, _, v = line.partition(":")
                        cur_k, val = k.strip().lower(), v.strip().strip("'\"")
                        if cur_k in ("name", "description", "required_tools"):
                            meta[cur_k] = "" if val in (">", "|", ">-", "|-") else (val or meta[cur_k])
        skills.append(meta)
    if len(_SKILLS_CACHE) > 8:
        _SKILLS_CACHE.clear()
    _SKILLS_CACHE[ck] = skills
    return [dict(item) for item in skills]


def rank_relevant_skills(query: str, skills: List[Dict[str, str]], top_k: int = 4) -> List[Dict[str, str]]:
    """Deterministically rank procedural skills against `query` by token overlap."""
    tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9_-]{3,}", query or "") if t.lower() not in _STOP_WORDS}
    if not tokens:
        return skills[:top_k]
    scored: List[Tuple[int, str, Dict[str, str]]] = []
    for s in skills:
        hay = f"{s.get('name', '')} {s.get('description', '')} {s.get('dags', '')} {s.get('scripts', '')}".lower()
        hits = sum(2 if tok in s.get("name", "").lower() else (1 if tok in hay else 0) for tok in tokens)
        if hits > 0:
            scored.append((hits, s.get("name", ""), s))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:top_k]]


def _walk_workspace_files(target_root: Path, limit: int = 0) -> List[Path]:
    """Walk `target_root` while pruning `SKIP_DIRS` in-place so `.git`/`.companion_backups` stay O(1)."""
    out: List[Path] = []
    if not target_root.is_dir():
        return out
    for cur_root, dirs, files in os.walk(target_root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        base = Path(cur_root)
        for fn in sorted(files):
            out.append(base / fn)
            if limit and len(out) >= limit:
                return out
    return out


@dataclass(frozen=True)
class EpistemicGrounding:
    """Pre-turn epistemic snapshot separating verified facts/files from unverified gaps."""

    known_facts: List[KnowledgeEntry] = field(default_factory=list)
    matched_files: List[str] = field(default_factory=list)
    unknown_terms: List[str] = field(default_factory=list)
    top_skills: List[Dict[str, str]] = field(default_factory=list)
    prompt_block: str = ""


def build_epistemic_grounding(
    query: str,
    store: Optional[MemoryStore] = None,
    root: Optional[Path] = None,
    skills: Optional[List[Dict[str, str]]] = None,
    top_k: int = 4,
) -> EpistemicGrounding:
    """Build a `<10ms` deterministic epistemic grounding block for the active turn."""
    clean_q = (query or "").strip()
    target_root = (root or settings.active_project_root).resolve()
    pid = SELF_PROJECT_ID if target_root == settings.root.resolve() else settings.get_workspace().project_id
    port = get_knowledge_port(store=store)
    all_skills = skills if skills is not None else discover_skills(settings.root)
    if not clean_q or clean_q == "turn" or clean_q.startswith(("/", ".")):
        base_facts = port.list_index(project_id=pid, limit=5)
        if not base_facts:
            return EpistemicGrounding(top_skills=all_skills[:top_k])
        fact_lns = "\n".join(f"  - `{f.key}`: {f.summary}" for f in base_facts)
        return EpistemicGrounding(known_facts=base_facts, top_skills=all_skills[:top_k], prompt_block=f"\n[KNOWN PROJECT FACTS]\n{fact_lns}\n")

    raw_terms = re.findall(r"[A-Za-z][A-Za-z0-9_./-]{2,}", clean_q)
    salient: List[str] = []
    for t in raw_terms:
        low = t.lower().strip("./-")
        if len(low) >= 3 and low not in _STOP_WORDS and low not in salient:
            salient.append(low)

    facts = port.search(clean_q, project_id=pid, limit=6)
    ws_files = [str(p.relative_to(target_root)).replace("\\", "/") for p in _walk_workspace_files(target_root, limit=120)]

    matched_files = [f for f in ws_files if any(tok in f.lower() for tok in salient[:10])][:6]
    known_blob = " ".join([f.key.lower() + " " + f.summary.lower() for f in facts] + [mf.lower() for mf in matched_files])
    unknowns = [
        t for t in raw_terms
        if t.lower().strip("./-") in salient
        and (t[0].isupper() or any(ch in t for ch in ("_", "-", "/", ".")) or len(t) >= 8)
        and t.lower().strip("./-") not in known_blob
    ][:5]
    ranked_skills = rank_relevant_skills(clean_q, all_skills, top_k=top_k)

    lines = ["", "[EPISTEMIC GROUNDING — KNOWN CONTEXT vs UNVERIFIED GAPS]"]
    lines.append("  Known Facts: " + ("; ".join(f"`{f.key}`={f.summary[:100]}" for f in facts) if facts else "(none recorded for this query yet)"))
    if matched_files:
        lines.append("  Matched Workspace Files: " + ", ".join(f"`{m}`" for m in matched_files))
    if unknowns:
        lines.append("  Unverified / Unknown Terms: " + ", ".join(f"`{u}`" for u in unknowns) + " -> Do NOT guess parameters, paths, or values; inspect via tools or align with the user.")
    if ranked_skills:
        lines.append("  Top Relevant Runbooks: " + ", ".join(f"`{s['name']}`" for s in ranked_skills))
    return EpistemicGrounding(facts, matched_files, unknowns, ranked_skills, "\n".join(lines) + "\n")


def build_workspace_radar(root: Optional[Path] = None, max_files: int = 30) -> str:
    """Render Python AST line-budget radar plus a domain-neutral inventory of non-Python workspace files."""
    target_root = (root or settings.active_project_root).resolve()
    py_radar = build_repo_headroom_map(target_root, max_files=max_files)
    if (target_root / "companion").is_dir() and "(No Python modules" not in py_radar:
        return py_radar
    other_files = [
        str(p.relative_to(target_root)).replace("\\", "/")
        for p in _walk_workspace_files(target_root)
        if p.suffix.lower() in GENERAL_TRACKED_EXTENSIONS and p.suffix.lower() != ".py"
    ]
    if "(No Python modules" in py_radar and other_files:
        shown = ", ".join(f"`{f}`" for f in other_files[:18])
        more = f" (+{len(other_files) - 18} more)" if len(other_files) > 18 else ""
        return f"  Workspace Artifacts ({len(other_files)} non-Python file(s)): {shown}{more}"
    if other_files:
        shown = ", ".join(f"`{f}`" for f in other_files[:10])
        return f"{py_radar}\n  Non-Python Artifacts ({len(other_files)}): {shown}"
    return py_radar


class ExecutionTarget(Protocol):
    """Domain-layer execution seam enabling local workspaces today and isolated sandboxes/endpoints tomorrow."""

    @property
    def project_root(self) -> Path: ...
    @property
    def is_self_project(self) -> bool: ...
    def exec_env_and_cwd(self) -> Tuple[Dict[str, str], str]: ...
    def read_text(self, path: Path) -> str: ...
    def write_text(self, path: Path, content: str, backup: bool = True) -> None: ...
    def list_dir(self, path: Path) -> List[str]: ...
    def exec_command(
        self,
        cmd_or_args: Any,
        shell: bool = False,
        timeout: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None,
        cwd_override: Optional[str] = None,
    ) -> Tuple[int, str, str, Optional[str]]: ...
    def run_python_code(self, code: str, timeout: Optional[int] = None) -> str: ...
    def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]: ...
    def list_checkpoints(self) -> List[Dict[str, Any]]: ...
    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]: ...


class LocalWorkspaceTarget:
    """Default `ExecutionTarget` executing inside the active `WorkspaceContext` with strict `PYTHONPATH` isolation."""

    @property
    def project_root(self) -> Path:
        return settings.get_workspace().project_root

    @property
    def is_self_project(self) -> bool:
        return settings.get_workspace().is_self_project

    def _cp_mgr(self) -> CheckpointManager:
        return get_active_checkpoint_manager()

    def exec_env_and_cwd(self) -> Tuple[Dict[str, str], str]:
        ws = settings.get_workspace()
        return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONPATH": str(ws.project_root)}, str(ws.project_root)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: Path, content: str, backup: bool = True) -> None:
        if backup and path.is_file():
            self._cp_mgr().backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")

    def list_dir(self, path: Path) -> List[str]:
        return [e.name + "/" if e.is_dir() else e.name for e in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))]

    def _snapshot_priv_db(self, store: MemoryStore) -> Tuple[str, str, List[Tuple[str, str, str, str]]]:
        prof = store.get_user_profile()
        approvals: List[Tuple[str, str, str, str]] = []
        with store._lock:
            has_apr = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_requests'").fetchone()
            if has_apr:
                approvals = [(str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in store.conn.execute("SELECT approval_id, status, decided_by, decided_at FROM approval_requests").fetchall()]
        return prof.get("self_dev_mode", "off"), prof.get("trust_mode", "balanced"), approvals

    def _guard_self_dev_db(self, store: MemoryStore, pre_state: Tuple[str, str, List[Tuple[str, str, str, str]]]) -> Optional[str]:
        pre_sd, pre_tm, pre_apr = pre_state
        prof = store.get_user_profile()
        violated: List[str] = []
        with store._lock:
            if prof.get("self_dev_mode", "off") != pre_sd:
                store.conn.execute("UPDATE user_profile SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'self_dev_mode'", (pre_sd,))
                violated.append("self_dev_mode")
            if prof.get("trust_mode", "balanced") != pre_tm:
                store.conn.execute("UPDATE user_profile SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = 'trust_mode'", (pre_tm,))
                violated.append("trust_mode")
            has_apr = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_requests'").fetchone()
            if has_apr:
                cur_apr = [(str(r[0]), str(r[1]), str(r[2] or ""), str(r[3] or "")) for r in store.conn.execute("SELECT approval_id, status, decided_by, decided_at FROM approval_requests").fetchall()]
                if cur_apr != pre_apr:
                    pre_ids = {r[0] for r in pre_apr}
                    store.conn.execute(f"DELETE FROM approval_requests WHERE approval_id NOT IN ({','.join('?' for _ in pre_ids)})" if pre_ids else "DELETE FROM approval_requests", tuple(pre_ids))
                    for aid, st, db_by, d_at in pre_apr:
                        store.conn.execute("UPDATE approval_requests SET status = ?, decided_by = ?, decided_at = ? WHERE approval_id = ?", (st, db_by, d_at or None, aid))
                    violated.append("approval_requests")
            if violated:
                store.conn.commit()
        if not violated:
            return None
        return f"SAFETY GUARD REJECTED subprocess privilege escalation: direct SQLite modification of `{', '.join(violated)}` was blocked and reverted (`ADR-019`)."

    def exec_command(
        self, cmd_or_args: Any, shell: bool = False, timeout: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None, cwd_override: Optional[str] = None,
    ) -> Tuple[int, str, str, Optional[str]]:
        active_cp, self_cp, store = self._cp_mgr(), get_self_checkpoint_manager(), get_memory_store()
        pre_snap = active_cp.snapshot_py_files()
        pre_priv = self._snapshot_priv_db(store)
        ro_snap = self_cp.snapshot_readonly_tree() if ((not self.is_self_project) or (not settings.is_self_dev_enabled())) else None
        env, cwd = self.exec_env_and_cwd()
        if extra_env:
            env = {**env, **extra_env}
        eff_timeout = timeout or settings.shell_timeout
        try:
            r = subprocess.run(
                cmd_or_args, shell=shell, cwd=cwd_override or cwd, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=eff_timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            active_cp.guard_py_files_after_exec(pre_snap)
            self._guard_self_dev_db(store, pre_priv)
            if ro_snap is not None:
                self_cp.enforce_readonly_tree(ro_snap)
            return -1, "", "", f"timed out after {eff_timeout}s"
        guard_err = self._guard_self_dev_db(store, pre_priv) or active_cp.guard_py_files_after_exec(pre_snap) or (
            self_cp.enforce_readonly_tree(ro_snap) if ro_snap is not None else None
        )
        return r.returncode, r.stdout or "", r.stderr or "", guard_err

    def run_python_code(self, code: str, timeout: Optional[int] = None) -> str:
        if not (code or "").strip():
            return "error: empty Python code"
        syn = analyze_python_source(code, filename="<run_python>")
        if not syn.valid_syntax:
            return f"error: Python syntax error in code: {syn.syntax_error}"
        rc, stdout, stderr, guard_err = self.exec_command([sys.executable, "-c", code], shell=False, timeout=timeout)
        if rc == -1 and guard_err:
            return f"error: Python execution {guard_err}"
        if guard_err:
            return f"exit=1\n{guard_err}"
        out = (stdout + (("\n[stderr]\n" + stderr) if stderr else "")).strip() or "(no output)"
        return f"exit={rc}\n{out[: settings.out_limit] + chr(10) + '... [truncated]' if len(out) > settings.out_limit else out}"

    def create_checkpoint(self, label: str = "auto") -> Dict[str, Any]:
        return self._cp_mgr().create_checkpoint(label=label)

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        return self._cp_mgr().list_checkpoints()

    def rollback_checkpoint(self, checkpoint_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        return self._cp_mgr().rollback_checkpoint(checkpoint_id=checkpoint_id, reason=reason)


_CUSTOM_EXECUTION_TARGET: Optional[ExecutionTarget] = None
_DEFAULT_LOCAL_TARGET = LocalWorkspaceTarget()


def register_execution_target(target: Optional[ExecutionTarget]) -> None:
    """Register or clear a custom `ExecutionTarget` (e.g. container/remote sandbox manager)."""
    global _CUSTOM_EXECUTION_TARGET
    _CUSTOM_EXECUTION_TARGET = target


def get_execution_target() -> ExecutionTarget:
    """Return the active `ExecutionTarget` (`LocalWorkspaceTarget` by default)."""
    return _CUSTOM_EXECUTION_TARGET if _CUSTOM_EXECUTION_TARGET is not None else _DEFAULT_LOCAL_TARGET
