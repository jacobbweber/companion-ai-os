"""
Full-Fidelity Multimodal Image & Document Attachment Engine (`companion/attachments.py`).

Provides 100% Python 3.12 stdlib ingestion, validation, secret-scrubbed text extraction,
full on-disk `storage_path` + `extracted_path` handoff (zero 12KB/80-row silent mutilation),
multi-turn vision context persistence, and Co-Owned Wiki `03_resources/` filing for:
- Images (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) -> Vision routing + base64 `data:image/...` URIs + `storage_path`
- PDFs (`.pdf`) -> `companion/scripts/extract_pdf.py` (`pypdf`) subprocess + stdlib stream fallback + `storage_path`
- Office (`.docx`, `.xlsx`, `.pptx`) -> Pure stdlib `zipfile` + `xml.etree.ElementTree` + `storage_path`
- Data, Text & Code (`.csv`, `.tsv`, `.json`, `.yaml`, `.md`, `.txt`, code) -> Schema + sample rows + exact `storage_path`
"""

import base64
import csv
import hashlib
import html.parser
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from companion.config import settings
from companion.memory_store import MemoryStore, get_memory_store
from companion.vault import get_vault_manager
from companion.wiki_engine import get_wiki_engine

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB per file
MAX_UNCOMPRESSED_STREAM_BYTES = 32 * 1024 * 1024  # 32 MB zip/zlib bomb cap
INLINE_DOC_CHAR_LIMIT = 12_000
MAX_EXTRACTED_CHARS = 500_000

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_MIMES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}
VIDEO_MIMES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx"}
DATA_EXTENSIONS = {".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg"}
TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".log", ".html", ".htm"}
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".sql", ".sh", ".ps1",
    ".bat", ".c", ".cpp", ".h", ".rs", ".go", ".java", ".cs", ".rb", ".php",
}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | set(AUDIO_MIMES) | set(VIDEO_MIMES) | {".pdf"} | OFFICE_EXTENSIONS | DATA_EXTENSIONS | TEXT_EXTENSIONS | CODE_EXTENSIONS

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ATT_ID_RE = re.compile(r"\b(att_\d+_[a-f0-9]+)\b")


@dataclass(frozen=True)
class PreparedAttachmentTurn:
    """Prepared user chat message + ephemeral wire context for multimodal/document turns."""

    display_message: str
    wire_doc_context: str
    wire_image_uris: Tuple[str, ...]
    attachments: List[Dict[str, Any]]
    followup_doc_note: str = ""


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Lightweight stdlib HTML-to-text stripper ignoring `<script>` and `<style>` blocks."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        t = tag.lower()
        if t in ("script", "style", "noscript"):
            self._skip += 1
        elif t in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "noscript") and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and data.strip():
            self.parts.append(data.strip() + " ")


def _attachments_base_dir(store: MemoryStore) -> Path:
    if store.db_path.resolve() == settings.db_path.resolve():
        base = settings.effective_data_dir
    else:
        parent = store.db_path.parent
        base = parent if parent.name == "data" else (parent / "data")
    out = (base / "attachments").resolve()
    out.mkdir(parents=True, exist_ok=True)
    return out


