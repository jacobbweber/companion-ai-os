"""
Zero-Dependency Responsive Web UI Assets (`surfaces/web_assets.py` — A-Frame Presentation Layer).

Loads clean standalone HTML5 (`surfaces/static/index.html`), CSS3 (`surfaces/static/styles.css`),
and ES2022 (`surfaces/static/app.js`) files with mtime caching, deterministic SHA-256 content-hash
versioning (`?v=<hash>`), and version metadata injection with zero CDN dependencies.
"""

import hashlib
import html
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from companion import __version__

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_CONTENT_TYPES: Dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}

_ASSET_CACHE: Dict[str, Tuple[int, str]] = {}
_HASH_CACHE: Tuple[Tuple[int, int, int], str] = ((0, 0, 0), "")
_TITLE_RE = re.compile(r"<title>[^<]*</title>", re.IGNORECASE)


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


def compute_static_asset_hash() -> str:
    """Compute a 12-hex SHA-256 content digest across `index.html`, `styles.css`, and `app.js`."""
    global _HASH_CACHE
    names = ("index.html", "styles.css", "app.js")
    mtimes = []
    texts = []
    for nm in names:
        texts.append(_read_asset_text(nm))
        cached = _ASSET_CACHE.get(nm)
        mtimes.append(cached[0] if cached is not None else 0)
    sig = (mtimes[0], mtimes[1], mtimes[2])
    if _HASH_CACHE[0] == sig and _HASH_CACHE[1]:
        return _HASH_CACHE[1]
    digest = hashlib.sha256()
    digest.update(__version__.encode("utf-8"))
    for txt in texts:
        digest.update(b"\x00")
        digest.update(txt.encode("utf-8", errors="replace"))
    short_hash = digest.hexdigest()[:12]
    _HASH_CACHE = (sig, short_hash)
    return short_hash


def render_web_index_html(csrf_token: str = "", inline_assets: bool = True) -> str:
    """Assemble and return the versioned HTML5 document for Companion's LAN Web UI."""
    tpl = _read_asset_text("index.html")
    css = _read_asset_text("styles.css")
    js = _read_asset_text("app.js").replace("</script", "<\\/script")
    asset_hash = compute_static_asset_hash()
    safe_tok = html.escape(csrf_token or "", quote=True)
    safe_ver = html.escape(__version__, quote=True)
    safe_hash = html.escape(asset_hash, quote=True)
    meta_tags = (
        f'<meta name="companion-csrf" content="{safe_tok}">\n'
        f'  <meta name="companion-version" content="{safe_ver}">\n'
        f'  <meta name="companion-asset-hash" content="{safe_hash}">'
    )
    css_url = f"/static/styles.css?v={safe_hash}"
    js_url = f"/static/app.js?v={safe_hash}"
    css_block = (
        f'<style data-asset-url="{css_url}">\n{css}\n</style>'
        if inline_assets
        else f'<link rel="stylesheet" href="{css_url}">'
    )
    js_block = (
        f'<script data-asset-url="{js_url}">\n{js}\n</script>'
        if inline_assets
        else f'<script defer src="{js_url}"></script>'
    )
    rendered = _TITLE_RE.sub(f"<title>Companion AI OS v{safe_ver}</title>", tpl, count=1)
    return (
        rendered.replace("<!-- COMPANION_CSRF_META -->", meta_tags)
        .replace("<!-- COMPANION_INLINE_CSS -->", css_block)
        .replace("<!-- COMPANION_INLINE_JS -->", js_block)
    )
