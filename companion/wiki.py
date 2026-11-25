"""
Co-Owned Knowledge Wiki Engine, Governed Templates, Maintenance & AI Freshness/Staging
(`companion/wiki.py`).

Consolidates `wiki_templates.py`, `wiki_engine.py`, and `wiki_maintenance.py` into a single
cohesive module providing:
1. 11 Built-in Governed Templates + `03_resources/tag-authority.md` Canonical Ontology Registry.
2. Pure-stdlib YAML frontmatter parser, ordered serializer, and ironclad `02_knowledge/` validator.
3. `WikiEngine`: 4-folder warehouse (`01_inbox`..`04_archive`), FTS5 + RRF hybrid search, ontology bridges,
   optimistic concurrency (`expected_hash`), user-author approval guard, and recoverable `trash/`.
4. Visual Unified + Side-by-Side diffs (`compute_wiki_diff`), ontology consolidation, safe image assets,
   4-folder directory tree + YAML facets (`build_wiki_directory_tree`).
5. Real `fast`-tier LLM `verify_note_freshness` claim audit and `stage_chat_turn_to_inbox` distillation
   bounded by deterministic validation and timestamps.
"""

import base64
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from companion.approvals import get_approval_queue, get_trust_mode
from companion.config import get_execution_origin, settings, walk_pruned_files
from companion.llm import chat, normalize_assistant_message, tier_context
from companion.memory_store import MemoryStore, get_memory_store, hybrid_rrf_rank
from companion.telemetry import redact_secrets

__all__ = [
    "BUILTIN_TEMPLATES",
    "BUILTIN_TEMPLATE_MAP",
    "CANONICAL_FRONTMATTER_ORDER",
    "TemplateSpec",
    "WIKI_FOLDERS",
    "WikiEngine",
    "as_slug_list",
    "build_wiki_directory_tree",
    "canonicalize_ontology",
    "compute_body_metrics",
    "compute_note_staleness",
    "compute_wiki_diff",
    "consolidate_ontology",
    "default_review_days",
    "execute_wiki_maintenance_subaction",
    "extract_wikilinks",
    "get_template",
    "get_wiki_engine",
    "hydrate_note_meta",
    "ingest_wiki_asset",
    "list_stale_notes",
    "list_templates",
    "load_tag_authority",
    "make_note_uid",
    "parse_frontmatter",
    "read_wiki_asset_raw",
    "recommend_templates",
    "register_authority_terms",
    "render_frontmatter",
    "save_template",
    "scaffold_foreground_body",
    "scrub_conversational_chatter",
    "seed_templates_and_authority",
    "sha16_bytes",
    "slugify_wiki",
    "stage_chat_turn_to_inbox",
    "validate_curated_note",
    "verify_note_freshness",
]

# ---------------------------------------------------------------------------
# 1. Governed Templates, YAML Frontmatter & Taxonomy Authority
# ---------------------------------------------------------------------------
WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|([^\]]+))?\]\]")
_HEADING_RE = re.compile(r"^#{1,4}\s+(?:\d+[\.\)]\s*)?(.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(
    r"(\$\{[A-Z0-9_:-]+\}|\{\{[A-Za-z0-9_.-]+\}\}|\[(?:TODO|TBD|FILL[\s_-]?IN|INSERT|DESCRIBE|PLACEHOLDER)[^\]]*\])",
    re.IGNORECASE,
)
_CHATTER_PREFIX_RE = re.compile(
    r"^\s*(?:(?:sure|certainly|of course|absolutely)[!.,]?(?:\s+here (?:is|are) [^\n:]+:)?|here (?:is|are) (?:the|your|a) [^\n:]+:)\s*\n+",
    re.IGNORECASE,
)

CANONICAL_FRONTMATTER_ORDER: Tuple[str, ...] = (
    "uid", "title", "summary", "author", "status", "template", "document_type",
    "domain", "topic", "tags", "ontology", "related", "supersedes", "superseded_by",
    "source", "asset_path", "confidence", "sensitivity", "word_count", "context_tokens",
    "content_hash", "created_at", "updated_at", "last_verified_at", "review_after_days",
    "last_audit_summary", "archive_reason", "curation_tx_id",
)


@dataclass(frozen=True)
class TemplateSpec:
    """Declarative specification for a governed Knowledge Wiki note template."""

    slug: str
    title: str
    document_type: str
    when_to_use: str
    required_sections: Tuple[str, ...]
    default_ontology: Tuple[str, ...]
    body_skeleton: str


BUILTIN_TEMPLATES: Tuple[TemplateSpec, ...] = (
    TemplateSpec(
        "zettelkasten-atomic", "Zettelkasten Atomic Concept", "concept",
        "Single self-contained concept, principle, mechanism, or durable fact (1 core idea per note).",
        ("Core Idea", "Mechanism & Context", "Connections"), ("knowledge-synthesis",),
        "# <Title>\n\n## Core Idea\nState the single atomic claim, principle, or fact in plain prose.\n\n## Mechanism & Context\nExplain why it works, boundary conditions, evidence, and examples.\n\n## Connections\n- Related notes: [[related-note]]\n",
    ),
    TemplateSpec(
        "adr-decision", "Architecture & Design Decision (ADR)", "decision",
        "Hardware, infrastructure, software architecture, or workflow decision with explicit options and tradeoffs.",
        ("Context & Problem", "Options Considered", "Decision", "Consequences"), ("architecture-governance", "decision-rationale"),
        "# <Title>\n\n## Context & Problem\nWhat constraint or problem forced this decision?\n\n## Options Considered\n1. **Option A** — Pros / Cons.\n2. **Option B** — Pros / Cons.\n\n## Decision\nChosen option and decisive rationale.\n\n## Consequences\n- **Outcomes & Follow-ups**: [[related-note]]\n",
    ),
    TemplateSpec(
        "sop-runbook", "Standard Operating Procedure & Runbook", "procedure",
        "Repeatable step-by-step maintenance manual, homelab runbook, equipment service guide, or development workflow.",
        ("Prerequisites", "Procedure", "Verification", "Rollback & Troubleshooting"), ("operational-runbook", "preventive-maintenance"),
        "# <Title>\n\n## Prerequisites\n- Required tools, parts, permissions, or pre-checks.\n\n## Procedure\n1. Step one.\n2. Step two.\n\n## Verification\nHow to confirm the procedure succeeded.\n\n## Rollback & Troubleshooting\nSafe recovery steps and common failure remedies. See [[related-note]].\n",
    ),
    TemplateSpec(
        "concept-map-system-hub", "Concept Map & Domain System Hub (MOC)", "hub",
        "Map of Content (MOC) or Domain Hub indexing 3+ notes in a domain/subsystem and bridging related domains.",
        ("Domain Overview", "Component & Note Map", "Cross-Domain Bridges"), ("system-topology", "knowledge-synthesis"),
        "# <Title>\n\n## Domain Overview\nScope, purpose, and organizing structure of this domain.\n\n## Component & Note Map\n- [[core-note-1]] — Role and summary.\n- [[core-note-2]] — Role and summary.\n\n## Cross-Domain Bridges\nHow this domain connects conceptually or operationally to other areas.\n",
    ),
    TemplateSpec(
        "concept-comparison", "Comparative Evaluation & Tradeoff Matrix", "comparison",
        "Side-by-side comparison of competing platforms, tools, hardware models, or methodologies.",
        ("Evaluation Criteria", "Comparison Matrix", "Tradeoffs & Recommendation"), ("comparative-analysis", "decision-rationale"),
        "# <Title>\n\n## Evaluation Criteria\nKey dimensions for this comparison.\n\n## Comparison Matrix\n| Criterion | Candidate A | Candidate B |\n| :--- | :--- | :--- |\n| Core Strength | ... | ... |\n\n## Tradeoffs & Recommendation\nWhen to pick each option and links to [[related-note]].\n",
    ),
    TemplateSpec(
        "dikw-pyramid-of-insight", "DIKW Pyramid of Insight Synthesis", "synthesis",
        "Deep synthesis distilling raw observations/metrics into structured information, knowledge, and wisdom.",
        ("Raw Data & Observations", "Structured Information", "Synthesized Knowledge", "Actionable Wisdom"),
        ("knowledge-synthesis", "telemetry-observability"),
        "# <Title>\n\n## Raw Data & Observations\nConcrete measurements, logs, or primary facts.\n\n## Structured Information\nPatterns and grouped relationships visible in the data.\n\n## Synthesized Knowledge\nUnderlying causal model explaining why those patterns occur.\n\n## Actionable Wisdom\nRules of thumb and next actions grounded in [[related-note]].\n",
    ),
    TemplateSpec(
        "weekly-worklog", "Chronological Engineering & Maintenance Worklog", "log",
        "Periodic lab journal, project milestone log, equipment service history, or sprint retro.",
        ("Highlights & Outcomes", "Completed Work", "Open Threads & Next Steps"), ("operational-history",),
        "# <Title>\n\n## Highlights & Outcomes\nExecutive summary of key changes and outcomes.\n\n## Completed Work\n- Item completed and verified.\n\n## Open Threads & Next Steps\n- Follow-up item linked to [[related-note]].\n",
    ),
    TemplateSpec(
        "standard-note", "Standard Reference Note", "reference",
        "General-purpose structured reference sheet, quick fact guide, or topic summary when specialized templates do not apply.",
        ("Overview", "Key Details"), ("reference-knowledge",),
        "# <Title>\n\n## Overview\nConcise context and purpose of this reference note.\n\n## Key Details\nStructured facts, specifications, or notes linked to [[related-note]].\n",
    ),
    TemplateSpec(
        "homelab-host-spec", "Homelab & Datacenter Host Specification", "host-spec",
        "Physical blade, server, hypervisor host (VMware ESXi, Cisco UCS, Proxmox), GPU node, NAS, or workstation profile.",
        ("Hardware & Compute Profile", "Network & Management", "Services & Workloads", "Operations & Notes"),
        ("datacenter-compute", "infrastructure-hardware"),
        "# <Title>\n\n## Hardware & Compute Profile\n- **Chassis / CPU / Memory / Storage**:\n\n## Network & Management\n- **Hostname / Primary IP / Out-of-Band (IPMI/CIMC/iDRAC)**:\n\n## Services & Workloads\nHosted VMs, containers, inference engines, or cluster roles.\n\n## Operations & Notes\nPower, thermal, firmware, and dependencies on [[related-note]].\n",
    ),
    TemplateSpec(
        "homelab-network-matrix", "Homelab Network, VLAN & IPAM Matrix", "network-matrix",
        "Unified VLAN segmentation table, subnet IPAM allocations, and inter-VLAN routing/firewall policy.",
        ("VLAN & Subnet Matrix", "IPAM & Static Allocations", "Routing & Firewall Policy"),
        ("network-fabric", "infrastructure-security"),
        "# <Title>\n\n## VLAN & Subnet Matrix\n| VLAN ID | Name | CIDR | Gateway | Purpose |\n| :--- | :--- | :--- | :--- | :--- |\n\n## IPAM & Static Allocations\n| IP Address | Hostname | Interface | Role |\n| :--- | :--- | :--- | :--- |\n\n## Routing & Firewall Policy\nSegmentation rules, DNS/DHCP notes, and links to [[related-note]].\n",
    ),
    TemplateSpec(
        "proxy-asset-note", "Binary & External Asset Proxy Note", "asset-proxy",
        "Searchable Markdown proxy companion representing a PDF manual, image diagram, spreadsheet, or external work document.",
        ("Asset Metadata & Location", "Executive Summary & Key Facts", "Extracted Highlights"),
        ("asset-provenance", "reference-knowledge"),
        "# <Title>\n\n## Asset Metadata & Location\n- **Source File / Format / Checksum**:\n\n## Executive Summary & Key Facts\nSearchable synthesis of what this document/asset contains.\n\n## Extracted Highlights\nKey specifications, tables, part numbers, or takeaways linked to [[related-note]].\n",
    ),
)

BUILTIN_TEMPLATE_MAP: Dict[str, TemplateSpec] = {t.slug: t for t in BUILTIN_TEMPLATES}

