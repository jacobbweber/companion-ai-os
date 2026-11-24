"""
Layered Context Assembler, Progressive Skill & Domain-Pack Disclosure, and LLM Semantic Compactor
(`companion/context.py` — Frontier Primitives 1 & 5).

Unifies:
1. Identity & Soul (`IDENTITY.md`), ICM Root Front Door (`data/CONTEXT.md`), and Multi-Project `WorkspaceContext`.
2. 3-Tier Progressive Disclosure over Runtime `skills/`, User `data/skills/`, and Domain Capability Packs (`data/domains/<pack>/CONTEXT.md`) with 1-line `name — description (path)` L1 indexing.
3. Pluggable `KnowledgePort` (`LocalKnowledgeAdapter`) and `<10ms` epistemic grounding.
4. Real `fast`-tier LLM Stage-2 Semantic Compaction bounded deterministically by `KeyTokens` verification, plus durable fact distillation into `knowledge_facts` and `MEMORY.md`.
"""

import json
import os
import platform
import re
import socket
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from companion.approvals import get_approval_queue, get_trust_mode
from companion.config import (
    GENERAL_TRACKED_EXTENSIONS, MODEL_TIERS, SELF_PROJECT_ID, SKIP_DIRS,
    settings, slugify_project_id, walk_pruned_files,
)
from companion.education_engine import format_education_hud
from companion.llm import chat, format_model_tiers_status, tier_context
from companion.memory_store import (
    MemoryStore, expand_semantic_terms, get_memory_store, semantic_similarity,
)
from companion.project_graph import format_project_graph_hud, get_project_graph
from companion.telemetry import telemetry
from companion.wiki_engine import get_wiki_engine

__all__ = [
    "DEFAULT_SOUL_AND_USER", "EpistemicGrounding", "KeyTokens", "KnowledgeEntry", "KnowledgePort",
    "LocalKnowledgeAdapter", "VALID_TONES", "bootstrap_identity", "build_epistemic_grounding",
    "build_lossless_phase_handoff", "build_system_prompt", "build_turn_context", "build_workspace_radar",
    "classify_task_autoskip", "compact_messages_if_needed", "consolidate_turn_memory",
    "discover_domain_packs", "discover_skills", "estimate_messages_chars", "estimate_messages_tokens",
    "execute_identity_action", "extract_key_tokens", "format_context_budget_status",
    "format_identity_summary", "format_skills_catalog", "format_task_carry_forward",
    "get_knowledge_port", "invalidate_skills_cache", "is_identity_bootstrapped",
    "manage_active_project", "manage_companion_tone", "manage_model_tier",
    "normalize_compaction_text", "prune_stale_tool_outputs", "rank_relevant_skills",
    "register_knowledge_adapter", "run_first_boot_onboarding", "should_compact",
    "sync_identity_md", "verify_compaction_summary",
]

DEFAULT_SOUL_AND_USER: Dict[str, str] = {
    "companion_name": "Companion",
    "companion_soul": "Warm, grounded, loyal thinking partner and autonomous lead engineer who owns technical complexity and communicates in plain, honest English.",
    "user_name": "Partner",
    "user_profile": "Thoughtful human partner who values clear explanations, well-reasoned recommendations, and reliable outcomes.",
}
VALID_TONES: Dict[str, str] = {
    "concise": "Terse, bullet-first executive answers with zero fluff.",
    "balanced": "Warm, plain-English explanations with concise action summaries.",
    "elaborate": "Detailed walkthroughs with architectural reasoning, tradeoffs, and examples.",
}

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

# Builder-only skills that live in `.agents/skills/` and must NEVER appear in Companion's runtime prompt or UI
_BUILDER_ONLY_SKILL_NAMES = frozenset({
    "anti-sprawl-gate", "product-intent-dogfood", "sdd-compliance",
    "web-surface-dev", "release-readiness", "devops-ops", "engineering-workflow", "test-driven-verification",
})


# ---------------------------------------------------------------------------
# 1. KnowledgePort & Progressive Skill / Domain Capability Pack Discovery
# ---------------------------------------------------------------------------
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
    """Port decoupling Companion's reasoning from any Wiki or knowledge backend."""

    def search(self, query: str, project_id: Optional[str] = None, limit: int = 6) -> List[KnowledgeEntry]: ...
    def read_entry(self, key: str, project_id: Optional[str] = None) -> Optional[KnowledgeEntry]: ...
    def list_index(self, project_id: Optional[str] = None, limit: int = 20) -> List[KnowledgeEntry]: ...
    def propose_update(self, key: str, value: str, project_id: Optional[str] = None, source: str = "agent", confidence: float = 1.0) -> str: ...


def _doc_heading(txt: str, fallback: str) -> str:
    body = txt.replace("\r\n", "\n")
    if body.startswith("---\n"):
        end_idx = body.find("\n---\n", 4)
        if end_idx != -1:
            body = body[end_idx + 5 :]
    return next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("---")), fallback)


class LocalKnowledgeAdapter:
    """Default zero-dependency `KnowledgePort` backed by SQLite `knowledge_facts`, notes, and Wiki."""

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
    global _CUSTOM_KNOWLEDGE_ADAPTER
    _CUSTOM_KNOWLEDGE_ADAPTER = adapter


def get_knowledge_port(store: Optional[MemoryStore] = None) -> KnowledgePort:
    return _CUSTOM_KNOWLEDGE_ADAPTER if _CUSTOM_KNOWLEDGE_ADAPTER is not None else LocalKnowledgeAdapter(store=store)


_SKILLS_CACHE: Dict[Tuple[str, int, int, int], List[Dict[str, str]]] = {}


def invalidate_skills_cache() -> None:
    _SKILLS_CACHE.clear()


def _extract_context_description(md_text: str, fallback: str) -> str:
    """Extract a 1-sentence plain-English description from YAML frontmatter or the first prose line of `CONTEXT.md`."""
    norm = md_text.replace("\r\n", "\n").strip()
    if norm.startswith("---\n"):
        end_idx = norm.find("\n---", 4)
        if end_idx != -1:
            for ln in norm[4:end_idx].splitlines():
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    if k.strip().lower() == "description" and v.strip().strip("'\""):
                        return v.strip().strip("'\"")[:220]
            norm = norm[end_idx + 4 :].strip()
    for ln in norm.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "```", "|", "---")):
            continue
        clean = re.sub(r"^[>\-*+]\s*", "", s).strip()
        if len(clean) >= 12:
            return clean[:220]
    return fallback


def discover_domain_packs(domains_dir: Optional[Path] = None) -> List[Dict[str, str]]:
    """Discover ICM Self-Contained Domain Capability Packs under `data/domains/<pack>/`."""
    base = Path(domains_dir or settings.domains_dir).resolve()
    if not base.is_dir():
        return []
    packs: List[Dict[str, str]] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        ctx_md = d / "CONTEXT.md" if (d / "CONTEXT.md").is_file() else ((d / "SKILL.md") if (d / "SKILL.md").is_file() else None)
        if ctx_md is None:
            continue
        txt = ctx_md.read_text(encoding="utf-8", errors="replace")
        desc = _extract_context_description(txt, f"Domain capability pack `{d.name}`.")
        scripts = [p.name for p in sorted((d / "scripts").glob("*.py"))] if (d / "scripts").is_dir() else []
        wfs = [p.name for p in sorted((d / "workflows").glob("*.workflow.json"))] if (d / "workflows").is_dir() else []
        dbs = [p.name for p in (*sorted(d.glob("*.db")), *(sorted((d / "data").glob("*.db")) if (d / "data").is_dir() else []))]
        stages = [sub.name for sub in sorted(d.iterdir()) if sub.is_dir() and re.match(r"^\d{2}_", sub.name)]
        rel_p = f"data/domains/{d.name}/{ctx_md.name}"
        packs.append({
            "name": d.name,
            "description": desc,
            "required_tools": "read_file, run_command",
            "path": rel_p,
            "abs_path": str(ctx_md),
            "scope": "domain",
            "references": ", ".join(stages),
            "dags": ", ".join(wfs),
            "workflows": ", ".join(wfs),
            "scripts": ", ".join(scripts),
            "databases": ", ".join(dbs),
        })
    return packs


