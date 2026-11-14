"""
Zero-Dependency Responsive Web UI Assets (`surfaces/web_assets.py` — A-Frame Presentation Layer).

Loads clean standalone HTML5 (`surfaces/static/index.html`), CSS3 (`surfaces/static/styles.css`),
and ES2022 (`surfaces/static/app.js`) files with mtime caching, assembling a single-roundtrip
mobile/desktop document or serving individual `/static/<asset>` files with zero CDN dependencies.
"""

import html
from pathlib import Path
from typing import Dict, Optional, Tuple

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_CONTENT_TYPES: Dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}

_ASSET_CACHE: Dict[str, Tuple[int, str]] = {}


def read_static_asset(name: str) -> Optional[Tuple[str, bytes]]:
    """Read a whitelisted static asset from `surfaces/static/` with mtime-keyed caching."""
    clean = (name or "").strip().lstrip("/")
    ctype = STATIC_CONTENT_TYPES.get(clean)
    if not ctype:
        return None
    path = (STATIC_DIR / clean).resolve()
    if path.parent != STATIC_DIR.resolve() or not path.is_file():
        return None
    mtime_ns = int(path.stat().st_mtime_ns)
    cached = _ASSET_CACHE.get(clean)
    if cached is not None and cached[0] == mtime_ns:
        return ctype, cached[1].encode("utf-8")
    text = path.read_text(encoding="utf-8", errors="replace")
    _ASSET_CACHE[clean] = (mtime_ns, text)
    return ctype, text.encode("utf-8")


def _read_asset_text(name: str) -> str:
    loaded = read_static_asset(name)
    return loaded[1].decode("utf-8") if loaded is not None else ""


def render_web_index_html(csrf_token: str = "") -> str:
    """Assemble and return the self-contained HTML5 document for Companion's LAN Web UI."""
    tpl = _read_asset_text("index.html")
    css = _read_asset_text("styles.css")
    js = _read_asset_text("app.js").replace("</script", "<\\/script")
    safe_tok = html.escape(csrf_token or "", quote=True)
    meta_tag = f'<meta name="companion-csrf" content="{safe_tok}">'
    return (
        tpl.replace("<!-- COMPANION_CSRF_META -->", meta_tag)
        .replace("<!-- COMPANION_INLINE_CSS -->", f"<style>\n{css}\n</style>")
        .replace("<!-- COMPANION_INLINE_JS -->", f"<script>\n{js}\n</script>")
    )