DEFAULT_TAG_AUTHORITY_MD = """# Governed Wiki Taxonomy & Canonical Ontology Authority

> Canonical registry of approved domains, tags, and cross-domain `ontology` bridge concepts.

## Approved Domains
- `homelab` — Servers, hypervisors, GPU nodes, networking, storage, and lab services
- `automation` — Ansible, Terraform, scripts, CI/CD, and configuration management
- `software-dev` — Architecture, Python/TypeScript patterns, AI engineering, and APIs
- `equipment-ops` — Home, yard, lawn-care equipment manuals, parts, and seasonal maintenance
- `health-performance` — Nutrition, weight-loss science, fitness metrics, and habits
- `cooking` — Culinary recipes, techniques, pantry staples, and meal planning
- `household` — Home maintenance, personal finance, family logistics, and projects
- `work-ops` — Sanitized work procedures, architecture notes, and external document proxies
- `general` — Cross-cutting reference notes and miscellaneous facts

## Approved Tags
- `vmware`, `esxi`, `vsphere`, `cisco-ucs`, `proxmox`, `docker`, `gpu`, `nvidia`, `gpu-node`, `networking`, `vlan`, `ipam`, `firewall`, `storage`, `ups`
- `ansible`, `playbook`, `linux`, `windows`, `winrm`, `ssh`, `systemd`, `backup`
- `lawn-care`, `mower`, `small-engine`, `oil-change`, `seasonal-service`, `parts-manual`
- `nutrition`, `weight-loss`, `metabolism`, `training`, `recovery`, `recipe`, `meal-prep`, `cooking`
- `adr`, `runbook`, `comparison`, `moc`, `worklog`, `pdf-proxy`, `attachment`

## Canonical Ontology Concepts (Indirect Cross-Domain Bridges)
- `datacenter-compute` — Blade chassis, hypervisors, GPU servers, and cluster compute nodes (`vmware` <-> `cisco-ucs` <-> `gpu-node`)
- `virtualization-fabric` — Hypervisors, VM lifecycle, vSwitches, and container runtimes
- `network-fabric` — Switching, VLANs, IPAM, routing, DNS, and firewall segmentation
- `infrastructure-hardware` — Physical servers, blades, power/UPS, storage media, and thermals
- `infrastructure-security` — Credentials, segmentation, hardening, and least-privilege access
- `configuration-automation` — Declarative provisioning, Ansible roles, and idempotent automation
- `preventive-maintenance` — Scheduled servicing, wear-part replacement, and health checks across equipment (`equipment-ops` <-> `homelab`)
- `equipment-lifecycle` — Manuals, specifications, serial/part tracking, and replacement schedules
- `operational-runbook` — Step-by-step execution, verification, and rollback procedures
- `architecture-governance` — System boundaries, ADRs, contracts, and design tradeoffs
- `decision-rationale` — Evaluated alternatives and selection criteria
- `comparative-analysis` — Multi-option benchmarks and tradeoff matrices
- `knowledge-synthesis` — Atomic concepts, MOCs, and higher-order mental models
- `telemetry-observability` — Metrics, logs, diagnostics, and performance baselines
- `metabolic-health` — Caloric balance, body composition, nutrition, and physiology
- `culinary-technique` — Cooking methods, flavor pairing, sauces, and recipe execution
- `asset-provenance` — Searchable Markdown proxies for binary PDFs, diagrams, and external artifacts
- `operational-history` — Chronological engineering, lab, and maintenance logs
- `reference-knowledge` — Durable factual lookup sheets and specifications

## Ontology Synonym Aliases (alias -> canonical)
- `server-compute` -> `datacenter-compute`
- `compute-nodes` -> `datacenter-compute`
- `cluster-compute` -> `datacenter-compute`
- `hypervisor-fabric` -> `virtualization-fabric`
- `esxi-virtualization` -> `virtualization-fabric`
- `small-engine-maintenance` -> `preventive-maintenance`
- `routine-maintenance` -> `preventive-maintenance`
- `equipment-maintenance` -> `preventive-maintenance`
- `configuration-management` -> `configuration-automation`
- `automation-orchestration` -> `configuration-automation`
- `ansible-automation` -> `configuration-automation`
- `fat-loss` -> `metabolic-health`
- `nutrition-science` -> `metabolic-health`
- `pdf-proxy` -> `asset-provenance`
"""


def default_review_days(tpl_slug: str) -> int:
    if tpl_slug in ("homelab-host-spec", "homelab-network-matrix", "sop-runbook", "weekly-worklog"):
        return 90
    return 180 if tpl_slug in ("adr-decision", "concept-comparison", "proxy-asset-note") else 365


def slugify_wiki(raw: str, fallback: str = "note") -> str:
    return re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")[:72] or fallback


def as_slug_list(val: Any) -> List[str]:
    items = val if isinstance(val, (list, tuple)) else ([x.strip() for x in str(val).split(",")] if val else [])
    return list(dict.fromkeys(slugify_wiki(str(x), "") for x in items if slugify_wiki(str(x), "")))


def _normalize_related_links(raw_related: Any, body: str) -> List[str]:
    explicit: List[str] = []
    if isinstance(raw_related, (list, tuple)):
        for x in raw_related:
            s = str(x).strip()
            if not s:
                continue
            links = extract_wikilinks(s)
            explicit.extend(f"[[{lk}]]" for lk in (links or [s.strip("[]").removesuffix(".md")]))
    inline = [f"[[{r}]]" for r in extract_wikilinks(body)]
    return list(dict.fromkeys([*explicit, *inline]))


