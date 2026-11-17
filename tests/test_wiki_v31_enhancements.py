"""End-to-end tests for Wiki 3.1 Enhancements (`companion/wiki_maintenance.py` & CoreActions)."""

import base64
import struct
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.archive_and_approvals import set_trust_mode
from companion.core_actions import execute_core_action, handle_harvest_slash
from companion.memory_store import MemoryStore, memory_store_context
from companion.wiki_engine import get_wiki_engine, parse_frontmatter, render_frontmatter
from surfaces.web_server import create_web_server, dispatch_raw_http


def _make_png(width: int = 24, height: int = 16) -> bytes:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = b"".join(b"\x00" + (b"\x18\x90\xd0" * width) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw_rows)) + _chunk(b"IEND", b"")


class TestWikiV31Enhancements(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = MemoryStore(db_path=self.base / "db" / "conversations.db")
        self.wk = get_wiki_engine(wiki_dir=self.base / "db" / "wiki", store=self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_ontology_alias_canonicalization_and_consolidation(self) -> None:
        with memory_store_context(self.store):
            set_trust_mode("autonomous", actor="user", store=self.store)
            cap = self.wk.capture_inbox(
                title="Cisco UCS Blade C240 Spec",
                content=(
                    "# Cisco UCS Blade C240 Spec\n\n## Hardware & Compute Profile\nDual Xeon Gold, 512GB ECC.\n\n"
                    "## Network & Management\nCIMC 10.10.20.15.\n\n## Services & Workloads\nESXi cluster compute.\n\n"
                    "## Operations & Notes\nRedundant PSUs."
                ),
                tags=["cisco-ucs", "blade"],
                ontology=["server-compute", "compute-nodes"],
                template="homelab-host-spec",
                actor="companion",
            )
            self.assertTrue(cap["ok"])
            rd = self.wk.read_note(cap["path"])
            self.assertEqual(rd["ontology"], ["datacenter-compute"])

            # Graduate UCS note, then verify synonym query still bridges across domains
            grad = self.wk.graduate_note(
                cap["path"], domain="homelab", new_slug="ucs-c240", actor="user",
                template="homelab-host-spec", summary="Cisco UCS C240 M5 rack/blade compute host specification.",
            )
            self.assertTrue(grad["ok"], grad)
            bridges = self.wk.find_ontology_bridges([], ["cluster-compute"], [], limit=5)
            self.assertEqual([b["rel_path"] for b in bridges], ["02_knowledge/homelab/ucs-c240.md"])

            # Simulate legacy manual note on disk with raw unmapped synonyms + near-duplicate slug
            legacy_p = self.wk.root / "02_knowledge" / "equipment-ops" / "honda-mower.md"
            legacy_p.parent.mkdir(parents=True, exist_ok=True)
            legacy_meta = {
                "uid": "wk-leg-1", "title": "Honda HRX217 Service", "summary": "Seasonal oil and blade maintenance SOP for Honda HRX217.",
                "author": "companion", "status": "active", "template": "sop-runbook", "document_type": "procedure",
                "domain": "equipment-ops", "tags": ["mower"], "ontology": ["small-engine-maintenance", "custom-blade-care", "virtualization-fabrics"],
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            }
            legacy_p.write_text(render_frontmatter(legacy_meta, "# Honda HRX217 Service\n\n## Prerequisites\n10W-30 oil.\n\n## Procedure\nDrain and refill.\n\n## Verification\nCheck dipstick.\n\n## Rollback & Troubleshooting\nWipe spill.\n"), encoding="utf-8")
            self.wk.rebuild_index()

            dry = execute_core_action("wiki", {"subaction": "consolidate_ontology", "dry_run": True}, actor="user", store=self.store)
            self.assertTrue(dry["ok"])
            self.assertEqual(len(dry["updated_notes"]), 1)
            # Disk unchanged on dry_run
            disk_m, _ = parse_frontmatter(legacy_p.read_text(encoding="utf-8"))
            self.assertIn("small-engine-maintenance", disk_m["ontology"])

            # Apply consolidation with a custom alias rule -> Never-Delete archive snapshot created
            applied = execute_core_action(
                "wiki",
                {"subaction": "consolidate_ontology", "aliases": "custom-blade-care -> preventive-maintenance"},
                actor="user", store=self.store,
            )
            self.assertTrue(applied["ok"], applied)
            self.assertEqual(applied["added_aliases"], {"custom-blade-care": "preventive-maintenance"})
            upd_entry = applied["updated_notes"][0]
            self.assertEqual(upd_entry["after"], ["preventive-maintenance", "virtualization-fabric"])
            self.assertTrue((self.wk.root / upd_entry["archived_prior"]).is_file())

    def test_unified_and_split_diff_for_lineage_drafts_and_pending_approvals(self) -> None:
        with memory_store_context(self.store):
            set_trust_mode("strict", actor="user", store=self.store)
            # Create initial user-authored note in 02_knowledge/
            save1 = self.wk.save_or_update_note(
                "02_knowledge/homelab/esxi-01.md",
                "# ESXi Host 01\n\n## Hardware & Compute Profile\n64GB ECC RAM.\n\n## Network & Management\n192.168.1.50\n\n## Services & Workloads\nvCenter.\n\n## Operations & Notes\nBIOS 2.1.",
                title="ESXi Host 01", tags=["vmware", "esxi"], actor="user", template="homelab-host-spec",
                summary="Primary VMware ESXi 8.0 hypervisor host profile in homelab.",
            )
            self.assertTrue(save1["ok"], save1)

            # User updates RAM -> creates 04_archive/ prior revision
            save2 = self.wk.save_or_update_note(
                "02_knowledge/homelab/esxi-01.md",
                "# ESXi Host 01\n\n## Hardware & Compute Profile\n128GB ECC RAM + NVMe tier.\n\n## Network & Management\n192.168.1.50\n\n## Services & Workloads\nvCenter + DNS.\n\n## Operations & Notes\nBIOS 2.4.",
                title="ESXi Host 01", tags=["vmware", "esxi"], actor="user", template="homelab-host-spec",
                summary="Primary VMware ESXi 8.0 hypervisor host profile in homelab.",
            )
            self.assertTrue(save2["ok"], save2)

            lineage_diff = execute_core_action("wiki", {"subaction": "diff", "path": "02_knowledge/homelab/esxi-01.md"}, actor="user", store=self.store)
            self.assertTrue(lineage_diff["ok"], lineage_diff)
            self.assertEqual(lineage_diff["mode"], "lineage_diff")
            self.assertGreater(lineage_diff["total_additions"], 0)
            self.assertIn("128GB ECC RAM", lineage_diff["unified_diff"])
            self.assertTrue(any(r["kind"] in ("change", "add") for r in lineage_diff["files"][0]["rows"]))

            # Companion attempts to edit user-authored note -> triggers awaiting_approval -> preview diff by approval_id!
            blocked = self.wk.save_or_update_note(
                "02_knowledge/homelab/esxi-01.md",
                "# ESXi Host 01\n\n## Hardware & Compute Profile\n256GB ECC RAM + Dual NVMe.\n\n## Network & Management\n192.168.1.50\n\n## Services & Workloads\nvCenter + DNS.\n\n## Operations & Notes\nBIOS 2.5.",
                actor="companion", template="homelab-host-spec",
                summary="Updated VMware ESXi 8.0 host profile with 256GB RAM upgrade.",
            )
            self.assertTrue(blocked.get("awaiting_approval"))
            apr_diff = execute_core_action("wiki", {"subaction": "diff", "approval_id": blocked["approval_id"]}, actor="user", store=self.store)
            self.assertTrue(apr_diff["ok"], apr_diff)
            self.assertEqual(apr_diff["mode"], "approval_preview")
            self.assertIn("256GB ECC RAM", apr_diff["unified_diff"])

    def test_staleness_cadences_list_stale_and_verify_note(self) -> None:
        with memory_store_context(self.store):
            old_iso = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # 1) 90-day cadence homelab-host-spec -> STALE at 120 days
            spec_body = f"---\nlast_verified_at: {old_iso}\ncreated_at: {old_iso}\n---\n\n# Proxmox Node\n\n## Hardware & Compute Profile\nRyzen 9.\n\n## Network & Management\n10.0.0.11\n\n## Services & Workloads\nLXC.\n\n## Operations & Notes\nUPS A."
            r1 = self.wk.save_or_update_note("02_knowledge/homelab/pve-01.md", spec_body, title="Proxmox Node", tags=["proxmox"], actor="user", template="homelab-host-spec", summary="Proxmox VE compute node hardware and network specification.")
            self.assertTrue(r1["ok"], r1)

            # 2) 365-day cadence zettelkasten-atomic -> NOT stale at 120 days
            atom_body = f"---\nlast_verified_at: {old_iso}\ncreated_at: {old_iso}\n---\n\n# Caloric Deficit Principle\n\n## Core Idea\nNet energy balance governs mass change.\n\n## Mechanism & Context\nThermodynamic conservation across intake and expenditure.\n\n## Connections\nSeed note."
            r2 = self.wk.save_or_update_note("02_knowledge/health-performance/caloric-deficit.md", atom_body, title="Caloric Deficit Principle", tags=["nutrition"], actor="user", template="zettelkasten-atomic", summary="Thermodynamic energy balance principle governing weight loss.")
            self.assertTrue(r2["ok"], r2)

            stale_res = execute_core_action("wiki", {"subaction": "list_stale"}, actor="user", store=self.store)
            self.assertTrue(stale_res["ok"])
            stale_paths = [s["rel_path"] for s in stale_res["stale_notes"]]
            self.assertEqual(stale_paths, ["02_knowledge/homelab/pve-01.md"])
            self.assertEqual(stale_res["stale_notes"][0]["review_after_days"], 90)

            # Companion cannot stamp verification on user-authored note
            comp_ver = execute_core_action("wiki", {"subaction": "verify_note", "path": "02_knowledge/homelab/pve-01.md"}, actor="companion", store=self.store)
            self.assertFalse(comp_ver["ok"])

            # User 1-click verification clears staleness immediately
            usr_ver = execute_core_action("wiki", {"subaction": "verify_note", "path": "02_knowledge/homelab/pve-01.md"}, actor="user", store=self.store)
            self.assertTrue(usr_ver["ok"], usr_ver)
            self.assertFalse(usr_ver["stale"])
            stale_after = execute_core_action("wiki", {"subaction": "list_stale"}, actor="user", store=self.store)
            self.assertEqual(stale_after["stale_notes"], [])
            self.assertIn("0 stale", handle_harvest_slash("/wiki", "stale", store=self.store))

    def test_inline_wiki_image_asset_upload_security_and_http_route(self) -> None:
        with memory_store_context(self.store):
            png_b64 = "data:image/png;base64," + base64.b64encode(_make_png(32, 20)).decode("ascii")
            up = execute_core_action(
                "wiki",
                {"subaction": "upload_asset", "filename": "Rack Topology Diagram.png", "data_base64": png_b64, "alt_text": "Rack Topology"},
                actor="web", store=self.store,
            )
            self.assertTrue(up["ok"], up)
            self.assertTrue(up["asset_rel_path"].startswith("03_resources/assets/"))
            self.assertEqual(up["markdown"], f"![Rack Topology]({up['asset_rel_path']})")
            self.assertEqual((up["width"], up["height"]), (32, 20))
            # Binary image assets never pollute Markdown note index
            self.assertEqual(self.wk.list_notes(), [])

            # Reject fake non-image bytes
            bad = execute_core_action("wiki", {"subaction": "upload_asset", "filename": "evil.png", "data_base64": base64.b64encode(b"not-an-image").decode("ascii")}, actor="web", store=self.store)
            self.assertFalse(bad["ok"])

            # Verify HTTP GET /api/wiki/assets/<name> + path traversal rejection
            srv, _sess = create_web_server(host="127.0.0.1", port=0, store=self.store)
            try:
                status, _raw_body = dispatch_raw_http(srv, "GET", up["raw_url"])
                self.assertEqual(status, 200)

                st_bad, _ = dispatch_raw_http(srv, "GET", "/api/wiki/assets/..%2Ftag-authority.md")
                self.assertEqual(st_bad, 404)
            finally:
                srv.server_close()

    def test_stage_chat_turn_to_inbox_distillation(self) -> None:
        with memory_store_context(self.store):
            staged = execute_core_action(
                "wiki",
                {
                    "subaction": "stage_turn",
                    "user_prompt": "Give me the Ansible playbook procedure for patching Ubuntu nodes.",
                    "content": "Sure! Here is the procedure:\n\n## Prerequisites\nSSH key in Blind Vault.\n\n## Procedure\n1. Run `ansible-playbook patch.yml`.",
                },
                actor="web", store=self.store,
            )
            self.assertTrue(staged["ok"], staged)
            self.assertTrue(staged["path"].startswith("01_inbox/"))
            self.assertEqual(staged["template"], "sop-runbook")
            rd = self.wk.read_note(staged["path"])
            self.assertNotIn("Sure! Here is", rd["body"])
            self.assertIn("Origin Prompt", rd["body"])
            self.assertIn("preventive-maintenance", rd["ontology"])

    def test_yaml_frontmatter_search_and_directory_tree(self) -> None:
        with memory_store_context(self.store):
            n1 = self.wk.save_or_update_note(
                "02_knowledge/homelab/proxmox-nvme.md",
                (
                    "---\n"
                    "title: Proxmox NVMe Node\n"
                    "summary: ZFS NVMe mirror configuration for cluster node pve-01.\n"
                    "template: homelab-host-spec\n"
                    "tags: [proxmox, nvme, zfs]\n"
                    "ontology: [datacenter-compute, virtualization-fabric]\n"
                    "rack_unit: u14\n"
                    "---\n\n"
                    "# Proxmox NVMe Node\n\n"
                    "## Hardware & Compute Profile\nDual NVMe ZFS pool.\n\n"
                    "## Network & Management\n10GbE trunk.\n\n"
                    "## Services & Workloads\nKVM guests.\n\n"
                    "## Operations & Notes\nLinked to [[vlan-matrix]].\n"
                ),
                actor="user",
                confirmed=True,
            )
            self.assertTrue(n1["ok"], n1)

            n2 = self.wk.save_or_update_note(
                "02_knowledge/software-dev/dag-patterns.md",
                (
                    "---\n"
                    "title: Deterministic DAG Patterns\n"
                    "summary: Code-controlled workflow graphs.\n"
                    "template: zettelkasten-atomic\n"
                    "tags: [dags, python]\n"
                    "ontology: [configuration-automation]\n"
                    "---\n\n"
                    "# Deterministic DAG Patterns\n\n"
                    "## Core Idea\nAcyclic execution.\n\n"
                    "## Mechanism & Context\nTopological sort.\n\n"
                    "## Connections\nLinked to [[proxmox-nvme]].\n"
                ),
                actor="user",
                confirmed=True,
            )
            self.assertTrue(n2["ok"], n2)

            # Directory tree returns all 4 canonical warehouse roots + nested folders + frontmatter facets
            tree_res = execute_core_action("wiki", {"subaction": "tree"}, actor="web", store=self.store)
            self.assertTrue(tree_res["ok"], tree_res)
            root_names = [r["rel_path"] for r in tree_res["tree"]]
            self.assertEqual(root_names, ["01_inbox", "02_knowledge", "03_resources", "04_archive"])
            know_node = next(r for r in tree_res["tree"] if r["rel_path"] == "02_knowledge")
            self.assertEqual(know_node["file_count"], 2)
            sub_domains = [c["name"] for c in know_node["children"] if c["kind"] == "folder"]
            self.assertEqual(sub_domains, ["homelab", "software-dev"])
            self.assertIn("rack_unit", tree_res["facets"]["keys"])

            # Structured YAML frontmatter queries (standard + custom YAML keys + #tag shorthand)
            h_tpl = execute_core_action("wiki", {"subaction": "search", "query": "template:homelab-host-spec"}, actor="web", store=self.store)
            self.assertEqual([h["rel_path"] for h in h_tpl["hits"]], ["02_knowledge/homelab/proxmox-nvme.md"])

            h_custom = execute_core_action("wiki", {"subaction": "search", "query": "rack_unit:u14 #zfs"}, actor="web", store=self.store)
            self.assertEqual([h["rel_path"] for h in h_custom["hits"]], ["02_knowledge/homelab/proxmox-nvme.md"])
            self.assertEqual(h_custom["hits"][0]["match_kind"], "frontmatter")

            h_has = execute_core_action("wiki", {"subaction": "search", "query": "has:rack_unit"}, actor="web", store=self.store)
            self.assertEqual([h["rel_path"] for h in h_has["hits"]], ["02_knowledge/homelab/proxmox-nvme.md"])

            h_combo = execute_core_action("wiki", {"subaction": "search", "query": "domain:software-dev Topological"}, actor="web", store=self.store)
            self.assertEqual([h["rel_path"] for h in h_combo["hits"]], ["02_knowledge/software-dev/dag-patterns.md"])


if __name__ == "__main__":
    unittest.main()
