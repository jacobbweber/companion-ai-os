"""End-to-end tests for Forever Chat Multimodal Vision & Document Attachments (`companion/attachments.py`)."""

import base64
import io
import json
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from companion.attachments import ingest_attachment, prepare_turn_attachments
from companion.agent import classify_turn_intent, select_turn_step_tier
from companion.llm import _sanitize_wire_messages, current_tier_name
from companion.memory_store import MemoryStore
from companion.streaming import wire_attachment_context
from companion.vault import get_vault_manager
from companion.wiki_engine import get_wiki_engine
from surfaces.web_server import create_web_server, dispatch_raw_http


def _make_valid_png(width: int = 16, height: int = 12) -> bytes:
    """Construct a valid minimal RGB PNG image in pure Python stdlib."""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = b"".join(b"\x00" + (b"\x20\x80\xe0" * width) for _ in range(height))
    idat = zlib.compress(raw_rows)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _make_valid_docx(paragraph_text: str, table_rows: list[list[str]]) -> bytes:
    """Construct a valid `.docx` archive with a paragraph and table in pure stdlib."""
    tr_xml = ""
    for row in table_rows:
        tcs = "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
        tr_xml += f"<w:tr>{tcs}</w:tr>"
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{paragraph_text}</w:t></w:r></w:p><w:tbl>{tr_xml}</w:tbl></w:body>"
        "</w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def _make_valid_xlsx() -> bytes:
    """Construct a minimal `.xlsx` workbook with sharedStrings and sheet1."""
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>Host</t></si><si><t>VRAM_GB</t></si><si><t>GPU Node A</t></si>"
        "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>128</v></c></row>'
        "</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


