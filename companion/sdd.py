"""
Living Canonical Spec-Driven Development (SDD) Engine (A-Frame Domain Layer).

Solves the "Spec Sprawl & ADR Pile-Up" problem of traditional SDD frameworks by
enforcing a strict 4-Document Living Spec Pyramid in `specs/`:
  1. specs/PRODUCT.md      — Vision, User Persona, Capability Matrix & Product Invariants
  2. specs/ARCHITECTURE.md — A-Frame Layers, Authoritative Module Registry & Budgets
  3. specs/CONTRACTS.md    — Bounded Subsystem Contracts (SPEC-01..SPEC-06) & Test Links
  4. specs/DECISIONS.md    — Self-Compacting Architecture Decision Records (ADRs)

Provides bidirectional Spec <-> Code drift verification and anti-sprawl enforcement.
"""

import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from companion.config import MODEL_TIERS, settings
from companion.disciplines import FORBIDDEN_EXTERNAL_IMPORTS, iter_ast_import_targets
from companion.memory_store import MemoryStore, get_memory_store

_STDLIB_OR_LOCAL_IMPORTS = {m.lower() for m in sys.stdlib_module_names} | {
    "companion", "surfaces", "configs", "memory", "harness", "__future__"
}


def is_undeclared_skill_import(top_pkg: str, allowed_imports: Set[str], sibling_stems: Iterable[str] = ()) -> bool:
    """Return `True` if `top_pkg` is a third-party package not declared in PEP 723 `dependencies`."""
    low = (top_pkg or "").strip().lower()
    if not low or low in allowed_imports:
        return False
    if low in FORBIDDEN_EXTERNAL_IMPORTS:
        return True
    return low not in _STDLIB_OR_LOCAL_IMPORTS and low not in {s.lower() for s in sibling_stems}


CANONICAL_SPEC_FILES = (
    "PRODUCT.md",
    "ARCHITECTURE.md",
    "CONTRACTS.md",
    "DECISIONS.md",
)
MAX_ACTIVE_ADRS = 19
ALLOWED_SKILL_SUBDIRS = {"references", "workflows", "scripts"}

_MODULE_PATH_RE = re.compile(r"`((?:companion|configs|memory|surfaces|tests)/[a-zA-Z0-9_]+\.py|harness\.py)`")
_SPEC_HEADER_RE = re.compile(r"^##\s+(SPEC-\d+:\s+.+)$", re.MULTILINE)
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SKILL_ASSET_REF_RE = re.compile(
    r"(?<![a-zA-Z0-9_<*/-])((?:skills/[a-z0-9-]+/)?(?:references/[a-zA-Z0-9_-]+\.md|workflows/[a-zA-Z0-9_-]+\.json|scripts/[a-zA-Z0-9_-]+\.py))(?![a-zA-Z0-9_>*-])"
)


