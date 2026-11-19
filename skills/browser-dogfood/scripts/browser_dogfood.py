# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
Headless Chromium CDP Dogfood Runner (`skills/browser-dogfood/scripts/browser_dogfood.py`).

Pure Python 3.12 stdlib Chrome DevTools Protocol (CDP) WebSocket runner that:
1. Launches local headless Chromium/Chrome/Edge in an isolated temp profile & process group.
2. Traps `Runtime.consoleAPICalled` (`error`), `Runtime.exceptionThrown`, and HTTP `4xx`/`5xx` responses.
3. Executes optional selector/text assertions and click interactions.
4. Captures Desktop (`1440x900`) + Mobile (`390x844`) PNG screenshots and optionally runs VLM critique.
"""

import argparse
import base64
import glob
import http.server
import json
import os
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from companion.attachments import ingest_attachment, inspect_image_bytes  # noqa: E402
from companion.config import settings  # noqa: E402
from companion.llm import chat, normalize_assistant_message, tier_context  # noqa: E402
from companion.streaming import wire_attachment_context  # noqa: E402

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def discover_browser_binary() -> Optional[str]:
    """Locate a local Chromium, Chrome, or Edge executable across Windows, Linux, and macOS."""
    override = (os.environ.get("COMPANION_CHROME_BIN") or "").strip()
    if override and Path(override).is_file():
        return override
    local_app = os.environ.get("LOCALAPPDATA") or ""
    home = str(Path.home())
    pw_globs = [
        os.path.join(local_app, "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe"),
        os.path.join(local_app, "ms-playwright", "chromium-*", "chrome-win", "chrome.exe"),
        os.path.join(home, ".cache", "ms-playwright", "chromium-*", "chrome-linux", "chrome"),
        os.path.join(home, "Library", "Caches", "ms-playwright", "chromium-*", "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
    ]
    for pattern in pw_globs:
        hits = sorted(glob.glob(pattern), reverse=True)
        if hits and Path(hits[0]).is_file():
            return hits[0]
    for cmd in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "msedge", "chrome"):
        found = shutil.which(cmd)
        if found:
            return found
    fixed = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in fixed:
        if Path(candidate).is_file():
            return candidate
    return None


class _CDPWebSocket:
    """Minimal RFC-6455 localhost WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, ws_url: str, timeout: float = 20.0) -> None:
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP WebSocket handshake closed unexpectedly.")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.splitlines()[0]:
            raise RuntimeError(f"CDP WebSocket upgrade failed: {head.splitlines()[0]!r}")
        self._next_id = 1
        self.events: List[Dict[str, Any]] = []

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise EOFError("CDP WebSocket closed while reading frame.")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_message(self) -> str:
        chunks: List[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            data = self._read_exact(ln)
            if masked:
                data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
            if opcode == 0x8:
                raise EOFError("CDP WebSocket closed by browser.")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode in (0x0, 0x1, 0x2):
                chunks.append(data)
                if fin:
                    return b"".join(chunks).decode("utf-8", errors="replace")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        self._send_frame(0x1, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode("utf-8"))
        while True:
            msg = json.loads(self._recv_message())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP `{method}` error: {msg['error']}")
                return msg.get("result") or {}
            if "method" in msg:
                self.events.append(msg)

    def drain_for(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.05, seconds)
        prev = self._sock.gettimeout()
        try:
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                self._sock.settimeout(min(0.1, rem))
                try:
                    msg = json.loads(self._recv_message())
                    if "method" in msg:
                        self.events.append(msg)
                except (socket.timeout, TimeoutError):
                    break
        finally:
            self._sock.settimeout(prev)

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


def _kill_browser_proc(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=6)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _launch_cdp_page(chrome_bin: str, user_dir: Path) -> Tuple[subprocess.Popen[str], _CDPWebSocket]:
    args = [
        chrome_bin, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-background-networking", "--no-first-run",
        "--no-default-browser-check", "--hide-scrollbars", "--remote-debugging-port=0",
        f"--user-data-dir={user_dir}", "about:blank",
    ]
    popen_kw: Dict[str, Any] = {
        "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True,
        "encoding": "utf-8", "errors": "replace",
    }
    if os.name == "nt":
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(args, **popen_kw)
    ws_browser = ""
    deadline = time.monotonic() + 15.0
    assert proc.stderr is not None
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if not line and proc.poll() is not None:
            break
        if "DevTools listening on ws://" in line:
            ws_browser = line.split("DevTools listening on ", 1)[1].strip()
            break
    if not ws_browser:
        _kill_browser_proc(proc)
        raise RuntimeError("Headless Chromium did not emit a DevTools WebSocket URL within 15s.")
    parsed = urllib.parse.urlparse(ws_browser)
    list_url = f"http://{parsed.hostname}:{parsed.port}/json/list"
    page_ws = ""
    for _ in range(25):
        with _NO_PROXY_OPENER.open(list_url, timeout=3.0) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if pages:
            page_ws = str(pages[0]["webSocketDebuggerUrl"])
            break
        time.sleep(0.1)
    if not page_ws:
        _kill_browser_proc(proc)
        raise RuntimeError("No attachable CDP `page` target found in headless Chromium.")
    return proc, _CDPWebSocket(page_ws)


def _eval_js(cdp: _CDPWebSocket, expr: str) -> Any:
    res = cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if "exceptionDetails" in res:
        cdp.events.append({"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": res["exceptionDetails"]}})
        return None
    return (res.get("result") or {}).get("value")