def discover_skills(root: Optional[Path] = None, include_domains: bool = True) -> List[Dict[str, str]]:
    """Scan runtime `skills/*/SKILL.md`, user `data/skills/*/SKILL.md`, and optional `data/domains/*/` packs."""
    target_root = (root or settings.root).resolve()
    is_main_root = root is None or target_root == settings.root.resolve()
    dirs = [target_root / "skills", *([settings.user_skills_dir.resolve()] if is_main_root else [])]
    skill_map: Dict[str, Path] = {}
    for d in dirs:
        if d.is_dir():
            for smd in sorted(d.glob("*/SKILL.md")):
                if smd.parent.name not in _BUILDER_ONLY_SKILL_NAMES:
                    skill_map[smd.parent.name] = smd
    domain_packs = discover_domain_packs() if (include_domains and is_main_root) else []
    if not skill_map and not domain_packs:
        return []
    skill_mds = [skill_map[k] for k in sorted(skill_map)]
    tier3_assets = [
        a for smd in skill_mds
        for a in (
            *smd.parent.glob("scripts/*.py"),
            *smd.parent.glob("workflows/*.workflow.json"),
            *smd.parent.glob("dags/*.py"),
            *smd.parent.glob("references/*.md"),
        )
    ]
    dom_files = [Path(dp["abs_path"]) for dp in domain_packs if dp.get("abs_path") and Path(dp["abs_path"]).is_file()]
    max_ns = max((int(p.stat().st_mtime_ns) for p in (*skill_mds, *tier3_assets, *dom_files)), default=0)
    ck = (f"{target_root}::{dirs[-1]}::{include_domains}", len(skill_mds) + len(domain_packs), len(tier3_assets), max_ns)
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
        wfs = [p.name for p in sorted((sdir / "workflows").glob("*.workflow.json"))] if (sdir / "workflows").is_dir() else []
        dags = [p.name for p in sorted((sdir / "dags").glob("*.py"))] if (sdir / "dags").is_dir() else []
        scripts = [p.name for p in sorted((sdir / "scripts").glob("*.py"))] if (sdir / "scripts").is_dir() else []
        is_core = sdir.parent.resolve() == (target_root / "skills").resolve()
        rel_p = f"skills/{sdir.name}/SKILL.md" if is_core else f"data/skills/{sdir.name}/SKILL.md"
        meta: Dict[str, str] = {
            "name": sdir.name, "description": "Operational runbook skill.", "required_tools": "",
            "path": rel_p, "scope": "core" if is_core else "user", "references": ", ".join(refs),
            "dags": ", ".join([*wfs, *dags]), "workflows": ", ".join(wfs), "scripts": ", ".join(scripts),
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

    seen_names = {s["name"] for s in skills}
    for dp in domain_packs:
        if dp["name"] not in seen_names:
            skills.append({k: v for k, v in dp.items() if k != "abs_path"})

    if len(_SKILLS_CACHE) > 8:
        _SKILLS_CACHE.clear()
    _SKILLS_CACHE[ck] = skills
    return [dict(item) for item in skills]


def rank_relevant_skills(query: str, skills: List[Dict[str, str]], top_k: int = 4) -> List[Dict[str, str]]:
    """Rank skills and domain capability packs against `query` using both token overlap and stem similarity."""
    tokens = {t.lower() for t in re.findall(r"[a-zA-Z0-9_-]{3,}", query or "") if t.lower() not in _STOP_WORDS}
    if not tokens:
        return skills[:top_k]
    scored: List[Tuple[float, str, Dict[str, str]]] = []
    for s in skills:
        hay = f"{s.get('name', '')} {s.get('description', '')} {s.get('dags', '')} {s.get('scripts', '')}".lower()
        hits = float(sum(2 if tok in s.get("name", "").lower() else (1 if tok in hay else 0) for tok in tokens))
        sim = semantic_similarity(query, hay)
        total = hits + (sim * 2.5)
        if total >= 0.35:
            scored.append((total, s.get("name", ""), s))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:top_k]]


@dataclass(frozen=True)
class EpistemicGrounding:
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
    salient = list(dict.fromkeys(t.lower().strip("./-") for t in raw_terms if len(t.lower().strip("./-")) >= 3 and t.lower().strip("./-") not in _STOP_WORDS))
    facts = port.search(clean_q, project_id=pid, limit=6)
    ws_files = [str(p.relative_to(target_root)).replace("\\", "/") for p in walk_pruned_files(target_root)[:120]]
    matched_files = [f for f in ws_files if any(tok in f.lower() for tok in salient[:10])][:6]
    known_blob = " ".join([f.key.lower() + " " + f.summary.lower() for f in facts] + [mf.lower() for mf in matched_files])
    unknowns = [
        t for t in raw_terms
        if t.lower().strip("./-") in salient
        and (t[0].isupper() or any(ch in t for ch in ("_", "-", "/", ".")) or len(t) >= 8)
        and t.lower().strip("./-") not in known_blob
    ][:5]
    ranked_skills = rank_relevant_skills(clean_q, all_skills, top_k=top_k)

    lines = ["", "[EPISTEMIC GROUNDING]"]
    lines.append("  Known Facts: " + ("; ".join(f"`{f.key}`={f.summary[:100]}" for f in facts) if facts else "(none recorded for this query yet)"))
    if matched_files:
        lines.append("  Matched Workspace Files: " + ", ".join(f"`{m}`" for m in matched_files))
    if ranked_skills:
        lines.append("  Relevant Skills / Packs: " + ", ".join(f"`{s['name']}` ({s['path']})" for s in ranked_skills))
    return EpistemicGrounding(facts, matched_files, unknowns, ranked_skills, "\n".join(lines) + "\n")


def build_workspace_radar(root: Optional[Path] = None, max_files: int = 30) -> str:
    target_root = (root or settings.active_project_root).resolve()
    files = [
        str(p.relative_to(target_root)).replace("\\", "/")
        for p in walk_pruned_files(target_root)
        if p.suffix.lower() in GENERAL_TRACKED_EXTENSIONS
    ]
    if not files:
        return "  (Empty workspace)"
    shown = ", ".join(f"`{f}`" for f in files[:max_files])
    more = f" (+{len(files) - max_files} more)" if len(files) > max_files else ""
    return f"  Workspace Files ({len(files)}): {shown}{more}"


# ---------------------------------------------------------------------------
# 2. Identity, Workspace & Layered System Prompt Assembler (Primitive 1)
# ---------------------------------------------------------------------------
def _resolve_tone(prof: Dict[str, str]) -> str:
    t = prof.get("companion_tone", "balanced").strip().lower()
    return t if t in VALID_TONES else "balanced"