def _ensure_attachments_schema(store: MemoryStore) -> None:
    store.ensure_schema(
        "attachments",
        lambda m: m.conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_attachments (
                attachment_id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime_type TEXT NOT NULL,
                kind TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                storage_path TEXT NOT NULL, extracted_path TEXT NOT NULL DEFAULT '',
                extracted_text TEXT NOT NULL DEFAULT '', extracted_chars INTEGER NOT NULL DEFAULT 0,
                meta_label TEXT NOT NULL DEFAULT '', wiki_resource_path TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            )"""
        )
        and None,
    )


def _read_zip_member_safe(zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_UNCOMPRESSED_STREAM_BYTES) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise ValueError(f"Archive entry `{name}` exceeds {max_bytes // (1024 * 1024)} MB uncompressed limit.")
    with zf.open(info) as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Archive entry `{name}` exceeds {max_bytes // (1024 * 1024)} MB uncompressed limit.")
    return data


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    return f"{num_bytes / 1024.0:.1f} KB" if num_bytes < 1024 * 1024 else f"{num_bytes / (1024.0 * 1024.0):.2f} MB"


def inspect_image_bytes(raw: bytes) -> Optional[Tuple[str, int, int]]:
    """Validate image magic bytes and return `(mime_type, width, height)` or `None`."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        w, h = struct.unpack(">II", raw[16:24])
        return "image/png", int(w), int(h)
    if raw.startswith((b"GIF87a", b"GIF89a")) and len(raw) >= 10:
        w, h = struct.unpack("<HH", raw[6:10])
        return "image/gif", int(w), int(h)
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" and len(raw) >= 30:
        chunk = raw[12:16]
        if chunk == "VP8X" or chunk == b"VP8X":
            w = 1 + int.from_bytes(raw[24:27], "little")
            h = 1 + int.from_bytes(raw[27:30], "little")
            return "image/webp", w, h
        return "image/webp", 0, 0
    if raw.startswith(b"\xff\xd8\xff"):
        idx = 2
        while idx + 9 < len(raw):
            if raw[idx] != 0xFF:
                idx += 1
                continue
            marker = raw[idx + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", raw[idx + 5 : idx + 9])
                return "image/jpeg", int(w), int(h)
            if marker in (0xD8, 0xD9) or idx + 4 > len(raw):
                break
            seg_len = struct.unpack(">H", raw[idx + 2 : idx + 4])[0]
            if seg_len < 2:
                break
            idx += 2 + seg_len
        return "image/jpeg", 0, 0
    return None


def _extract_pdf_stdlib_fallback(raw: bytes) -> str:
    """Minimal pure-stdlib FlateDecode PDF text token fallback when `pypdf` is unavailable."""
    out: List[str] = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, re.S):
        chunk = m.group(1)
        try:
            decomp = zlib.decompressobj()
            dec = decomp.decompress(chunk, 4 * 1024 * 1024)
        except Exception:
            dec = chunk[: 4 * 1024 * 1024]
        txt = dec.decode("latin-1", errors="ignore")
        for lit in re.findall(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)\s*Tj", txt):
            cleaned = lit.replace(r"\(", "(").replace(r"\)", ")").replace(r"\n", "\n").strip()
            if cleaned:
                out.append(cleaned)
        for arr in re.findall(r"\[(.*?)\]\s*TJ", txt, re.S):
            seg = "".join(re.findall(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)", arr)).strip()
            if seg:
                out.append(seg)
    return "\n".join(out).strip()


def _extract_pdf(pdf_path: Path, raw: bytes) -> Tuple[str, str]:
    """Extract PDF text via first-class runtime `companion/scripts/extract_pdf.py` (`uv run --script` / `pypdf`)."""
    script = (settings.root / "companion" / "scripts" / "extract_pdf.py").resolve()
    if script.is_file():
        uv_bin = shutil.which("uv")
        cmds = (
            ([uv_bin, "run", "--script", str(script), str(pdf_path)], [sys.executable, "-B", str(script), str(pdf_path)])
            if uv_bin
            else ([sys.executable, "-B", str(script), str(pdf_path)],)
        )
        for cmd in cmds:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=35)
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout.strip().splitlines()[-1])
                    if data.get("ok"):
                        pages = int(data.get("pages") or 0)
                        txt = str(data.get("text") or "").strip()
                        label = f"{pages} page{'s' if pages != 1 else ''}"
                        if not txt:
                            txt = "[Scanned or image-only PDF: no selectable text layer found. Attach page screenshots for vision OCR.]"
                        return txt, label
            except Exception:
                continue
    fb = _extract_pdf_stdlib_fallback(raw)
    return (fb or "[PDF attached — no selectable text stream extracted.]"), "PDF"


