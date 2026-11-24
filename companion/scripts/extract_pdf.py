# /// script
# dependencies = ["pypdf"]
# ///
"""
First-Class Runtime PDF Text Extractor (`companion/scripts/extract_pdf.py`).

Extracts page-indexed UTF-8 text from `.pdf` attachments using `pypdf` in an isolated
subprocess so `companion/attachments.py` stays 100% Python stdlib.
"""

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from pypdf import PdfReader, PdfWriter


def extract_pdf_file(pdf_path: Path, max_pages: int = 250) -> Dict[str, Any]:
    """Read `pdf_path` and return `{"ok": True, "pages": count, "text": "..."}`."""
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)
    chunks: List[str] = []
    limit = min(total_pages, max_pages)
    for idx in range(limit):
        page_txt = (reader.pages[idx].extract_text() or "").strip()
        if page_txt:
            chunks.append(f"### Page {idx + 1}\n{page_txt}")
    if total_pages > limit:
        chunks.append(f"\n[Truncated after {limit} of {total_pages} pages]")
    return {
        "ok": True,
        "pages": total_pages,
        "extracted_pages": len(chunks),
        "text": "\n\n".join(chunks).strip(),
    }


def _run_self_test() -> int:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    reader = PdfReader(io.BytesIO(buf.getvalue()))
    assert len(reader.pages) == 1, "Expected 1-page synthetic PDF"
    print("PASS: extract_pdf.py self-test ok")
    return 0


def main(argv: List[str]) -> int:
    if "--self-test" in argv:
        return _run_self_test()
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "Usage: extract_pdf.py <pdf_path>"}))
        return 1
    target = Path(argv[1]).resolve()
    parts_low = [p.lower() for p in target.parts]
    if ".ssh" in parts_low or target.name.lower() in (".env", "secrets.env") or target.suffix.lower() != ".pdf":
        print(json.dumps({"ok": False, "error": f"SAFETY GUARD REJECTED: invalid or restricted PDF target `{target.name}`"}))
        return 1
    if not target.is_file():
        print(json.dumps({"ok": False, "error": f"File not found: {target}"}))
        return 1
    try:
        res = extract_pdf_file(target)
        sys.stdout.buffer.write(json.dumps(res, ensure_ascii=False).encode("utf-8") + b"\n")
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