def sha16_bytes(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()[:16]


def make_note_uid(prefix: str = "wk") -> str:
    return f"{slugify_wiki(prefix, 'wk')}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{os.urandom(3).hex()}"


def extract_wikilinks(text: str) -> List[str]:
    return list(dict.fromkeys(tgt for m in WIKILINK_RE.finditer(text or "") if (tgt := m.group(1).strip().replace("\\", "/").removesuffix(".md"))))


def _parse_scalar(val: str) -> Any:
    clean = val.strip()
    if clean.startswith('"') and clean.endswith('"') and len(clean) >= 2:
        return re.sub(r'\\(["\\])', r"\1", clean[1:-1])
    if clean.startswith("'") and clean.endswith("'") and len(clean) >= 2:
        return clean[1:-1].replace("''", "'").replace(r"\'", "'")
    if clean.lower() in ("true", "false"):
        return clean.lower() == "true"
    if re.fullmatch(r"-?\d+", clean):
        return int(clean)
    if re.fullmatch(r"-?\d+\.\d+", clean):
        return round(float(clean), 4)
    return clean


def _split_inline_array(inner: str) -> List[str]:
    items: List[str] = []
    cur: List[str] = []
    depth_sq, in_q = 0, ""
    for ch in inner:
        if in_q:
            in_q = "" if ch == in_q else in_q
            if in_q:
                cur.append(ch)
        elif ch in ('"', "'"):
            in_q = ch
        elif ch == "[":
            depth_sq += 1
            cur.append(ch)
        elif ch == "]":
            depth_sq = max(0, depth_sq - 1)
            cur.append(ch)
        elif ch == "," and depth_sq == 0:
            if tok := "".join(cur).strip().strip("'\""):
                items.append(tok)
            cur = []
        else:
            cur.append(ch)
    if tail := "".join(cur).strip().strip("'\""):
        items.append(tail)
    return items


_NUMBERED_LINE_RE = re.compile(r"^\s{0,5}\d+\|(.*)$")


def _strip_read_file_numbering(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").splitlines()
    if lines and re.match(r"^#\s+.+\(lines\s+\d+-\d+\s+of\s+\d+\)\s*$", lines[0]):
        lines = lines[1:]
    if len(lines) >= 3 and all(_NUMBERED_LINE_RE.match(ln) for ln in lines[:3]):
        return "\n".join(m.group(1) if (m := _NUMBERED_LINE_RE.match(ln)) else ln for ln in lines)
    return (text or "").replace("\r\n", "\n")


def parse_frontmatter(markdown_text: str) -> Tuple[Dict[str, Any], str]:
    """Parse rich YAML frontmatter (scalars, inline `[...]` lists, block `- item` lists) without mutating text."""
    norm = _strip_read_file_numbering(markdown_text)
    if not norm.startswith("---\n") or (end := norm.find("\n---\n", 4)) == -1:
        return {}, norm
    fm_raw, body = norm[4:end], norm[end + 5 :]
    meta: Dict[str, Any] = {}
    list_key = ""
    for raw_ln in fm_raw.splitlines():
        if not raw_ln.strip() or raw_ln.lstrip().startswith("#"):
            continue
        if list_key and re.match(r"^\s*-\s+", raw_ln):
            if item := re.sub(r"^\s*-\s+", "", raw_ln).strip().strip("'\""):
                meta.setdefault(list_key, []).append(item)
            continue
        list_key = ""
        if ":" not in raw_ln:
            continue
        k, _, v = raw_ln.partition(":")
        key, val = k.strip().lower(), v.strip()
        if not key:
            continue
        if not val:
            meta[key], list_key = [], key
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = _split_inline_array(val[1:-1])
        else:
            meta[key] = _parse_scalar(val)
    return meta, body


def _format_yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        cleaned = [f'"{sx}"' if any(c in sx for c in (",", ":", "[", "]", "#")) else sx for x in v if (sx := str(x).strip())]
        return f"[{', '.join(cleaned)}]"
    s = str(v).replace("\n", " ").strip()
    if not s:
        return '""'
    needs_q = any(ch in s for ch in (":", "#", "[", "]", "{", "}", ",", '"', "'")) or s.lower() in ("true", "false", "null")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"' if needs_q else s


def render_frontmatter(meta: Dict[str, Any], body: str) -> str:
    """Render deterministic ordered YAML frontmatter + normalized Markdown body."""
    clean_body = (body or "").replace("\r\n", "\n").lstrip("\n")
    ordered = list(CANONICAL_FRONTMATTER_ORDER)
    extra = sorted(k for k in meta if k not in ordered)
    lines = ["---"] + [f"{k}: {_format_yaml_value(meta[k])}" for k in ordered + extra if k in meta and meta[k] not in (None, "")] + ["---"]
    return "\n".join(lines) + "\n\n" + clean_body + ("" if clean_body.endswith("\n") else "\n")


def scrub_conversational_chatter(markdown_text: str) -> str:
    txt = _CHATTER_PREFIX_RE.sub("", (markdown_text or "").replace("\r\n", "\n").strip()).strip()
    if txt.startswith("```") and txt.endswith("```"):
        first_nl = txt.find("\n")
        if first_nl != -1 and txt[:first_nl].strip().lower() in ("```markdown", "```md", "```yaml", "```"):
            txt = txt[first_nl + 1 : -3].strip()
    return txt


def compute_body_metrics(body: str) -> Dict[str, int]:
    clean = (body or "").strip()
    return {
        "word_count": len(re.findall(r"\S+", clean)),
        "context_tokens": max(1, (len(clean) + 3) // 4) if clean else 0,
        "code_blocks": len(re.findall(r"^```", clean, re.MULTILINE)) // 2,
        "heading_count": len(_HEADING_RE.findall(clean)),
    }


def _render_template_file(spec: TemplateSpec) -> str:
    return render_frontmatter(
        {
            "template_slug": spec.slug,
            "title": spec.title,
            "document_type": spec.document_type,
            "when_to_use": spec.when_to_use,
            "required_sections": list(spec.required_sections),
            "default_ontology": list(spec.default_ontology),
        },
        spec.body_skeleton,
    )


def seed_templates_and_authority(wiki_root: Path) -> Dict[str, Any]:
    """Idempotently seed all 11 built-in non-education templates + `03_resources/tag-authority.md`."""
    tpl_dir = wiki_root / "03_resources" / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    seeded = 0
    for spec in BUILTIN_TEMPLATES:
        p = tpl_dir / f"{spec.slug}.md"
        if not p.is_file():
            p.write_text(_render_template_file(spec), encoding="utf-8", newline="\n")
            seeded += 1
    auth_path = wiki_root / "03_resources" / "tag-authority.md"
    if not auth_path.is_file():
        auth_path.write_text(DEFAULT_TAG_AUTHORITY_MD.strip() + "\n", encoding="utf-8", newline="\n")
    return {"seeded_templates": seeded, "total_builtin": len(BUILTIN_TEMPLATES), "authority_path": "03_resources/tag-authority.md"}


def list_templates(wiki_root: Path) -> List[Dict[str, Any]]:
    seed_templates_and_authority(wiki_root)
    items: List[Dict[str, Any]] = []
    for p in sorted((wiki_root / "03_resources" / "templates").glob("*.md")):
        meta, skeleton = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        slug, req, ont = str(meta.get("template_slug") or p.stem), meta.get("required_sections"), meta.get("default_ontology")
        items.append({
            "slug": slug,
            "title": str(meta.get("title") or slug),
            "document_type": str(meta.get("document_type") or "reference"),
            "when_to_use": str(meta.get("when_to_use") or ""),
            "required_sections": list(req) if isinstance(req, list) else [],
            "default_ontology": list(ont) if isinstance(ont, list) else [],
            "skeleton": skeleton.strip(),
            "path": f"03_resources/templates/{p.name}",
            "builtin": slug in BUILTIN_TEMPLATE_MAP,
        })
    return items


def get_template(wiki_root: Path, slug: str) -> Dict[str, Any]:
    clean, all_tpls = slugify_wiki((slug or "").removesuffix(".md"), fallback=""), list_templates(wiki_root)
    match = next((item for item in all_tpls if item["slug"] == clean), None)
    return {"ok": True, "template": match} if match else {"ok": False, "error": f"Template `{slug}` not found. Available templates: {', '.join(t['slug'] for t in all_tpls)}"}


def save_template(wiki_root: Path, slug: str, title: str, document_type: str, when_to_use: str, required_sections: List[str], body_skeleton: str) -> Dict[str, Any]:
    clean_slug, secs = slugify_wiki(slug, fallback=""), [s.strip() for s in required_sections if str(s).strip()]
    if not clean_slug or clean_slug == "inbox-raw":
        return {"ok": False, "error": "Valid kebab-case template `slug` (not `inbox-raw`) is required."}
    if not secs:
        return {"ok": False, "error": "At least one `required_sections` heading is required for a governed template."}
    seed_templates_and_authority(wiki_root)
    spec = TemplateSpec(
        clean_slug,
        (title or clean_slug).strip(),
        slugify_wiki(document_type, "reference"),
        (when_to_use or "Custom domain template.").strip(),
        tuple(secs),
        ("reference-knowledge",),
        (body_skeleton or f"# <Title>\n\n## {secs[0]}\n").strip(),
    )
    (wiki_root / "03_resources" / "templates" / f"{clean_slug}.md").write_text(_render_template_file(spec), encoding="utf-8", newline="\n")
    return {"ok": True, "slug": clean_slug, "path": f"03_resources/templates/{clean_slug}.md", "summary": f"Saved governed template `{clean_slug}` with required sections {secs}."}


def load_tag_authority(wiki_root: Path) -> Dict[str, Any]:
    seed_templates_and_authority(wiki_root)
    raw = (wiki_root / "03_resources" / "tag-authority.md").read_text(encoding="utf-8", errors="replace")
    section, domains, tags, ontology, aliases = "", [], [], [], {}
    for ln in raw.splitlines():
        if ln.startswith("## "):
            low = ln.lower()
            section = "aliases" if "alias" in low else ("ontology" if "ontology" in low else ("tags" if "tag" in low else ("domains" if "domain" in low else "")))
            continue
        tokens = re.findall(r"`([a-z0-9][a-z0-9_-]*)`", ln)
        if not tokens or not section:
            continue
        if section == "aliases" and len(tokens) >= 2 and "->" in ln:
            aliases[tokens[0]] = tokens[1]
        elif section in ("domains", "ontology"):
            tgt = domains if section == "domains" else ontology
            if tokens[0] not in tgt:
                tgt.append(tokens[0])
        elif section == "tags":
            for t in tokens:
                if t not in tags:
                    tags.append(t)
    return {"ok": True, "path": "03_resources/tag-authority.md", "domains": domains, "tags": tags, "ontology": ontology, "aliases": aliases, "raw": raw}


def canonicalize_ontology(wiki_root: Path, raw_ont: Any) -> List[str]:
    slugs = as_slug_list(raw_ont)
    if not slugs:
        return []
    auth = load_tag_authority(wiki_root)
    aliases, canon_set = auth["aliases"], set(auth["ontology"])
    out: List[str] = []
    for s in slugs:
        mapped = aliases.get(s, s[:-1] if s.endswith("s") and s[:-1] in canon_set else s)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def register_authority_terms(
    wiki_root: Path, domain: str = "", tags: Iterable[str] = (), ontology: Iterable[str] = (),
) -> Dict[str, Any]:
    auth = load_tag_authority(wiki_root)
    raw = str(auth["raw"]).rstrip() + "\n"
    dom_s = slugify_wiki(domain, "") if domain else ""
    new_dom = [dom_s] if dom_s and dom_s not in auth["domains"] and dom_s != "inbox" else []
    new_tags = list(dict.fromkeys(slugify_wiki(t, "") for t in tags if slugify_wiki(t, "") and slugify_wiki(t, "") not in auth["tags"]))
    canon_ont = canonicalize_ontology(wiki_root, list(ontology))
    new_ont = [o for o in canon_ont if o not in auth["ontology"]]
    if new_dom:
        raw = raw.replace("## Approved Tags", "\n".join(f"- `{d}` — Curated domain" for d in new_dom) + "\n\n## Approved Tags", 1)
    if new_tags:
        raw = raw.replace("## Canonical Ontology Concepts", f"- {', '.join(f'`{t}`' for t in new_tags)}\n\n## Canonical Ontology Concepts", 1)
    if new_ont:
        ont_lines = "\n".join(f"- `{o}` — Curated cross-domain ontology bridge" for o in new_ont)
        raw = raw.replace("## Ontology Synonym Aliases", f"{ont_lines}\n\n## Ontology Synonym Aliases", 1) if "## Ontology Synonym Aliases" in raw else f"{raw}{ont_lines}\n"
    if new_dom or new_tags or new_ont:
        (wiki_root / "03_resources" / "tag-authority.md").write_text(raw, encoding="utf-8", newline="\n")
    return {"added_domains": new_dom, "added_tags": new_tags, "added_ontology": new_ont}


def hydrate_note_meta(
    base_meta: Dict[str, Any], title: str, body: str, author: str, status: str,
    tpl_slug: str, domain: str, topic: str, tags: List[str], ontology: List[str],
    now_iso: str, summary: str = "", wiki_root: Optional[Path] = None,
) -> Dict[str, Any]:
    tpl_spec, metrics = BUILTIN_TEMPLATE_MAP.get(tpl_slug), compute_body_metrics(body)
    custom_tpl = get_template(wiki_root, tpl_slug).get("template") if (wiki_root is not None and tpl_spec is None and tpl_slug and tpl_slug != "inbox-raw") else None
    default_ont = list(tpl_spec.default_ontology) if tpl_spec else (list(custom_tpl.get("default_ontology") or []) if custom_tpl else [])
    eff_ont = ontology or (default_ont if tpl_slug != "inbox-raw" else [])
    raw_sum = summary.strip() or str(base_meta.get("summary") or "").strip()
    if not raw_sum or raw_sum.startswith(("- ", "* ")):
        prose = [
            re.sub(r"^[-*+]\s+|\*\*", "", ln.strip()).strip()
            for ln in (body or "").splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", "|", "```", "---"))
        ]
        joined = re.sub(r"\s+", " ", " ".join(prose)).strip()
        raw_sum = summary.strip() or (joined[:220] if len(joined) >= 24 else f"Curated reference note covering {title}.")
    created_iso, raw_rev = str(base_meta.get("created_at") or now_iso), base_meta.get("review_after_days")
    rev_days = int(raw_rev) if str(raw_rev or "").isdigit() and int(raw_rev) > 0 else default_review_days(tpl_slug)
    doc_type = tpl_spec.document_type if tpl_spec else (str(custom_tpl.get("document_type")) if custom_tpl and custom_tpl.get("document_type") else ("inbox-staging" if tpl_slug == "inbox-raw" else str(base_meta.get("document_type") or "reference")))
    return {
        **base_meta,
        "uid": str(base_meta.get("uid") or make_note_uid("wk")),
        "title": title[:140],
        "summary": raw_sum[:260],
        "author": author,
        "status": status,
        "template": tpl_slug,
        "domain": domain,
        "topic": topic,
        "tags": tags,
        "ontology": eff_ont,
        "document_type": doc_type,
        "related": _normalize_related_links(base_meta.get("related"), body),
        "word_count": metrics["word_count"],
        "context_tokens": metrics["context_tokens"],
        "created_at": created_iso,
        "updated_at": now_iso,
        "last_verified_at": str(base_meta.get("last_verified_at") or created_iso),
        "review_after_days": rev_days,
    }


def recommend_templates(title: str, body: str, source: str = "", wiki_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    hay = f"{title} {source} {body[:2500]}".lower()
    headings_norm = [re.sub(r"[^a-z0-9]+", " ", h.lower()).strip() for h in _HEADING_RE.findall(body or "")]
    custom_candidates: List[Tuple[int, Dict[str, Any]]] = []
    if wiki_root is not None:
        for ct in list_templates(wiki_root):
            if ct.get("builtin"):
                continue
            req_s = [str(s).lower().strip() for s in (ct.get("required_sections") or []) if str(s).strip()]
            sec_hits = sum(2 for rs in req_s if any(rs in h or h in rs for h in headings_norm if h))
            tok_hits = sum(1 for tok in ct["slug"].split("-") if len(tok) >= 4 and tok in hay)
            if sec_hits + tok_hits >= 2:
                custom_candidates.append((sec_hits + tok_hits, ct))
    custom_candidates.sort(key=lambda x: x[0], reverse=True)
    rules = (
        (("pdf", "attachment", "datasheet", "scanned", "03_resources/"), "proxy-asset-note"),
        (("vlan", "cidr", "subnet", "ipam", "/24", "firewall"), "homelab-network-matrix"),
        (("cpu", "ram", "ipmi", "cimc", "idrac", "chassis", "blade server", "esxi", "ucs", "hypervisor", "proxmox"), "homelab-host-spec"),
        (("step 1", "prerequisites", "runbook", "playbook", "preventive maintenance", "oil change", "engine oil", "torque", "rollback", "sop"), "sop-runbook"),
        (("options considered", "decision", "adr", "consequences"), "adr-decision"),
        (("vs ", "versus", "comparison", "| :---", "tradeoff"), "concept-comparison"),
        (("worklog", "weekly", "completed work", "sprint"), "weekly-worklog"),
        (("map of content", "system hub", "domain overview", "moc"), "concept-map-system-hub"),
        (("raw data", "actionable wisdom", "dikw"), "dikw-pyramid-of-insight"),
    )
    scored = sorted([(sum(1 for k in keys if k in hay), slug) for keys, slug in rules], key=lambda x: x[0], reverse=True)
    seen: List[Dict[str, Any]] = [
        {"slug": ct["slug"], "title": ct["title"], "document_type": ct["document_type"], "when_to_use": ct["when_to_use"], "required_sections": list(ct["required_sections"])}
        for _, ct in custom_candidates[:2]
    ]
    picks = [slug for hits, slug in scored if hits > 0] + ["standard-note", "zettelkasten-atomic"]
    for slug in picks:
        if slug in BUILTIN_TEMPLATE_MAP and all(x["slug"] != slug for x in seen):
            sp = BUILTIN_TEMPLATE_MAP[slug]
            seen.append({"slug": sp.slug, "title": sp.title, "document_type": sp.document_type, "when_to_use": sp.when_to_use, "required_sections": list(sp.required_sections)})
        if len(seen) >= 4:
            break
    return seen


def scaffold_foreground_body(wiki_root: Path, tpl_slug: str, title: str, summary: str, body: str, rel: str) -> str:
    """Structure missing `required_sections` cleanly around direct-saved foreground Markdown without fake IT boilerplate."""
    if _PLACEHOLDER_RE.search(body or "") or "<title>" in (body or "").lower():
        return body
    t_res = get_template(wiki_root, tpl_slug) if tpl_slug and tpl_slug != "inbox-raw" else {"ok": False}
    req_secs = list((t_res.get("template") or {}).get("required_sections") or []) if t_res.get("ok") else []
    if not req_secs:
        return body
    headings_norm = [re.sub(r"[^a-z0-9]+", " ", h.lower()).strip() for h in _HEADING_RE.findall(body or "")]
    missing = [s for s in req_secs if not any(re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip() in h for h in headings_norm if h)]
    if not missing:
        return body
    core_b = re.sub(r"^\s*#\s+[^\n]+\n*", "", (body or "").strip()) or summary
    if len(core_b) < 40:
        core_b = f"{core_b}\n\n{summary}".strip()
    if len(missing) == len(req_secs):
        if len(req_secs) == 1:
            return f"# {title}\n\n## {req_secs[0]}\n{core_b}\n"
        tail = "".join(f"\n## {s}\nSee details above for {title}.\n" for s in req_secs[2:])
        return f"# {title}\n\n## {req_secs[0]}\n{summary}\n\n## {req_secs[1]}\n{core_b}\n{tail}"
    return (body or "").rstrip() + "\n" + "".join(f"\n## {s}\nSee {title} notes above.\n" for s in missing)


def validate_curated_note(
    wiki_root: Path, meta: Dict[str, Any], body: str, existing_domain_notes: int = 0, split_sibling_count: int = 0,
) -> List[str]:
    """Validate ironclad `02_knowledge/` frontmatter, template sections, zero placeholders, and anti-orphan links."""
    errors: List[str] = []
    tpl_slug = str(meta.get("template") or "").strip().lower()
    t_res = get_template(wiki_root, tpl_slug) if tpl_slug and tpl_slug != "inbox-raw" else {"ok": False, "error": "Missing governed `template` (cannot use `inbox-raw` in `02_knowledge/`)."}
    tpl_info = t_res.get("template") if t_res.get("ok") else None
    if not t_res.get("ok"):
        errors.append(str(t_res.get("error")))

    for req_key in ("uid", "title", "summary", "author", "status", "document_type", "domain", "created_at", "updated_at"):
        if not str(meta.get(req_key) or "").strip():
            errors.append(f"Missing required YAML frontmatter field `{req_key}`.")
    summary = str(meta.get("summary") or "").strip()
    if summary and len(summary) < 20:
        errors.append(f"Frontmatter `summary` is too brief ({len(summary)} chars; write >=20 chars).")
    if not [t for t in (meta.get("tags") if isinstance(meta.get("tags"), list) else []) if str(t).strip()]:
        errors.append("Frontmatter `tags` must contain at least 1 descriptive tag.")
    if not [o for o in (meta.get("ontology") if isinstance(meta.get("ontology"), list) else []) if str(o).strip()]:
        errors.append("Frontmatter `ontology` must contain at least 1 canonical concept slug for cross-domain discovery.")

    clean_body = (body or "").strip()
    if len(clean_body) < 40:
        errors.append("Note Markdown body is too short (<40 chars) for curated `02_knowledge/`.")
    for ph in _PLACEHOLDER_RE.findall(f"{summary}\n{clean_body}")[:3]:
        errors.append(f"Unfilled template placeholder `{ph}` must be replaced with real content.")
    if "<title>" in clean_body.lower():
        errors.append("Replace `<Title>` skeleton heading with the actual note title.")

    if tpl_info:
        headings_norm = [re.sub(r"[^a-z0-9]+", " ", h.lower()).strip() for h in _HEADING_RE.findall(clean_body)]
        for req_sec in tpl_info["required_sections"]:
            target_norm = re.sub(r"[^a-z0-9]+", " ", str(req_sec).lower()).strip()
            min_prefix = target_norm.split(" ")[0] if target_norm else ""
            if not any(target_norm in h or (len(h) >= max(4, len(min_prefix)) and target_norm.startswith(h)) for h in headings_norm if h):
                errors.append(f"Missing required section `## {req_sec}` mandated by template `{tpl_slug}`.")

    rel_meta = meta.get("related") if isinstance(meta.get("related"), list) else []
    all_links = extract_wikilinks(clean_body + "\n" + " ".join(str(x) for x in rel_meta))
    if (existing_domain_notes > 0 or split_sibling_count > 1) and not all_links:
        errors.append("ANTI-ORPHAN GUARD: Domain already contains curated notes (or multi-note split), so at least 1 `[[wikilink]]` is required.")
    return errors


# ---------------------------------------------------------------------------
# 2. Knowledge Warehouse Engine (`WikiEngine`)
# ---------------------------------------------------------------------------
WIKI_FOLDERS = ("01_inbox", "02_knowledge", "03_resources", "04_archive")
_INLINE_TAG_RE = re.compile(r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_-]{1,39})\b")

WIKI_DDL: Tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS wiki_notes_index (rel_path TEXT PRIMARY KEY, stem TEXT NOT NULL, folder TEXT NOT NULL, domain TEXT DEFAULT '', topic TEXT DEFAULT '', title TEXT NOT NULL, summary TEXT DEFAULT '', author TEXT NOT NULL DEFAULT 'companion', status TEXT DEFAULT 'active', template TEXT DEFAULT '', document_type TEXT DEFAULT '', tags_json TEXT DEFAULT '[]', ontology_json TEXT DEFAULT '[]', frontmatter_json TEXT DEFAULT '{}', supersedes_json TEXT DEFAULT '[]', superseded_by TEXT DEFAULT '', uid TEXT DEFAULT '', word_count INTEGER DEFAULT 0, context_tokens INTEGER DEFAULT 0, content_hash TEXT NOT NULL, updated_at TEXT DEFAULT '', last_verified_at TEXT DEFAULT '', review_after_days INTEGER DEFAULT 365, excerpt TEXT DEFAULT '', body_text TEXT DEFAULT '')",
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
    ("frontmatter_json", "TEXT DEFAULT '{}'"),
    ("supersedes_json", "TEXT DEFAULT '[]'"), ("superseded_by", "TEXT DEFAULT ''"), ("uid", "TEXT DEFAULT ''"),
    ("word_count", "INTEGER DEFAULT 0"), ("context_tokens", "INTEGER DEFAULT 0"),
    ("last_verified_at", "TEXT DEFAULT ''"), ("review_after_days", "INTEGER DEFAULT 365"),
)
_SUMMARY_COLS = ["rel_path", "stem", "folder", "domain", "topic", "title", "summary", "author", "status", "template", "document_type", "tags_json", "ontology_json", "frontmatter_json", "superseded_by", "content_hash", "updated_at", "last_verified_at", "review_after_days", "excerpt"]
_FM_KEY_ALIASES: Dict[str, str] = {
    "tag": "tags", "ont": "ontology", "dom": "domain", "tpl": "template",
    "type": "document_type", "doc": "document_type", "dir": "folder", "path": "folder",
}
_FM_TOKEN_RE = re.compile(r'(?:^|\s)(?:#([a-zA-Z0-9_-]+)|([a-zA-Z_][a-zA-Z0-9_-]*):(?:"([^"]+)"|\'([^\']+)\'|([^\s]+)))')


def _flatten_frontmatter_fts(meta: Dict[str, Any], tags: List[str], ont: List[str], summary: str) -> str:
    parts: List[str] = [*tags, *ont, summary]
    for k, v in meta.items():
        ks = str(k).strip().lower()
        if not ks or v in (None, "", []):
            continue
        vals = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, (list, tuple)) else [str(v).strip()]
        for sv in vals:
            parts.extend([ks, sv, f"{ks}:{sv}"])
    return " ".join(parts)


def _parse_frontmatter_query(query: str) -> Tuple[List[Tuple[str, str]], str]:
    filters: List[Tuple[str, str]] = []
    spans: List[Tuple[int, int]] = []
    for m in _FM_TOKEN_RE.finditer(query or ""):
        if m.group(1):
            filters.append(("tags", m.group(1).lower()))
            spans.append(m.span())
            continue
        raw_k = (m.group(2) or "").lower()
        raw_v = (m.group(3) or m.group(4) or m.group(5) or "").strip()
        if raw_k in ("http", "https") or raw_v.startswith("//") or not raw_v:
            continue
        filters.append((_FM_KEY_ALIASES.get(raw_k, raw_k), raw_v))
        spans.append(m.span())
    if not spans:
        return [], (query or "").strip()
    rem: List[str] = []
    last = 0
    for s, e in spans:
        rem.append(query[last:s])
        last = e
    rem.append(query[last:])
    return filters, re.sub(r"\s+", " ", "".join(rem)).strip()


def _match_frontmatter_row(row: Dict[str, Any], filters: List[Tuple[str, str]]) -> Tuple[bool, List[str]]:
    meta = row.get("frontmatter") if isinstance(row.get("frontmatter"), dict) else {}
    matched_labels: List[str] = []
    for key, raw_val in filters:
        choices = [c.strip().lower() for c in raw_val.split(",") if c.strip()]
        if not choices:
            continue
        if key == "has":
            ok = any((c in meta and meta[c] not in (None, "", [])) or bool(row.get(c)) for c in choices)
        elif key == "stale":
            want = choices[0] in ("true", "1", "yes")
            ok = bool(row.get("stale")) is want
        elif key == "folder":
            rp = str(row.get("rel_path") or "").lower()
            fl = str(row.get("folder") or "").lower()
            ok = any(fl == c or rp.startswith(c.rstrip("/") + "/") or c in rp for c in choices)
        else:
            cand_vals: List[str] = []
            for src in (row.get(key), meta.get(key)):
                if isinstance(src, (list, tuple)):
                    cand_vals.extend(str(x).lower() for x in src if str(x).strip())
                elif src not in (None, ""):
                    cand_vals.append(str(src).lower())
            ok = any(any(c == cv or c in cv for cv in cand_vals) for c in choices)
        if not ok:
            return False, []
        matched_labels.append(f"{key}:{raw_val}")
    return True, matched_labels


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


_INDEXED_WIKI_ROOTS: set[Tuple[int, str]] = set()
_WIKI_FS_LOCK = threading.RLock()


class WikiEngine:
    """Co-owned Markdown Knowledge Warehouse with Dual-Author provenance, 11 templates & Ontology graph."""

    def __init__(self, wiki_dir: Optional[Path] = None, store: Optional[MemoryStore] = None):
        self._wiki_dir = wiki_dir
        self._store = store

    @property
    def store(self) -> MemoryStore:
        mem = self._store or get_memory_store()

        def _init_wiki(m: MemoryStore) -> None:
            for ddl in WIKI_DDL:
                m.conn.execute(ddl)
            existing = {str(r[1]) for r in m.conn.execute("PRAGMA table_info(wiki_notes_index)").fetchall()}
            for col_name, col_def in _EXTRA_INDEX_COLS:
                if col_name not in existing:
                    m.conn.execute(f"ALTER TABLE wiki_notes_index ADD COLUMN {col_name} {col_def}")

        mem.ensure_schema("wiki", _init_wiki)
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
        with _WIKI_FS_LOCK:
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
                "INSERT OR REPLACE INTO wiki_notes_index (rel_path, stem, folder, domain, topic, title, summary, author, status, template, document_type, tags_json, ontology_json, frontmatter_json, supersedes_json, superseded_by, uid, word_count, context_tokens, content_hash, updated_at, last_verified_at, review_after_days, excerpt, body_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rel, full_path.stem.lower(), folder, domain, topic, title, summary, author, str(meta.get("status") or ("inbox" if folder == "01_inbox" else "active")), str(meta.get("template") or ""), str(meta.get("document_type") or ""), json.dumps(all_tags), json.dumps(all_ont), json.dumps(meta, ensure_ascii=False), json.dumps(sup_list), sup_by_str, str(meta.get("uid") or ""), int(meta.get("word_count") or metrics["word_count"]), int(meta.get("context_tokens") or metrics["context_tokens"]), sha16_bytes(raw_bytes), upd, ver_at, rev_days, excerpt, body),
            )
            cur.execute("INSERT INTO wiki_fts (rel_path, title, tags, body_text) VALUES (?, ?, ?, ?)", (rel, title, _flatten_frontmatter_fts(meta, all_tags, all_ont, summary), body))
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
        mem = self.store
        _INDEXED_WIKI_ROOTS.add((id(mem), str(base)))
        md_files = [p for f in WIKI_FOLDERS if (base / f).is_dir() for p in walk_pruned_files(base / f) if p.suffix.lower() == ".md" and not _is_system_wiki_file(str(p.relative_to(base)))]
        if not md_files and not mem._fetch_rows("SELECT 1 FROM wiki_notes_index LIMIT 1"):
            return {"ok": True, "indexed_notes": 0, "indexed_links": 0, "summary": "Reindexed 0 Wiki note(s) and 0 wikilink(s)."}
        with mem._lock:
            for tbl in ("wiki_notes_index", "wiki_fts", "wiki_links_index"):
                mem.conn.execute(f"DELETE FROM {tbl}")
            mem.conn.commit()
        for f in md_files:
            self._index_single_file(f)
        with mem._lock:
            cur = mem.conn.cursor()
            for src_p, tgt_ref in cur.execute("SELECT source_path, target_ref FROM wiki_links_index WHERE resolved_path = ''").fetchall():
                if res := self._resolve_wikilink_target(cur, str(tgt_ref)):
                    cur.execute("UPDATE wiki_links_index SET resolved_path = ? WHERE source_path = ? AND target_ref = ?", (res, src_p, tgt_ref))
            link_cnt = int((cur.execute("SELECT COUNT(*) FROM wiki_links_index").fetchone() or (0,))[0])
            mem.conn.commit()
        return {"ok": True, "indexed_notes": len(md_files), "indexed_links": link_cnt, "summary": f"Reindexed {len(md_files)} Wiki note(s) and {link_cnt} wikilink(s)."}

    def _ensure_initial_index(self) -> None:
        base = self.ensure_warehouse()
        mem = self.store
        key = (id(mem), str(base))
        with _WIKI_FS_LOCK:
            if key not in _INDEXED_WIKI_ROOTS and (
                not mem._fetch_rows("SELECT 1 FROM wiki_notes_index LIMIT 1")
                or mem._fetch_rows("SELECT 1 FROM wiki_notes_index WHERE frontmatter_json = '{}' LIMIT 1")
            ):
                self.rebuild_index()
            else:
                _INDEXED_WIKI_ROOTS.add(key)

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
            "domain": ("" if parts[0] == "01_inbox" and str(meta.get("domain") or "").strip().lower() in ("", "inbox", "01-inbox", "01_inbox") else str(meta.get("domain") or (parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else ""))),
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
        valid_req_tpl = bool(req_tpl and req_tpl != "inbox-raw" and (req_tpl in BUILTIN_TEMPLATE_MAP or get_template(self.root, req_tpl).get("ok")))
        chosen_tpl = req_tpl if valid_req_tpl else (recommend_templates(eff_title, clean_body, source, wiki_root=self.root)[0]["slug"] if eff_author == "companion" and source not in ("chat-export", "job-overflow") else "inbox-raw")
        body_out = clean_body if clean_body.startswith("#") else f"# {eff_title}\n\n{clean_body}"
        meta = hydrate_note_meta({**in_meta, "source": source}, eff_title, body_out, eff_author, "inbox", chosen_tpl, slugify_wiki(domain_hint or str(in_meta.get("domain") or "inbox"), "inbox"), "", as_slug_list(tags if tags is not None else in_meta.get("tags")) or ["inbox"], canonicalize_ontology(self.root, ontology if ontology is not None else in_meta.get("ontology")), now_iso, summary=summary, wiki_root=self.root)
        full.write_text(render_frontmatter(meta, body_out), encoding="utf-8", newline="\n")
        self._index_single_file(full)
        return {"ok": True, "path": rel, "author": eff_author, "template": chosen_tpl, "content_hash": sha16_bytes(full.read_bytes()), "summary": f"Captured note `{rel}` in `01_inbox/` (`author: {eff_author}`, `template: {chosen_tpl}`)."}

    def _count_other_domain_notes(self, domain: str, exclude_rel: str) -> int:
        mem = self.store
        with mem._lock:
            row = mem.conn.execute("SELECT COUNT(*) FROM wiki_notes_index WHERE folder = '02_knowledge' AND domain = ? AND rel_path != ?", (domain, exclude_rel)).fetchone()
        return int(row[0]) if row else 0

    def _find_domain_peer_link(self, domain: str, exclude_rel: str) -> str:
        rows = self.store._fetch_rows(
            "SELECT stem FROM wiki_notes_index WHERE folder = '02_knowledge' AND domain = ? AND rel_path != ? "
            "ORDER BY CASE WHEN document_type = 'hub' OR stem LIKE '%hub%' OR stem LIKE '%index%' THEN 0 ELSE 1 END, rel_path ASC LIMIT 1",
            (domain, exclude_rel),
        )
        return f"[[{rows[0][0]}]]" if rows else ""

    def save_or_update_note(self, rel_path: str, content: Optional[str] = None, title: str = "", tags: Optional[List[str]] = None, actor: str = "companion", expected_hash: str = "", confirmed: bool = False, summary: str = "", template: str = "", ontology: Optional[List[str]] = None, status: str = "") -> Dict[str, Any]:
        is_user = actor.strip().lower() in ("user", "cli", "web")
        is_fg = (not is_user) and get_execution_origin() == "foreground_turn" and get_trust_mode(store=self.store) != "strict"
        norm_in = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not is_user and not is_fg and norm_in.startswith("02_knowledge/"):
            cand_check, _ = self._validate_rel_note_path(norm_in)
            if cand_check is not None and not cand_check.is_file():
                return {"ok": False, "error": "WIKI ONE-DOOR INBOX GUARD: New Companion notes must be captured in `01_inbox/` first (then curated via `inspect_graduation` -> `commit_curation` or `graduate_note`)."}
        full, path_err = self._validate_rel_note_path(norm_in)
        if path_err or full is None:
            return {"ok": False, "error": path_err or "Invalid path."}
        rel = str(full.relative_to(self.root)).replace("\\", "/")
        existed, old_meta, old_body = full.is_file(), {}, ""
        if existed:
            disk_bytes = full.read_bytes()
            disk_hash = sha16_bytes(disk_bytes)
            if expected_hash and expected_hash.strip() != disk_hash:
                return {"ok": False, "conflict": True, "current_hash": disk_hash, "error": f"CONCURRENCY CONFLICT on `{rel}`: expected hash `{expected_hash}`, found `{disk_hash}`. Re-read the note before saving."}
            old_meta, old_body = parse_frontmatter(disk_bytes.decode("utf-8", errors="replace"))
        if content is None:
            clean_body_raw, in_meta, in_body = old_body, {}, old_body
        else:
            clean_body_raw = redact_secrets(content)
            in_meta, parsed_b = parse_frontmatter(clean_body_raw)
            in_body = parsed_b if (parsed_b.strip() or not existed) else old_body
        if existed and not is_user and str(old_meta.get("author") or "companion").strip().lower() in ("user", "human", "partner"):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue("wiki_edit", "edit_user_note", rel, f"Companion requests permission to update your user-authored Wiki note `{rel}`.", payload={"path": rel, "content": in_body, "title": title, "tags": tags, "summary": summary, "template": template, "ontology": ontology, "content_sha": sha16_bytes(in_body.encode("utf-8"))}, requester=actor, confirmed=confirmed, always_require_in_autonomous=True)
            if not allowed:
                return {"ok": False, "awaiting_approval": True, "approval_id": apr["approval_id"], "error": f"WIKI AUTHOR GUARD REJECTED (`{apr['approval_id']}`): `{rel}` has `author: user` and awaits your approval before Companion can edit it."}
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        eff_author = "user" if is_user else str(old_meta.get("author") or "companion")
        eff_title = title.strip() or str(in_meta.get("title") or old_meta.get("title") or full.stem.replace("-", " ").title())
        parts = rel.split("/")
        eff_domain = parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else parts[0]
        eff_topic = parts[2] if parts[0] == "02_knowledge" and len(parts) == 4 else str(in_meta.get("topic") or old_meta.get("topic") or "")
        eff_tpl = slugify_wiki(template or str(in_meta.get("template") or old_meta.get("template") or ("standard-note" if parts[0] == "02_knowledge" else "inbox-raw")), "standard-note")
        eff_status = status.strip() or str(in_meta.get("status") or old_meta.get("status") or ("inbox" if parts[0] == "01_inbox" else "active"))
        eff_sum = (summary or str(in_meta.get("summary") or old_meta.get("summary") or "")).strip()
        if is_fg and parts[0] == "02_knowledge":
            if len(eff_sum) < 20:
                first_ln = next((ln.strip(" #-*>") for ln in in_body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), eff_title)
                eff_sum = f"{eff_title} — {first_ln}"[:220]
                if len(eff_sum) < 24:
                    eff_sum = f"Curated reference note for {eff_title} in {eff_domain}."
            in_body = scaffold_foreground_body(self.root, eff_tpl, eff_title, eff_sum, in_body, rel)
        raw_tags = tags if tags is not None else (in_meta["tags"] if "tags" in in_meta else old_meta.get("tags") or ([eff_domain] if is_fg else []))
        raw_ont = ontology if ontology is not None else (in_meta["ontology"] if "ontology" in in_meta else old_meta.get("ontology") or (["reference-knowledge"] if is_fg else []))
        merged_meta = hydrate_note_meta({**old_meta, **in_meta}, eff_title, in_body, eff_author, eff_status, eff_tpl, eff_domain, eff_topic, as_slug_list(raw_tags) or ([eff_domain] if is_fg else []), canonicalize_ontology(self.root, raw_ont) or (["reference-knowledge"] if is_fg else []), now_iso, summary=eff_sum, wiki_root=self.root)
        dom_others = self._count_other_domain_notes(eff_domain, rel) if parts[0] == "02_knowledge" else 0
        if is_fg and dom_others > 0 and not extract_wikilinks(in_body + " " + " ".join(str(x) for x in (merged_meta.get("related") or []))):
            if peer_lk := self._find_domain_peer_link(eff_domain, rel):
                merged_meta["related"] = [*(merged_meta.get("related") or []), peer_lk]
        prior_governed = existed and not is_user and not validate_curated_note(self.root, old_meta, old_body, existing_domain_notes=0)
        if parts[0] == "02_knowledge" and (is_fg or prior_governed or bool(template.strip()) or bool(in_meta.get("template"))):
            errs = validate_curated_note(self.root, merged_meta, in_body, existing_domain_notes=dom_others)
            if errs:
                return {"ok": False, "validation_errors": errs, "error": "WIKI SCHEMA GUARD REJECTED:\n- " + "\n- ".join(errs)}
        tx_id = f"ctx_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.urandom(2).hex()}" if (is_fg and parts[0] == "02_knowledge") else ""
        archived_to = self._archive_prior_version(full, "fg_edit" if tx_id else "edit", superseded_by=rel, tx_id=tx_id, subdir="revisions" if tx_id else "") if existed else None
        if tx_id:
            merged_meta["curation_tx_id"] = tx_id
            if archived_to:
                merged_meta["supersedes"] = [archived_to]
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(render_frontmatter(merged_meta, in_body), encoding="utf-8", newline="\n")
        self._index_single_file(full)
        if tx_id:
            kb_arch = [{"original_path": rel, "archive_path": archived_to}] if archived_to else []
            with self.store._lock:
                self.store.conn.execute(
                    "INSERT INTO wiki_curation_history (tx_id, curation_id, strategy, actor, summary, inbox_archives_json, knowledge_archives_json, output_paths_json, repointed_links_json, reverted, created_at) VALUES (?, 'foreground', 'FOREGROUND_SAVE', ?, ?, '[]', ?, ?, '[]', 0, ?)",
                    (tx_id, actor, f"Foreground curated save `{rel}`", json.dumps(kb_arch), json.dumps([rel]), now_iso),
                )
                self.store.conn.commit()
        return {"ok": True, "path": rel, "author": eff_author, "tx_id": tx_id, "archived_prior": archived_to, "content_hash": sha16_bytes(full.read_bytes()), "summary": f"Saved `{rel}` (`author: {eff_author}`" + (f", archived prior -> `{archived_to}`" if archived_to else "") + ")."}

    def graduate_note(self, inbox_path: str, domain: str, topic: str = "", new_slug: str = "", actor: str = "companion", confirmed: bool = False, template: str = "", summary: str = "", ontology: Optional[List[str]] = None) -> Dict[str, Any]:
        src_rel = (inbox_path or "").strip().replace("\\", "/").lstrip("/")
        if not src_rel.startswith("01_inbox/"):
            src_rel = f"01_inbox/{Path(src_rel).name}"
        src_full, err = self._validate_rel_note_path(src_rel)
        if err or src_full is None or not src_full.is_file():
            return {"ok": False, "error": f"Inbox note `{src_rel}` not found."}
        dom_slug = slugify_wiki(domain, fallback="")
        if dom_slug in ("", "inbox", "01-inbox", "01_inbox"):
            return {"ok": False, "error": "Graduation requires a real curated `domain` folder (cannot graduate into `01-inbox` or `inbox`). Use AI Graduation (`propose_graduation`) to infer taxonomy."}
        top_slug = slugify_wiki(topic, fallback="") if (topic or "").strip() else ""
        note_slug = slugify_wiki(new_slug or src_full.stem, fallback=src_full.stem)
        dst_rel = f"02_knowledge/{dom_slug}/{top_slug}/{note_slug}.md" if top_slug else f"02_knowledge/{dom_slug}/{note_slug}.md"
        dst_full, dst_err = self._validate_rel_note_path(dst_rel)
        if dst_err or dst_full is None:
            return {"ok": False, "error": dst_err or "Invalid destination path."}
        is_user = actor.strip().lower() in ("user", "cli", "web")
        is_fg = (not is_user) and get_execution_origin() == "foreground_turn" and get_trust_mode(store=self.store) != "strict"
        dst_user_owned = False
        if dst_full.is_file():
            dst_old_m, _ = parse_frontmatter(dst_full.read_text(encoding="utf-8", errors="replace"))
            dst_user_owned = str(dst_old_m.get("author") or "").strip().lower() in ("user", "human", "partner")
        meta, body = parse_frontmatter(src_full.read_text(encoding="utf-8", errors="replace"))
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw_tpl = slugify_wiki(template or str(meta.get("template") or ""), "")
        must_validate = bool(template.strip()) or raw_tpl in ("", "inbox-raw")
        cur_tpl = ("standard-note" if (is_fg and raw_tpl in ("", "inbox-raw")) else (raw_tpl or "inbox-raw")) if must_validate else raw_tpl
        clean_tags = [t for t in as_slug_list(meta.get("tags")) if t not in ("inbox", "01-inbox", "01_inbox")] or [dom_slug]
        out_meta = hydrate_note_meta({**meta, "supersedes": [src_rel]}, str(meta.get("title") or note_slug), body, str(meta.get("author") or "companion"), "active", cur_tpl, dom_slug, top_slug, clean_tags, canonicalize_ontology(self.root, ontology if ontology is not None else meta.get("ontology")) or (["reference-knowledge"] if is_fg else []), now_iso, summary=summary, wiki_root=self.root)
        dom_others = self._count_other_domain_notes(dom_slug, dst_rel)
        if is_fg and dom_others > 0 and not extract_wikilinks(body + " " + " ".join(str(x) for x in (out_meta.get("related") or []))):
            if peer_lk := self._find_domain_peer_link(dom_slug, dst_rel):
                out_meta["related"] = [*(out_meta.get("related") or []), peer_lk]
        if must_validate:
            errs = validate_curated_note(self.root, out_meta, body, existing_domain_notes=dom_others)
            if errs:
                return {"ok": False, "validation_errors": errs, "error": "WIKI SCHEMA GUARD REJECTED:\n- " + "\n- ".join(errs)}
        if not is_user and (not is_fg or dst_user_owned):
            allowed, apr = get_approval_queue(self.store).check_or_enqueue("wiki_edit", "graduate_note", f"{src_rel} -> {dst_rel}", f"Graduate inbox note `{src_rel}` into curated knowledge `{dst_rel}`.", payload={"from": src_rel, "to": dst_rel, "template": template, "summary": summary, "ontology": ontology}, requester=actor, confirmed=confirmed, always_require_in_autonomous=dst_user_owned)
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
        self._ensure_initial_index()
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
            r["frontmatter"] = json.loads(r.pop("frontmatter_json", "") or "{}")
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
                r.update({"tags": json.loads(r.pop("tags_json") or "[]"), "ontology": r_ont, "frontmatter": json.loads(r.pop("frontmatter_json", "") or "{}"), "match_kind": "ontology_bridge", "shared_ontology": shared, "bridge_via": via_label})
                scored.append((len(shared), r))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:limit]]

    def search_notes(self, query: str, limit: int = 12, include_archive: bool = False, include_ontology_bridges: bool = True) -> List[Dict[str, Any]]:
        self._ensure_initial_index()
        clean_q = (query or "").strip()
        if not clean_q:
            return self.list_notes(limit=limit)
        fm_filters, free_q = _parse_frontmatter_query(clean_q)
        eff_arch = include_archive or any(
            (k == "folder" and "04_archive" in v.lower()) or (k == "status" and "archive" in v.lower())
            for k, v in fm_filters
        )
        cols, arch_sql = [*_SUMMARY_COLS, "body_text"], ("" if eff_arch else "WHERE folder != '04_archive' ")
        raw_rows = self.store._fetch_dicts(cols, f"SELECT {', '.join(cols)} FROM wiki_notes_index {arch_sql}ORDER BY updated_at DESC, rel_path ASC LIMIT 400")
        by_path: Dict[str, Dict[str, Any]] = {}
        matched_map: Dict[str, List[str]] = {}
        for r in raw_rows:
            r["tags"] = json.loads(r.get("tags_json") or "[]")
            r["ontology"] = json.loads(r.get("ontology_json") or "[]")
            r["frontmatter"] = json.loads(r.get("frontmatter_json") or "{}")
            age_d, rev_d, is_stale = compute_note_staleness(str(r.get("folder") or ""), str(r.get("template") or ""), str(r.get("last_verified_at") or ""), str(r.get("updated_at") or ""), r.get("review_after_days"))
            r["review_after_days"], r["days_since_verified"], r["stale"] = rev_d, age_d, is_stale
            if fm_filters:
                ok_fm, labels = _match_frontmatter_row(r, fm_filters)
                if not ok_fm:
                    continue
                matched_map[str(r["rel_path"])] = labels
            by_path[str(r["rel_path"])] = r
        if not by_path:
            return []
        if not free_q:
            ranked_paths = list(by_path.keys())[: max(1, int(limit))]
        else:
            tokens = [t for t in re.findall(r"[a-zA-Z0-9_-]+", free_q) if t.lower() not in ("and", "or")]
            lex_paths: List[str] = []
            if tokens:
                with self.store._lock:
                    for joiner in (" AND ", " OR "):
                        hits = self.store.conn.execute("SELECT rel_path FROM wiki_fts WHERE wiki_fts MATCH ? ORDER BY rank LIMIT ?", (joiner.join(f'"{t}"*' for t in tokens), max(limit * 2, 30))).fetchall()
                        if hits:
                            lex_paths = [str(h[0]) for h in hits if str(h[0]) in by_path]
                            if lex_paths:
                                break
            ranked_paths = hybrid_rrf_rank(
                free_q,
                [(p, f"{r['title']} {r['summary']} {r['domain']} {r['topic']} {r['template']} {r['document_type']} {r['author']} {r['status']} {r['tags_json']} {r['ontology_json']} {r['frontmatter_json']} {r['body_text'][:1200]}") for p, r in by_path.items()],
                lexical_ids=lex_paths,
                limit=limit,
            )
        out: List[Dict[str, Any]] = []
        seed_ont: List[str] = []
        for p in ranked_paths:
            if p in by_path:
                raw_r = by_path[p]
                seed_ont.extend(raw_r.get("ontology") or [])
                item = {**raw_r, "match_kind": "frontmatter" if (fm_filters and not free_q) else "direct", "matched_fields": matched_map.get(p, [])}
                for k_drop in ("tags_json", "ontology_json", "frontmatter_json", "body_text"):
                    item.pop(k_drop, None)
                out.append(item)
        if include_ontology_bridges and not fm_filters and seed_ont and len(out) < limit:
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


