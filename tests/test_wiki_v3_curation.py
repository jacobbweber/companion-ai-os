"""Comprehensive unit & integration tests for Wiki 3.0 Templates, Multi-Turn Curation, Ontology Bridges & Lineage."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.approvals import set_trust_mode
from companion.jobs import marshal_job_envelope
from companion.core_actions import execute_core_action, handle_harvest_slash
from companion.memory_store import MemoryStore
from companion.routines import BUILTIN_IDLE_ROUTINES, RoutineManager
from companion.tools import check_phase_tool_permission
from companion.wiki_curation import pending_curation_nudge
from companion.wiki_engine import WikiEngine
from companion.wiki_templates import list_templates, load_tag_authority, parse_frontmatter, render_frontmatter


class TestWikiV3TemplatesAndCuration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(db_path=self.root / "wiki_v3.db")
        self.addCleanup(self.store.close)
        self.wiki = WikiEngine(wiki_dir=self.root / "wiki", store=self.store)
        self.wiki_patch = patch("companion.core_actions.get_wiki_engine", return_value=self.wiki)
        self.wiki_patch.start()
        self.addCleanup(self.wiki_patch.stop)

    def _seed_companion_note(
        self, domain: str, slug: str, title: str, summary: str, template: str,
        tags: list[str], ontology: list[str], body: str,
    ) -> dict:
        cap = self.wiki.capture_inbox(
            title=title, content=body, tags=tags, slug=f"seed-{slug}", actor="companion",
            template=template, summary=summary, ontology=ontology,
        )
        self.assertTrue(cap["ok"], cap)
        grad = self.wiki.graduate_note(
            cap["path"], domain=domain, new_slug=slug, actor="user",
            template=template, summary=summary, ontology=ontology,
        )
        self.assertTrue(grad["ok"], grad)
        return grad

    def test_eleven_templates_authority_and_yaml_roundtrip(self) -> None:
        """Verify 11 non-education templates seed cleanly, system files stay unindexed, and YAML block lists round-trip."""
        self.wiki.ensure_warehouse()
        tpls = list_templates(self.wiki.root)
        slugs = {t["slug"] for t in tpls}
        expected = {
            "zettelkasten-atomic", "adr-decision", "sop-runbook", "concept-map-system-hub",
            "concept-comparison", "dikw-pyramid-of-insight", "weekly-worklog", "standard-note",
            "homelab-host-spec", "homelab-network-matrix", "proxy-asset-note",
        }
        self.assertEqual(slugs, expected)
        self.assertTrue(all("feynman" not in s and "quiz" not in s for s in slugs))

        auth = load_tag_authority(self.wiki.root)
        self.assertIn("datacenter-compute", auth["ontology"])
        self.assertIn("preventive-maintenance", auth["ontology"])
        self.assertIn("homelab", auth["domains"])
        self.assertIn("equipment-ops", auth["domains"])

        # System template files must not pollute note listings or graph
        self.assertEqual(self.wiki.list_notes(), [])
        self.assertEqual(self.wiki.export_mindmap_graph()["stats"]["note_count"], 0)

        # Custom template registration via CoreActions
        custom = execute_core_action(
            "wiki",
            {
                "subaction": "save_template", "slug": "lab-checklist", "title": "Lab Checklist",
                "document_type": "procedure", "when_to_use": "Pre-flight hardware checks.",
                "required_sections": ["Checks", "Signoff"], "content": "## Checks\n\n## Signoff\n",
            },
            actor="user", store=self.store,
        )
        self.assertTrue(custom["ok"])
        self.assertEqual(len(list_templates(self.wiki.root)), 12)

        # YAML block-list & inline-array round-trip
        raw_md = (
            "---\ntitle: Test YAML\nsummary: Clean YAML parser test note for Wiki 3.0.\ntags:\n  - vmware\n  - esxi\n"
            "ontology: [datacenter-compute, virtualization-fabric]\nsupersedes:\n  - 04_archive/old.md\n---\n\n## Overview\nBody\n"
        )
        meta, body = parse_frontmatter(raw_md)
        self.assertEqual(meta["tags"], ["vmware", "esxi"])
        self.assertEqual(meta["ontology"], ["datacenter-compute", "virtualization-fabric"])
        self.assertEqual(meta["supersedes"], ["04_archive/old.md"])
        reparsed_meta, reparsed_body = parse_frontmatter(render_frontmatter(meta, body))
        self.assertEqual(reparsed_meta["tags"], ["vmware", "esxi"])
        self.assertEqual(reparsed_meta["ontology"], ["datacenter-compute", "virtualization-fabric"])
        self.assertIn("## Overview", reparsed_body)

    def test_option_a_staging_strict_schema_and_domain_anti_orphan(self) -> None:
        """Verify Option A fast inbox staging vs. strict 02_knowledge validation and Domain-Aware Anti-Orphan rule."""
        human_in = self.wiki.capture_inbox("Quick Lawn Note", "Buy SAE 10W-30 oil and spark plug.", tags=["lawnmower"], actor="user")
        self.assertTrue(human_in["ok"])
        self.assertEqual(human_in["template"], "inbox-raw")

        comp_in = self.wiki.capture_inbox(
            "Honda HRX217 Blade Maintenance Runbook",
            "Step 1: Disconnect spark plug wire before tilting carburetor side up.",
            tags=["lawnmower", "honda"], ontology=["preventive-maintenance"], actor="companion",
        )
        self.assertTrue(comp_in["ok"])
        self.assertEqual(comp_in["template"], "sop-runbook")

        bad_grad = self.wiki.graduate_note(human_in["path"], domain="equipment-ops", actor="user")
        self.assertFalse(bad_grad["ok"])
        self.assertIn("Missing governed `template`", bad_grad["error"])

        bad_save = self.wiki.save_or_update_note(
            "02_knowledge/equipment-ops/honda-hrx217.md",
            "## Prerequisites\n${FILL_ME}\n\n## Procedure\n1. Do work across the mower deck carefully.\n",
            title="Honda HRX217 SOP", summary="Seasonal blade and oil maintenance procedure for Honda HRX217.",
            template="sop-runbook", tags=["lawnmower"], ontology=["preventive-maintenance"], actor="user",
        )
        self.assertFalse(bad_save["ok"])
        self.assertIn("Missing required section", bad_save["error"])
        self.assertIn("Unfilled template placeholder", bad_save["error"])

        # Seed note #1 in brand-new domain `equipment-ops` succeeds with ZERO wikilinks!
        seed_body = (
            "## Prerequisites\nSocket wrench, torque wrench, Honda 10W-30 oil, new NGK BPR5ES plug.\n\n"
            "## Procedure\n1. Disconnect spark plug cap.\n2. Drain warm crankcase oil and torque blade bolts to 40 ft-lb.\n\n"
            "## Verification\nConfirm oil dipstick sits at upper crosshatch and mower starts on first pull.\n\n"
            "## Rollback & Troubleshooting\nIf white smoke appears, verify mower was tilted spark-plug-side up.\n"
        )
        seed_ok = self.wiki.save_or_update_note(
            "02_knowledge/equipment-ops/honda-hrx217-maintenance.md", seed_body,
            title="Honda HRX217 Seasonal Maintenance", summary="Complete oil, spark plug, and twin-blade torque procedure for Honda HRX217.",
            template="sop-runbook", tags=["lawnmower", "honda"], ontology=["preventive-maintenance", "equipment-lifecycle"], actor="user",
        )
        self.assertTrue(seed_ok["ok"], seed_ok)

        # Note #2 in `equipment-ops` WITHOUT [[wikilink]] is blocked by Domain-Aware Anti-Orphan Rule!
        orphan_second = self.wiki.save_or_update_note(
            "02_knowledge/equipment-ops/stihl-trimmer-fuel.md",
            "## Core Idea\nUse ethanol-free 50:1 premix fuel to prevent carburetor diaphragm stiffening.\n\n"
            "## Mechanism & Context\nEthanol phase-separates in small 2-stroke tanks within 30 days.\n\n"
            "## Connections\nSmall engine seasonal storage.\n",
            title="Stihl 2-Stroke Fuel Rule", summary="Why ethanol-free 50:1 premix prevents 2-stroke carburetor failure.",
            template="zettelkasten-atomic", tags=["trimmer", "fuel"], ontology=["preventive-maintenance"], actor="user",
        )
        self.assertFalse(orphan_second["ok"])
        self.assertIn("ANTI-ORPHAN GUARD", orphan_second["error"])

        # Adding [[honda-hrx217-maintenance]] satisfies Anti-Orphan Rule!
        linked_second = self.wiki.save_or_update_note(
            "02_knowledge/equipment-ops/stihl-trimmer-fuel.md",
            "## Core Idea\nUse ethanol-free 50:1 premix fuel to prevent carburetor stiffening.\n\n"
            "## Mechanism & Context\nEthanol phase-separates in small 2-stroke tanks within 30 days.\n\n"
            "## Connections\nPair with [[honda-hrx217-maintenance]] during spring yard prep.\n",
            title="Stihl 2-Stroke Fuel Rule", summary="Why ethanol-free 50:1 premix prevents 2-stroke carburetor failure.",
            template="zettelkasten-atomic", tags=["trimmer", "fuel"], ontology=["preventive-maintenance"], actor="user",
        )
        self.assertTrue(linked_second["ok"], linked_second)

    def test_ontology_two_hop_indirect_search_and_cross_domain_graph_bridges(self) -> None:
        """Verify searching `vmware` surfaces `cisco-ucs` via shared `ontology` even with zero lexical overlap."""
        esxi_body = (
            "## Hardware & Compute Profile\nDell R740 running ESXi 8.0U2 with dual Xeon Gold CPUs and 256 GB ECC RAM.\n\n"
            "## Network & Management\nDual 25GbE SFP28 vSwitch uplinks, iDRAC9 Enterprise, and NVMe datastore.\n\n"
            "## Services & Workloads\nvCenter appliance and core lab VMs.\n\n"
            "## Operations & Notes\nPatch quarterly via Lifecycle Manager.\n"
        )
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/homelab/esxi-cluster-01.md", esxi_body,
            title="VMware ESXi Cluster 01", summary="Primary VMware ESXi 8.0 hypervisor host specification.",
            template="homelab-host-spec", tags=["vmware", "esxi", "vsphere"],
            ontology=["datacenter-compute", "virtualization-fabric"], actor="user",
        )["ok"])

        # Cisco UCS note does NOT contain the string "vmware" or "esxi" anywhere in title/body/tags!
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/homelab/san-storage-hub.md",
            "## Overview\nShared iSCSI NVMe SAN storage array serving [[esxi-cluster-01]].\n\n"
            "## Key Details\nMultipath 25GbE storage network.\n",
            title="Lab SAN Storage Array", summary="Shared enterprise NVMe storage array for homelab compute hosts.",
            template="standard-note", tags=["storage", "san"], ontology=["infrastructure-hardware"], actor="user",
        )["ok"])
        ucs_body = (
            "## Hardware & Compute Profile\nCisco UCS B200 M5 blade server inside UCS 5108 chassis managed by Fabric Interconnect 6454.\n\n"
            "## Network & Management\nVIC 1440 converged 40GbE vNIC/vHBA service profile templates.\n\n"
            "## Services & Workloads\nStateless enterprise compute blades booting from [[san-storage-hub]].\n\n"
            "## Operations & Notes\nVerify UCS Manager firmware bundle compatibility before FI failover.\n"
        )
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/homelab/cisco-ucs-b200.md", ucs_body,
            title="Cisco UCS B200 M5 Blade Spec", summary="Stateless Cisco UCS B200 M5 blade hardware and Fabric Interconnect profile.",
            template="homelab-host-spec", tags=["cisco-ucs", "blade-server"],
            ontology=["datacenter-compute", "virtualization-fabric"], actor="user",
        )["ok"])

        # Cross-domain bridge: Lawnmower SOP (`equipment-ops`) <-> Homelab UPS SOP (`homelab`) via `preventive-maintenance`
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/equipment-ops/mower-winterize.md",
            "## Prerequisites\nFuel stabilizer and wrench.\n\n## Procedure\nRun stabilizer through carburetor for 5 minutes.\n\n"
            "## Verification\nConfirm float bowl is dry.\n\n## Rollback & Troubleshooting\nClean main carburetor jet.\n",
            title="Lawnmower Winterization SOP", summary="Annual winterization procedure for walk-behind lawnmower.",
            template="sop-runbook", tags=["lawnmower"], ontology=["preventive-maintenance"], actor="user",
        )["ok"])
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/homelab/ups-battery-test.md",
            "## Prerequisites\nAPC Smart-UPS 1500 feeding [[esxi-cluster-01]].\n\n## Procedure\nRun self-test runtime calibration.\n\n"
            "## Verification\nCheck battery impedance reading.\n\n## Rollback & Troubleshooting\nHot-swap RBC battery cartridge.\n",
            title="Homelab UPS Battery Calibration SOP", summary="Semi-annual battery health test for homelab rack UPS.",
            template="sop-runbook", tags=["ups", "power"], ontology=["preventive-maintenance"], actor="user",
        )["ok"])

        hits = self.wiki.search_notes("vmware", limit=10, include_ontology_bridges=True)
        hit_map = {h["rel_path"]: h for h in hits}
        self.assertIn("02_knowledge/homelab/esxi-cluster-01.md", hit_map)
        self.assertEqual(hit_map["02_knowledge/homelab/esxi-cluster-01.md"]["match_kind"], "direct")
        self.assertIn("02_knowledge/homelab/cisco-ucs-b200.md", hit_map)
        self.assertEqual(hit_map["02_knowledge/homelab/cisco-ucs-b200.md"]["match_kind"], "ontology_bridge")
        self.assertIn("datacenter-compute", hit_map["02_knowledge/homelab/cisco-ucs-b200.md"]["shared_ontology"])

        graph = self.wiki.export_mindmap_graph()
        self.assertGreaterEqual(graph["stats"]["ontology_edge_count"], 2)
        ont_pairs = {tuple(sorted((e["source"], e["target"]))) for e in graph["ontology_edges"]}
        self.assertIn(("02_knowledge/equipment-ops/mower-winterize.md", "02_knowledge/homelab/ups-battery-test.md"), ont_pairs)
        self.assertIn(("02_knowledge/homelab/cisco-ucs-b200.md", "02_knowledge/homelab/esxi-cluster-01.md"), ont_pairs)

    def test_multi_turn_merge_guards_read_gate_lazy_dump_and_summary_loss(self) -> None:
        """Verify inspect_graduation, Mandatory Full-Read Gate, zero-mutation read, Anti-Lazy-Dump, and Anti-Summary-Loss."""
        orig_body = (
            "## Hardware & Compute Profile\nSupermicro SYS-5019 running Proxmox VE 8.1 with Xeon E-2288G and 64 GB ECC RAM.\n\n"
            "```bash\npvecm status\nzpool status rpool\n```\n\n"
            "## Network & Management\nMirrored 2TB enterprise NVMe ZFS rpool, IPMI LAN, and dual Intel X550 10GbE NICs.\n\n"
            "## Services & Workloads\nHosts OPNsense router VM, DNS resolver, and monitoring stack.\n\n"
            "## Operations & Notes\nRun ZFS scrub on the first Sunday of every month and verify SMART wearout.\n"
        )
        # Direct Companion save to brand-new 02_knowledge/ path is blocked by One-Door Inbox Guard
        blocked = self.wiki.save_or_update_note(
            "02_knowledge/homelab/proxmox-node-01.md", orig_body,
            title="Proxmox Node 01 Specification", summary="Hardware, ZFS pool, and management spec for Proxmox Node 01.",
            template="homelab-host-spec", tags=["proxmox", "zfs", "homelab"], ontology=["datacenter-compute"], actor="companion",
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("ONE-DOOR INBOX GUARD", blocked["error"])

        self._seed_companion_note(
            "homelab", "proxmox-node-01", "Proxmox Node 01 Specification",
            "Hardware, ZFS pool, and management spec for Proxmox Node 01.",
            "homelab-host-spec", ["proxmox", "zfs", "homelab"], ["datacenter-compute"], orig_body,
        )

        in_res = self.wiki.capture_inbox(
            "Proxmox Node 01 Specification RAM & TPU Update",
            "Upgraded Proxmox Node 01 Supermicro SYS-5019 to 128 GB ECC RAM and passed PCIe Coral TPU to Frigate.",
            tags=["proxmox", "zfs", "homelab"], ontology=["datacenter-compute"], actor="companion",
        )
        audit = execute_core_action("wiki", {"subaction": "inspect_graduation", "path": in_res["path"]}, actor="companion", store=self.store)
        self.assertTrue(audit["ok"])
        cid = audit["curation_id"]
        self.assertIn("02_knowledge/homelab/proxmox-node-01.md", audit["required_reads"])

        # 1. Skipping read_note MUST fail validate_curation!
        no_read = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": cid, "strategy": "MERGE_AND_REFINE",
                "outputs": [{"path": "02_knowledge/homelab/proxmox-node-01.md", "title": "Proxmox Node 01 Specification", "summary": "Hardware, ZFS pool, and Coral TPU spec for Proxmox Node 01.", "template": "homelab-host-spec", "tags": ["proxmox"], "ontology": ["datacenter-compute"], "content": orig_body}],
            },
            actor="companion", store=self.store,
        )
        self.assertFalse(no_read["ok"])
        self.assertIn("MANDATORY FULL-READ GATE", no_read["error"])

        # 2. Reading note satisfies gate with ZERO disk mutation and preserves clean `note_summary`
        kb_file = self.wiki.root / "02_knowledge/homelab/proxmox-node-01.md"
        before_bytes = kb_file.read_bytes()
        rd = execute_core_action("wiki", {"subaction": "read", "path": "02_knowledge/homelab/proxmox-node-01.md"}, actor="companion", store=self.store)
        self.assertTrue(rd["ok"])
        self.assertEqual(rd["note_summary"], "Hardware, ZFS pool, and management spec for Proxmox Node 01.")
        self.assertEqual(kb_file.read_bytes(), before_bytes)

        # 3. Lazy bottom-dump (`## Appended Update`) MUST be rejected!
        lazy_res = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": cid, "strategy": "MERGE_AND_REFINE",
                "outputs": [{"path": "02_knowledge/homelab/proxmox-node-01.md", "title": "Proxmox Node 01 Specification", "summary": "Hardware, ZFS pool, and Coral TPU spec for Proxmox Node 01.", "template": "homelab-host-spec", "tags": ["proxmox"], "ontology": ["datacenter-compute"], "content": orig_body + "\n## Appended Update\nAdded 128 GB RAM.\n"}],
            },
            actor="companion", store=self.store,
        )
        self.assertFalse(lazy_res["ok"])
        self.assertIn("Lazy bottom-dump heading detected", lazy_res["error"])

        # 4. Dropping code blocks / >25% words (`ANTI-LOSS GUARD`) MUST be rejected!
        lossy_body = (
            "## Hardware & Compute Profile\n128 GB RAM.\n\n## Network & Management\nNVMe.\n\n"
            "## Services & Workloads\nOPNsense.\n\n## Operations & Notes\nScrub monthly.\n"
        )
        lossy_res = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": cid, "strategy": "MERGE_AND_REFINE",
                "outputs": [{"path": "02_knowledge/homelab/proxmox-node-01.md", "title": "Proxmox Node 01 Specification", "summary": "Hardware, ZFS pool, and Coral TPU spec for Proxmox Node 01.", "template": "homelab-host-spec", "tags": ["proxmox"], "ontology": ["datacenter-compute"], "content": lossy_body}],
            },
            actor="companion", store=self.store,
        )
        self.assertFalse(lossy_res["ok"])
        self.assertIn("ANTI-LOSS GUARD", lossy_res["error"])

    def test_merge_commit_link_repointing_and_one_click_revert(self) -> None:
        """Verify clean MERGE_AND_REFINE commit, inbound wikilink repointing, and 1-click revert_curation."""
        set_trust_mode("autonomous", actor="user", store=self.store)
        orig_body = (
            "## Hardware & Compute Profile\nSupermicro SYS-5019 running Proxmox VE 8.1 with Xeon E-2288G and 64 GB ECC RAM.\n\n"
            "```bash\npvecm status\nzpool status rpool\n```\n\n"
            "## Network & Management\nMirrored 2TB enterprise NVMe ZFS rpool, IPMI LAN, and dual Intel X550 10GbE NICs.\n\n"
            "## Services & Workloads\nHosts OPNsense router VM, DNS resolver, and monitoring stack.\n\n"
            "## Operations & Notes\nRun ZFS scrub on the first Sunday of every month and verify SMART wearout.\n"
        )
        self._seed_companion_note(
            "homelab", "proxmox-node-01", "Proxmox Node 01 Specification",
            "Hardware, ZFS pool, and management spec for Proxmox Node 01.",
            "homelab-host-spec", ["proxmox", "zfs", "homelab"], ["datacenter-compute"], orig_body,
        )
        in_res = self.wiki.capture_inbox(
            "Proxmox Node 01 RAM & Coral TPU Upgrade",
            "Upgraded Proxmox Node 01 to 128 GB ECC RAM and passed PCIe Coral M.2 TPU through to Frigate NVR VM.",
            tags=["proxmox", "homelab"], ontology=["datacenter-compute"], slug="proxmox-tpu-upgrade", actor="companion",
        )
        self._seed_companion_note(
            "homelab", "nvr-overview", "Homelab NVR Overview",
            "Camera NVR service hosted on Proxmox Node 01.",
            "standard-note", ["frigate", "homelab"], ["datacenter-compute"],
            "## Overview\nFrigate NVR runs on [[proxmox-tpu-upgrade]] hardware.\n\n"
            "## Key Details\nUses PCIe Coral M.2 TPU acceleration on [[proxmox-node-01]].\n",
        )

        audit = execute_core_action("wiki", {"subaction": "inspect_graduation", "path": in_res["path"]}, actor="companion", store=self.store)
        cid = audit["curation_id"]
        for req_p in audit["required_reads"]:
            execute_core_action("wiki", {"subaction": "read", "path": req_p}, actor="companion", store=self.store)

        merged_body = (
            "## Hardware & Compute Profile\nSupermicro SYS-5019 running Proxmox VE 8.1 with Xeon E-2288G, 128 GB ECC RAM (upgraded from 64 GB), and PCIe Coral M.2 TPU.\n\n"
            "```bash\npvecm status\nzpool status rpool\n```\n\n"
            "## Network & Management\nMirrored 2TB enterprise NVMe ZFS rpool, IPMI LAN, and dual Intel X550 10GbE NICs.\n\n"
            "## Services & Workloads\nHosts OPNsense router VM, DNS resolver, monitoring stack, and [[nvr-overview]] Frigate NVR with Coral TPU passthrough.\n\n"
            "## Operations & Notes\nRun ZFS scrub on the first Sunday of every month and verify SMART wearout and TPU apex0 device permissions.\n"
        )
        val_ok = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": cid, "strategy": "MERGE_AND_REFINE",
                "outputs": [{"path": "02_knowledge/homelab/proxmox-node-01.md", "title": "Proxmox Node 01 Specification", "summary": "Hardware, 128GB RAM, Coral TPU, and ZFS spec for Proxmox Node 01.", "template": "homelab-host-spec", "tags": ["proxmox", "zfs", "tpu"], "ontology": ["datacenter-compute"], "content": merged_body}],
            },
            actor="companion", store=self.store,
        )
        self.assertTrue(val_ok["ok"], val_ok)

        commit_res = execute_core_action("wiki", {"subaction": "commit_curation", "curation_id": cid}, actor="companion", store=self.store)
        self.assertTrue(commit_res["ok"], commit_res)
        tx_id = commit_res["tx_id"]
        self.assertFalse((self.wiki.root / in_res["path"]).exists())
        self.assertEqual(len(commit_res["inbox_archives"]), 1)
        self.assertEqual(len(commit_res["knowledge_archives"]), 1)

        nvr_after = self.wiki.read_note("02_knowledge/homelab/nvr-overview.md")
        self.assertIn("[[proxmox-node-01]]", nvr_after["body"])
        self.assertNotIn("[[proxmox-tpu-upgrade]]", nvr_after["body"])

        rev_res = execute_core_action("wiki", {"subaction": "revert_curation", "tx_id": tx_id}, actor="user", store=self.store)
        self.assertTrue(rev_res["ok"], rev_res)
        self.assertTrue((self.wiki.root / in_res["path"]).is_file())
        restored_node = self.wiki.read_note("02_knowledge/homelab/proxmox-node-01.md")
        self.assertIn("64 GB ECC RAM", restored_node["body"])
        self.assertNotIn("128 GB ECC RAM", restored_node["body"])

    def test_split_multi_and_balanced_approval_queue_auto_apply(self) -> None:
        """Verify SPLIT_MULTI cross-linking and balanced Trust Mode ApprovalQueue auto-execution."""
        set_trust_mode("balanced", actor="user", store=self.store)
        dump = self.wiki.capture_inbox(
            "Ansible Vault Decision and Playbook Runbook",
            "We decided to standardize on Ansible Vault with password-file client scripts, plus the step-by-step playbook execution runbook.",
            tags=["ansible", "automation"], ontology=["configuration-automation"], actor="companion",
        )
        cid = execute_core_action("wiki", {"subaction": "inspect_graduation", "path": dump["path"]}, actor="companion", store=self.store)["curation_id"]

        outputs = [
            {
                "path": "02_knowledge/automation/adr-ansible-vault.md",
                "title": "ADR: Standardize on Ansible Vault",
                "summary": "Decision to encrypt role secrets with Ansible Vault while keeping keys out of Git.",
                "template": "adr-decision", "tags": ["ansible", "security"], "ontology": ["configuration-automation"],
                "content": (
                    "## Context & Problem\nPlaybooks need encrypted host variables.\n\n"
                    "## Options Considered\n1. Ansible Vault.\n2. SOPS + age.\n\n"
                    "## Decision\nAdopt Ansible Vault paired with [[ansible-playbook-sop]].\n\n"
                    "## Consequences\nZero plaintext secrets in Git.\n"
                ),
            },
            {
                "path": "02_knowledge/automation/ansible-playbook-sop.md",
                "title": "Ansible Playbook Execution SOP",
                "summary": "Step-by-step procedure for linting, dry-running, and applying Ansible roles.",
                "template": "sop-runbook", "tags": ["ansible", "devops"], "ontology": ["configuration-automation"],
                "content": (
                    "## Prerequisites\nConfigure vault client per [[adr-ansible-vault]].\n\n"
                    "## Procedure\nRun `ansible-playbook site.yml --check --diff` before apply.\n\n"
                    "## Verification\nConfirm `failed=0` across all hosts.\n\n"
                    "## Rollback & Troubleshooting\nRe-run previous Git tag inventory.\n"
                ),
            },
        ]
        val = execute_core_action(
            "wiki",
            {"subaction": "validate_curation", "curation_id": cid, "strategy": "SPLIT_MULTI", "outputs_json": json.dumps(outputs)},
            actor="companion", store=self.store,
        )
        self.assertTrue(val["ok"], val)

        queued = execute_core_action("wiki", {"subaction": "commit_curation", "curation_id": cid}, actor="companion", store=self.store)
        self.assertFalse(queued["ok"])
        self.assertTrue(queued["awaiting_approval"])

        approved = execute_core_action(
            "approvals", {"subaction": "approve", "approval_id": queued["approval_id"]}, actor="user", store=self.store
        )
        self.assertTrue(approved["ok"])
        self.assertTrue((self.wiki.root / "02_knowledge/automation/adr-ansible-vault.md").is_file())
        self.assertTrue((self.wiki.root / "02_knowledge/automation/ansible-playbook-sop.md").is_file())

    def test_user_author_protection_proxy_notes_phase_gates_and_routines(self) -> None:
        """Verify user-author protection even in autonomous mode, proxy asset notes, phase gates, hobbies, and routines."""
        set_trust_mode("autonomous", actor="user", store=self.store)
        self.assertTrue(self.wiki.save_or_update_note(
            "02_knowledge/health-performance/protein-intake-target.md",
            "## Core Idea\nTarget 0.8g to 1.0g protein per lb of goal lean body mass during caloric deficit.\n\n"
            "## Mechanism & Context\nPreserves lean muscle mass and increases satiety thermogenesis.\n\n"
            "## Connections\nResistance training retention.\n",
            title="Protein Intake Target During Cut", summary="Daily protein target for lean mass preservation during weight loss.",
            template="zettelkasten-atomic", tags=["nutrition", "weight-loss"], ontology=["metabolic-health"], actor="user",
        )["ok"])
        in_u = self.wiki.capture_inbox("Leucine Threshold Note", "Add 3g leucine per meal note to protein target.", tags=["nutrition"], actor="companion")
        insp_u = execute_core_action("wiki", {"subaction": "inspect_graduation", "path": "01_inbox"}, actor="companion", store=self.store)
        self.assertIn("CURATION CONTINUATION GATE", pending_curation_nudge(self.store, "2020-01-01T00:00:00Z", "I'll draft and validate the curation.") or "")
        execute_core_action("wiki", {"subaction": "read", "path": "02_knowledge/health-performance/protein-intake-target.md"}, actor="companion", store=self.store)
        merged_u = (
            "## Core Idea\nTarget 0.8g to 1.0g protein per lb of goal lean body mass (hitting ~3g leucine per meal) during caloric deficit.\n\n"
            "## Mechanism & Context\nPreserves lean muscle mass, stimulates mTOR via leucine threshold, and increases satiety thermogenesis.\n\n"
            "## Connections\nResistance training retention.\n"
        )
        val_u = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": insp_u["curation_id"], "strategy": "MERGE_AND_REFINE",
                "outputs": [{"path": "02_knowledge/health-performance/protein-intake-target.md", "title": "Protein Intake Target During Cut", "summary": "Daily protein and leucine threshold target during weight loss.", "template": "zettelkasten-atomic", "tags": ["nutrition", "weight-loss"], "ontology": ["metabolic-health"], "content": merged_u}],
            },
            actor="companion", store=self.store,
        )
        self.assertTrue(val_u["ok"], val_u)
        self.assertIn("commit_curation", pending_curation_nudge(self.store, "2020-01-01T00:00:00Z", "Validated!") or "")
        commit_u = execute_core_action("wiki", {"subaction": "commit_curation", "curation_id": insp_u["curation_id"]}, actor="companion", store=self.store)
        self.assertFalse(commit_u["ok"])
        self.assertTrue(commit_u["awaiting_approval"])
        self.assertIsNone(pending_curation_nudge(self.store, "2020-01-01T00:00:00Z", "Awaiting user approval."))

        proxy = execute_core_action(
            "wiki",
            {
                "subaction": "create_proxy_note", "asset_path": "03_resources/manuals/q2-architecture-deck.pdf",
                "title": "Q2 UCS Refresh Deck Proxy", "summary": "Searchable companion proxy note for binary work PDF deck.",
                "highlights": "- Replaces M4 blades with M6.\n- Moves vMotion to 25GbE VLAN 24.",
                "mime_type": "application/pdf", "size_label": "4.2 MB", "tags": ["work-proxy", "cisco-ucs"],
                "ontology": ["datacenter-compute", "asset-provenance"],
            },
            actor="companion", store=self.store,
        )
        self.assertTrue(proxy["ok"], proxy)
        self.assertEqual(proxy["template"], "proxy-asset-note")
        self.assertTrue(any(h["rel_path"] == proxy["path"] for h in self.wiki.search_notes("M6 blades")))

        self.assertIsNone(check_phase_tool_permission("wiki_manage", "DISCOVER", {"action": "inspect_graduation"}))
        self.assertIsNone(check_phase_tool_permission("wiki_manage", "PLAN", {"action": "validate_curation"}))
        self.assertIsNotNone(check_phase_tool_permission("wiki_manage", "PLAN", {"action": "commit_curation"}))

        self.assertTrue(any(r["routine_id"] == "wiki_gardener" for r in BUILTIN_IDLE_ROUTINES))
        rm = RoutineManager(store=self.store)
        r_save = rm.create_or_update_routine("nightly-wiki-curate", "daily 02:00", "wiki_deep_curate", actor="user")
        self.assertTrue(r_save["ok"])
        self.assertIn("Governed Wiki Templates", handle_harvest_slash("/wiki", "templates", store=self.store))
        self.assertIn("Wiki Curation Lineage", handle_harvest_slash("/wiki", "history", store=self.store))
        env_txt, overflow_rel = marshal_job_envelope("bgj_cur", "Deep Wiki Curation", "x" * 1200, store=self.store, skip_inbox_overflow=True)
        self.assertEqual(overflow_rel, "")
        self.assertIn("bgj_cur", env_txt)

    def test_keep_separate_rationale_and_domain_hub_moc_nudge(self) -> None:
        """Verify KEEP_SEPARATE requires explicit rationale and >=3 domain notes trigger concept-map-system-hub MOC nudge."""
        set_trust_mode("autonomous", actor="user", store=self.store)
        for idx in range(1, 4):
            link_line = f"Related to [[host-01]]." if idx > 1 else "Primary lab seed host."
            self.assertTrue(self.wiki.save_or_update_note(
                f"02_knowledge/homelab/host-0{idx}.md",
                f"## Overview\nHomelab node {idx}. {link_line}\n\n## Key Details\n25GbE rack server {idx}.\n",
                title=f"Homelab Host 0{idx}", summary=f"Specification overview for homelab rack host 0{idx}.",
                template="standard-note", tags=["homelab"], ontology=["datacenter-compute"], actor="user",
            )["ok"])

        sep_in = self.wiki.capture_inbox("Homelab Host 04 Lab Sandbox", "Isolated nested lab host.", tags=["homelab"], actor="companion")
        audit = execute_core_action("wiki", {"subaction": "inspect_graduation", "path": sep_in["path"]}, actor="companion", store=self.store)
        self.assertTrue(any("concept-map-system-hub" in h for h in audit["hub_recommendations"]))
        for req_p in audit["required_reads"]:
            execute_core_action("wiki", {"subaction": "read", "path": req_p}, actor="companion", store=self.store)

        out_spec = [{
            "path": "02_knowledge/homelab/host-04.md", "title": "Homelab Host 04 Sandbox",
            "summary": "Isolated nested lab sandbox host kept distinct from production rack nodes.",
            "template": "standard-note", "tags": ["homelab"], "ontology": ["datacenter-compute"],
            "content": "## Overview\nIsolated nested sandbox peering with [[host-01]].\n\n## Key Details\nEphemeral testbed.\n",
        }]
        no_rat = execute_core_action(
            "wiki", {"subaction": "validate_curation", "curation_id": audit["curation_id"], "strategy": "KEEP_SEPARATE", "outputs": out_spec},
            actor="companion", store=self.store,
        )
        self.assertFalse(no_rat["ok"])
        self.assertIn("rationale", no_rat["error"])

        ok_rat = execute_core_action(
            "wiki",
            {
                "subaction": "validate_curation", "curation_id": audit["curation_id"], "strategy": "KEEP_SEPARATE",
                "rationale": "Host 04 is an ephemeral nested testbed with distinct lifecycle from production nodes.", "outputs": out_spec,
            },
            actor="companion", store=self.store,
        )
        self.assertTrue(ok_rat["ok"], ok_rat)
        self.assertTrue(execute_core_action("wiki", {"subaction": "commit_curation", "curation_id": audit["curation_id"]}, actor="companion", store=self.store)["ok"])

    def test_ai_librarian_graduation_strips_inbox_and_it_bias_on_recipes(self) -> None:
        from companion.wiki_curation import propose_ai_graduation
        from companion.wiki_templates import save_template

        save_template(self.wiki.root, "cooking-recipe", "Cooking Recipe", "reference", "Culinary dish recipes.", ["ingredients", "details", "instructions"], "# <Title>\n\n## ingredients\n\n## details\n\n## instructions\n")
        cap = self.wiki.capture_inbox(
            "Test: Chicken Stir Fry (Granular Details)",
            "## ingredients\n- 1 lb chicken thighs\n- 2 tbsp vegetable oil\n\n## details\n- Prep: 15m\n\n## instructions\n1. Stir-fry on high heat.\n",
            slug="cooking-recipe", tags=["inbox"], ontology=["operational-runbook", "preventive-maintenance"], actor="companion",
        )
        self.assertEqual(cap["template"], "cooking-recipe")
        rd = self.wiki.read_note(cap["path"])
        self.assertEqual(rd["domain"], "")
        bad_inbox_dom = self.wiki.graduate_note(cap["path"], domain="01-inbox", actor="user")
        self.assertFalse(bad_inbox_dom["ok"])

        fake_llm = lambda *_a, **_kw: {"choices": [{"message": {"role": "assistant", "content": '{"domain":"cooking","topic":"stir-fry","slug":"chicken-stir-fry","title":"Chicken Stir Fry","summary":"High-heat weeknight chicken thigh and bell pepper stir-fry ready in 27 minutes.","template":"cooking-recipe","tags":["cooking","recipe","stir-fry","chicken"],"ontology":["culinary-recipe","meal-prep"],"related":[],"rationale":"Grouped under cooking/stir-fry with cooking-recipe template."}'}}]}
        prop = propose_ai_graduation(self.wiki, cap["path"], chat_fn=fake_llm)
        self.assertTrue(prop["ok"], prop)
        self.assertEqual(prop["proposed"]["path"], "02_knowledge/cooking/stir-fry/chicken-stir-fry.md")
        self.assertNotIn("inbox", prop["proposed"]["tags"])
        self.assertNotIn("preventive-maintenance", prop["proposed"]["ontology"])
        committed = execute_core_action("wiki", {"subaction": "commit_curation", "curation_id": prop["curation_id"], "confirmed": True}, actor="web", store=self.store)
        self.assertTrue(committed["ok"], committed)
        final_note = self.wiki.read_note("02_knowledge/cooking/stir-fry/chicken-stir-fry.md")
        self.assertTrue(final_note["ok"])
        self.assertEqual(final_note["domain"], "cooking")
        self.assertEqual(final_note["topic"], "stir-fry")


if __name__ == "__main__":
    unittest.main()
