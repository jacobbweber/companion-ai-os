---
name: knowledge-wiki
description: Search, capture, and deliberately curate Companion's Co-Owned 4-Folder Knowledge Wiki Warehouse (`01_inbox`..`04_archive`) using 11 governed templates, multi-turn overlap absorption, `ontology` 2-hop cross-domain bridges, and Never-Delete `04_archive/` lineage.
required_tools: [wiki_manage, read_file]
---

# Co-Owned Knowledge Wiki 3.0 & Deliberate Curation Runbook

## When to Use This Skill
Use this runbook whenever:
- Looking up personal, homelab, equipment maintenance, work-proxy, health, or software knowledge (`wiki_manage(action="search", query="...")` surfaces both direct hits and 2-hop `ontology` bridges across domains).
- Capturing raw notes, research, or binary PDF/image companion notes (`capture_inbox` / `create_proxy_note`).
- Deliberately graduating notes from `01_inbox/` into `02_knowledge/` — **quality, completeness, and structure always take priority over speed** (take multiple tool turns whenever needed).
- Consulting Companion's internal harness architecture reference cards in `skills/knowledge-wiki/references/INDEX.md` and `skills/knowledge-wiki/references/curation-and-templates.md`.

## Core Warehouse Rules (Non-Negotiable)
1. **Inbox First (Option A)**: Always stage rough or new notes into `01_inbox/` via `wiki_manage(action="capture_inbox", ...)`. Companion captures automatically pick one of the 11 governed templates (`zettelkasten-atomic`, `adr-decision`, `sop-runbook`, `concept-map-system-hub`, `concept-comparison`, `dikw-pyramid-of-insight`, `weekly-worklog`, `standard-note`, `homelab-host-spec`, `homelab-network-matrix`, `proxy-asset-note`).
2. **Ironclad `02_knowledge/` Schema**: Every note in `02_knowledge/` MUST use a governed template (`list_templates` / `read_template`), include rich YAML (`title`, `summary`, `template`, `domain`, `tags`, `ontology`), include every `## Required Section`, contain zero unfilled `${PLACEHOLDER}` / `[TODO]` markers, and satisfy the Domain-Aware Anti-Orphan Rule (`>=1 [[wikilink]]` whenever the target domain already has notes, while brand-new domain seed notes may graduate cleanly).
3. **Ontology Cross-Domain Bridges & Synonym Healing**: Populate `ontology` with 1-3 higher-order concepts from `tag_authority` (e.g. `datacenter-compute,virtualization-fabric` linking `vmware` and `cisco-ucs`, or `preventive-maintenance` linking a lawnmower SOP and a homelab UPS SOP). Synonyms listed under `## Ontology Synonym Aliases` auto-map on write, and `wiki_manage(action="consolidate_ontology")` canonicalizes active notes with Never-Delete archive snapshots.
4. **Never Delete, Visual Diffs & Freshness Cadences**: Curating or merging NEVER deletes originals — `01_inbox/` sources and prior `02_knowledge/` revisions are archived in `04_archive/` with `supersedes` / `superseded_by` lineage, Unified/Split diffs (`wiki_manage(action="diff", path="...")` or `approval_id="apr_..."`), 1-click `revert_curation`, and template freshness tracking (`list_stale` / `verify_note`).

## Step-by-Step Procedure (Deliberate Multi-Turn Graduation)
1. **Turn 1 — Audit Overlap, Bridges & Templates**:
   - Call `wiki_manage(action="inspect_graduation", path="01_inbox/<note>.md")` to obtain `curation_id`, `required_reads` (high-overlap `02_knowledge/` notes), `ontology_bridges`, `domains`, and `recommended_templates`.
2. **Turn 2 — Read Every Overlap & Template Skeleton in Full**:
   - Call `wiki_manage(action="read", path="<candidate>")` on **every** `must_read` candidate (and any target note you plan to merge into). Skipping full reads fails `validate_curation` mechanically.
   - Call `wiki_manage(action="read_template", template="<slug>")` whenever you need the exact `required_sections` skeleton.
3. **Turn 3 — Synthesize & Validate Curation Plan**:
   - Choose `CREATE_NEW`, `MERGE_AND_REFINE` (weave facts cleanly into existing sections — never append a lazy `## Appended Update` dump and never drop `>30%` of words or code blocks), `SPLIT_MULTI` (cross-linked sibling notes for multi-concept dumps), or `KEEP_SEPARATE` (with explicit `rationale` + `[[wikilink]]`).
   - Call `wiki_manage(action="validate_curation", curation_id="...", strategy="...", outputs_json="[...]")` and resolve any reported validation errors.
4. **Turn 4 — Commit with Trust-Mode & Archive Lineage**:
   - Call `wiki_manage(action="commit_curation", curation_id="...")` (executes immediately in `autonomous` trust mode for Companion notes, or queues in `ApprovalQueue` in `strict`/`balanced` mode and whenever touching `author: user` notes).