# ---------------------------------------------------------------------------
# 3. Visual Diffs, Ontology Consolidation, AI Freshness Audit & Chat Staging
# ---------------------------------------------------------------------------
_SAFE_ASSET_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIME_EXT_MAP = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _extract_llm_json_dict(raw_text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    if "</think>" in cleaned.lower():
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1].strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e > s:
        try:
            obj = json.loads(cleaned[s : e + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def _build_diff_file(title: str, old_label: str, new_label: str, old_text: str, new_text: str) -> Dict[str, Any]:
    old_lines = (old_text or "").replace("\r\n", "\n").splitlines()
    new_lines = (new_text or "").replace("\r\n", "\n").splitlines()
    uni = "\n".join(difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label, lineterm="", n=3))
    rows: List[Dict[str, Any]] = []
    add_cnt = del_cnt = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for group in matcher.get_grouped_opcodes(3):
        if not group:
            continue
        i_start, i_end = group[0][1] + 1, max(group[-1][2] - group[0][1], 0)
        j_start, j_end = group[0][3] + 1, max(group[-1][4] - group[0][3], 0)
        rows.append({"kind": "hunk", "old_ln": None, "new_ln": None, "old_text": f"@@ -{i_start},{i_end} +{j_start},{j_end} @@", "new_text": ""})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append({"kind": "context", "old_ln": i1 + k + 1, "new_ln": j1 + k + 1, "old_text": old_lines[i1 + k], "new_text": new_lines[j1 + k]})
            elif tag == "delete":
                del_cnt += i2 - i1
                for k in range(i1, i2):
                    rows.append({"kind": "del", "old_ln": k + 1, "new_ln": None, "old_text": old_lines[k], "new_text": ""})
            elif tag == "insert":
                add_cnt += j2 - j1
                for k in range(j1, j2):
                    rows.append({"kind": "add", "old_ln": None, "new_ln": k + 1, "old_text": "", "new_text": new_lines[k]})
            elif tag == "replace":
                del_cnt += i2 - i1
                add_cnt += j2 - j1
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    rows.append({"kind": "change", "old_ln": i1 + k + 1, "new_ln": j1 + k + 1, "old_text": old_lines[i1 + k], "new_text": new_lines[j1 + k]})
                for k in range(i1 + paired, i2):
                    rows.append({"kind": "del", "old_ln": k + 1, "new_ln": None, "old_text": old_lines[k], "new_text": ""})
                for k in range(j1 + paired, j2):
                    rows.append({"kind": "add", "old_ln": None, "new_ln": k + 1, "old_text": "", "new_text": new_lines[k]})
    return {
        "title": title, "old_label": old_label, "new_label": new_label,
        "additions": add_cnt, "deletions": del_cnt, "identical": add_cnt == 0 and del_cnt == 0,
        "unified_diff": uni, "rows": rows[:400],
    }


def _diff_pending_approval(engine: WikiEngine, approval_id: str) -> Dict[str, Any]:
    req = get_approval_queue(engine.store).get_request((approval_id or "").strip())
    if not req:
        return {"ok": False, "error": f"Approval request `{approval_id}` not found."}
    act = str(req.get("action_name") or "")
    payload = json.loads(req.get("payload_json") or "{}")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files: List[Dict[str, Any]] = []

    if act == "edit_user_note" and payload.get("path"):
        rel = str(payload["path"]).replace("\\", "/").lstrip("/")
        full = engine.root / rel
        old_raw = full.read_text(encoding="utf-8", errors="replace") if full.is_file() else ""
        old_meta, _ = parse_frontmatter(old_raw)
        in_meta, in_body = parse_frontmatter(str(payload.get("content") or ""))
        parts = rel.split("/")
        dom = parts[1] if parts[0] == "02_knowledge" and len(parts) >= 3 else parts[0]
        top = parts[2] if parts[0] == "02_knowledge" and len(parts) == 4 else ""
        tpl = slugify_wiki(str(payload.get("template") or in_meta.get("template") or old_meta.get("template") or "standard-note"))
        merged = hydrate_note_meta(
            {**old_meta, **in_meta}, str(payload.get("title") or in_meta.get("title") or old_meta.get("title") or full.stem),
            in_body, str(old_meta.get("author") or "user"), "active", tpl, dom, top,
            as_slug_list(payload.get("tags") or in_meta.get("tags") or old_meta.get("tags")),
            canonicalize_ontology(engine.root, payload.get("ontology") or in_meta.get("ontology") or old_meta.get("ontology")),
            now_iso, summary=str(payload.get("summary") or ""), wiki_root=engine.root,
        )
        files.append(_build_diff_file(rel, f"{rel} (current)", f"{rel} (proposed edit)", old_raw, render_frontmatter(merged, in_body)))
    elif act == "commit_curation" and payload.get("curation_id"):
        rows = engine.store._fetch_rows("SELECT inbox_paths_json, validated_plan_json FROM wiki_curation_sessions WHERE curation_id = ?", (str(payload["curation_id"]),))
        if not rows or not rows[0][1]:
            return {"ok": False, "error": f"Validated curation plan for `{payload.get('curation_id')}` not found."}
        in_paths, plan = json.loads(rows[0][0] or "[]"), json.loads(rows[0][1])
        in_raw = (engine.root / in_paths[0]).read_text(encoding="utf-8", errors="replace") if in_paths and (engine.root / in_paths[0]).is_file() else ""
        for out in plan.get("outputs", []):
            dst_rel = str(out["path"])
            dst_full = engine.root / dst_rel
            old_raw = dst_full.read_text(encoding="utf-8", errors="replace") if dst_full.is_file() else in_raw
            old_lbl = f"{dst_rel} (existing)" if dst_full.is_file() else f"{in_paths[0] if in_paths else '01_inbox'} (staged)"
            files.append(_build_diff_file(dst_rel, old_lbl, f"{dst_rel} (curated)", old_raw, render_frontmatter(out["meta"], str(out["body"]))))
    elif act == "graduate_note" and payload.get("from") and payload.get("to"):
        src_rel, dst_rel = str(payload["from"]), str(payload["to"])
        src_raw = (engine.root / src_rel).read_text(encoding="utf-8", errors="replace") if (engine.root / src_rel).is_file() else ""
        meta, body = parse_frontmatter(src_raw)
        to_parts = [x for x in dst_rel.split("/") if x]
        dom = to_parts[1] if len(to_parts) >= 3 else "general"
        top = to_parts[2] if len(to_parts) >= 4 else ""
        tpl = slugify_wiki(str(payload.get("template") or meta.get("template") or "standard-note"))
        out_m = hydrate_note_meta({**meta, "supersedes": [src_rel]}, str(meta.get("title") or Path(dst_rel).stem), body, str(meta.get("author") or "companion"), "active", tpl, dom, top, as_slug_list(meta.get("tags")) or [dom], canonicalize_ontology(engine.root, payload.get("ontology") or meta.get("ontology")), now_iso, summary=str(payload.get("summary") or ""), wiki_root=engine.root)
        old_raw = (engine.root / dst_rel).read_text(encoding="utf-8", errors="replace") if (engine.root / dst_rel).is_file() else src_raw
        files.append(_build_diff_file(dst_rel, src_rel, dst_rel, old_raw, render_frontmatter(out_m, body)))
    else:
        return {"ok": False, "error": f"Approval `{approval_id}` (`{act}`) does not contain a diffable Wiki payload."}

    tot_add, tot_del = sum(f["additions"] for f in files), sum(f["deletions"] for f in files)
    uni_all = "\n\n".join(f["unified_diff"] for f in files if f["unified_diff"])
    return {
        "ok": True, "mode": "approval_preview", "approval_id": approval_id, "action_name": act,
        "files": files, "total_additions": tot_add, "total_deletions": tot_del, "unified_diff": uni_all,
        "summary": f"Pending Approval `{approval_id}` (`{act}`): +{tot_add} / -{tot_del} lines across {len(files)} file(s).",
    }


def compute_wiki_diff(
    engine: WikiEngine, path: str = "", compare_to: str = "", approval_id: str = "", proposed_content: str = "",
) -> Dict[str, Any]:
    """Compute structured Unified + Side-by-Side Markdown diffs for lineage revisions or pending approvals."""
    engine.ensure_warehouse()
    if (approval_id or "").strip():
        return _diff_pending_approval(engine, approval_id)
    cur_note = engine.read_note(path)
    if not cur_note.get("ok"):
        return {"ok": False, "error": cur_note.get("error") or f"Wiki note `{path}` not found."}
    cur_rel, cur_raw, cur_meta = str(cur_note["path"]), str(cur_note["raw"]), dict(cur_note["frontmatter"])

    if proposed_content:
        df = _build_diff_file(cur_rel, f"{cur_rel} (on disk)", f"{cur_rel} (unsaved draft)", cur_raw, proposed_content)
        return {
            "ok": True, "mode": "draft_preview", "path": cur_rel, "files": [df],
            "total_additions": df["additions"], "total_deletions": df["deletions"], "unified_diff": df["unified_diff"],
            "summary": f"Draft diff for `{cur_rel}`: +{df['additions']} / -{df['deletions']} lines.",
        }

    other_rel = (compare_to or "").strip().replace("\\", "/").lstrip("/")
    direction = "prior_to_current"
    if not other_rel:
        sups = cur_meta.get("supersedes") if isinstance(cur_meta.get("supersedes"), list) else []
        if sups:
            other_rel = str(sups[-1])
        elif cur_meta.get("superseded_by"):
            other_rel = str(cur_meta["superseded_by"]).split(",")[0].strip()
            direction = "archive_to_successor"
        else:
            stem_tag = f"_{cur_rel.replace('/', '__').removesuffix('.md')}.md"
            arch_dir = engine.root / "04_archive"
            arch_hits = sorted(p for p in walk_pruned_files(arch_dir) if p.name.endswith(stem_tag)) if arch_dir.is_dir() else []
            if arch_hits:
                other_rel = str(arch_hits[-1].relative_to(engine.root)).replace("\\", "/")
    if not other_rel:
        return {"ok": False, "error": f"Note `{cur_rel}` has no archived prior revision or `supersedes` lineage yet."}
    other_note = engine.read_note(other_rel)
    if not other_note.get("ok"):
        return {"ok": False, "error": f"Comparison revision `{other_rel}` not found."}
    old_lbl, new_lbl = (cur_rel, str(other_note["path"])) if direction == "archive_to_successor" else (str(other_note["path"]), cur_rel)
    old_txt, new_txt = (cur_raw, str(other_note["raw"])) if direction == "archive_to_successor" else (str(other_note["raw"]), cur_raw)
    df = _build_diff_file(new_lbl, old_lbl, new_lbl, old_txt, new_txt)
    return {
        "ok": True, "mode": "lineage_diff", "path": cur_rel, "compare_to": str(other_note["path"]), "files": [df],
        "total_additions": df["additions"], "total_deletions": df["deletions"], "unified_diff": df["unified_diff"],
        "summary": f"Lineage diff `{old_lbl}` -> `{new_lbl}`: +{df['additions']} / -{df['deletions']} lines.",
    }


def _parse_alias_rules(raw_aliases: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(raw_aliases, dict):
        for k, v in raw_aliases.items():
            ks, vs = slugify_wiki(str(k), ""), slugify_wiki(str(v), "")
            if ks and vs and ks != vs:
                out[ks] = vs
    elif isinstance(raw_aliases, str) and raw_aliases.strip():
        for chunk in re.split(r"[,\n;]+", raw_aliases):
            sep = "->" if "->" in chunk else ("=" if "=" in chunk else "")
            if sep:
                lhs, _, rhs = chunk.partition(sep)
                ks, vs = slugify_wiki(lhs, ""), slugify_wiki(rhs, "")
                if ks and vs and ks != vs:
                    out[ks] = vs
    return out


def consolidate_ontology(engine: WikiEngine, aliases: Any = None, dry_run: bool = False, actor: str = "user") -> Dict[str, Any]:
    """Canonicalize ontology synonyms across active notes with Never-Delete `04_archive/` snapshots & near-duplicate scan."""
    engine.ensure_warehouse()
    is_user = actor.strip().lower() in ("user", "cli", "web")
    custom_map = _parse_alias_rules(aliases)
    auth = load_tag_authority(engine.root)
    merged_aliases = {**auth["aliases"], **custom_map}
    added_rules = {k: v for k, v in custom_map.items() if auth["aliases"].get(k) != v}
    if added_rules and not dry_run:
        raw_auth = str(auth["raw"]).rstrip() + "\n"
        rule_lines = "\n".join(f"- `{k}` -> `{v}`" for k, v in sorted(added_rules.items()))
        if "## Ontology Synonym Aliases" not in raw_auth:
            raw_auth += f"\n## Ontology Synonym Aliases (alias -> canonical)\n{rule_lines}\n"
        else:
            raw_auth += f"{rule_lines}\n"
        (engine.root / "03_resources" / "tag-authority.md").write_text(raw_auth, encoding="utf-8", newline="\n")

    canon_set = set(auth["ontology"]) | set(merged_aliases.values())
    updated_notes: List[Dict[str, Any]] = []
    skipped_user_notes: List[str] = []
    seen_unknown: set[str] = set()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for row in engine.list_notes(limit=500):
        rel = str(row["rel_path"])
        full = engine.root / rel
        if not full.is_file() or row["folder"] not in ("01_inbox", "02_knowledge"):
            continue
        meta, body = parse_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
        old_ont = as_slug_list(meta.get("ontology"))
        new_ont: List[str] = []
        for s in old_ont:
            mapped = merged_aliases.get(s, s[:-1] if s.endswith("s") and s[:-1] in canon_set else s)
            if mapped and mapped not in new_ont:
                new_ont.append(mapped)
            if mapped not in canon_set:
                seen_unknown.add(mapped)
        if new_ont != old_ont:
            if not is_user and str(meta.get("author") or "").lower() in ("user", "human", "partner"):
                skipped_user_notes.append(rel)
                continue
            arch_p = ""
            if not dry_run:
                arch_p = engine._archive_prior_version(full, "ontology_consolidate", superseded_by=rel, subdir="revisions") or ""
                meta["ontology"] = new_ont
                meta["updated_at"] = now_iso
                full.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
            updated_notes.append({"path": rel, "before": old_ont, "after": new_ont, "archived_prior": arch_p})

    if updated_notes and not dry_run:
        engine.rebuild_index()

    suggestions: List[Dict[str, Any]] = []
    for unk in sorted(seen_unknown):
        best_c, best_score = "", 0.0
        unk_toks = set(unk.split("-"))
        for can in canon_set:
            ratio = difflib.SequenceMatcher(None, unk, can).ratio()
            tok_overlap = len(unk_toks & set(can.split("-"))) / max(1, len(unk_toks | set(can.split("-"))))
            score = round(max(ratio, 0.55 * ratio + 0.45 * tok_overlap), 2)
            if score > best_score:
                best_c, best_score = can, score
        if best_c and best_score >= 0.68:
            suggestions.append({"alias": unk, "suggested_canonical": best_c, "similarity": best_score})

    mode_lbl = "DRY-RUN" if dry_run else "APPLIED"
    return {
        "ok": True, "dry_run": dry_run, "added_aliases": added_rules, "updated_notes": updated_notes,
        "skipped_user_notes": skipped_user_notes, "near_duplicate_suggestions": suggestions,
        "summary": f"Ontology Consolidation ({mode_lbl}): {len(updated_notes)} note(s) canonicalized, {len(added_rules)} alias rule(s) added, {len(suggestions)} near-duplicate suggestion(s).",
    }


def verify_note_freshness(
    engine: WikiEngine,
    path: str,
    review_after_days: int = 0,
    actor: str = "user",
    verified_at_override: str = "",
    quick_stamp: bool = False,
    chat_fn: Any = None,
) -> Dict[str, Any]:
    """Audit note claims/completeness via `fast`-tier LLM reasoning (unless `quick_stamp=True`) and stamp `last_verified_at`."""
    rd = engine.read_note(path)
    if not rd.get("ok"):
        return {"ok": False, "error": rd.get("error") or f"Wiki note `{path}` not found."}
    rel = str(rd["path"])
    full = engine.root / rel
    meta, body = parse_frontmatter(full.read_text(encoding="utf-8", errors="replace"))
    is_user = actor.strip().lower() in ("user", "cli", "web")
    if not is_user and str(meta.get("author") or "").lower() in ("user", "human", "partner"):
        return {"ok": False, "error": f"WIKI AUTHOR GUARD REJECTED: `{rel}` is user-authored; only the user can stamp verification."}

    eff_days = int(review_after_days) if int(review_after_days or 0) > 0 else int(meta.get("review_after_days") or default_review_days(str(meta.get("template") or "")))
    audit_summary = ""
    flagged_claims: List[str] = []
    used_ai = False

    should_audit = (not quick_stamp) and (chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1"))
    if should_audit:
        llm_call = chat_fn or chat
        sys_msg = (
            "You are Companion's Knowledge Freshness Auditor reviewing a curated Wiki note.\n"
            "Think very briefly (at most 3 short sentences), then inspect the note's concrete facts, measurements, versions, IP addresses, steps, or dates for internal contradictions, "
            "incomplete placeholders, or time-sensitive claims worth re-checking.\n"
            "Respond ONLY with valid JSON containing keys `fresh` (boolean), `audit_summary` (1-2 concise sentences), and `flagged_claims` (array of strings, empty if none)."
        )
        usr_msg = (
            f"Note Path: `{rel}`\nTitle: {meta.get('title') or rd['title']}\nTemplate: {meta.get('template')}\n"
            f"Last Verified: {meta.get('last_verified_at') or 'n/a'} (cadence: {eff_days}d)\n\n"
            f"Markdown Body:\n{body[:5000]}"
        )
        try:
            with tier_context("fast"):
                resp = llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none", max_tokens=4096) if chat_fn is None else llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none")
            txt, _think_t, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
            parsed = _extract_llm_json_dict(txt)
            if parsed and str(parsed.get("audit_summary") or "").strip() not in ("", "...", "1-2 concise sentences"):
                used_ai = True
                audit_summary = str(parsed.get("audit_summary") or "").strip()[:260]
                if isinstance(parsed.get("flagged_claims"), list):
                    flagged_claims = [str(c).strip() for c in parsed["flagged_claims"] if str(c).strip() and str(c).strip() != "..."][:5]
        except Exception:
            used_ai = False

    stamp = (verified_at_override or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")).strip()
    meta["last_verified_at"] = stamp
    meta["review_after_days"] = eff_days
    if audit_summary:
        meta["last_audit_summary"] = audit_summary
    full.write_text(render_frontmatter(meta, body), encoding="utf-8", newline="\n")
    engine._index_single_file(full)
    age_d, _, is_stale = compute_note_staleness(rel.split("/")[0], str(meta.get("template") or ""), stamp, stamp, eff_days)
    summary_msg = (
        f"AI Freshness Audit: `{rel}` verified — {audit_summary}"
        + (f" (Flagged: {'; '.join(flagged_claims)})" if flagged_claims else f" (next review in {eff_days}d).")
        if used_ai and audit_summary
        else f"Marked `{rel}` verified at `{stamp}` (next review in {eff_days} days)."
    )
    return {
        "ok": True,
        "path": rel,
        "last_verified_at": stamp,
        "review_after_days": eff_days,
        "days_since_verified": age_d,
        "stale": is_stale,
        "ai_audited": used_ai,
        "audit_summary": audit_summary,
        "flagged_claims": flagged_claims,
        "content_hash": sha16_bytes(full.read_bytes()),
        "summary": summary_msg,
    }


def list_stale_notes(engine: WikiEngine, as_of_iso: str = "", include_due_soon_days: int = 14) -> Dict[str, Any]:
    """List curated `02_knowledge/` notes past their template verification cadence (`90d`/`180d`/`365d`)."""
    ref_dt = datetime.now(timezone.utc)
    if (as_of_iso or "").strip():
        ref_dt = datetime.fromisoformat(as_of_iso.strip().replace("Z", "+00:00"))
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
    curated = engine.list_notes(folder="02_knowledge", limit=400)
    stale_items: List[Dict[str, Any]] = []
    due_soon: List[Dict[str, Any]] = []
    for n in curated:
        age_d, rev_d, is_stale = compute_note_staleness("02_knowledge", str(n.get("template") or ""), str(n.get("last_verified_at") or ""), str(n.get("updated_at") or ""), n.get("review_after_days"), now_dt=ref_dt)
        entry = {**n, "days_since_verified": age_d, "review_after_days": rev_d, "overdue_days": age_d - rev_d, "stale": is_stale}
        if is_stale:
            stale_items.append(entry)
        elif rev_d - age_d <= max(0, int(include_due_soon_days)):
            due_soon.append(entry)
    stale_items.sort(key=lambda x: x["overdue_days"], reverse=True)
    lines = [f"=== Wiki Freshness & Staleness Audit ({len(stale_items)} stale / {len(curated)} curated) ==="]
    lines.extend([f"  * [STALE +{s['overdue_days']}d] `{s['rel_path']}` (tpl={s['template']}, verified={s.get('last_verified_at') or 'n/a'}, cadence={s['review_after_days']}d)" for s in stale_items] or ["  (All curated `02_knowledge/` notes are within their verification cadence.)"])
    return {"ok": True, "stale_notes": stale_items, "due_soon": due_soon, "total_curated": len(curated), "summary": "\n".join(lines)}


def ingest_wiki_asset(engine: WikiEngine, filename: str, data_base64: str, alt_text: str = "") -> Dict[str, Any]:
    """Validate and save an inline diagram/screenshot inside `03_resources/assets/` for Obsidian + Web rendering."""
    from companion.attachments import MAX_ATTACHMENT_BYTES, inspect_image_bytes

    b64_clean = (data_base64 or "").strip()
    if "," in b64_clean[:256] and b64_clean.lower().startswith("data:"):
        b64_clean = b64_clean.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64_clean, validate=False)
    except Exception as exc:
        return {"ok": False, "error": f"Invalid base64 image payload: {exc}"}
    if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
        return {"ok": False, "error": "Wiki image payload must be non-empty and <= 20 MB."}
    img_info = inspect_image_bytes(raw)
    if img_info is None:
        return {"ok": False, "error": "Unsupported image bytes — only valid PNG, JPEG, WEBP, or GIF diagrams/images are allowed."}
    mime_type, width, height = img_info
    ext = _MIME_EXT_MAP.get(mime_type, ".png")
    stem = slugify_wiki(Path((filename or "diagram").strip()).stem, fallback="diagram")[:40]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    asset_name = f"{stamp}_{os.urandom(2).hex()}_{stem}{ext}"
    assets_dir = engine.ensure_warehouse() / "03_resources" / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / asset_name).write_bytes(raw)
    rel_asset = f"03_resources/assets/{asset_name}"
    clean_alt = re.sub(r"[\[\]\r\n]+", " ", (alt_text or stem.replace("-", " ").title())).strip() or "Diagram"
    return {
        "ok": True, "asset_name": asset_name, "asset_rel_path": rel_asset, "raw_url": f"/api/wiki/assets/{asset_name}",
        "mime_type": mime_type, "width": width, "height": height, "size_bytes": len(raw),
        "markdown": f"![{clean_alt}]({rel_asset})",
        "summary": f"Saved Wiki image `{rel_asset}` ({width}x{height} {mime_type}).",
    }


def read_wiki_asset_raw(engine: WikiEngine, asset_name: str) -> Optional[Tuple[str, bytes, str]]:
    """Safely serve raw image bytes from `03_resources/assets/<asset_name>` with traversal & magic-byte checks."""
    from companion.attachments import inspect_image_bytes

    clean = _SAFE_ASSET_RE.sub("", (asset_name or "").strip())
    if not clean or clean != asset_name.strip() or ".." in clean:
        return None
    assets_dir = (engine.ensure_warehouse() / "03_resources" / "assets").resolve()
    cand = (assets_dir / clean).resolve()
    if not cand.is_file() or not cand.is_relative_to(assets_dir):
        return None
    raw = cand.read_bytes()
    img_info = inspect_image_bytes(raw)
    if img_info is None:
        return None
    return img_info[0], raw, clean


def stage_chat_turn_to_inbox(
    engine: WikiEngine,
    assistant_content: str,
    user_prompt: str = "",
    title: str = "",
    actor: str = "user",
    message_id: Optional[int] = None,
    chat_fn: Any = None,
) -> Dict[str, Any]:
    """Distill a single Forever Chat assistant response (+ user prompt context) cleanly into `01_inbox/`."""
    body = scrub_conversational_chatter(redact_secrets((assistant_content or "").strip()))
    if not body:
        return {"ok": False, "error": "Assistant message `content` is required to stage a chat turn."}
    clean_prompt = redact_secrets((user_prompt or "").strip())
    if not clean_prompt and message_id is not None:
        rows = engine.store._fetch_rows(
            "SELECT content FROM conversations WHERE id < ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (int(message_id),),
        )
        if rows:
            clean_prompt = redact_secrets(str(rows[0][0] or "").strip())

    raw_title = (title or "").strip()
    if re.match(r"^chat synthesis\s+\d{4}-\d{2}-\d{2}$", raw_title, flags=re.IGNORECASE):
        raw_title = ""

    ai_summary = ""
    ai_domain = ""
    ai_tags: List[str] = []
    ai_tpl = ""
    should_ai = chat_fn is not None or ("unittest" not in sys.modules and os.environ.get("COMPANION_TEST_MODE") != "1")
    if should_ai:
        llm_call = chat_fn or chat
        avail_tpls = [t["slug"] for t in list_templates(engine.root)]
        sys_msg = (
            "You are Companion's Knowledge Inbox Curator staging a Chat turn into `01_inbox/`.\n"
            "Think very briefly (at most 2 short sentences), then choose a crisp descriptive `title`, a 1-2 sentence `summary` (30-200 chars), "
            f"a `domain_hint` (e.g. cooking, household, homelab, software-dev), 2-4 kebab-case `tags`, and best `template` from {avail_tpls}.\n"
            "Respond ONLY with a JSON object containing keys `title`, `summary`, `domain_hint`, `tags`, and `template`."
        )
        usr_msg = f"User Prompt:\n{clean_prompt[:600]}\n\nAssistant Response:\n{body[:3500]}"
        try:
            with tier_context("fast"):
                resp = llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none", max_tokens=4096) if chat_fn is None else llm_call([{"role": "system", "content": sys_msg}, {"role": "user", "content": usr_msg}], None, "none")
            txt, _thk, _ = normalize_assistant_message((resp.get("choices") or [{}])[0].get("message") or {})
            parsed = _extract_llm_json_dict(txt)
            if parsed:
                t_cand = str(parsed.get("title") or "").strip()
                if not raw_title and t_cand and t_cand != "...":
                    raw_title = t_cand
                ai_summary = str(parsed.get("summary") or "").strip()
                ai_domain = slugify_wiki(str(parsed.get("domain_hint") or ""), "")
                ai_tags = [t for t in as_slug_list(parsed.get("tags")) if t not in ("inbox", "01-inbox", "01_inbox")]
                cand_tpl = slugify_wiki(str(parsed.get("template") or ""), "")
                if cand_tpl in avail_tpls:
                    ai_tpl = cand_tpl
        except Exception:
            pass

    heading_m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    eff_title = raw_title or (heading_m.group(1).strip() if heading_m else "")
    if not eff_title:
        seed_line = clean_prompt.splitlines()[0] if clean_prompt else body.splitlines()[0]
        eff_title = re.sub(r"[#*`>\[\]]+", "", seed_line).strip()[:84] or "Staged Chat Insight"
    rec = recommend_templates(eff_title, f"{clean_prompt}\n{body}", source="chat-turn", wiki_root=engine.root)
    chosen_tpl = ai_tpl or (rec[0]["slug"] if rec else "zettelkasten-atomic")
    tpl_spec = BUILTIN_TEMPLATE_MAP.get(chosen_tpl)
    ont = canonicalize_ontology(engine.root, list(tpl_spec.default_ontology) if tpl_spec else ["knowledge-synthesis"])
    ctx_header = f"> **Origin Prompt**: {clean_prompt[:280].replace(chr(10), ' ')}\n\n" if clean_prompt else ""
    staged_body = f"# {eff_title}\n\n{ctx_header}{body}" if not body.lstrip().startswith("#") else f"{body.splitlines()[0]}\n\n{ctx_header}" + "\n".join(body.splitlines()[1:]).lstrip("\n")
    eff_tags = list(dict.fromkeys(["chat-turn", chosen_tpl, *ai_tags]))
    return engine.capture_inbox(
        title=eff_title,
        content=staged_body,
        tags=eff_tags,
        source="chat-turn",
        actor=actor,
        template=chosen_tpl,
        summary=ai_summary,
        ontology=ont,
        domain_hint=ai_domain,
    )


def _collect_frontmatter_facets(active_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, Dict[str, int]] = {
        "domains": {}, "tags": {}, "ontology": {}, "templates": {},
        "document_types": {}, "authors": {}, "statuses": {},
    }
    key_set: set[str] = set()
    scalar_map = (
        ("domains", "domain"), ("templates", "template"),
        ("document_types", "document_type"), ("authors", "author"), ("statuses", "status"),
    )
    for r in active_rows:
        for b_name, field in scalar_map:
            if val := str(r.get(field) or "").strip():
                buckets[b_name][val] = buckets[b_name].get(val, 0) + 1
        for tg in r.get("tags") or []:
            if s := str(tg).strip():
                buckets["tags"][s] = buckets["tags"].get(s, 0) + 1
        for ont in r.get("ontology") or []:
            if s := str(ont).strip():
                buckets["ontology"][s] = buckets["ontology"].get(s, 0) + 1
        fm = r.get("frontmatter") if isinstance(r.get("frontmatter"), dict) else {}
        for k, v in fm.items():
            if v not in (None, "", []):
                key_set.add(str(k).strip().lower())
    return {
        **{
            k: [{"value": val, "count": cnt} for val, cnt in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:30]]
            for k, d in buckets.items()
        },
        "keys": sorted(key_set),
    }