def manage_companion_tone(tone: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    mem, req = (store or get_memory_store()), (tone or "").strip().lower()
    if req:
        if req not in VALID_TONES:
            return f"Invalid tone '{tone}'. Valid options: {', '.join(VALID_TONES)}."
        mem.set_user_profile("companion_tone", req)
        sync_identity_md(store=mem)
        mem.sync_memory_md()
    curr = _resolve_tone(mem.get_user_profile())
    return f"Communication Tone: `{curr}` — {VALID_TONES[curr]} (Options: `/tone concise|balanced|elaborate`)"


def manage_model_tier(tier_or_action: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    mem, req = (store or get_memory_store()), (tier_or_action or "").strip().lower()
    curr = mem.get_user_profile().get("active_model_tier", "auto").strip().lower() or "auto"
    if req in ("", "status", "check"):
        return format_model_tiers_status(active_mode=curr, check_health=(req == "check"))
    valid_modes = ("auto", *MODEL_TIERS)
    if req not in valid_modes:
        return f"Invalid model tier '{tier_or_action}'. Valid options: {', '.join(valid_modes)}, status, check."
    mem.set_user_profile("active_model_tier", req)
    mem.sync_memory_md()
    return f"Active model routing mode set to `{req}`.\n\n" + format_model_tiers_status(active_mode=req)


def _load_known_projects(mem: MemoryStore) -> Dict[str, str]:
    raw = mem.get_user_profile().get("known_projects_json", "{}")
    try:
        parsed = json.loads(raw) if raw else {}
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _persist_workspace_selection(mem: MemoryStore, pid: str, proot: Path, known: Dict[str, str]) -> None:
    pg = get_project_graph(store=mem)
    if pid != SELF_PROJECT_ID:
        known[pid] = str(proot)
        pg.create_project(title=pid.replace("-", " ").title(), kind="project", workspace_id=pid, workspace_path=str(proot), project_id=pid)
    pg.set_focus_project(pid)
    mem.set_user_profile("active_project_id", pid)
    mem.set_user_profile("active_project_path", str(proot))
    mem.set_user_profile("known_projects_json", json.dumps(known, sort_keys=True))
    mem.sync_memory_md()


def _resolve_external_target(raw_target: str, known: Dict[str, str]) -> Path:
    clean, slug = raw_target.strip().strip("'\""), slugify_project_id(raw_target.strip().strip("'\""))
    if clean in known:
        return Path(known[clean]).resolve()
    if slug in known and "/" not in clean and "\\" not in clean and ":" not in clean:
        return Path(known[slug]).resolve()
    cand = Path(clean)
    return cand.resolve() if cand.is_absolute() else (settings.root.parent / clean).resolve()


def manage_active_project(arg: Optional[str] = None, store: Optional[MemoryStore] = None) -> str:
    mem = store or get_memory_store()
    known, raw = _load_known_projects(mem), (arg or "").strip()
    parts = raw.split(None, 1)
    verb, rest = (parts[0].lower() if parts else "status"), (parts[1].strip() if len(parts) > 1 else "")

    if verb in ("status", "current", ""):
        ws = settings.get_workspace()
        lock = "PERMANENTLY IMMUTABLE (`data/`, `data/domains/`, & `data/scratch/` writable)" if ws.is_self_project else f"IMMUTABLE (`{ws.self_root}` protected)"
        return f"=== Active WorkspaceContext ===\nProject ID    : `{ws.project_id}`\nProject Root  : {ws.project_root}\nCompanion Core: {lock}\n(Use `/project list`, `/project init <name|path>`, `/project switch <name|path>`, or `/project self`.)"
    if verb == "list":
        ws = settings.get_workspace()
        lines = ["=== Registered Workspaces ===", f"  * `{SELF_PROJECT_ID}` -> {settings.root.resolve()}{' [ACTIVE]' if ws.is_self_project else ''}"]
        for k, v in sorted(known.items()):
            lines.append(f"  * `{k}` -> {v}{' [ACTIVE]' if (not ws.is_self_project and ws.project_id == k) else ''}")
        return "\n".join(lines)
    if verb in ("self", SELF_PROJECT_ID, "reset", "home"):
        ws = settings.reset_workspace()
        _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
        return f"Switched active WorkspaceContext back to `{ws.project_id}` ({ws.project_root}). Installed `app/` remains immutable (`data/` & `data/domains/` writable)."
    if verb in ("init", "create", "new"):
        if not rest:
            return "error: project name or path is required for `init` (e.g., `/project init my-app`)."
        target = _resolve_external_target(rest, known)
        if target != settings.root.resolve() and target.is_relative_to(settings.root.resolve()):
            return f"error: external project `{target}` must live outside Companion core (`{settings.root.resolve()}`)."
        target.mkdir(parents=True, exist_ok=True)
        (target / "tests").mkdir(exist_ok=True)
        readme = target / "README.md"
        if not readme.exists():
            readme.write_text(f"# {target.name}\n\nManaged by Companion.\n", encoding="utf-8")
        ws = settings.set_workspace(target)
        _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
        return f"Initialized & activated project `{ws.project_id}` at `{ws.project_root}` (`{ws.self_root}` locked READ-ONLY)."

    target_str = rest if verb == "switch" else raw
    if not target_str:
        return "error: target project name or path is required for `switch`."
    if target_str.lower() in ("self", SELF_PROJECT_ID):
        return manage_active_project("self", store=mem)
    target = _resolve_external_target(target_str, known)
    if target != settings.root.resolve() and target.is_relative_to(settings.root.resolve()):
        return f"error: external project `{target}` must live outside Companion core (`{settings.root.resolve()}`)."
    if not target.is_dir():
        return f"error: project directory not found: `{target}` (use `/project init {target_str}` to create it)."
    ws = settings.set_workspace(target)
    _persist_workspace_selection(mem, ws.project_id, ws.project_root, known)
    return f"Switched active WorkspaceContext to `{ws.project_id}` at `{ws.project_root}` (`{ws.self_root}` locked READ-ONLY)."


def sync_identity_md(store: Optional[MemoryStore] = None, path: Optional[Path] = None) -> Path:
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name = prof.get("companion_name") or settings.companion_name or DEFAULT_SOUL_AND_USER["companion_name"]
    c_soul = prof.get("companion_soul") or settings.companion_soul or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone = _resolve_tone(prof)
    u_name = prof.get("user_name") or settings.user_name or DEFAULT_SOUL_AND_USER["user_name"]
    u_prof = prof.get("user_profile") or settings.user_profile or DEFAULT_SOUL_AND_USER["user_profile"]
    default_id = settings.identity_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "IDENTITY.md")
    out_path = Path(path or default_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Companion AI OS — Soul & User Identity (`IDENTITY.md`)\n\n## 1. Companion Soul\n"
        f"- **Name**: {c_name}\n- **Soul & Ethos**: {c_soul}\n- **Communication Tone**: {c_tone} ({VALID_TONES[c_tone]})\n\n"
        f"## 2. User Identity\n- **Name**: {u_name}\n- **Profile & Style**: {u_prof}\n"
    )
    out_path.write_text(content, encoding="utf-8", newline="\n")
    return out_path


def format_identity_summary(store: Optional[MemoryStore] = None) -> str:
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name, c_soul = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"], prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone, onboarded = _resolve_tone(prof), prof.get("onboarded", "false").lower() in ("true", "1", "yes")
    u_name, u_prof = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"], prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    return (
        f"=== Companion Soul & User Identity (Bootstrapped: {'YES' if onboarded else 'PENDING FIRST BOOT'}) ===\n"
        f"Companion Name : {c_name}\nCompanion Soul : {c_soul}\nActive Tone    : {c_tone} ({VALID_TONES[c_tone]})\n"
        f"User Name      : {u_name}\nUser Profile   : {u_prof}\n"
        "(Run `/onboard`, `/tone [concise|balanced|elaborate]`, or set COMPANION_NAME in `.env` to customize.)"
    )