def _capture_viewport_png(cdp: _CDPWebSocket, out_path: Path, width: int, height: int, mobile: bool) -> Dict[str, Any]:
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": mobile},
    )
    _eval_js(cdp, "window.dispatchEvent(new Event('resize'))")
    cdp.drain_for(0.25)
    overflow = bool(_eval_js(cdp, "document.documentElement.scrollWidth > window.innerWidth + 4"))
    shot = cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
    raw_png = base64.b64decode(str(shot.get("data") or ""))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw_png)
    info = inspect_image_bytes(raw_png)
    w, h = (info[1], info[2]) if info else (0, 0)
    return {"path": out_path.as_posix(), "bytes": len(raw_png), "width": w, "height": h, "horizontal_overflow": overflow}


def _collect_trapped_errors(events: List[Dict[str, Any]]) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
    console_errors: List[str] = []
    page_errors: List[str] = []
    http_errors: List[Dict[str, Any]] = []
    for ev in events:
        m = ev.get("method")
        p = ev.get("params") or {}
        if m == "Runtime.consoleAPICalled" and p.get("type") == "error":
            parts = [str(a.get("value") if "value" in a else a.get("description") or a.get("type") or "") for a in (p.get("args") or [])]
            console_errors.append(" ".join(x for x in parts if x).strip() or "console.error")
        elif m == "Runtime.exceptionThrown":
            det = p.get("exceptionDetails") or {}
            exc = det.get("exception") or {}
            page_errors.append(str(exc.get("description") or det.get("text") or "Uncaught page exception").strip())
        elif m == "Network.responseReceived":
            resp = p.get("response") or {}
            status = int(resp.get("status") or 0)
            r_url = str(resp.get("url") or "")
            if status >= 400 and not r_url.endswith("/favicon.ico"):
                http_errors.append({"status": status, "url": r_url})
    return console_errors, page_errors, http_errors


def _run_optional_vision_critique(desktop_path: Path, mobile_path: Path, url: str) -> Dict[str, Any]:
    d_att = ingest_attachment(desktop_path.name, desktop_path.read_bytes(), "image/png")
    m_att = ingest_attachment(mobile_path.name, mobile_path.read_bytes(), "image/png")
    att_ids = [str(d_att.get("id") or ""), str(m_att.get("id") or "")]
    att_ids = [x for x in att_ids if x]
    try:
        prompt = (
            f"Critique the Desktop (1440x900) and Mobile (390x844) UI screenshots of `{url}`. "
            "Report any clipped text, overlapping controls, unreadable contrast, or broken layout in <=4 bullets."
        )
        with wire_attachment_context(att_ids), tier_context("image"):
            resp = chat([{"role": "user", "content": prompt}])
        msg = normalize_assistant_message(resp).get("content") or ""
        return {"ok": True, "attachment_ids": att_ids, "critique": str(msg).strip()}
    except Exception as exc:
        return {"ok": False, "attachment_ids": att_ids, "error": f"{type(exc).__name__}: {exc}"}