def _make_text_pdf(label: str) -> bytes:
    """Build a valid single-page PDF containing `label` in a PDF text stream."""
    stream_body = f"BT /F1 14 Tf 36 120 Td ({label}) Tj ET".encode("latin-1")
    objs = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n",
        f"4 0 obj << /Length {len(stream_body)} >>\nstream\n".encode("latin-1") + stream_body + b"\nendstream\nendobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objs:
        offsets.append(len(out))
        out.extend(obj)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("latin-1"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1"))
    return bytes(out)


class TestMultimodalChatAttachments(unittest.TestCase):
    """Verify image vision wire routing, document extraction, Vault scrubbing, and Wiki overflow."""

    def test_image_upload_vision_routing_and_clean_history_after_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            store = MemoryStore(db_path=tmp_root / "data" / "companion.db")
            captured_wires: list[tuple[str, list[dict]]] = []

            def fake_chat_fn(messages: list[dict], *_args: object, **_kwargs: object) -> dict:
                tier = current_tier_name() or ""
                captured_wires.append((tier, _sanitize_wire_messages(messages)))
                return {"choices": [{"message": {"role": "assistant", "content": "Image shows 16x12 RGB grid."}}]}

            srv, sess = create_web_server(host="127.0.0.1", port=0, store=store, chat_fn=fake_chat_fn)
            try:
                png_bytes = _make_valid_png(16, 12)
                b64_png = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

                # Reject missing CSRF when Origin is present
                c_bad, _ = dispatch_raw_http(
                    srv, "POST", "/api/attachments",
                    body=json.dumps({"filename": "grid.png", "data_base64": b64_png}).encode("utf-8"),
                    headers={"Origin": "http://127.0.0.1"},
                )
                self.assertEqual(c_bad, 403)

                # Valid upload
                c_ok, up_raw = dispatch_raw_http(
                    srv, "POST", "/api/attachments",
                    body=json.dumps({"filename": "grid.png", "data_base64": b64_png}).encode("utf-8"),
                    headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(c_ok, 200)
                att = json.loads(up_raw)["attachment"]
                self.assertEqual(att["kind"], "image")
                self.assertEqual((att["width"], att["height"]), (16, 12))

                # Raw image GET endpoint
                c_raw, _ = dispatch_raw_http(srv, "GET", att["raw_url"])
                self.assertEqual(c_raw, 200)

                # Even if user profile tier is 'fast', image turn routes to 'reasoning' with OpenAI image_url wire block
                sess.update_settings({"tier": "fast"})
                c_turn, turn_raw = dispatch_raw_http(
                    srv, "POST", "/api/chat",
                    body=json.dumps({"message": "Describe this screenshot", "attachment_ids": [att["id"]]}).encode("utf-8"),
                    headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(c_turn, 200)
                self.assertTrue(json.loads(turn_raw)["ok"])
                self.assertEqual(len(captured_wires), 1)
                used_tier, wire_msgs = captured_wires[0]
                self.assertIn(used_tier, ("reasoning", "image"))
                last_user = [m for m in wire_msgs if m.get("role") == "user"][-1]
                self.assertIsInstance(last_user["content"], list)
                self.assertEqual(last_user["content"][1]["type"], "image_url")
                self.assertTrue(last_user["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

                # Subsequent text-only turn must NOT carry base64 image_url arrays in history!
                c_next, _ = dispatch_raw_http(
                    srv, "POST", "/api/chat",
                    body=json.dumps({"message": "Thanks! Quick follow-up."}).encode("utf-8"),
                    headers={"Origin": "http://127.0.0.1", "X-Companion-CSRF": sess.csrf_token},
                )
                self.assertEqual(c_next, 200)
                next_tier, next_wire = captured_wires[-1]
                self.assertEqual(next_tier, "fast")
                self.assertTrue(all(isinstance(m["content"], str) for m in next_wire if m.get("role") == "user"))
            finally:
                srv.server_close()
                store.close()

    def test_pdf_docx_xlsx_csv_vault_scrubbing_and_large_wiki_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            store = MemoryStore(db_path=tmp_root / "data" / "companion.db")
            try:
                vault = get_vault_manager(store=store)
                vault.set_secret("TEST_API_TOKEN", "sk-super-secret-attachment-998877", cred_type="token")

                # 1. PDF extraction
                pdf_raw = _make_text_pdf("QUARTERLY REVENUE 42M")
                r_pdf = ingest_attachment("q3_report.pdf", base64.b64encode(pdf_raw).decode("ascii"), store=store)
                self.assertTrue(r_pdf["ok"], r_pdf)
                prep_pdf = prepare_turn_attachments("Check revenue", [r_pdf["attachment"]["id"]], store=store)
                self.assertIn("QUARTERLY REVENUE 42M", prep_pdf.wire_doc_context)

                # 2. DOCX paragraph + table extraction (including full 84-char Chrome FileReader OpenXML data: URL prefix!)
                docx_raw = _make_valid_docx("Executive Summary Line", [["Node", "Status"], ["NodeA", "Online"]])
                docx_data_url = (
                    "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
                    + base64.b64encode(docx_raw).decode("ascii")
                )
                r_docx = ingest_attachment("nodes.docx", docx_data_url, store=store)
                self.assertTrue(r_docx["ok"])
                prep_docx = prepare_turn_attachments("", [r_docx["attachment"]["id"]], store=store)
                self.assertIn("Executive Summary Line", prep_docx.wire_doc_context)
                self.assertIn("| NodeA | Online |", prep_docx.wire_doc_context)

                # 3. XLSX table extraction
                xlsx_raw = _make_valid_xlsx()
                r_xlsx = ingest_attachment("fleet.xlsx", base64.b64encode(xlsx_raw).decode("ascii"), store=store)
                self.assertTrue(r_xlsx["ok"])
                prep_xlsx = prepare_turn_attachments("Read sheet", [r_xlsx["attachment"]["id"]], store=store)
                self.assertIn("GPU Node A", prep_xlsx.wire_doc_context)
                self.assertIn("128", prep_xlsx.wire_doc_context)

                # 4. Blind Vault secret scrubbing + >12k Wiki 03_resources auto-filing
                big_md = (
                    "# Architecture Handbook\nToken leak test: sk-super-secret-attachment-998877\n\n"
                    + ("Section detail about distributed GPU inference telemetry.\n" * 260)
                ).encode("utf-8")
                r_big = ingest_attachment("handbook.md", base64.b64encode(big_md).decode("ascii"), store=store)
                self.assertTrue(r_big["ok"])
                wpath = r_big["attachment"]["wiki_resource_path"]
                self.assertTrue(wpath.startswith("03_resources/"), f"Expected 03_resources/ path, got {wpath}")

                wiki_note = get_wiki_engine(store=store).read_note(wpath)
                self.assertTrue(wiki_note["ok"])
                self.assertNotIn("sk-super-secret-attachment-998877", wiki_note["body"])

                prep_big = prepare_turn_attachments("Summarize handbook", [r_big["attachment"]["id"]], store=store)
                self.assertIn("03_resources/", prep_big.wire_doc_context)
                self.assertNotIn("sk-super-secret-attachment-998877", prep_big.wire_doc_context)

                # Verify wire_attachment_context routing helper
                with wire_attachment_context("doc block", ("data:image/png;base64,AAA",)):
                    self.assertEqual(classify_turn_intent("Build 4 files now").mode, "CHAT_DIRECT")
                    self.assertIn(select_turn_step_tier("fast", None, 0), ("reasoning", "image"))

                # 5. Large multi-row CSV preserves exact on-disk storage_path without 80-row/12KB mutilation
                csv_lines = ["date,merchant,category,amount"] + [f"2026-11-{(i % 28) + 1:02d},Grocer_{i},groceries,{10.25 + i:.2f}" for i in range(250)]
                csv_raw = ("\n".join(csv_lines) + "\n").encode("utf-8")
                r_csv = ingest_attachment("household_november.csv", base64.b64encode(csv_raw).decode("ascii"), store=store)
                self.assertTrue(r_csv["ok"])
                csv_att = r_csv["attachment"]
                self.assertTrue(Path(csv_att["storage_path"]).is_file())
                self.assertEqual(len(Path(csv_att["storage_path"]).read_text(encoding="utf-8").splitlines()), 251)
                prep_csv = prepare_turn_attachments("Sum all 250 grocery rows", [csv_att["id"]], store=store)
                self.assertIn(csv_att["storage_path"], prep_csv.wire_doc_context)
                self.assertIn("251", prep_csv.wire_doc_context)
                self.assertIn(csv_att["storage_path"], prep_csv.followup_doc_note)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