def _walk_wiki_dir_node(base: Path, cur_dir: Path, by_rel: Dict[str, Dict[str, Any]], include_system: bool) -> Dict[str, Any]:
    rel_dir = str(cur_dir.relative_to(base)).replace("\\", "/")
    root_folder = rel_dir.split("/", 1)[0]
    children: List[Dict[str, Any]] = []
    total_files = 0
    entries = sorted(cur_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())) if cur_dir.is_dir() else []
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        rel_entry = str(entry.relative_to(base)).replace("\\", "/")
        if entry.is_dir():
            if not include_system and rel_entry in ("03_resources/templates", "03_resources/assets"):
                continue
            sub_node = _walk_wiki_dir_node(base, entry, by_rel, include_system)
            total_files += int(sub_node["file_count"])
            children.append(sub_node)
            continue
        is_sys = rel_entry.startswith(("03_resources/templates/", "03_resources/assets/")) or rel_entry == "03_resources/tag-authority.md"
        if not include_system and is_sys:
            continue
        suffix = entry.suffix.lower()
        if suffix == ".md":
            idx = by_rel.get(rel_entry)
            if idx:
                node: Dict[str, Any] = {
                    "kind": "file", "name": entry.name, "rel_path": rel_entry, "folder": root_folder,
                    "title": idx["title"], "domain": idx["domain"], "topic": idx.get("topic") or "",
                    "author": idx["author"], "template": idx.get("template") or "",
                    "document_type": idx.get("document_type") or "", "status": idx.get("status") or "",
                    "tags": idx["tags"], "ontology": idx["ontology"], "stale": bool(idx.get("stale")),
                    "system_file": False,
                }
            else:
                meta, body = parse_frontmatter(entry.read_text(encoding="utf-8", errors="replace"))
                title = str(meta.get("title") or next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.strip()), entry.stem))[:120]
                node = {
                    "kind": "file", "name": entry.name, "rel_path": rel_entry, "folder": root_folder,
                    "title": title, "domain": str(meta.get("domain") or root_folder), "topic": "",
                    "author": str(meta.get("author") or "system"), "template": str(meta.get("template") or ""),
                    "document_type": str(meta.get("document_type") or ("template" if "/templates/" in rel_entry else "authority")),
                    "status": "system", "tags": as_slug_list(meta.get("tags")), "ontology": as_slug_list(meta.get("ontology")),
                    "stale": False, "system_file": True,
                }
            children.append(node)
            total_files += 1
        elif suffix in _MIME_EXT_MAP and rel_entry.startswith("03_resources/assets/"):
            children.append({
                "kind": "file", "name": entry.name, "rel_path": rel_entry, "folder": root_folder,
                "title": entry.name, "domain": "assets", "topic": "", "author": "user",
                "template": "", "document_type": "asset", "status": "asset",
                "tags": [], "ontology": [], "stale": False, "system_file": True,
                "is_asset": True, "asset_url": f"/api/wiki/assets/{entry.name}",
            })
            total_files += 1
    return {
        "kind": "folder", "name": cur_dir.name, "rel_path": rel_dir,
        "folder": root_folder, "file_count": total_files, "children": children,
    }