@dataclass
class SDDVerificationReport:
    """Result of bidirectional Spec-Driven Development verification."""

    valid: bool
    present_spec_files: List[str] = field(default_factory=list)
    missing_spec_files: List[str] = field(default_factory=list)
    sprawl_files: List[str] = field(default_factory=list)
    undocumented_modules: List[str] = field(default_factory=list)
    missing_referenced_files: List[str] = field(default_factory=list)
    contract_ids: List[str] = field(default_factory=list)
    contract_issues: List[str] = field(default_factory=list)
    active_adr_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "present_spec_files": self.present_spec_files,
            "missing_spec_files": self.missing_spec_files,
            "sprawl_files": self.sprawl_files,
            "undocumented_modules": self.undocumented_modules,
            "missing_referenced_files": self.missing_referenced_files,
            "contract_ids": self.contract_ids,
            "contract_issues": self.contract_issues,
            "active_adr_count": self.active_adr_count,
        }

    def format_summary(self) -> str:
        status = "SYNCED (PASS)" if self.valid else "SPEC DRIFT / SPRAWL DETECTED (FAIL)"
        lines = [
            f"=== Spec-Driven Development (SDD) Report: {status} ===",
            f"Canonical Specs : {len(self.present_spec_files)}/4 present ({', '.join(self.present_spec_files)})",
            f"Subsystem Specs : {len(self.contract_ids)} contracts verified",
            f"Active ADRs     : {self.active_adr_count}/{MAX_ACTIVE_ADRS} budget",
        ]
        if self.valid:
            lines.append("  * Zero spec file sprawl (strictly 4 canonical files in specs/)")
            lines.append("  * 100% bidirectional alignment between companion/*.py modules and specs/")
            lines.append("  * All subsystem contracts linked to existing verification tests")
            return "\n".join(lines)

        if self.missing_spec_files:
            lines.append(f"  [!] Missing Canonical Spec Files: {self.missing_spec_files}")
        if self.sprawl_files:
            lines.append(
                f"  [!] Spec Sprawl Violation (extra files in specs/ — consolidate into the 4 canonical files): {self.sprawl_files}"
            )
        if self.undocumented_modules:
            lines.append(f"  [!] Undocumented Code Modules (missing from specs/ARCHITECTURE.md or CONTRACTS.md): {self.undocumented_modules}")
        if self.missing_referenced_files:
            lines.append(f"  [!] Phantom Files Referenced in Specs (do not exist on disk): {self.missing_referenced_files}")
        if self.contract_issues:
            lines.append(f"  [!] Contract Structure Issues: {self.contract_issues}")
        return "\n".join(lines)


def sync_decisions_md(store: Optional[MemoryStore] = None, root: Optional[Path] = None) -> Path:
    """Render `specs/DECISIONS.md` from SQLite ADRs with automatic compaction of superseded ADRs."""
    mem = store or get_memory_store()
    default_root = settings.root if mem.db_path.resolve() == settings.db_path.resolve() else mem.db_path.parent
    target_root = (root or default_root).resolve()
    specs_dir = target_root / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = specs_dir / "DECISIONS.md"

    adrs = mem.get_adrs()
    active_adrs = [a for a in adrs if a.get("status", "accepted") == "accepted"]
    superseded_adrs = [a for a in adrs if a.get("status") != "accepted"]

    # If active ADRs exceed MAX_ACTIVE_ADRS, compact oldest beyond limit into historical summary
    if len(active_adrs) > MAX_ACTIVE_ADRS:
        overflow = active_adrs[:-MAX_ACTIVE_ADRS]
        active_adrs = active_adrs[-MAX_ACTIVE_ADRS:]
        superseded_adrs = overflow + superseded_adrs

    lines = [
        "# Companion AI OS — Architecture Decision Records (Living ADR Log)",
        "",
        "> **Anti-Pileup Policy**: This single canonical document replaces scattered `ADR-xxx.md` files.",
        f"> At most **{MAX_ACTIVE_ADRS} active decisions** are kept in full detail. Superseded decisions",
        "> are automatically compacted into single-line historical notes at the bottom.",
        "",
        "## Active Architectural Decisions",
        "",
    ]

    for adr in active_adrs:
        lines.extend([
            f"### {adr['title']}",
            f"- **Status**: `{adr['status']}`",
            f"- **Context**: {adr['context']}",
            f"- **Decision**: {adr['decision']}",
            f"- **Consequences**: {adr['consequences']}",
            "",
        ])

    lines.append("## Historical / Superseded Decisions (Compacted)")
    if superseded_adrs:
        for old in superseded_adrs:
            lines.append(f"- **{old['title']}** (`{old['status']}`): {old['decision']}")
    else:
        lines.append("- *(None yet — all foundational ADRs are currently active.)*")
    lines.append("")

    decisions_path.write_text("\n".join(lines), encoding="utf-8")
    return decisions_path