def _extract_docx(raw: bytes) -> Tuple[str, str]:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml_bytes = _read_zip_member_safe(zf, "word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find("w:body", ns)
    if body is None:
        return "", "DOCX"
    blocks: List[str] = []
    for child in list(body):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            txt = "".join(t.text or "" for t in child.findall(".//w:t", ns)).strip()
            if txt:
                blocks.append(txt)
        elif tag == "tbl":
            rows: List[List[str]] = []
            for tr in child.findall(".//w:tr", ns):
                cells = ["".join(t.text or "" for t in tc.findall(".//w:t", ns)).strip().replace("|", "\\|") for tc in tr.findall("./w:tc", ns)]
                if any(cells):
                    rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                padded = [r + [""] * (width - len(r)) for r in rows]
                md_tbl = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
                md_tbl.extend("| " + " | ".join(r) + " |" for r in padded[1:])
                blocks.append("\n".join(md_tbl))
    return "\n\n".join(blocks).strip(), f"{len(blocks)} blocks"


def _col_letters_to_idx(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _extract_xlsx(raw: bytes) -> Tuple[str, str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            s_root = ET.fromstring(_read_zip_member_safe(zf, "xl/sharedStrings.xml"))
            for si in s_root.iter():
                if si.tag.rsplit("}", 1)[-1] == "si":
                    shared.append("".join((node.text or "") for node in si.iter() if node.tag.rsplit("}", 1)[-1] == "t"))
        sheet_names = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        sections: List[str] = []
        for s_idx, s_name in enumerate(sheet_names, 1):
            s_root = ET.fromstring(_read_zip_member_safe(zf, s_name))
            rows_out: List[List[str]] = []
            total_rows = 0
            for row_el in s_root.iter():
                if row_el.tag.rsplit("}", 1)[-1] != "row":
                    continue
                vals: Dict[int, str] = {}
                for c_el in list(row_el):
                    if c_el.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    col_i = _col_letters_to_idx(c_el.attrib.get("r", "A1"))
                    c_type = c_el.attrib.get("t", "")
                    v_txt = ""
                    for sub in c_el.iter():
                        stag = sub.tag.rsplit("}", 1)[-1]
                        if stag == "v" and sub.text:
                            v_txt = shared[int(sub.text)] if c_type == "s" and sub.text.isdigit() and int(sub.text) < len(shared) else sub.text
                        elif stag == "t" and c_type == "inlineStr" and sub.text:
                            v_txt += sub.text
                    vals[col_i] = v_txt.strip().replace("|", "\\|").replace("\n", " ")
                if vals:
                    total_rows += 1
                    if len(rows_out) < 50:
                        max_c = min(max(vals) + 1, 32)
                        rows_out.append([vals.get(ci, "") for ci in range(max_c)])
            if rows_out:
                w = max(len(r) for r in rows_out)
                norm = [r + [""] * (w - len(r)) for r in rows_out]
                tbl = [f"### Sheet {s_idx} ({total_rows} total rows)", "| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
                tbl.extend("| " + " | ".join(r) + " |" for r in norm[1:])
                sections.append("\n".join(tbl))
    return "\n\n".join(sections).strip(), f"{len(sheet_names)} sheet{'s' if len(sheet_names) != 1 else ''}"


def _extract_pptx(raw: bytes) -> Tuple[str, str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        slides = sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        out: List[str] = []
        for idx, s_name in enumerate(slides, 1):
            root = ET.fromstring(_read_zip_member_safe(zf, s_name))
            runs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "t" and el.text and el.text.strip()]
            if runs:
                out.append(f"### Slide {idx}\n" + "\n".join(runs))
    return "\n\n".join(out).strip(), f"{len(slides)} slide{'s' if len(slides) != 1 else ''}"


def _extract_csv_or_text(ext: str, raw: bytes) -> Tuple[str, str, str]:
    """Extract CSV schema + sample rows (preserving 100% of raw file at `storage_path`) or structured text/code."""
    text = raw.decode("utf-8", errors="replace")
    if ext in (".csv", ".tsv"):
        delim = "\t" if ext == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        if not rows:
            return "", "data", "0 rows"
        headers = [h.strip() for h in rows[0]]
        data_rows = rows[1:]
        sample_limit = 25
        preview = [rows[0], *data_rows[:sample_limit]]
        w = max(1, min(max(len(r) for r in preview), 40))
        norm = [[c.strip().replace("|", "\\|") for c in (r[:w] + [""] * max(0, w - len(r)))] for r in preview]
        hdr_summary = f"**Columns ({len(headers)})**: " + ", ".join(f"`{h or f'col_{i+1}'}`" for i, h in enumerate(headers[:40]))
        row_summary = f"**Total Rows**: {len(rows)} ({len(data_rows)} data row(s))"
        md = [hdr_summary, row_summary, "", "| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * w) + " |"]
        md.extend("| " + " | ".join(r) + " |" for r in norm[1:])
        if len(data_rows) > sample_limit:
            md.append(
                f"\n*(Showing first {sample_limit} of {len(data_rows)} data rows in inline preview. "
                "100% of rows are preserved untouched on disk at `storage_path` — ALWAYS use `run_command(python_code=...)` "
                "with `csv.DictReader` / `sqlite3` on `storage_path` for exact totals, sums, filtering, or SQLite ingestion!)*"
            )
        return "\n".join(md), "data", f"{len(rows)} rows • {len(headers)} cols"
    if ext == ".json":
        try:
            parsed = json.loads(text)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            return f"```json\n{pretty}\n```", "data", "JSON"
        except Exception:
            return text, "data", "JSON (raw)"
    if ext in (".html", ".htm"):
        parser = _HTMLTextExtractor()
        parser.feed(text)
        cleaned = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()
        return cleaned or text, "text", "HTML"
    if ext in DATA_EXTENSIONS:
        return text, "data", ext.lstrip(".").upper()
    if ext in CODE_EXTENSIONS:
        lang = ext.lstrip(".")
        return f"```{lang}\n{text}\n```", "code", f"{len(text.splitlines())} lines"
    return text, "text", f"{len(text.splitlines())} lines"


def _serialize_row(row: Tuple[Any, ...]) -> Dict[str, Any]:
    (aid, fname, mime, kind, sz, sha, sp, ep, _etxt, echars, mlabel, wpath, w, h, cat) = row
    return {
        "id": str(aid), "filename": str(fname), "mime_type": str(mime), "kind": str(kind),
        "size_bytes": int(sz), "size_human": _format_size(int(sz)), "sha256": str(sha),
        "storage_path": str(sp), "extracted_path": str(ep),
        "extracted_chars": int(echars), "meta_label": str(mlabel), "wiki_resource_path": str(wpath),
        "width": int(w), "height": int(h), "raw_url": f"/api/attachments/{aid}/raw", "created_at": str(cat),
    }


_OFFICE_EXTRACTORS = {
    ".docx": (_extract_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": (_extract_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": (_extract_pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}


def ingest_attachment(filename: str, data_base64: str, mime_type: str = "", store: Optional[MemoryStore] = None) -> Dict[str, Any]:
    """Validate, persist, extract, scrub, and index an uploaded Forever Chat attachment."""
    mem = store or get_memory_store()
    _ensure_attachments_schema(mem)
    safe_name = _SAFE_NAME_RE.sub("_", Path((filename or "attachment").strip()).name).strip("._") or "attachment.txt"
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {"ok": False, "error": f"Unsupported file extension `{ext or '(none)'}`. Allowed: images (.png/.jpg/.webp/.gif), .pdf, .docx, .xlsx, .pptx, .md, .txt, .csv, .json, .yaml, and source code."}
    if isinstance(data_base64, (bytes, bytearray)):
        raw = bytes(data_base64)
    else:
        b64_clean = (data_base64 or "").strip()
        if "," in b64_clean[:256] and b64_clean.lower().startswith("data:"):
            b64_clean = b64_clean.split(",", 1)[1]
        if len(b64_clean) > ((MAX_ATTACHMENT_BYTES * 4) // 3) + 256:
            return {"ok": False, "error": "Attachment exceeds 20 MB limit."}
        try:
            raw = base64.b64decode(b64_clean, validate=False)
        except Exception as exc:
            return {"ok": False, "error": f"Invalid base64 attachment payload: {exc}"}
    if not raw or len(raw) > MAX_ATTACHMENT_BYTES:
        return {"ok": False, "error": "Attachment file is empty (0 bytes)." if not raw else f"Attachment exceeds 20 MB limit ({_format_size(len(raw))})."}

    now_dt, aid = datetime.now(timezone.utc), f"att_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
    month_dir = _attachments_base_dir(mem) / now_dt.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    raw_path = month_dir / f"{aid}_{safe_name}"
    width = height = 0
    extracted_text = extracted_path_str = wiki_path = ""
    if ext in IMAGE_EXTENSIONS:
        img_info = inspect_image_bytes(raw)
        if img_info is None:
            return {"ok": False, "error": f"File `{safe_name}` does not contain valid PNG/JPEG/WEBP/GIF image bytes."}
        eff_mime, width, height = img_info
        kind, meta_label = "image", (f"{width}×{height} • {_format_size(len(raw))}" if width and height else _format_size(len(raw)))
        raw_path.write_bytes(raw)
    elif ext in AUDIO_MIMES or ext in VIDEO_MIMES:
        kind = "audio" if ext in AUDIO_MIMES else "video"
        eff_mime, meta_label = AUDIO_MIMES.get(ext) or VIDEO_MIMES[ext], f"{kind.upper()} • {_format_size(len(raw))}"
        raw_path.write_bytes(raw)
    else:
        raw_path.write_bytes(raw)
        try:
            if ext == ".pdf":
                if not raw.lstrip().startswith(b"%PDF-"):
                    raw_path.unlink(missing_ok=True)
                    return {"ok": False, "error": f"File `{safe_name}` is not a valid PDF document."}
                extracted_text, sub_lbl = _extract_pdf(raw_path, raw)
                kind, eff_mime = "pdf", "application/pdf"
            elif ext in _OFFICE_EXTRACTORS:
                fn_ext, eff_mime = _OFFICE_EXTRACTORS[ext]
                extracted_text, sub_lbl = fn_ext(raw)
                kind = "office"
            else:
                extracted_text, kind, sub_lbl = _extract_csv_or_text(ext, raw)
                eff_mime = (mime_type or "text/plain; charset=utf-8").strip()
        except Exception as exc:
            raw_path.unlink(missing_ok=True)
            return {"ok": False, "error": f"Failed to parse `{safe_name}`: {type(exc).__name__}: {exc}"}

        extracted_text = get_vault_manager(store=mem).scrub_output(extracted_text[:MAX_EXTRACTED_CHARS]).strip()
        meta_label = f"{sub_lbl} • {_format_size(len(raw))}"
        ext_file = month_dir / f"{aid}.extracted.md"
        ext_file.write_text(f"# Extracted Attachment: {safe_name}\n- storage_path: `{raw_path}`\n\n{extracted_text}\n", encoding="utf-8")
        extracted_path_str = str(ext_file)
        if len(extracted_text) > INLINE_DOC_CHAR_LIMIT:
            slug = _SLUG_RE.sub("-", Path(safe_name).stem.lower()).strip("-")[:48] or "document"
            rel_wiki = f"03_resources/{now_dt.strftime('%Y-%m-%d')}-{slug}-{aid[-6:]}.md"
            proxy_body = (
                f"# {safe_name}\n\n## Asset Metadata & Location\n"
                f"- **Source**: `{safe_name}` (`{meta_label}`, id=`{aid}`)\n"
                f"- **Raw File Path (`storage_path`)**: `{raw_path}`\n"
                f"- **Extracted Text Path (`extracted_path`)**: `{ext_file}`\n\n"
                f"## Executive Summary & Key Facts\nAuto-filed Forever Chat `{kind}` attachment.\n\n## Extracted Highlights\n{extracted_text}"
            )
            w_res = get_wiki_engine(store=mem).save_or_update_note(rel_wiki, content=proxy_body, title=f"Attachment: {safe_name}", tags=["attachment", "resource", kind], actor="companion", template="proxy-asset-note", ontology=["asset-provenance", "reference-knowledge"])
            if w_res.get("ok"):
                wiki_path = str(w_res.get("path") or rel_wiki)

    sha, created_at = hashlib.sha256(raw).hexdigest()[:24], now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with mem._lock:
        mem.conn.execute(
            "INSERT INTO chat_attachments (attachment_id, filename, mime_type, kind, size_bytes, sha256, storage_path, extracted_path, extracted_text, extracted_chars, meta_label, wiki_resource_path, width, height, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, safe_name, eff_mime, kind, len(raw), sha, str(raw_path), extracted_path_str, extracted_text, len(extracted_text), meta_label, wiki_path, width, height, created_at),
        )
        mem.conn.commit()
    return {"ok": True, "attachment": get_attachment_record(aid, store=mem)}


def get_attachment_record(attachment_id: str, store: Optional[MemoryStore] = None) -> Optional[Dict[str, Any]]:
    """Fetch attachment metadata dictionary (including `storage_path` & `extracted_path`) by `attachment_id`."""
    mem = store or get_memory_store()
    _ensure_attachments_schema(mem)
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", (attachment_id or "").strip())
    if not clean_id:
        return None
    rows = mem._fetch_rows(
        """SELECT attachment_id, filename, mime_type, kind, size_bytes, sha256, storage_path,
                  extracted_path, extracted_text, extracted_chars, meta_label, wiki_resource_path, width, height, created_at
           FROM chat_attachments WHERE attachment_id = ? LIMIT 1""",
        (clean_id,),
    )
    return _serialize_row(rows[0]) if rows else None


def read_attachment_raw(attachment_id: str, store: Optional[MemoryStore] = None) -> Optional[Tuple[str, bytes, str]]:
    """Return `(mime_type, raw_bytes, safe_filename)` for `GET /api/attachments/<id>/raw`."""
    mem = store or get_memory_store()
    _ensure_attachments_schema(mem)
    clean_id = re.sub(r"[^A-Za-z0-9_-]", "", (attachment_id or "").strip())
    if not clean_id:
        return None
    rows = mem._fetch_rows("SELECT mime_type, storage_path, filename FROM chat_attachments WHERE attachment_id = ? LIMIT 1", (clean_id,))
    if not rows:
        return None
    mime, spath, fname = str(rows[0][0]), Path(str(rows[0][1])), str(rows[0][2])
    if not spath.is_file():
        return None
    safe_ct = mime if mime.startswith(("image/", "audio/", "video/")) or mime == "application/pdf" else "text/plain; charset=utf-8"
    return safe_ct, spath.read_bytes(), fname


_VISUAL_FOLLOWUP_RE = re.compile(
    r"\b(?:image|photo|picture|screenshot|receipt|diagram|chart|invoice|label|ocr|in\s+the\s+(?:pic|image|photo|receipt)|on\s+the\s+receipt)\b",
    re.I,
)


def rehydrate_recent_turn_images(
    messages: List[Dict[str, Any]], user_message: str = "", store: Optional[MemoryStore] = None, max_images: int = 2,
) -> Tuple[str, ...]:
    """Re-hydrate recent image attachments ONLY when a follow-up question explicitly references visual content."""
    if not messages or not _VISUAL_FOLLOWUP_RE.search(user_message or ""):
        return ()
    mem = store or get_memory_store()
    _ensure_attachments_schema(mem)
    found_ids: List[str] = []
    for msg in reversed(messages[-8:]):
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content") or "")
        for aid in _ATT_ID_RE.findall(content):
            if aid not in found_ids:
                found_ids.append(aid)
        if len(found_ids) >= max_images:
            break
    uris: List[str] = []
    for aid in reversed(found_ids[:max_images]):
        rows = mem._fetch_rows("SELECT kind, mime_type, storage_path FROM chat_attachments WHERE attachment_id = ? LIMIT 1", (aid,))
        if not rows:
            continue
        kind, mime, spath = str(rows[0][0]), str(rows[0][1]), Path(str(rows[0][2]))
        if kind == "image" and spath.is_file():
            uris.append(f"data:{mime};base64,{base64.b64encode(spath.read_bytes()).decode('ascii')}")
    return tuple(uris)


def prepare_turn_attachments(
    user_message: str, attachment_ids: Optional[List[str]], store: Optional[MemoryStore] = None,
) -> PreparedAttachmentTurn:
    """Assemble persistent chat badges + full-fidelity `<turn_context>` with exact `storage_path` for scripts/workflows."""
    clean_ids = [re.sub(r"[^A-Za-z0-9_-]", "", str(x)) for x in (attachment_ids or []) if str(x).strip()][:8]
    base_msg = (user_message or "").strip()
    if not clean_ids:
        return PreparedAttachmentTurn(base_msg, "", (), [])

    mem = store or get_memory_store()
    _ensure_attachments_schema(mem)
    badges: List[str] = []
    doc_blocks: List[str] = []
    image_uris: List[str] = []
    loaded_meta: List[Dict[str, Any]] = []

    for aid in clean_ids:
        rows = mem._fetch_rows(
            """SELECT attachment_id, filename, mime_type, kind, size_bytes, sha256, storage_path,
                      extracted_path, extracted_text, extracted_chars, meta_label, wiki_resource_path, width, height, created_at
               FROM chat_attachments WHERE attachment_id = ? LIMIT 1""",
            (aid,),
        )
        if not rows:
            continue
        row = rows[0]
        meta = _serialize_row(row)
        loaded_meta.append(meta)
        fname, mime, kind = meta["filename"], meta["mime_type"], meta["kind"]
        spath, ep_str, etxt = Path(str(row[6])), str(row[7]), str(row[8])
        mlabel, wpath, raw_url = meta["meta_label"], meta["wiki_resource_path"], meta["raw_url"]

        if kind in ("image", "audio", "video") and spath.is_file():
            b64 = base64.b64encode(spath.read_bytes()).decode("ascii")
            image_uris.append(f"data:{mime};base64,{b64}")
            badges.append(f"![{fname}]({raw_url})" if kind == "image" else f"[🎬 {fname} ({mlabel})]({raw_url})")
            doc_blocks.append(
                f"[ATTACHED {kind.upper()}: `{fname}` ({mlabel}) | attachment_id=`{aid}` | storage_path=`{spath}` — provided directly to your multimodal vision/media input.]"
            )
        else:
            wiki_tag = f" • Wiki: [[{wpath.removesuffix('.md')}]]" if wpath else ""
            badges.append(f"[📎 {fname} ({mlabel})]({raw_url}){wiki_tag}")
            if wpath or len(etxt) > INLINE_DOC_CHAR_LIMIT:
                preview = etxt[:6000]
                wiki_line = f"Wiki resource: `{wpath}` (`[[{wpath.removesuffix('.md')}]]`)\n" if wpath else ""
                doc_blocks.append(
                    f"[ATTACHED LARGE DOCUMENT: `{fname}` ({mlabel})]\n"
                    f"- Raw File on Disk (`storage_path`): `{spath}`\n"
                    f"- Full Extracted Text (`extracted_path`): `{ep_str}`\n"
                    f"{wiki_line}"
                    f"--- Preview (first {len(preview)} of {len(etxt)} extracted chars) ---\n{preview}\n"
                    f"--- Use `run_command(python_code=...)` or `read_file` on `{spath}` / `{ep_str}` to process 100% of rows/pages without truncation ---\n"
                    f"[/ATTACHED LARGE DOCUMENT]"
                )
            else:
                doc_blocks.append(
                    f"[ATTACHED DOCUMENT: `{fname}` ({mlabel}) | storage_path=`{spath}` | extracted_path=`{ep_str}`]\n{etxt}\n[/ATTACHED DOCUMENT]"
                )

    if not base_msg and loaded_meta:
        base_msg = "Please inspect the attached file(s) and summarize or extract the key details."
    badge_block = "\n\n".join(badges)
    display = f"{base_msg}\n\n{badge_block}".strip() if badge_block else base_msg
    followup_parts: List[str] = []
    for meta in loaded_meta:
        if meta["kind"] in ("image", "audio", "video"):
            followup_parts.append(
                f"[Attached {meta['kind'].title()} `{meta['filename']}` | attachment_id=`{meta['id']}` | storage_path=`{meta['storage_path']}`]"
            )
        elif meta["wiki_resource_path"]:
            followup_parts.append(
                f"[Attached Document `{meta['filename']}` | storage_path=`{meta['storage_path']}` | extracted_path=`{meta['extracted_path']}` | Wiki `{meta['wiki_resource_path']}`]"
            )
        else:
            rec_rows = mem._fetch_rows("SELECT extracted_text FROM chat_attachments WHERE attachment_id = ? LIMIT 1", (meta["id"],))
            ex_s = str(rec_rows[0][0])[:4000].strip() if rec_rows else ""
            followup_parts.append(
                f"[Attached Document `{meta['filename']}` | storage_path=`{meta['storage_path']}` | extracted_path=`{meta['extracted_path']}`]"
                + (f"\n{ex_s}" if ex_s else "")
            )
    return PreparedAttachmentTurn(display, "\n\n".join(doc_blocks).strip(), tuple(image_uris), loaded_meta, "\n\n".join(followup_parts))