def run_browser_dogfood(
    url: str,
    out_dir: Path,
    *,
    expect_selectors: Optional[List[str]] = None,
    expect_texts: Optional[List[str]] = None,
    click_selectors: Optional[List[str]] = None,
    wait_ms: int = 350,
    ingest_attachments: bool = False,
    vision_critique: bool = False,
) -> Dict[str, Any]:
    """Drive headless Chromium against `url`, capture Desktop + Mobile PNGs, and trap runtime errors."""
    chrome_bin = discover_browser_binary()
    if not chrome_bin:
        return {"ok": False, "url": url, "error": "No local Chromium/Chrome/Edge binary found for browser-dogfood."}
    profile_dir = Path(tempfile.mkdtemp(prefix="companion_cdp_dogfood_"))
    proc: Optional[subprocess.Popen[str]] = None
    cdp: Optional[_CDPWebSocket] = None
    assertion_failures: List[str] = []
    try:
        proc, cdp = _launch_cdp_page(chrome_bin, profile_dir)
        for domain in ("Page", "Runtime", "Network", "DOM"):
            cdp.call(f"{domain}.enable")
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            cdp.drain_for(0.15)
            if any(e.get("method") == "Page.loadEventFired" for e in cdp.events):
                break
        cdp.drain_for(max(0.1, wait_ms / 1000.0))

        for sel in expect_selectors or []:
            found = bool(_eval_js(cdp, f"Boolean(document.querySelector({json.dumps(sel)}))"))
            if not found:
                assertion_failures.append(f"Missing expected selector: {sel}")
        for click_sel in click_selectors or []:
            clicked = bool(_eval_js(cdp, f"(() => {{ const el = document.querySelector({json.dumps(click_sel)}); if (!el) return false; el.click(); return true; }})()"))
            if not clicked:
                assertion_failures.append(f"Click target not found: {click_sel}")
            cdp.drain_for(0.2)
        body_text = str(_eval_js(cdp, "document.body ? document.body.innerText : ''") or "")
        title = str(_eval_js(cdp, "document.title || ''") or "")
        for needle in expect_texts or []:
            if needle not in body_text:
                assertion_failures.append(f"Missing expected text: {needle!r}")

        desktop = _capture_viewport_png(cdp, out_dir / "desktop_1440x900.png", 1440, 900, mobile=False)
        mobile = _capture_viewport_png(cdp, out_dir / "mobile_390x844.png", 390, 844, mobile=True)
        console_errors, page_errors, http_errors = _collect_trapped_errors(cdp.events)
        ok = not (console_errors or page_errors or http_errors or assertion_failures)
        out: Dict[str, Any] = {
            "ok": ok,
            "url": url,
            "title": title,
            "browser_binary": Path(chrome_bin).name,
            "desktop_screenshot": desktop,
            "mobile_screenshot": mobile,
            "mobile_horizontal_overflow": mobile["horizontal_overflow"],
            "console_errors": console_errors,
            "page_errors": page_errors,
            "http_errors": http_errors,
            "assertion_failures": assertion_failures,
        }
        if vision_critique:
            out["vision"] = _run_optional_vision_critique(Path(desktop["path"]), Path(mobile["path"]), url)
        elif ingest_attachments:
            d_att = ingest_attachment("desktop_1440x900.png", Path(desktop["path"]).read_bytes(), "image/png")
            m_att = ingest_attachment("mobile_390x844.png", Path(mobile["path"]).read_bytes(), "image/png")
            out["attachment_ids"] = [d_att.get("id"), m_att.get("id")]
        return out
    finally:
        if cdp is not None:
            cdp.close()
        if proc is not None:
            _kill_browser_proc(proc)
        shutil.rmtree(profile_dir, ignore_errors=True)