def build_wiki_directory_tree(engine: WikiEngine, include_archive: bool = True, include_system: bool = True) -> Dict[str, Any]:
    """Return the full 4-folder Wiki directory tree (`01_inbox`..`04_archive`) and live YAML frontmatter facets."""
    base = engine.ensure_warehouse()
    active_notes = engine.list_notes(limit=500)
    arch_notes = engine.list_notes(folder="04_archive", limit=500) if include_archive else []
    by_rel = {str(r["rel_path"]): r for r in [*active_notes, *arch_notes]}
    roots = [f for f in WIKI_FOLDERS if include_archive or f != "04_archive"]
    tree = [_walk_wiki_dir_node(base, base / f, by_rel, include_system=include_system) for f in roots if (base / f).is_dir()]
    facets = _collect_frontmatter_facets(active_notes)
    total_files = sum(int(n["file_count"]) for n in tree)
    lines = [f"=== Knowledge Wiki Directory Tree ({total_files} file(s)) ==="] + [
        f"  * `{n['rel_path']}/` ({n['file_count']} file(s))" for n in tree
    ]
    return {
        "ok": True, "tree": tree, "facets": facets,
        "total_files": total_files, "active_note_count": len(active_notes),
        "summary": "\n".join(lines),
    }


def execute_wiki_maintenance_subaction(wk: WikiEngine, sub: str, p: Dict[str, Any], actor: str) -> Optional[Dict[str, Any]]:
    """Dispatch Wiki maintenance subactions (`tree`, `diff`, `consolidate_ontology`, `verify_note`, `list_stale`, `upload_asset`, `stage_turn`, `propose_graduation`)."""
    if sub in ("tree", "directory_tree", "facets"):
        inc_arch = str(p.get("include_archive", "true")).strip().lower() not in ("false", "0", "no")
        inc_sys = str(p.get("include_system", "true")).strip().lower() not in ("false", "0", "no")
        return build_wiki_directory_tree(wk, include_archive=inc_arch, include_system=inc_sys)
    if sub in ("diff", "diff_note"):
        return compute_wiki_diff(wk, path=str(p.get("path") or ""), compare_to=str(p.get("compare_to") or ""), approval_id=str(p.get("approval_id") or ""), proposed_content=str(p.get("proposed_content") or ""))
    if sub == "consolidate_ontology":
        raw_dr = p.get("dry_run")
        dry = (raw_dr.strip().lower() in ("true", "1", "yes")) if isinstance(raw_dr, str) else bool(raw_dr)
        return consolidate_ontology(wk, aliases=p.get("aliases"), dry_run=dry, actor=actor)
    if sub == "verify_note":
        q_stamp = str(p.get("quick_stamp", "false")).strip().lower() in ("true", "1", "yes")
        return verify_note_freshness(
            wk,
            path=str(p.get("path") or ""),
            review_after_days=int(p.get("review_after_days") or 0),
            actor=actor,
            verified_at_override=str(p.get("verified_at") or ""),
            quick_stamp=q_stamp,
        )
    if sub in ("list_stale", "stale"):
        return list_stale_notes(wk, as_of_iso=str(p.get("as_of") or ""), include_due_soon_days=int(p.get("due_soon_days") or 14))
    if sub == "upload_asset":
        return ingest_wiki_asset(wk, filename=str(p.get("filename") or "diagram.png"), data_base64=str(p.get("data_base64") or ""), alt_text=str(p.get("alt_text") or ""))
    if sub == "stage_turn":
        raw_mid = p.get("message_id")
        mid = int(raw_mid) if isinstance(raw_mid, (int, str)) and str(raw_mid).isdigit() else None
        return stage_chat_turn_to_inbox(
            wk,
            assistant_content=str(p.get("content") or ""),
            user_prompt=str(p.get("user_prompt") or ""),
            title=str(p.get("title") or ""),
            actor=actor,
            message_id=mid,
        )
    if sub in ("propose_graduation", "ai_graduate_preview"):
        from companion.wiki_curation import propose_ai_graduation

        res = propose_ai_graduation(wk, str(p.get("path") or ""))
        prop = res.get("proposed") if isinstance(res.get("proposed"), dict) else {}
        if res.get("ok") and prop.get("rendered_markdown") and res.get("inbox_path"):
            cmp_path = str(prop.get("merge_target") or res["inbox_path"])
            cmp_full = wk.root / cmp_path
            old_raw = cmp_full.read_text(encoding="utf-8", errors="replace") if cmp_full.is_file() else ""
            old_lbl = f"{cmp_path} (existing)" if prop.get("merge_target") and cmp_full.is_file() else f"{res['inbox_path']} (inbox)"
            df = _build_diff_file(str(prop["path"]), old_lbl, f"{prop['path']} (curated)", old_raw, str(prop["rendered_markdown"]))
            res["diff"] = {"ok": True, "mode": "graduation_preview", "files": [df], "total_additions": df["additions"], "total_deletions": df["deletions"], "unified_diff": df["unified_diff"], "summary": f"+{df['additions']} / -{df['deletions']} lines"}
        return res
    return None
