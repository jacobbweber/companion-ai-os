"""
Codebase Hygiene, Import-Qualified Dead-Code Graph & AST Repo Radar (A-Frame Domain Layer).

Provides deterministic Python AST analysis for:
1. Import-qualified dead-code detection & unused-import checks
2. Simplicity budget enforcement (`<=550L/module`, `<=100L/function`, zero orphan files)
3. Fast (`<15ms`, cached) AST Repo & Line-Budget Headroom Map for Step-0 orientation
4. AST symbol slicing (`extract_python_symbol`) for surgical `read_file(..., symbol=...)`
"""

import ast
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from companion.config import SKIP_DIRS, settings
from companion.disciplines import MODULE_LAYER_MAP, walk_pruned_files


ALLOWED_TOP_DIRS = {"companion", "configs", "memory", "skills", "surfaces", "tests"}
ALLOWED_ROOT_FILES = {"harness.py"}
GOVERNED_SYMBOL_DIRS = {"companion", "surfaces", "configs"}
GOVERNED_IMPORT_PREFIXES = ("companion.", "surfaces.", "configs.")
REEXPORT_FACADES = {
    "harness.py",
    "companion/__init__.py",
    "configs/__init__.py",
    "memory/__init__.py",
    "memory/chat_memory.py",
    "surfaces/__init__.py",
}


@dataclass
class HygieneReport:
    """Structured report from the AST Dead-Code & Simplicity Scanner."""

    clean: bool
    scanned_files: int = 0
    total_lines: int = 0
    unused_imports: List[str] = field(default_factory=list)
    dead_definitions: List[str] = field(default_factory=list)
    oversized_modules: List[str] = field(default_factory=list)
    oversized_functions: List[str] = field(default_factory=list)
    orphan_files: List[str] = field(default_factory=list)
    semicolon_chains: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clean": self.clean,
            "scanned_files": self.scanned_files,
            "total_lines": self.total_lines,
            "unused_imports": self.unused_imports,
            "dead_definitions": self.dead_definitions,
            "oversized_modules": self.oversized_modules,
            "oversized_functions": self.oversized_functions,
            "orphan_files": self.orphan_files,
            "semicolon_chains": self.semicolon_chains,
        }

    def format_summary(self) -> str:
        status = "CLEAN (PASS)" if self.clean else "HYGIENE ISSUES DETECTED (NEEDS SIMPLIFICATION)"
        lines = [
            f"=== Codebase Hygiene & Simplicity Report: {status} ===",
            f"Scanned Files : {self.scanned_files} Python files ({self.total_lines} total lines)",
        ]
        if self.clean:
            lines.extend([
                "  * Zero unused imports",
                "  * Zero dead/unreferenced functions or classes (import-qualified graph)",
                "  * Zero orphan Python files",
                "  * All modules and functions within simplicity line budgets",
            ])
            return "\n".join(lines)

        sections = [
            ("Unused Imports", self.unused_imports),
            ("Dead / Unreferenced Definitions", self.dead_definitions),
            ("Oversized Modules", self.oversized_modules),
            ("Oversized Functions", self.oversized_functions),
            ("Orphan Files", self.orphan_files),
            ("Semicolon-Chained Lines", self.semicolon_chains),
        ]
        for title, items in sections:
            if items:
                lines.append(f"\n{title} ({len(items)}):")
                lines.extend(f"  - {item}" for item in items)
        return "\n".join(lines)


def _collect_all_exports(tree: ast.AST) -> Set[str]:
    """Collect string literals listed in module `__all__ = [...]`."""
    exported: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            exported.add(elt.value)
    return exported