def _run_self_test() -> int:
    clean_html = (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Dogfood SelfTest</title><style>body{margin:0;padding:24px;background:#0f1117;color:#f1f5f9;font-family:sans-serif;}</style></head>"
        "<body><main data-testid='dogfood-ready'><h1>Companion Dogfood Ready</h1>"
        "<button id='ping-btn' onclick=\"document.body.dataset.clicked='1'\">Ping</button></main></body></html>"
    ).encode("utf-8")
    broken_html = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Broken Page</title></head><body>"
        "<script>console.error('dogfood-console-boom'); fetch('/api/fail-500');"
        "setTimeout(() => { throw new Error('dogfood-uncaught-boom'); }, 10);</script></body></html>"
    ).encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/broken":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(broken_html)
                return
            if self.path == "/api/fail-500":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":false}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(clean_html)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    port = int(srv.server_address[1])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    tmp_out = Path(tempfile.mkdtemp(prefix="companion_dogfood_selftest_"))
    try:
        good = run_browser_dogfood(
            f"http://127.0.0.1:{port}/",
            tmp_out / "good",
            expect_selectors=["[data-testid='dogfood-ready']"],
            expect_texts=["Companion Dogfood Ready"],
            click_selectors=["#ping-btn"],
            wait_ms=200,
        )
        assert good["ok"] is True, f"Clean page failed dogfood: {good}"
        assert (good["desktop_screenshot"]["width"], good["desktop_screenshot"]["height"]) == (1440, 900), good
        assert (good["mobile_screenshot"]["width"], good["mobile_screenshot"]["height"]) == (390, 844), good

        bad = run_browser_dogfood(f"http://127.0.0.1:{port}/broken", tmp_out / "bad", wait_ms=250)
        assert bad["ok"] is False, f"Broken page unexpectedly passed: {bad}"
        assert any("dogfood-console-boom" in c for c in bad["console_errors"]), bad
        assert any("dogfood-uncaught-boom" in p for p in bad["page_errors"]), bad
        assert any(h["status"] == 500 for h in bad["http_errors"]), bad
        print(json.dumps({
            "self_test": "PASS",
            "desktop_size": [good["desktop_screenshot"]["width"], good["desktop_screenshot"]["height"]],
            "mobile_size": [good["mobile_screenshot"]["width"], good["mobile_screenshot"]["height"]],
            "trapped_on_broken": {
                "console": len(bad["console_errors"]),
                "page": len(bad["page_errors"]),
                "http": len(bad["http_errors"]),
            },
        }))
        return 0
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp_out, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run headless Chromium CDP dogfood verification on a localhost preview URL.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic local HTTP + Chromium CDP self-test.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/", help="Target URL to inspect.")
    parser.add_argument("--out-dir", default="", help="Directory for desktop_1440x900.png and mobile_390x844.png.")
    parser.add_argument("--expect-selector", action="append", default=[], help="CSS selector expected on the page.")
    parser.add_argument("--expect-text", action="append", default=[], help="Visible text substring expected on the page.")
    parser.add_argument("--click-selector", action="append", default=[], help="CSS selector to click during dogfood pass.")
    parser.add_argument("--wait-ms", type=int, default=350, help="Post-load settle delay in milliseconds.")
    parser.add_argument("--ingest-attachments", action="store_true", help="Ingest captured PNGs into Companion attachment store.")
    parser.add_argument("--vision-critique", action="store_true", help="Critique Desktop + Mobile PNGs via Companion image tier.")
    args = parser.parse_args()
    if args.self_test:
        return _run_self_test()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (settings.scratch_dir / "browser_dogfood")
    res = run_browser_dogfood(
        args.url, out_dir,
        expect_selectors=args.expect_selector, expect_texts=args.expect_text,
        click_selectors=args.click_selector, wait_ms=args.wait_ms,
        ingest_attachments=args.ingest_attachments, vision_critique=args.vision_critique,
    )
    print(json.dumps(res, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
