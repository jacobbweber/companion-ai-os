"""
Governed Wiki Templates, Rich Frontmatter Schema & Taxonomy Authority
(`companion/wiki_templates.py` — A-Frame Domain Layer).

Implements the strict structural foundation of Companion Knowledge Wiki 3.0:
1. 11 Built-In Non-Education Templates seeded into `03_resources/templates/`.
2. Living Taxonomy & Canonical Ontology Registry (`03_resources/tag-authority.md`).
3. Pure-stdlib YAML frontmatter parser + deterministic ordered serializer.
4. Graduation structural & frontmatter validators (`Option A`: flexible `01_inbox/` staging,
   ironclad `02_knowledge/` template sections, zero leftover placeholders, domain-aware anti-orphan).
"""

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

__all__ = [
    "BUILTIN_TEMPLATES", "BUILTIN_TEMPLATE_MAP", "CANONICAL_FRONTMATTER_ORDER", "TemplateSpec",
    "as_slug_list", "canonicalize_ontology", "compute_body_metrics", "default_review_days", "extract_wikilinks",
    "get_template", "hydrate_note_meta", "list_templates", "load_tag_authority", "make_note_uid", "parse_frontmatter",
    "recommend_templates", "register_authority_terms", "render_frontmatter", "save_template",
    "scrub_conversational_chatter", "seed_templates_and_authority", "sha16_bytes", "slugify_wiki", "validate_curated_note",
]

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
    "archive_reason", "curation_tx_id",
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
- `work-ops` — Sanitized work procedures, architecture notes, and external document proxies
- `general` — Cross-cutting reference notes and miscellaneous facts

## Approved Tags
- `vmware`, `esxi`, `vsphere`, `cisco-ucs`, `proxmox`, `docker`, `gpu`, `nvidia`, `gpu-node`, `networking`, `vlan`, `ipam`, `firewall`, `storage`, `ups`
- `ansible`, `playbook`, `linux`, `windows`, `winrm`, `ssh`, `systemd`, `backup`
- `lawn-care`, `mower`, `small-engine`, `oil-change`, `seasonal-service`, `parts-manual`
- `nutrition`, `weight-loss`, `metabolism`, `training`, `recovery`, `adr`, `runbook`, `comparison`, `moc`, `worklog`, `pdf-proxy`, `attachment`

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


def hydrate_note_meta(
    base_meta: Dict[str, Any], title: str, body: str, author: str, status: str,
    tpl_slug: str, domain: str, topic: str, tags: List[str], ontology: List[str],
    now_iso: str, summary: str = "",
) -> Dict[str, Any]:
    tpl_spec, metrics = BUILTIN_TEMPLATE_MAP.get(tpl_slug), compute_body_metrics(body)
    eff_ont = ontology or (list(tpl_spec.default_ontology) if tpl_spec and tpl_slug != "inbox-raw" else [])
    raw_sum = summary.strip() or str(base_meta.get("summary") or "").strip()
    if not raw_sum:
        prose = [ln.strip() for ln in (body or "").splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "|", "```", "---"))]
        joined = re.sub(r"\s+", " ", " ".join(prose)).strip()
        raw_sum = joined[:240] if len(joined) >= 24 else f"Reference knowledge note covering {title} ({len(body or '')} chars)."
    created_iso, raw_rev = str(base_meta.get("created_at") or now_iso), base_meta.get("review_after_days")
    rev_days = int(raw_rev) if str(raw_rev or "").isdigit() and int(raw_rev) > 0 else default_review_days(tpl_slug)
    return {
        **base_meta, "uid": str(base_meta.get("uid") or make_note_uid("wk")), "title": title[:140], "summary": raw_sum[:260],
        "author": author, "status": status, "template": tpl_slug, "domain": domain, "topic": topic, "tags": tags, "ontology": eff_ont,
        "document_type": tpl_spec.document_type if tpl_spec else ("inbox-staging" if tpl_slug == "inbox-raw" else str(base_meta.get("document_type") or "reference")),
        "related": [f"[[{r}]]" for r in extract_wikilinks(body)], "word_count": metrics["word_count"], "context_tokens": metrics["context_tokens"],
        "created_at": created_iso, "updated_at": now_iso, "last_verified_at": str(base_meta.get("last_verified_at") or created_iso), "review_after_days": rev_days,
    }


