# Companion Repository SDLC & Ring-0 Rules

When developing or verifying a checkout of Companion AI OS:

1. **Ring-0 Zero-Dependency Law (`ADR-001`)**: Keep `harness.py`, `companion/`, `surfaces/`, `configs/`, and `memory/` 100% Python 3.12 standard library. Third-party dependencies belong only in isolated PEP 723 `skills/<pack>/scripts/*.py` helpers or `frontend/` build-time tooling.
2. **Simplicity & AST Hygiene (`ADR-006`)**: Keep every Python module `<= 700` lines, every function `<= 100` lines, zero semicolon-chained statements, zero `.rglob(` calls (use `walk_pruned_files()`), zero unused imports, and zero dead definitions.
3. **4-File Canonical SDD (`ADR-005`)**: Keep `specs/` strictly capped at `PRODUCT.md`, `ARCHITECTURE.md`, `CONTRACTS.md`, and `DECISIONS.md`, registering every module in `configs/architecture.py` and `companion/disciplines.py`.
4. **Verification Gate**: Run `python harness.py --doctor` (`7/7` PASS) and `npm --prefix frontend test` + `npm --prefix frontend run build` when modifying frontend assets.