def _collect_used_names(tree: ast.AST) -> Set[str]:
    """Collect all identifier names referenced in an AST (including `__all__` strings)."""
    used = _collect_all_exports(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            used.add(node.id)
    return used


def find_unused_imports_in_source(tree: ast.Module, rel_path: str) -> List[str]:
    """Return formatted unused-import strings for a single parsed Python module."""
    if rel_path in REEXPORT_FACADES:
        return []
    used_in_file = _collect_used_names(tree)
    unused: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound not in used_in_file:
                    unused.append(f"{rel_path}:{node.lineno} unused import `{bound}`")
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for alias in node.names:
                if alias.name != "*":
                    bound = alias.asname or alias.name
                    if bound not in used_in_file:
                        unused.append(f"{rel_path}:{node.lineno} unused import `{bound}`")
    return unused


def _collect_func_param_names(fn_node: ast.AST) -> Set[str]:
    """Collect parameter names of a function so shadowed arg names do not mask dead globals."""
    if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    args = fn_node.args
    names = {a.arg for a in (args.posonlyargs + args.args + args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _check_file_definitions(
    tree: ast.Module,
    rel_path: str,
    top_dir: str,
    fn_limit: int,
    report: HygieneReport,
    defined_symbols: List[Tuple[str, str, str, int]],
    private_methods: List[Tuple[str, str, str, int]],
) -> None:
    """Check function line budgets and collect top-level symbols across governed packages."""
    dotted_mod = rel_path.removesuffix(".py").replace("/", ".")
    in_governed = top_dir in GOVERNED_SYMBOL_DIRS
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_len = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
            if fn_len > fn_limit:
                report.oversized_functions.append(f"{rel_path}:{node.lineno} `{node.name}()` ({fn_len} lines > budget {fn_limit})")
            if node.args.args and node.args.args[0].arg in ("self", "cls"):
                report.dead_definitions.append(
                    f"{rel_path}:{node.lineno} `{node.name}({node.args.args[0].arg})` is at module top-level (mis-indented outside its class)"
                )
            elif in_governed and not node.name.startswith("__"):
                defined_symbols.append((rel_path, dotted_mod, node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            if in_governed and not node.name.startswith("__"):
                defined_symbols.append((rel_path, dotted_mod, node.name, node.lineno))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_len = getattr(sub, "end_lineno", sub.lineno) - sub.lineno + 1
                    if fn_len > fn_limit:
                        report.oversized_functions.append(
                            f"{rel_path}:{sub.lineno} `{node.name}.{sub.name}()` ({fn_len} lines > budget {fn_limit})"
                        )
                    if in_governed and sub.name.startswith("_") and not sub.name.startswith("__"):
                        private_methods.append((rel_path, node.name, sub.name, sub.lineno))


def _record_module_imports_and_refs(
    tree: ast.Module,
    rel_path: str,
    intra_refs: Dict[str, Set[str]],
    attr_refs: Dict[str, Set[str]],
    imported_from_mod: Dict[str, Set[str]],
    star_imported_mods: Set[str],
    dynamic_getattr_names: Set[str],
) -> None:
    """Populate intra-file symbol references, attribute references, and cross-file module-qualified bindings."""
    pkg = rel_path.rsplit("/", 1)[0].replace("/", ".") if "/" in rel_path else ""
    file_intra: Set[str] = set()
    file_attrs: Set[str] = set()
    mod_aliases: Dict[str, str] = {}

    for stmt in tree.body:
        self_name = stmt.name if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else None
        shadowed_params = _collect_func_param_names(stmt)
        for node in ast.walk(stmt):
            if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                if node.id != self_name and node.id not in shadowed_params:
                    file_intra.add(node.id)
            elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Store):
                file_attrs.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(GOVERNED_IMPORT_PREFIXES):
                        bound = alias.asname or alias.name.split(".")[-1]
                        mod_aliases[bound] = alias.name
            elif isinstance(node, ast.ImportFrom):
                base_mod = node.module or ""
                if node.level and pkg:
                    pkg_parts = [p for p in pkg.split(".") if p]
                    up = max(0, node.level - 1)
                    parent_pkg = ".".join(pkg_parts[: max(0, len(pkg_parts) - up)])
                    base_mod = f"{parent_pkg}.{base_mod}".strip(".") if base_mod else parent_pkg
                for alias in node.names:
                    if base_mod in GOVERNED_SYMBOL_DIRS:
                        mod_aliases[alias.asname or alias.name] = f"{base_mod}.{alias.name}"
                    elif base_mod.startswith(GOVERNED_IMPORT_PREFIXES):
                        if alias.name == "*":
                            star_imported_mods.add(base_mod)
                        else:
                            imported_from_mod.setdefault(base_mod, set()).add(alias.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    dynamic_getattr_names.add(node.args[1].value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in mod_aliases:
            imported_from_mod.setdefault(mod_aliases[node.value.id], set()).add(node.attr)

    intra_refs[rel_path] = file_intra
    attr_refs[rel_path] = file_attrs


def find_semicolon_chains(tree: ast.AST, rel_path: str) -> List[str]:
    """Detect semicolon-chained multi-statement lines (`stmt1; stmt2`) used to game line budgets."""
    found: List[str] = []
    seen_lines: Set[int] = set()
    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if isinstance(block, list):
                for idx in range(len(block) - 1):
                    end_ln = getattr(block[idx], "end_lineno", block[idx].lineno)
                    if end_ln == block[idx + 1].lineno and end_ln not in seen_lines:
                        seen_lines.add(end_ln)
                        found.append(f"{rel_path}:{end_ln} chains multiple statements with `;` on one line")
    return found


def analyze_workspace_hygiene(
    root: Optional[Path] = None,
    max_module_lines: Optional[int] = None,
    max_function_lines: Optional[int] = None,
) -> HygieneReport:
    """Perform a full deterministic AST scan with import-qualified dead-code detection and line budgets."""
    target_root = (root or settings.root).resolve()
    mod_limit = max_module_lines or settings.max_module_lines
    fn_limit = max_function_lines or settings.max_function_lines

    report = HygieneReport(clean=True)
    defined_symbols: List[Tuple[str, str, str, int]] = []
    private_methods: List[Tuple[str, str, str, int]] = []
    intra_refs: Dict[str, Set[str]] = {}
    attr_refs: Dict[str, Set[str]] = {}
    imported_from_mod: Dict[str, Set[str]] = {}
    star_imported_mods: Set[str] = set()
    dynamic_getattr_names: Set[str] = set()
    all_global_attrs: Set[str] = set()

    for py_file in (p for p in walk_pruned_files(target_root) if p.suffix.lower() == ".py"):
        rel_path = str(py_file.relative_to(target_root)).replace("\\", "/")
        parts = rel_path.split("/")

        if len(parts) == 1 and parts[0] not in ALLOWED_ROOT_FILES:
            report.orphan_files.append(f"{rel_path} (root file not in {sorted(ALLOWED_ROOT_FILES)})")
        elif len(parts) > 1 and parts[0] not in ALLOWED_TOP_DIRS:
            report.orphan_files.append(f"{rel_path} (directory '{parts[0]}' not in {sorted(ALLOWED_TOP_DIRS)})")

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            lines = source.splitlines()
            report.scanned_files += 1
            report.total_lines += len(lines)
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue

        if len(lines) > mod_limit:
            report.oversized_modules.append(f"{rel_path} ({len(lines)} lines > budget {mod_limit})")

        _check_file_definitions(tree, rel_path, parts[0], fn_limit, report, defined_symbols, private_methods)
        if parts[0] not in ("tests", "skills"):
            report.semicolon_chains.extend(find_semicolon_chains(tree, rel_path))
        report.unused_imports.extend(find_unused_imports_in_source(tree, rel_path))
        _record_module_imports_and_refs(
            tree, rel_path, intra_refs, attr_refs, imported_from_mod, star_imported_mods, dynamic_getattr_names
        )
        all_global_attrs.update(attr_refs.get(rel_path, set()))

    for rel_path, dotted_mod, sym_name, lineno in defined_symbols:
        alive = (
            sym_name in intra_refs.get(rel_path, set())
            or sym_name in imported_from_mod.get(dotted_mod, set())
            or dotted_mod in star_imported_mods
            or sym_name in dynamic_getattr_names
        )
        if not alive:
            report.dead_definitions.append(
                f"{rel_path}:{lineno} `{sym_name}` is never referenced intra-module or imported from `{dotted_mod}`"
            )

    for rel_path, cls_name, meth_name, lineno in private_methods:
        if meth_name not in all_global_attrs and meth_name not in dynamic_getattr_names:
            report.dead_definitions.append(
                f"{rel_path}:{lineno} `{cls_name}.{meth_name}()` private method is never called"
            )

    report.clean = not (
        report.unused_imports
        or report.dead_definitions
        or report.oversized_modules
        or report.oversized_functions
        or report.orphan_files
        or report.semicolon_chains
    )
    return report


def extract_python_symbol(
    source: str,
    symbol: str,
    rel_path: str = "<file>",
) -> Tuple[Optional[Tuple[int, int, str]], List[str]]:
    """Locate a Python `class`, `def`, or `Class.method` AST span `(start_line, end_line, kind)`."""
    target = (symbol or "").strip()
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return None, []

    available: List[str] = []
    exact_match: Optional[Tuple[int, int, str]] = None
    fallback_match: Optional[Tuple[int, int, str]] = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available.append(node.name)
            if node.name == target and exact_match is None:
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                exact_match = (start, getattr(node, "end_lineno", node.lineno), "function")
        elif isinstance(node, ast.ClassDef):
            available.append(node.name)
            if node.name == target and exact_match is None:
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                exact_match = (start, getattr(node, "end_lineno", node.lineno), "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{node.name}.{sub.name}"
                    available.append(qual)
                    start = sub.decorator_list[0].lineno if sub.decorator_list else sub.lineno
                    end = getattr(sub, "end_lineno", sub.lineno)
                    if target == qual and exact_match is None:
                        exact_match = (start, end, "method")
                    elif sub.name == target and fallback_match is None:
                        fallback_match = (start, end, f"method `{qual}`")
    return (exact_match or fallback_match), available


_REPO_MAP_CACHE: Dict[Tuple[str, int, int], str] = {}
_REPO_MAP_LOCK = threading.RLock()


def build_repo_headroom_map(root: Optional[Path] = None, max_files: int = 64) -> str:
    """Build a fast (`<15ms`, mtime-cached) AST Repo & Line-Budget Headroom Map for Step-0 orientation."""
    target_root = (root or settings.active_project_root).resolve()
    mod_limit = settings.max_module_lines
    is_companion_repo = (target_root / "companion").is_dir() and (target_root / "surfaces").is_dir()
    skip_set = SKIP_DIRS | ({"tests", "skills"} if is_companion_repo else set())
    py_files: List[Path] = []
    if target_root.is_dir():
        for cur_r, dirs, fnames in os.walk(target_root):
            dirs[:] = sorted(d for d in dirs if d not in skip_set)
            py_files.extend(Path(cur_r) / fn for fn in sorted(fnames) if fn.endswith(".py") and fn != "__init__.py")
    if not py_files:
        return "  (No Python modules in active workspace yet)"

    max_mtime = max((int(p.stat().st_mtime_ns) for p in py_files), default=0)
    cache_key = (str(target_root), len(py_files), max_mtime)
    with _REPO_MAP_LOCK:
        if cache_key in _REPO_MAP_CACHE:
            return _REPO_MAP_CACHE[cache_key]

    rows: List[str] = []
    for py_file in py_files[:max_files]:
        rel = str(py_file.relative_to(target_root)).replace("\\", "/")
        dotted = rel.removesuffix(".py").replace("/", ".")
        layer = MODULE_LAYER_MAP.get(dotted)
        layer_tag = f"{layer.value} | " if layer else ""
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            line_cnt = len(src.splitlines())
            tree = ast.parse(src, filename=rel)
        except Exception:
            continue
        headroom = mod_limit - line_cnt
        warn = f" [NEAR_LIMIT: {headroom}L left!]" if headroom <= 60 else ""
        syms: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                syms.append(f"class {node.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                syms.append(f"{node.name}()")
        sym_str = f" -> {', '.join(syms[:6])}" + ("..." if len(syms) > 6 else "") if syms else ""
        rows.append(f"  - `{rel}` [{layer_tag}{line_cnt}/{mod_limit}L{warn}]{sym_str}")

    if len(py_files) > max_files:
        rows.append(f"  - ... (+{len(py_files) - max_files} additional `.py` files)")
    rendered = "\n".join(rows)
    with _REPO_MAP_LOCK:
        if len(_REPO_MAP_CACHE) > 8:
            _REPO_MAP_CACHE.clear()
        _REPO_MAP_CACHE[cache_key] = rendered
    return rendered
