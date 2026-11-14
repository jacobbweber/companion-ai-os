# Wiki 3.0 Governed Templates, Ontology Bridges & Deliberate Curation Reference

## 1. The 11 Governed Non-Education Templates (`03_resources/templates/`)

Every document graduated into `02_knowledge/` MUST use a governed template (`template: <slug>`) and include every `## Required Section` with zero leftover `${PLACEHOLDER}` / `[TODO]` markers:

| Template Slug | `document_type` | Required Sections (`## ...`) | When to Pick |
| :--- | :--- | :--- | :--- |
| `zettelkasten-atomic` | `concept` | `Core Idea`, `Mechanism & Context`, `Connections` | Single atomic fact, mental model, health/nutrition fact, or evergreen principle. |
| `adr-decision` | `decision` | `Context & Problem`, `Options Considered`, `Decision`, `Consequences` | Architectural, homelab, tooling, or workflow decisions with explicit options and tradeoffs. |
| `sop-runbook` | `procedure` | `Prerequisites`, `Procedure`, `Verification`, `Rollback & Troubleshooting` | Repeatable procedures: lawnmower/equipment maintenance, Ansible playbooks, backups, patching. |
| `concept-map-system-hub` | `hub` | `Domain Overview`, `Component & Note Map`, `Cross-Domain Bridges` | Domain/system index (MOC) connecting `>=3` child notes inside a domain. |
| `concept-comparison` | `comparison` | `Evaluation Criteria`, `Comparison Matrix`, `Tradeoffs & Recommendation` | Side-by-side evaluation of competing tools, hypervisors, hardware, or strategies. |
| `dikw-pyramid-of-insight` | `synthesis` | `Raw Data & Observations`, `Structured Information`, `Synthesized Knowledge`, `Actionable Wisdom` | Multi-source synthesis distilling raw observations into durable principles. |
| `weekly-worklog` | `log` | `Highlights & Outcomes`, `Completed Work`, `Open Threads & Next Steps` | Dated operational logs, lab maintenance sessions, or weekly progress rollups. |
| `standard-note` | `reference` | `Overview`, `Key Details` | General structured reference documentation, quick guides, and factual summaries. |
| `homelab-host-spec` | `host-spec` | `Hardware & Compute Profile`, `Network & Management`, `Services & Workloads`, `Operations & Notes` | Physical servers, blades (Cisco UCS), hypervisors (VMware ESXi / Proxmox), NAS, or GPU nodes. |
| `homelab-network-matrix` | `network-matrix` | `VLAN & Subnet Matrix`, `IPAM & Static Allocations`, `Routing & Firewall Policy` | Unified VLAN, IPAM allocation, DNS, gateway, and firewall policy matrix. |
| `proxy-asset-note` | `asset-proxy` | `Asset Metadata & Location`, `Executive Summary & Key Facts`, `Extracted Highlights` | Searchable Markdown companion note for binary PDFs, manuals, images, spreadsheets, or work docs stored in `03_resources/`. |

## 2. Rich YAML Frontmatter Schema (`02_knowledge/`)
- `title`: Human-readable title
- `summary`: 1-2 sentence executive summary (`>=20` chars)
- `author`: `user` | `companion` (Dual-Author Guard protects `author: user` notes)
- `status`: `inbox` | `active` | `archived`
- `template`: Governed template slug from `03_resources/templates/`
- `document_type`: Template document category
- `domain`: Top-level domain folder under `02_knowledge/` (`homelab`, `automation`, `software-dev`, `equipment-ops`, `health-performance`, `work-ops`, `general`, etc.)
- `topic`: Optional second-level folder under `02_knowledge/<domain>/`
- `tags`: Concrete kebab-case tags (e.g. `[vmware, esxi, vsphere]`)
- `ontology`: Canonical higher-order bridge concepts from `03_resources/tag-authority.md` (e.g. `[datacenter-compute, virtualization-fabric]` or `[preventive-maintenance, equipment-lifecycle]`) enabling 2-hop indirect discovery across domains
- `supersedes` / `superseded_by`: Automatic Never-Delete lineage pointers into `04_archive/` (inspectable via `wiki_manage(action="diff")`)
- `last_verified_at` / `review_after_days`: Hardware & SOP freshness cadence (`90d` specs/runbooks/worklogs, `180d` ADRs/comparisons/proxies, `365d` evergreen concepts; audited via `list_stale` & `verify_note`)

## 3. Four Explicit Multi-Turn Curation Strategies
1. **`CREATE_NEW`**: Genuine new topic (or first seed note of a brand-new domain).
2. **`MERGE_AND_REFINE`**: Absorbs staged `01_inbox/` facts directly into the existing `02_knowledge/` note's governed sections — never lazy-appending at the bottom (`## Appended Update` is mechanically rejected) and never silently dropping `>30%` of word count or existing code blocks (`Anti-Summary-Loss`).
3. **`SPLIT_MULTI`**: Decomposes a multi-concept `01_inbox/` dump into `>=2` cross-linked `02_knowledge/` notes (e.g. `adr-decision` + `sop-runbook`).
4. **`KEEP_SEPARATE`**: Creates a distinct sibling note despite high overlap ONLY when accompanied by an explicit `rationale` (`>=20` chars) and bidirectional `[[wikilink]]`.