def validate_skill_markdown(content: str, expected_name: Optional[str] = None) -> Optional[str]:
    """Validate a `skills/<name>/SKILL.md` file's YAML frontmatter and required procedural headings."""
    norm = content.replace("\r\n", "\n").strip()
    if not norm.startswith("---\n"):
        return "SKILL.md must start with YAML frontmatter delimited by `---`."
    end_idx = norm.find("\n---", 4)
    if end_idx == -1:
        return "SKILL.md is missing closing `---` for YAML frontmatter."
    fm_block = norm[4:end_idx]
    body = norm[end_idx + 4 :].strip()
    meta: Dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip("'\"")
    name = meta.get("name", "")
    if not name or not _KEBAB_RE.match(name):
        return f"SKILL.md frontmatter `name` must be kebab-case (`[a-z0-9-]+`), got `{name}`."
    if expected_name and _KEBAB_RE.match(expected_name) and name != expected_name:
        return f"SKILL.md frontmatter `name: {name}` does not match folder name `{expected_name}`."
    if len(meta.get("description", "")) < 10:
        return "SKILL.md frontmatter must include a concise `description` (>= 10 chars)."
    if "required_tools" not in meta or not meta["required_tools"]:
        return "SKILL.md frontmatter must include `required_tools: [...]`."
    if not re.search(r"^##\s+When to Use", body, re.IGNORECASE | re.MULTILINE):
        return "SKILL.md body must include a `## When to Use This Skill` section."
    if not re.search(r"^##\s+.*(?:Procedure|How to)", body, re.IGNORECASE | re.MULTILINE):
        return "SKILL.md body must include a `## Step-by-Step Procedure` (or `## How to ...`) section."
    return None


def validate_workflow_recipe_json(content: str, expected_name: Optional[str] = None) -> Optional[str]:
    """Validate a declarative JSON workflow recipe (`skills/<name>/workflows/<recipe>.json`)."""
    try:
        data = json.loads(content)
    except Exception as exc:
        return f"Invalid JSON in workflow recipe: {exc}"
    if not isinstance(data, dict):
        return "Workflow recipe root must be a JSON object."
    name = str(data.get("name") or "").strip()
    if not name or not _KEBAB_RE.match(name):
        return f"Workflow recipe `name` must be kebab-case (`[a-z0-9-]+`), got `{name}`."
    if expected_name and _KEBAB_RE.match(expected_name) and name != expected_name:
        return f"Workflow recipe `name: {name}` does not match filename stem `{expected_name}`."
    if len(str(data.get("description") or "").strip()) < 10:
        return "Workflow recipe must include a `description` (>= 10 chars)."
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return "Workflow recipe must include a non-empty `nodes` list."
    node_ids: Set[str] = set()
    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            return f"Node #{idx} must be a JSON object."
        nid = str(node.get("id") or "").strip()
        ntype = str(node.get("type") or "").strip()
        if not nid or nid in ("COMPLETE", "ABORTED") or nid in node_ids:
            return f"Node #{idx} has invalid, reserved, or duplicate `id`: `{nid}`."
        node_ids.add(nid)
        if ntype not in ("tool", "subagent", "llm", "recipe"):
            return f"Node `{nid}` has invalid `type: {ntype}` (must be `tool`, `subagent`, `llm`, or `recipe`)."
        if ntype == "tool" and not str(node.get("tool") or "").strip():
            return f"Tool node `{nid}` must specify a non-empty `tool` name."
        if ntype == "recipe" and not str(node.get("recipe") or "").strip():
            return f"Recipe node `{nid}` must specify a non-empty `recipe` name."
        if ntype in ("subagent", "llm") and not str(node.get("prompt") or "").strip():
            return f"Node `{nid}` (`{ntype}`) must specify a non-empty `prompt`."
        ntier = str(node.get("tier") or "").strip().lower()
        if ntier and ntier not in MODEL_TIERS:
            return f"Node `{nid}` has invalid `tier: {ntier}` (must be one of {list(MODEL_TIERS)})."
    edges = data.get("edges")
    if not isinstance(edges, list) or not edges:
        return "Workflow recipe must include a non-empty `edges` list."
    valid_targets = node_ids | {"COMPLETE", "ABORTED"}
    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            return f"Edge #{idx} must be a JSON object."
        src, dst = str(edge.get("from") or "").strip(), str(edge.get("to") or "").strip()
        cond = str(edge.get("condition") or "always").strip()
        if src not in node_ids:
            return f"Edge #{idx} `from: {src}` does not match any node `id`."
        if dst not in valid_targets:
            return f"Edge #{idx} `to: {dst}` must be a valid node `id`, `COMPLETE`, or `ABORTED`."
        if cond not in ("always", "passed", "failed") and not cond.startswith(("contains:", "not_contains:")):
            return f"Edge #{idx} (`{src}->{dst}`) has invalid `condition: {cond}`."
    return None