def bootstrap_identity(companion_name: str = "", companion_soul: str = "", user_name: str = "", user_profile: str = "", store: Optional[MemoryStore] = None) -> str:
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    c_name = (companion_name.strip() or prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"])[:60]
    c_soul = (companion_soul.strip() or prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"])[:300]
    u_name = (user_name.strip() or prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"])[:60]
    u_prof = (user_profile.strip() or prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"])[:300]
    for k, v in (("companion_name", c_name), ("companion_soul", c_soul), ("user_name", u_name), ("user_profile", u_prof), ("onboarded", "true")):
        mem.set_user_profile(k, v)
    sync_identity_md(store=mem)
    mem.sync_memory_md()
    return format_identity_summary(store=mem)


def is_identity_bootstrapped(store: Optional[MemoryStore] = None) -> bool:
    mem = store or get_memory_store()
    prof = mem.get_user_profile()
    onboarded = prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes")
    if not onboarded and any((settings.companion_name, settings.companion_soul, settings.user_name, settings.user_profile)):
        bootstrap_identity(
            prof.get("companion_name") or settings.companion_name,
            prof.get("companion_soul") or settings.companion_soul,
            prof.get("user_name") or settings.user_name,
            prof.get("user_profile") or settings.user_profile,
            store=mem,
        )
        return True
    default_id = settings.identity_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "IDENTITY.md")
    if not default_id.is_file():
        sync_identity_md(store=mem, path=default_id)
    return onboarded


def _build_identity_snapshot(mem: MemoryStore, summary: Optional[str] = None) -> Dict[str, Any]:
    prof, ws = mem.get_user_profile(), settings.get_workspace()
    tone, onboarded = _resolve_tone(prof), prof.get("onboarded", "false").strip().lower() in ("true", "1", "yes")
    return {
        "ok": True, "onboarded": onboarded, "companion_tone": tone, "valid_tones": VALID_TONES,
        "companion_name": prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"],
        "companion_soul": prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"],
        "user_name": prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"],
        "user_profile": prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"],
        "role": prof.get("role") or "", "communication_style": prof.get("communication_style") or "",
        "workspace": {"project_id": ws.project_id, "project_root": str(ws.project_root), "is_self_project": ws.is_self_project, "self_root": str(ws.self_root), "known_projects": _load_known_projects(mem)},
        "paths": {"data_dir": str(settings.effective_data_dir), "domains_dir": str(settings.domains_dir), "db_path": str(mem.db_path), "identity_md_path": str(settings.identity_md_path), "memory_md_path": str(settings.memory_md_path), "user_skills_dir": str(settings.user_skills_dir)},
        "summary": summary or format_identity_summary(store=mem),
    }


def execute_identity_action(params: Optional[Dict[str, Any]] = None, actor: str = "user", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    p, mem = dict(params or {}), (store or get_memory_store())
    sub = str(p.get("subaction") or p.get("action") or "get").strip().lower()
    if sub in ("get", "status"):
        is_identity_bootstrapped(store=mem)
        return _build_identity_snapshot(mem)
    if actor.strip().lower() not in ("user", "cli", "web"):
        return {"ok": False, "error": f"SECURITY GUARD: `identity.{sub}` requires direct user/CLI/Web authorization."}
    if sub in ("update", "save", "bootstrap"):
        tone_in = str(p.get("companion_tone") or "").strip().lower()
        if tone_in and tone_in not in VALID_TONES:
            return {"ok": False, "error": f"Invalid tone `{tone_in}`. Valid: {', '.join(VALID_TONES)}."}
        if tone_in:
            mem.set_user_profile("companion_tone", tone_in)
        for fld in ("role", "communication_style"):
            if fld in p and str(p.get(fld) or "").strip():
                mem.set_user_profile(fld, str(p[fld]).strip()[:300])
        bootstrap_identity(str(p.get("companion_name") or ""), str(p.get("companion_soul") or ""), str(p.get("user_name") or ""), str(p.get("user_profile") or ""), store=mem)
        snap = _build_identity_snapshot(mem)
        snap["summary"] = f"Saved Identity & Profile (`{snap['companion_name']}` <-> `{snap['user_name']}`, tone=`{snap['companion_tone']}`)."
        return snap
    if sub in ("project_self", "project_switch", "project_init"):
        target = str(p.get("target") or p.get("path") or "").strip()
        cmd = "self" if sub == "project_self" else f"{'init' if sub == 'project_init' else 'switch'} {target}".strip()
        msg = manage_active_project(cmd, store=mem)
        return {"ok": False, "error": msg} if msg.lower().startswith("error:") else _build_identity_snapshot(mem, summary=msg)
    return {"ok": False, "error": f"Unknown `identity` subaction `{sub}`."}


def run_first_boot_onboarding(store: Optional[MemoryStore] = None, input_fn: Any = input) -> str:
    mem = store or get_memory_store()
    if input_fn is input and (sys.stdin is None or not sys.stdin.isatty()):
        return format_identity_summary(store=mem) + "\n\n(Interactive `/onboard` requires a terminal TTY.)"
    prof = mem.get_user_profile()
    d_cname = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"]
    d_csoul = prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    d_uname = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"]
    d_uprof = prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    print("\n=== Welcome! First-Boot Soul & User Identity Setup ===\nLet's take 30 seconds to orient your Companion (press Enter on any question to keep the default).\n")
    c_name = input_fn(f"1. Companion's Name [{d_cname}]: ").strip() or d_cname
    c_soul = input_fn(f"2. Companion's Soul / Personality (1-2 short sentences)\n   [{d_csoul}]: ").strip() or d_csoul
    u_name = input_fn(f"3. Your Name [{d_uname}]: ").strip() or d_uname
    u_prof = input_fn(f"4. Your Profile & Communication Preference (1-2 short sentences)\n   [{d_uprof}]: ").strip() or d_uprof
    summary = bootstrap_identity(c_name, c_soul, u_name, u_prof, store=mem)
    print("\nSetup complete! Grounding saved to SQLite and `IDENTITY.md`.\n")
    return summary


def format_skills_catalog(skill_name: Optional[str] = None, root: Optional[Path] = None) -> str:
    target_root = (root or settings.root).resolve()
    skills = discover_skills(target_root)
    if skill_name and skill_name.strip():
        query = skill_name.strip().lower()
        for s in skills:
            if s["name"].lower() == query or query in s["name"].lower():
                rel_p = s["path"]
                if rel_p.startswith("data/"):
                    full_p = settings.effective_data_dir / rel_p.removeprefix("data/")
                else:
                    cand_p = Path(rel_p)
                    full_p = cand_p if cand_p.is_absolute() else (target_root / cand_p)
                if full_p.is_file():
                    res_lines = [f"- `{k}`: {s[k]}" for k in ("references", "workflows", "dags", "scripts", "databases") if s.get(k)]
                    res_block = ("\n## Bundled Tier-3 Resources\n" + "\n".join(res_lines) + "\n") if res_lines else ""
                    return f"# Skill / Capability Pack: `{s['name']}` ({s['path']})\n\n" + full_p.read_text(encoding="utf-8", errors="replace") + res_block
        return f"Skill '{skill_name}' not found. Available skills: {', '.join(s['name'] for s in skills)}"

    lines = [
        f"=== Companion Progressive-Disclosure Skill Registry ({len(skills)} Active Skills & Domain Packs) ===",
        "Architecture: Core Tools are atomic primitives; Skills (`skills/<name>/`) and Domain Capability Packs (`data/domains/<pack>/`) load `SKILL.md` / `CONTEXT.md` on demand.",
        "",
    ]
    for s in skills:
        req = f"  [Tools: {s['required_tools']}]" if s.get("required_tools") else ""
        tier3 = [f"{k}=[{s[k]}]" for k in ("references", "workflows", "dags", "scripts", "databases") if s.get(k)]
        t3_str = f"\n    Tier-3 Resources: {' | '.join(tier3)}" if tier3 else ""
        lines.append(f"  * `{s['name']}` [{s.get('scope', 'core')}] (`{s['path']}`)\n    {s['description']}{req}{t3_str}")
    lines.append("\n(Use `/skills <name>` or `read_file` on a pack's `SKILL.md` / `CONTEXT.md` path to read its full runbook.)")
    return "\n".join(lines)


def _format_identity_hud_block(prof: Dict[str, str]) -> str:
    c_name = prof.get("companion_name") or DEFAULT_SOUL_AND_USER["companion_name"]
    c_soul = prof.get("companion_soul") or DEFAULT_SOUL_AND_USER["companion_soul"]
    c_tone = _resolve_tone(prof)
    u_name = prof.get("user_name") or DEFAULT_SOUL_AND_USER["user_name"]
    u_prof = prof.get("user_profile") or DEFAULT_SOUL_AND_USER["user_profile"]
    return (
        f"[COMPANION SOUL & USER IDENTITY]\n"
        f"- Your Name : {c_name}\n"
        f"- Your Soul : {c_soul}\n"
        f"- Active Tone: {c_tone} ({VALID_TONES[c_tone]})\n"
        f"- User Name : {u_name}\n"
        f"- User Style: {u_prof}"
    )


def _format_host_runtime_hud() -> str:
    shell_lbl = "Windows PowerShell (`run_command` / `run_python`)" if os.name == "nt" else "POSIX bash"
    return f"host=`{socket.gethostname()}` | os=`{platform.system()} {platform.release()}` | shell=`{shell_lbl}` | python=`{platform.python_version()}`"


def _read_root_context_md() -> str:
    """Read optional ICM Layer-1 Root Front Door `data/CONTEXT.md` (capped at 150 lines)."""
    ctx_p = settings.root_context_md_path
    if not ctx_p.is_file():
        return ""
    try:
        lines = ctx_p.read_text(encoding="utf-8", errors="replace").splitlines()[:150]
        body = "\n".join(lines).strip()
        return f"\n[WORKSPACE ROOT CONTEXT (`data/CONTEXT.md`)]\n{body}\n" if body else ""
    except Exception:
        return ""


def build_system_prompt(store: Optional[MemoryStore] = None, wf_engine: Optional[Any] = None, user_query: str = "") -> str:
    """Construct a session-stable Layered System Prompt with 1-line Progressive Disclosure index."""
    _ = wf_engine
    mem = store or get_memory_store()
    is_identity_bootstrapped(store=mem)
    default_md = settings.memory_md_path if mem.db_path.resolve() == settings.db_path.resolve() else (mem.db_path.parent / "MEMORY.md")
    if not default_md.is_file():
        mem.sync_memory_md(target_path=default_md)
    prof, ws = mem.get_user_profile(), settings.get_workspace()
    identity_text, host_hud = _format_identity_hud_block(prof), _format_host_runtime_hud()
    ws_lock = (
        f"Personal OS Daily-Driver (`{ws.project_id}` | `data/`, `data/domains/`, `data/scratch/`, & Wiki writable)"
        if ws.is_self_project
        else f"External Workspace (`{ws.project_id}` -> `{ws.project_root}`; `data/domains/` & `data/scratch/` also accessible)"
    )
    from companion.mcp_hub import format_connected_partners_hud

    trust_mode, partners_hud = get_trust_mode(store=mem), format_connected_partners_hud(store=mem)
    partners_section = f"\n{partners_hud}\n" if partners_hud else ""
    agile_hud = "\n".join(format_project_graph_hud(store=mem).splitlines()[:4])
    edu_hud = "\n".join(format_education_hud(store=mem).splitlines()[:3])
    root_ctx_block = _read_root_context_md()

    skills = discover_skills(settings.root)
    skill_index_lines = [
        f"  - `{s['name']}` ({s['path']}): {s['description'][:130]}"
        for s in skills
    ]
    skills_block = "\n".join(skill_index_lines) if skill_index_lines else "  (No runtime skills registered)"

    known_block = ""
    if user_query.strip():
        grounding = build_epistemic_grounding(user_query, store=mem, root=ws.project_root, skills=skills, top_k=4)
        if grounding.known_facts:
            known_block = "\n[KNOWN FACTS]\n" + "\n".join(f"  - [{e.source}] `{e.key}`: {e.content}" for e in grounding.known_facts[:5]) + "\n"

    ext_radar_block = ""
    if not ws.is_self_project and ws.project_root.is_dir():
        repo_radar = build_workspace_radar(ws.project_root)
        ext_radar_block = f"\n[ACTIVE WORKSPACE FILES (`{ws.project_id}`)]\n{repo_radar}\n"

    os_rule = (
        "- Windows Host: Use PowerShell-compatible syntax for shell commands, or pass `python_code` / run `.py` scripts directly (never bash heredocs)."
        if os.name == "nt"
        else "- Linux/POSIX Host: Use standard POSIX shell commands or structured Python scripts."
    )
    return (
        f"{identity_text}\n\n"
        f"[RUNTIME & GOVERNANCE]\n"
        f"- Runtime  : {host_hud}\n"
        f"- Workspace: {ws_lock}\n"
        f"- Paths    : data=`{settings.effective_data_dir}` | domains=`{settings.domains_dir}` | scratch=`{settings.scratch_dir}` | wiki=`{settings.wiki_dir}` | trust=`{trust_mode}` | privacy=`{settings.effective_privacy_mode()}`\n"
        f"{partners_section}{root_ctx_block}\n"
        f"{agile_hud}\n\n"
        f"{edu_hud}\n"
        f"{known_block}{ext_radar_block}\n"
        f"[AVAILABLE SKILLS & DOMAIN CAPABILITY PACKS — TIER-1 INDEX]\n"
        f"When a skill or domain pack applies, read its `SKILL.md` or `CONTEXT.md` via `read_file` before acting:\n"
        f"{skills_block}\n\n"
        f"[EXECUTION REMINDERS]\n"
        f"- Operate as one grounded, proactive ReAct agent: inspect first when uncertain, use deterministic scripts/SQL for exact math, and respond in clear plain English.\n"
        f"- Use `wiki_manage` for Knowledge Wiki paths (`01_inbox/`..`04_archive/`), `agile_manage` for Goals/Boards, `calendar_manage` for schedule items, and `education_manage` for Courses/Flashcards.\n"
        f"- Large tool outputs automatically spill over to `data/scratch/`; use `read_file` or `search` on the spillover path if you need specific lines.\n"
        f"{os_rule}\n"
    )


def build_turn_context(
    store: Optional[MemoryStore] = None, wf_engine: Optional[Any] = None,
    user_query: str = "", nudge_banner: str = "",
) -> str:
    """Build a compact dynamic `<turn_context>` block injected on the active user turn."""
    _ = wf_engine
    mem = store or get_memory_store()
    ws = settings.get_workspace()
    lines: List[str] = []
    active_wf = mem.get_active_workflow()
    if active_wf:
        lines.append(f"active_plan_or_goal: #{active_wf['id']} state={active_wf['current_state']} goal={str(active_wf.get('goal') or '')[:100]}")
    waiting_apr = len(get_approval_queue(mem).list_requests(status="waiting"))
    if waiting_apr:
        lines.append(f"pending_approvals: {waiting_apr}")
    pending_tasks = mem.get_pending_tasks()[:3]
    if pending_tasks:
        lines.append("focus_tasks: " + "; ".join(f"#{tid}({st}) {desc[:60]}" for tid, desc, st, _ in pending_tasks))
    if not ws.is_self_project and ws.project_root.is_dir():
        from companion.sdlc_tools import discover_repo_sdlc_context

        proj_rec = get_project_graph(mem).get_project()
        if sdlc_ctx := discover_repo_sdlc_context(ws.project_root, project=proj_rec):
            lines.append(sdlc_ctx)
    if user_query.strip():
        skills = discover_skills(settings.root)
        grounding = build_epistemic_grounding(user_query, store=mem, root=ws.project_root, skills=skills, top_k=3)
        if grounding.known_facts:
            lines.append("relevant_facts: " + "; ".join(f"{e.key}={e.content[:90]}" for e in grounding.known_facts[:4]))
        if grounding.top_skills:
            lines.append("matched_skills: " + ", ".join(f"{s['name']} ({s['path']})" for s in grounding.top_skills[:3]))
    if nudge_banner.strip():
        lines.append(nudge_banner.strip())
    return ("<turn_context>\n" + "\n".join(lines) + "\n</turn_context>") if lines else ""


# ---------------------------------------------------------------------------
# 3. Real LLM Semantic Compaction & KeyTokens Verification (Primitive 5)
# ---------------------------------------------------------------------------
_DASH_TO_HYPHEN = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})
_TOKEN_SPLIT_RE = re.compile(r"[`\"'(),;:<>\[\]{}\s]+")
_PATH_EXT_RE = re.compile(r"(?:[a-zA-Z]:[\\/]|/)?[\w.\-/\\]+\.(?:py|md|json|csv|pdf|xlsx|docx|db|sqlite|toml|yaml|yml|sh|env|html|js|ts|tsx|css)\b", re.I)
_NUMBER_TOKEN_RE = re.compile(r"^\d+(?:[.\-:/]\d+)+$|^\d{2,}%?$")
_NAME_TOKEN_RE = re.compile(r"^(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|(?:ADR|SPEC|TASK)-\d+|[A-Z][a-zA-Z0-9]*(?:Error|Exception))$", re.I)
_FENCED_JSON_RE = re.compile(r"```json\s*\{.*?\}\s*```", re.S)
_VERIFY_ONLY_RE = re.compile(
    r"^\s*(?:run|execute|perform|verify)\b.*\b(?:unit\s+tests?|unittest|test\s+suite|full\s+verification|system[_\s]?doctor|syntax\s+check)\b",
    re.I,
)
_MUTATION_CUE_RE = re.compile(r"\b(?:create|write|implement|edit|modify|update|refactor|build|add)\b", re.I)
_HANDOFF_PREFIXES = ("[Context Compaction Summary", "[CONTEXT CHECKPOINT HANDOFF")
_DURABLE_FACT_RE = re.compile(
    r"(?:^|\.\s+)(?:please\s+)?(?:remember\s+that|note\s+that|for\s+future\s+reference[,:]?)\s+([^.\n]{8,220})",
    re.I,
)


@dataclass(frozen=True)
class KeyTokens:
    paths: List[str]
    numbers: List[str]
    names: List[str]

    def all_tokens(self) -> List[str]:
        return [*self.paths, *self.numbers, *self.names]


def normalize_compaction_text(text: str) -> str:
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", str(text)).translate(_DASH_TO_HYPHEN)
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")


def _harvest_tokens_from_text(norm_text: str, paths: List[str], numbers: List[str], names: List[str], seen: set[str], max_paths: int = 45, max_nums: int = 35, max_names: int = 50) -> None:
    for pm in _PATH_EXT_RE.findall(norm_text):
        clean_p = pm.strip(".,;:").replace("\\", "/")
        if clean_p.lower() not in seen and len(paths) < max_paths:
            seen.add(clean_p.lower())
            paths.append(clean_p)
    for tok in _TOKEN_SPLIT_RE.split(norm_text):
        clean = tok.strip(".,;:")
        if len(clean) < 2 or clean.lower() in seen:
            continue
        if _NUMBER_TOKEN_RE.match(clean) and len(numbers) < max_nums:
            seen.add(clean.lower())
            numbers.append(clean)
        elif _NAME_TOKEN_RE.match(clean) and len(names) < max_names:
            seen.add(clean.lower())
            names.append(clean)


def extract_key_tokens(older: List[Dict[str, Any]]) -> KeyTokens:
    paths: List[str] = []
    numbers: List[str] = []
    names: List[str] = []
    seen: set[str] = set()
    for m in older:
        role = m.get("role")
        raw = str(m.get("content") or "")
        if role == "user" or (role == "system" and raw.startswith(_HANDOFF_PREFIXES)):
            _harvest_tokens_from_text(normalize_compaction_text(raw), paths, numbers, names, seen)
    for m in older:
        role = m.get("role")
        if role in ("assistant", "tool"):
            raw = str(m.get("content") or "")[:800]
            if role == "assistant" and m.get("tool_calls"):
                raw += " " + json.dumps(m["tool_calls"])[:500]
            _harvest_tokens_from_text(normalize_compaction_text(raw), paths, numbers, names, seen)
    return KeyTokens(paths=paths, numbers=numbers, names=names)


def verify_compaction_summary(older: List[Dict[str, Any]], summary_text: str) -> Tuple[bool, List[str]]:
    kt = extract_key_tokens(older)
    haystack = normalize_compaction_text(summary_text).replace("\\", "/").lower()
    missing = [t for t in kt.all_tokens() if normalize_compaction_text(t).replace("\\", "/").lower() not in haystack]
    return len(missing) == 0, missing


def build_lossless_phase_handoff(phase: str, raw_text: str, max_chars: int = 6000) -> str:
    clean = normalize_compaction_text(str(raw_text or "").strip())
    if not clean:
        return f"[{phase} Handoff: (empty)]"
    if len(clean) <= max_chars:
        return clean
    head_len = max(400, int(max_chars * 0.55))
    tail_len = max(300, int(max_chars * 0.30))
    probe_msgs = [{"role": "user", "content": clean}]
    preserved_json = [blk for blk in _FENCED_JSON_RE.findall(clean) if len(blk) <= 2200]
    candidate = (
        f"{clean[:head_len].rstrip()}\n"
        f"... [{phase} output compacted ({len(clean)} -> ~{max_chars} chars) with KeyTokens verification] ...\n"
        f"{clean[-tail_len:].lstrip()}"
    )
    for blk in preserved_json:
        if blk not in candidate:
            candidate += f"\n\n[Preserved {phase} Structured JSON]:\n{blk}"
    verified, missing = verify_compaction_summary(probe_msgs, candidate)
    if not verified and missing:
        candidate += f"\n\n[Preserved {phase} KeyTokens]: " + ", ".join(f"`{t}`" for t in missing)
    return candidate


def format_task_carry_forward(target_root: Path, built_rels: List[str], completed_notes: List[str]) -> str:
    if not built_rels and not completed_notes:
        return ""
    file_lines: List[str] = []
    for rel in built_rels[-12:]:
        p = target_root / rel
        if p.is_file():
            lc = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            file_lines.append(f"  - `{rel}` ({lc} lines on disk, verified)")
    parts = ["[WORK ALREADY COMPLETED IN THIS WORKFLOW]"]
    if completed_notes:
        parts.append("Completed Tasks:\n" + "\n".join(f"  - {n}" for n in completed_notes[-6:]))
    if file_lines:
        parts.append("Files Already Created/Modified on Disk:\n" + "\n".join(file_lines))
    parts.append("IMPORTANT: Do NOT recreate files already verified above unless modifying them.")
    return "\n".join(parts) + "\n\n"


def classify_task_autoskip(desc: str, built_rels: List[str], target_root: Path) -> Tuple[bool, bool]:
    if not built_rels:
        return False, False
    norm = normalize_compaction_text(desc or "")
    pure_verify = bool(_VERIFY_ONLY_RE.search(norm)) and not bool(_MUTATION_CUE_RE.search(norm))
    mentioned = [m.strip(".,;:").replace("\\", "/").lstrip("./") for m in _PATH_EXT_RE.findall(norm)]
    built_lower = {r.lower() for r in built_rels}
    already_built = bool(mentioned) and all(
        m.lower() in built_lower and (target_root / m).is_file() and (target_root / m).stat().st_size > 0
        for m in mentioned
    )
    return pure_verify, already_built


def estimate_tools_chars(tools: Optional[List[Dict[str, Any]]] = None) -> int:
    if not tools:
        return 0
    try:
        return len(json.dumps(tools))
    except Exception:
        return len(tools) * 400


def estimate_messages_chars(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> int:
    total = estimate_tools_chars(tools)
    for m in messages:
        total += len(str(m.get("content") or ""))
        if m.get("tool_calls"):
            try:
                total += len(json.dumps(m["tool_calls"]))
            except Exception:
                total += 200
    return total


def estimate_messages_tokens(
    messages: List[Dict[str, Any]], last_usage_tokens: int = 0, tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    char_est = (estimate_messages_chars(messages, tools=tools) // 4) + (len(messages) * 6)
    return max(char_est, int(last_usage_tokens or 0))


def should_compact(
    messages: List[Dict[str, Any]], tier: Optional[str] = None,
    last_usage_tokens: int = 0, tools: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    tier_win = settings.resolve_tier(tier).context_window_tokens
    trig_tokens = int(tier_win * (settings.compact_trigger_pct / 100.0))
    return estimate_messages_tokens(messages, last_usage_tokens=last_usage_tokens, tools=tools) > trig_tokens


def prune_stale_tool_outputs(
    messages: List[Dict[str, Any]], keep_recent: int = 8, max_old_tool_chars: int = 600, protect_from_idx: int = 0,
) -> int:
    cutoff = max(1, len(messages) - max(1, keep_recent))
    if protect_from_idx > 0:
        cutoff = min(cutoff, protect_from_idx)
    chars_freed = 0
    for idx in range(1, cutoff):
        msg = messages[idx]
        if msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if len(content) <= max_old_tool_chars or "[stale tool output pruned" in content:
            continue
        slimmed = (
            f"{content[:260].strip()}\n"
            f"... [stale tool output pruned ({len(content)} -> 460 chars) to preserve context; re-read if needed] ...\n"
            f"{content[-140:].strip()}"
        )
        chars_freed += max(0, len(content) - len(slimmed))
        msg["content"] = slimmed
    return chars_freed


def _default_fast_llm_summarizer(chat_fn: Optional[Callable[..., Dict[str, Any]]] = None) -> Optional[Callable[[str], str]]:
    """Build a real `fast`-tier LLM summarizer unless running inside an unmocked offline unit test."""
    if chat_fn is None and ("unittest" in sys.modules or os.environ.get("COMPANION_TEST_MODE") == "1"):
        return None
    eff_chat = chat_fn or chat

    def _summarize(prompt_text: str) -> str:
        with tier_context("fast"):
            resp = eff_chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Companion's context compaction engine. Summarize the conversation history accurately "
                            "and concisely in structured Markdown sections: (1) User Goals & Preferences, (2) Key Facts, "
                            "Decisions & Outcomes, (3) Files, Paths, Numbers & Open Next Steps. Preserve every file path, "
                            "identifier, and exact figure verbatim."
                        ),
                    },
                    {"role": "user", "content": prompt_text},
                ],
                tools=None,
                tool_choice="none",
            )
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        return str(msg.get("content") or "").strip()

    return _summarize


def _format_transcript_for_compaction(older: List[Dict[str, Any]], max_chars: int = 18000) -> str:
    """Render substantive transcript excerpts for real LLM semantic summarization."""
    chunks: List[str] = []
    used = 0
    for m in older:
        role = str(m.get("role") or "user")
        txt = normalize_compaction_text(str(m.get("content") or "").strip())
        if role == "tool":
            txt = txt[:500]
        elif len(txt) > 1600:
            txt = txt[:1100] + " ... " + txt[-400:]
        if m.get("tool_calls"):
            calls = ", ".join(f"{(tc.get('function') or {}).get('name', '')}" for tc in (m.get("tool_calls") or []))
            txt = f"[tool_calls: {calls}] {txt}".strip()
        if not txt:
            continue
        entry = f"[{role.upper()}]: {txt}"
        if used + len(entry) > max_chars:
            break
        chunks.append(entry)
        used += len(entry)
    return "\n\n".join(chunks)


def _persist_compaction_facts(summary_body: str, mem: MemoryStore) -> int:
    """Persist durable compacted checkpoint facts into `knowledge_facts` and sync `MEMORY.md`."""
    clean = (summary_body or "").strip()
    if not clean:
        return 0
    snippet = re.sub(r"\s+", " ", clean)[:380]
    mem.upsert_knowledge_fact("session:last_compaction_summary", snippet, source="compaction", confidence=0.9)
    if mem.db_path.resolve() == settings.db_path.resolve():
        mem.sync_memory_md()
    return 1


def _build_structured_handoff(
    older: List[Dict[str, Any]],
    summarizer_fn: Optional[Any] = None,
    mem: Optional[MemoryStore] = None,
) -> str:
    """Stage 2 Compaction: Real LLM semantic summary bounded deterministically by `KeyTokens` verification."""
    header = f"[CONTEXT CHECKPOINT HANDOFF — Context Compaction Summary of {len(older)} earlier messages]"
    transcript_view = _format_transcript_for_compaction(older)

    prior_summaries, user_goals, tools_touched, outcomes = [], [], [], []
    for m in older:
        role = m.get("role")
        text = normalize_compaction_text(str(m.get("content") or "").strip()).replace("\n", " ")
        if text.startswith(_HANDOFF_PREFIXES):
            prior_summaries.append(f"- {text[:320]}")
        elif role == "user" and text:
            user_goals.append(f"- {text[:260]}")
        elif role == "assistant" and text not in ("Understood - continuing from checkpoint.", "Understood — continuing from checkpoint."):
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name", "")
                raw_a = str((tc.get("function") or {}).get("arguments") or "")[:90]
                if fn:
                    tools_touched.append(f"- `{fn}({raw_a})`")
            if text:
                outcomes.append(f"- {text[:280]}")

    det_sections = [
        header,
        *(["0. Prior Compacted Session Context:\n" + "\n".join(prior_summaries[-3:])] if prior_summaries else []),
        "1. Earlier User Goals & Requests:\n" + ("\n".join(user_goals[-8:]) if user_goals else "- (session initialization)"),
        "2. Key Tools & Actions Executed:\n" + ("\n".join(tools_touched[-10:]) if tools_touched else "- (conversation only)"),
        "3. Key Decisions & Assistant Outcomes:\n" + ("\n".join(outcomes[-8:]) if outcomes else "- (in progress)"),
    ]
    candidate = "\n\n".join(det_sections)

    if summarizer_fn is not None and transcript_view:
        framed_input = (
            "Summarize the factual progress, user preferences, decisions, and open items in `<conversation>`. "
            "Do NOT continue the dialogue or invent details; preserve all file paths, numbers, and identifiers.\n"
            f"<conversation>\n{transcript_view}\n</conversation>"
        )
        try:
            llm_sum = normalize_compaction_text(str(summarizer_fn(framed_input) or "").strip())
            if llm_sum and not llm_sum.lower().startswith("phase step complete"):
                candidate = f"{header}\n\n{llm_sum[:2600]}"
                if mem is not None:
                    _persist_compaction_facts(llm_sum, mem)
        except Exception:
            pass

    verified, missing_tokens = verify_compaction_summary(older, candidate)
    if not verified and missing_tokens:
        candidate += "\n\n4. Preserved Key Entities (Lossless KeyTokens Verification):\n- " + ", ".join(f"`{t}`" for t in missing_tokens)
    return candidate


def _find_stage2_split_index(body: List[Dict[str, Any]], eff_keep: int) -> Tuple[int, bool]:
    split_idx = max(1, len(body) - eff_keep)
    while split_idx < len(body) and body[split_idx].get("role") != "user":
        split_idx += 1
    if split_idx < len(body) and body[split_idx].get("role") == "user":
        return split_idx, False
    split_idx = max(1, len(body) - eff_keep)
    while split_idx > 1 and body[split_idx].get("role") != "user":
        split_idx -= 1
    if 0 < split_idx < len(body) and body[split_idx].get("role") == "user":
        return split_idx, False
    cand = max(2, len(body) - eff_keep)
    while cand < len(body) and body[cand].get("role") == "tool":
        cand += 1
    if 2 <= cand < len(body) and body[cand].get("role") == "assistant":
        return cand, True
    cand = max(2, len(body) - eff_keep)
    while cand > 1 and body[cand].get("role") != "assistant":
        cand -= 1
    if 2 <= cand < len(body) and body[cand].get("role") == "assistant":
        return cand, True
    return -1, False


def compact_messages_if_needed(
    messages: List[Dict[str, Any]],
    max_chars: Optional[int] = None,
    keep_recent: Optional[int] = None,
    store: Optional[MemoryStore] = None,
    force: bool = False,
    last_usage_tokens: int = 0,
    summarizer_fn: Optional[Any] = None,
    tier: Optional[str] = None,
    system_prompt_fn: Optional[Callable[[], str]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    active_turn_start_idx: int = 0,
    chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Adaptive 2-stage compaction using real `fast`-tier LLM summarization + deterministic `KeyTokens` check."""
    mem = store or get_memory_store()
    eff_tier = tier or mem.get_user_profile().get("active_model_tier", "auto")
    tier_win = settings.resolve_tier(eff_tier).context_window_tokens
    trig_tokens = int(tier_win * (settings.compact_trigger_pct / 100.0))
    schema_chars = estimate_tools_chars(tools)
    trigger_chars = max_chars if max_chars is not None else int(trig_tokens * 4)
    keep = keep_recent if keep_recent is not None else settings.keep_recent_messages
    pre_chars = max(estimate_messages_chars(messages) + schema_chars, int(last_usage_tokens or 0) * 4)
    if not force and (pre_chars <= trigger_chars or len(messages) <= keep + 2):
        return messages

    is_live_db = mem.db_path.resolve() == settings.db_path.resolve()
    exceeds_70pct = pre_chars > int(tier_win * 0.70 * 4)
    protect_idx = 0 if (force or exceeds_70pct or max_chars is not None) else active_turn_start_idx
    freed = prune_stale_tool_outputs(messages, keep_recent=max(2, keep // 2) if not force else 1, protect_from_idx=protect_idx)
    post_stage1_chars = estimate_messages_chars(messages) + schema_chars
    if freed > 0 and is_live_db:
        telemetry.record_span("compaction", "stage1_tool_prune", 0.5, status="ok", detail=f"freed_chars={freed}")
    if not force and post_stage1_chars <= trigger_chars:
        return messages

    eff_keep = keep_recent if keep_recent is not None else (2 if force else settings.keep_recent_messages)
    if len(messages) <= eff_keep + 2:
        return messages

    fallback_sys = (system_prompt_fn or (lambda: build_system_prompt(store=mem)))()
    system_msg = messages[0] if messages and messages[0].get("role") == "system" else {"role": "system", "content": fallback_sys}
    body = messages[1:] if messages and messages[0].get("role") == "system" else list(messages)
    split_idx, starts_with_assistant = _find_stage2_split_index(body, eff_keep)
    if split_idx <= 0 or split_idx >= len(body):
        return messages

    older, recent = body[:split_idx], body[split_idx:]
    eff_summarizer = summarizer_fn if summarizer_fn is not None else _default_fast_llm_summarizer(chat_fn=chat_fn)
    compacted_note = _build_structured_handoff(older, summarizer_fn=eff_summarizer, mem=mem)
    if starts_with_assistant:
        messages[:] = [system_msg, {"role": "user", "content": compacted_note}, *recent]
    else:
        messages[:] = [
            system_msg,
            {"role": "user", "content": compacted_note},
            {"role": "assistant", "content": "Understood — continuing from checkpoint."},
            *recent,
        ]
    after_chars = estimate_messages_chars(messages)
    if is_live_db:
        telemetry.record_span("compaction", "stage2_structured_handoff", 1.0, status="ok", detail=f"msgs={len(older)} chars={pre_chars}->{after_chars}")
    return messages


def format_context_budget_status(
    messages: List[Dict[str, Any]], last_usage_tokens: int = 0, tier: Optional[str] = None, tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    active_tokens = estimate_messages_tokens(messages, last_usage_tokens=last_usage_tokens, tools=tools)
    t_cfg = settings.resolve_tier(tier)
    win = t_cfg.context_window_tokens
    trig = int(win * (settings.compact_trigger_pct / 100.0))
    targ = int(win * (settings.compact_target_pct / 100.0))
    win_pct, trig_pct = (active_tokens / max(1, win)) * 100.0, (active_tokens / max(1, trig)) * 100.0
    comp_flag = "YES" if should_compact(messages, tier=tier, last_usage_tokens=last_usage_tokens, tools=tools) else "NO"
    return (
        "=== Adaptive Context Window & Compaction Status ===\n"
        f"Active Tier & Model   : `{t_cfg.tier}` (`{t_cfg.model}`)\n"
        f"Model Context Window  : {win:,} tokens\n"
        f"High-Watermark Trigger: {settings.compact_trigger_pct:.1f}% ({trig:,} tokens — auto-compacts above this | compact_due={comp_flag})\n"
        f"Low-Watermark Target  : {settings.compact_target_pct:.1f}% ({targ:,} tokens — sweet-spot target)\n"
        f"Current Active Context: ~{active_tokens:,} tokens ({win_pct:.1f}% of window | {trig_pct:.1f}% of trigger budget)\n"
        f"Active Message Count  : {len(messages)} messages ({estimate_messages_chars(messages):,} chars)\n"
        "(Run `/compact` anytime to trigger Stage-1 tool pruning + Stage-2 LLM semantic handoff.)"
    )


def consolidate_turn_memory(
    user_text: str,
    assistant_text: str = "",
    store: Optional[MemoryStore] = None,
    chat_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> int:
    """Distill durable user facts/preferences post-turn (deterministic explicit capture + optional `fast` LLM extraction)."""
    clean = (user_text or "").strip()
    if not clean or clean.startswith(("/", "[")):
        return 0
    mem = store or get_memory_store()
    saved = 0
    for m in _DURABLE_FACT_RE.finditer(clean):
        fact_val = m.group(1).strip(" .,;:")
        slug = re.sub(r"[^a-z0-9]+", "_", fact_val.lower()).strip("_")[:36]
        if slug:
            mem.upsert_knowledge_fact(f"user_note:{slug}", fact_val, source="post_turn", confidence=0.95)
            saved += 1

    # When running live (non-unittest) on a substantive preference/fact turn not caught by explicit regex, ask `fast` LLM
    if (
        saved == 0
        and len(clean) >= 45
        and "unittest" not in sys.modules
        and os.environ.get("COMPANION_TEST_MODE") != "1"
        and any(cue in clean.lower() for cue in ("my ", "i prefer", "i always", "i never", "we use", "our ", "allergic", "diet", "budget"))
    ):
        eff_chat = chat_fn or chat
        try:
            with tier_context("fast"):
                resp = eff_chat(
                    [
                        {
                            "role": "system",
                            "content": (
                                'Extract 0 to 2 durable long-term user facts or preferences from this exchange as strict JSON: '
                                '{"facts": [{"key": "short_snake_key", "value": "concise factual statement"}]}. '
                                'Return {"facts": []} if nothing durable was stated.'
                            ),
                        },
                        {"role": "user", "content": f"User: {clean[:800]}\nAssistant: {(assistant_text or '')[:600]}"},
                    ],
                    tools=None,
                    tool_choice="none",
                )
            raw_c = str((((resp.get("choices") or [{}])[0]).get("message") or {}).get("content") or "")
            jm = re.search(r"\{.*\}", raw_c, re.S)
            if jm:
                parsed = json.loads(jm.group(0))
                for item in (parsed.get("facts") or [])[:2]:
                    if isinstance(item, dict) and item.get("key") and item.get("value"):
                        k_slug = re.sub(r"[^a-z0-9_:-]+", "_", str(item["key"]).lower()).strip("_")[:42]
                        val_s = str(item["value"]).strip()[:240]
                        if k_slug and len(val_s) >= 8:
                            mem.upsert_knowledge_fact(f"user_pref:{k_slug}", val_s, source="llm_distill", confidence=0.88)
                            saved += 1
        except Exception:
            pass

    if saved and mem.db_path.resolve() == settings.db_path.resolve():
        mem.sync_memory_md()
    return saved
