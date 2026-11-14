"""
Presentation Surfaces Package (`surfaces/` — A-Frame Presentation Apex).

Provides 100% Python 3.12 stdlib LAN-accessible Web UI surfaces strictly
decoupled from Companion's headless `companion/` core per ADR-001 and ADR-008.
"""

from surfaces.web_assets import render_web_index_html
from surfaces.web_server import (
    CompanionWebSession,
    create_web_server,
    detect_lan_ip,
    dispatch_raw_http,
    start_web_server,
)

__all__ = [
    "CompanionWebSession",
    "create_web_server",
    "detect_lan_ip",
    "dispatch_raw_http",
    "render_web_index_html",
    "start_web_server",
]