_WIKI_USER_AUTHOR_RE = re.compile(r"(?im)^author\s*:\s*['\"]?(user|human|partner)\b")
_PEP723_BLOCK_RE = re.compile(r"(?m)^# /// script\s*\n((?:#(?: .*|)\n)*?)# ///\s*$")
_DEP_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PYPI_IMPORT_ALIASES: Dict[str, str] = {
    "beautifulsoup4": "bs4",
    "pillow": "pil",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pymupdf": "fitz",
    "python-dotenv": "dotenv",
}


def parse_pep723_dependencies(source: str) -> Tuple[List[str], Set[str], Optional[str]]:
    """Parse PEP 723 `# /// script` metadata, returning `(raw_deps, allowed_import_roots, error)`."""
    match = _PEP723_BLOCK_RE.search((source or "").replace("\r\n", "\n"))
    if not match:
        return [], set(), None
    uncommented = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in match.group(1).splitlines())
    try:
        meta = tomllib.loads(uncommented)
    except Exception as exc:
        return [], set(), f"Invalid PEP 723 `# /// script` TOML metadata: {exc}"
    deps = meta.get("dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        return [], set(), "PEP 723 `dependencies` must be a list of package requirement strings."
    raw_deps: List[str] = []
    allowed_imports: Set[str] = set()
    for req in deps:
        clean_req = req.strip()
        if not clean_req:
            continue
        raw_deps.append(clean_req)
        m = _DEP_NAME_RE.match(clean_req)
        if m:
            pkg = m.group(1).lower()
            norm = pkg.replace("-", "_").replace(".", "_")
            allowed_imports.update({pkg, norm})
            if pkg in _PYPI_IMPORT_ALIASES:
                allowed_imports.add(_PYPI_IMPORT_ALIASES[pkg])
    return raw_deps, allowed_imports, None


def check_wiki_author_guard(path: Path) -> Optional[str]:
    """Block overwriting an existing Knowledge Wiki reference article that carries `author: user` provenance."""
    if path.suffix.lower() != ".md" or path.parent.name.lower() != "references" or not path.is_file():
        return None
    head = path.read_text(encoding="utf-8", errors="replace")[:800]
    if _WIKI_USER_AUTHOR_RE.search(head):
        return (
            f"WIKI AUTHOR GUARD REJECTED {path}:\n"
            "  Existing Knowledge Wiki article has `author: user` provenance and cannot be overwritten by Companion.\n"
            "File was NOT modified."
        )
    return None


def validate_skill_or_recipe_path(path: Path, content: str) -> Optional[str]:
    """If `path` targets `skills/<name>/SKILL.md`, `workflows/*.json`, `scripts/*.py`, or Wiki articles, enforce guards."""
    parts = [pt.lower() for pt in path.parts]
    if "skills" not in parts:
        return None
    author_err = check_wiki_author_guard(path)
    if author_err:
        return author_err
    if path.name == "SKILL.md":
        err = validate_skill_markdown(content, expected_name=path.parent.name)
        return f"SKILL LINTER REJECTED {path}:\n  {err}\nFile was NOT modified. Fix SKILL.md structure and retry." if err else None
    if path.suffix.lower() == ".json" and path.parent.name.lower() == "workflows":
        err = validate_workflow_recipe_json(content, expected_name=path.stem)
        return f"WORKFLOW RECIPE LINTER REJECTED {path}:\n  {err}\nFile was NOT modified. Fix recipe JSON and retry." if err else None
    if path.suffix.lower() == ".py" and path.parent.name.lower() == "scripts":
        if "--self-test" not in content:
            return f"SKILL LINTER REJECTED {path}:\n  Skill script must support a `--self-test` mode.\nFile was NOT modified."
        _raw_deps, allowed_imports, pep_err = parse_pep723_dependencies(content)
        if pep_err:
            return f"SKILL LINTER REJECTED {path}:\n  {pep_err}\nFile was NOT modified."
        try:
            tree = ast.parse(content, filename=path.name)
        except SyntaxError:
            return None
        sibs = [p.stem for p in path.parent.glob("*.py")] if path.parent.is_dir() else []
        for target, lineno in iter_ast_import_targets(tree):
            top_pkg = target.split(".")[0].lower()
            if is_undeclared_skill_import(top_pkg, allowed_imports, sibling_stems=sibs):
                return (
                    f"SKILL LINTER REJECTED {path}:{lineno}:\n"
                    f"  Undeclared third-party import `{top_pkg}` violates ADR-001. Declare it in a PEP 723 `# /// script` "
                    "`dependencies = [...]` block for isolated `uv run` execution, or use Python stdlib.\nFile was NOT modified."
                )
    return None


def verify_skills_directory(root: Optional[Path] = None) -> List[str]:
    """Verify all 3-Tier Skill Packs under `skills/` (scaffolding, SKILL.md, workflows/*.json, and Wiki index)."""
    target_root = (root or settings.root).resolve()
    skills_dir = target_root / "skills"
    if not skills_dir.is_dir():
        return []
    issues: List[str] = []
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if not entry.is_dir():
            if entry.name != "README.md":
                issues.append(f"Loose file in `skills/{entry.name}` — every skill must live in `skills/<kebab-name>/`.")
            continue
        if not _KEBAB_RE.match(entry.name):
            issues.append(f"Skill folder `skills/{entry.name}` must use kebab-case (`[a-z0-9-]+`).")
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            issues.append(f"Skill folder `skills/{entry.name}` is missing required `SKILL.md`.")
        else:
            md_text = skill_md.read_text(encoding="utf-8", errors="replace")
            err = validate_skill_markdown(md_text, expected_name=entry.name)
            if err:
                issues.append(f"`skills/{entry.name}/SKILL.md`: {err}")
            for ref_match in _SKILL_ASSET_REF_RE.findall(md_text):
                ref_path = (target_root / ref_match) if ref_match.startswith("skills/") else (entry / ref_match)
                if not ref_path.is_file():
                    issues.append(f"`skills/{entry.name}/SKILL.md` references missing Tier-3 file `{ref_match}`.")
        for sub in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
            if sub.name.startswith(".") or sub.name in ("__pycache__", "SKILL.md"):
                continue
            if sub.is_dir():
                if sub.name not in ALLOWED_SKILL_SUBDIRS:
                    issues.append(f"Disallowed subfolder `skills/{entry.name}/{sub.name}/` (allowed: {sorted(ALLOWED_SKILL_SUBDIRS)}).")
                elif sub.name == "workflows":
                    for wf_file in sorted(sub.glob("*.json")):
                        wf_err = validate_workflow_recipe_json(wf_file.read_text(encoding="utf-8", errors="replace"), expected_name=wf_file.stem)
                        if wf_err:
                            issues.append(f"`skills/{entry.name}/workflows/{wf_file.name}`: {wf_err}")
            else:
                issues.append(f"Unexpected root file `skills/{entry.name}/{sub.name}` (place in `references/`, `workflows/`, or `scripts/`).")

    wiki_refs = skills_dir / "knowledge-wiki" / "references"
    index_md = wiki_refs / "INDEX.md"
    if wiki_refs.is_dir() and index_md.is_file():
        idx_text = index_md.read_text(encoding="utf-8", errors="replace")
        for md_file in sorted(wiki_refs.glob("*.md")):
            if md_file.name != "INDEX.md" and md_file.name not in idx_text:
                issues.append(f"Unindexed Knowledge Wiki article `skills/knowledge-wiki/references/{md_file.name}` (add to `INDEX.md`).")
    return issues


def verify_sdd(root: Optional[Path] = None, store: Optional[MemoryStore] = None) -> SDDVerificationReport:
    """Verify the 4-Document Living Spec Pyramid and bidirectional Spec <-> Code alignment."""
    target_root = (root or settings.root).resolve()
    specs_dir = target_root / "specs"
    report = SDDVerificationReport(valid=True)

    if not specs_dir.is_dir():
        report.valid = False
        report.missing_spec_files = list(CANONICAL_SPEC_FILES)
        return report

    # 1. Check canonical files & anti-sprawl rule
    existing_files = sorted(f.name for f in specs_dir.iterdir() if f.is_file())
    for canon in CANONICAL_SPEC_FILES:
        if canon in existing_files:
            report.present_spec_files.append(canon)
        else:
            report.missing_spec_files.append(canon)

    for fname in existing_files:
        if fname not in CANONICAL_SPEC_FILES:
            report.sprawl_files.append(fname)

    # 2. Check bidirectional module mapping in ARCHITECTURE.md and CONTRACTS.md
    arch_path = specs_dir / "ARCHITECTURE.md"
    contracts_path = specs_dir / "CONTRACTS.md"
    arch_refs: Set[str] = set()
    contract_refs: Set[str] = set()

    if arch_path.is_file():
        arch_text = arch_path.read_text(encoding="utf-8", errors="replace")
        arch_refs.update(_MODULE_PATH_RE.findall(arch_text))

    if contracts_path.is_file():
        contracts_text = contracts_path.read_text(encoding="utf-8", errors="replace")
        contract_refs.update(_MODULE_PATH_RE.findall(contracts_text))
        report.contract_ids = [m.strip() for m in _SPEC_HEADER_RE.findall(contracts_text)]
        if not report.contract_ids:
            report.contract_issues.append("No `## SPEC-XX: ...` sections found in specs/CONTRACTS.md")
        else:
            blocks = re.split(r"(?=^##\s+SPEC-\d+:)", contracts_text, flags=re.MULTILINE)
            for block in [b for b in blocks if b.lstrip().startswith("## SPEC-")]:
                header = block.splitlines()[0].strip()
                for req_section in ("Owned Modules:", "Invariants:", "Verification Tests:"):
                    if req_section not in block:
                        report.contract_issues.append(f"Missing `{req_section}` in `{header}` of specs/CONTRACTS.md")

    # Verify every referenced module/test file in ARCHITECTURE.md and CONTRACTS.md exists on disk
    all_referenced = sorted(arch_refs | contract_refs)
    for ref_rel in all_referenced:
        if not (target_root / ref_rel).is_file():
            report.missing_referenced_files.append(ref_rel)

    # Verify every actual module in companion/*.py and surfaces/*.py (excluding __init__.py) is documented in both ARCHITECTURE.md & CONTRACTS.md
    for pkg_name in ("companion", "surfaces"):
        pkg_dir = target_root / pkg_name
        if pkg_dir.is_dir():
            for py_mod in sorted(pkg_dir.glob("*.py")):
                if py_mod.name == "__init__.py":
                    continue
                rel = f"{pkg_name}/{py_mod.name}"
                if rel not in arch_refs or rel not in contract_refs:
                    report.undocumented_modules.append(rel)

    # 3. Check ADR budget and 3-Tier Skill Pack integrity
    mem = store or get_memory_store()
    active_adrs = [a for a in mem.get_adrs() if a.get("status", "accepted") == "accepted"]
    report.active_adr_count = len(active_adrs)
    if report.active_adr_count > MAX_ACTIVE_ADRS:
        report.contract_issues.append(
            f"Active ADR count ({report.active_adr_count}) exceeds MAX_ACTIVE_ADRS ({MAX_ACTIVE_ADRS}); run sync_decisions_md to compact."
        )
    report.contract_issues.extend(verify_skills_directory(target_root))

    report.valid = not (
        report.missing_spec_files
        or report.sprawl_files
        or report.undocumented_modules
        or report.missing_referenced_files
        or report.contract_issues
    )
    return report