def sha16_bytes(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()[:16]


def make_note_uid(prefix: str = "wk") -> str:
    return f"{slugify_wiki(prefix, 'wk')}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{os.urandom(3).hex()}"


def extract_wikilinks(text: str) -> List[str]:
    return list(dict.fromkeys(tgt for m in WIKILINK_RE.finditer(text or "") if (tgt := m.group(1).strip().replace("\\", "/").removesuffix(".md"))))


def _parse_scalar(val: str) -> Any:
    clean = val.strip()
    if (clean.startswith('"') and clean.endswith('"')) or (clean.startswith("'") and clean.endswith("'")):
        return clean[1:-1]
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


def parse_frontmatter(markdown_text: str) -> Tuple[Dict[str, Any], str]:
    """Parse rich YAML frontmatter (scalars, inline `[...]` lists, block `- item` lists) without mutating text."""
    norm = (markdown_text or "").replace("\r\n", "\n")
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
    needs_q = any(ch in s for ch in (":", "#", "[", "]", "{", "}", ",")) or s.lower() in ("true", "false", "null")
    return '"' + s.replace('"', '\\"') + '"' if needs_q else s


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
    return {"word_count": len(re.findall(r"\S+", clean)), "context_tokens": max(1, (len(clean) + 3) // 4) if clean else 0, "code_blocks": len(re.findall(r"^```", clean, re.MULTILINE)) // 2, "heading_count": len(_HEADING_RE.findall(clean))}


def _render_template_file(spec: TemplateSpec) -> str:
    return render_frontmatter({"template_slug": spec.slug, "title": spec.title, "document_type": spec.document_type, "when_to_use": spec.when_to_use, "required_sections": list(spec.required_sections), "default_ontology": list(spec.default_ontology)}, spec.body_skeleton)


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
        items.append({"slug": slug, "title": str(meta.get("title") or slug), "document_type": str(meta.get("document_type") or "reference"), "when_to_use": str(meta.get("when_to_use") or ""), "required_sections": list(req) if isinstance(req, list) else [], "default_ontology": list(ont) if isinstance(ont, list) else [], "skeleton": skeleton.strip(), "path": f"03_resources/templates/{p.name}", "builtin": slug in BUILTIN_TEMPLATE_MAP})
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
    spec = TemplateSpec(clean_slug, (title or clean_slug).strip(), slugify_wiki(document_type, "reference"), (when_to_use or "Custom domain template.").strip(), tuple(secs), ("reference-knowledge",), (body_skeleton or f"# <Title>\n\n## {secs[0]}\n").strip())
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


def recommend_templates(title: str, body: str, source: str = "") -> List[Dict[str, Any]]:
    hay = f"{title} {source} {body[:2500]}".lower()
    rules = (
        (("pdf", "attachment", "datasheet", "scanned", "03_resources/"), "proxy-asset-note"),
        (("vlan", "cidr", "subnet", "ipam", "/24", "firewall"), "homelab-network-matrix"),
        (("cpu", "ram", "ipmi", "cimc", "idrac", "chassis", "blade server", "esxi", "ucs", "hypervisor", "proxmox"), "homelab-host-spec"),
        (("step 1", "prerequisites", "runbook", "procedure", "playbook", "maintenance", "oil", "torque", "rollback", "sop"), "sop-runbook"),
        (("options considered", "decision", "adr", "consequences"), "adr-decision"),
        (("vs ", "versus", "comparison", "| :---", "tradeoff"), "concept-comparison"),
        (("worklog", "weekly", "completed work", "sprint"), "weekly-worklog"),
        (("map of content", "system hub", "domain overview", "moc"), "concept-map-system-hub"),
        (("raw data", "actionable wisdom", "dikw"), "dikw-pyramid-of-insight"),
    )
    scored = sorted(
        [(sum(1 for k in keys if k in hay), slug) for keys, slug in rules],
        key=lambda x: x[0], reverse=True,
    )
    picks = [slug for hits, slug in scored if hits > 0] + ["zettelkasten-atomic", "standard-note"]
    seen: List[Dict[str, Any]] = []
    for slug in picks:
        if slug in BUILTIN_TEMPLATE_MAP and all(x["slug"] != slug for x in seen):
            sp = BUILTIN_TEMPLATE_MAP[slug]
            seen.append({"slug": sp.slug, "title": sp.title, "document_type": sp.document_type, "when_to_use": sp.when_to_use, "required_sections": list(sp.required_sections)})
        if len(seen) >= 3:
            break
    return seen


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
            if not any(target_norm in h or h in target_norm for h in headings_norm if h):
                errors.append(f"Missing required section `## {req_sec}` mandated by template `{tpl_slug}`.")

    rel_meta = meta.get("related") if isinstance(meta.get("related"), list) else []
    all_links = extract_wikilinks(clean_body + "\n" + " ".join(str(x) for x in rel_meta))
    if (existing_domain_notes > 0 or split_sibling_count > 1) and not all_links:
        errors.append("ANTI-ORPHAN GUARD: Domain already contains curated notes (or multi-note split), so at least 1 `[[wikilink]]` is required.")
    return errors
