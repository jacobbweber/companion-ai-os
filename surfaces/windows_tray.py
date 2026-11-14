"""
Zero-Dependency Windows System Tray Launcher & Port Guard (`surfaces/windows_tray.py`).

Provides:
- Single-instance health detection (`is_companion_running_on_port`) so double-clicking a shortcut
  simply opens the already-running Companion Studio tab instead of spawning duplicate processes
- Graceful port probing (`select_available_port`) and plain-English port-conflict diagnostics
- Pure-stdlib Win32 System Tray icon (`Shell_NotifyIconW` via `ctypes`) with right-click menu:
  Open Companion Studio, Open User Data Folder, and Quit Companion (with non-Windows fallback)
"""

import ctypes
from ctypes import wintypes
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from companion.config import settings
from companion.telemetry import telemetry
from surfaces.web_server import create_web_server


def is_companion_running_on_port(port: int, timeout: float = 0.6) -> bool:
    """Return `True` if a live Companion Web UI already answers `/api/health` on `127.0.0.1:<port>`."""
    url = f"http://127.0.0.1:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read(4096).decode("utf-8", errors="replace"))
            return (
                isinstance(data, dict)
                and bool(data.get("ok"))
                and (data.get("service") == "companion-web-ui" or "version" in data or "status" in data)
            )
    except Exception:
        return False


def select_available_port(host: str = "0.0.0.0", preferred_port: int = 8000, max_tries: int = 10) -> int:
    """Return `preferred_port` if bindable, otherwise probe consecutive ports up to `max_tries`."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    for offset in range(max(1, int(max_tries))):
        candidate = int(preferred_port) + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((probe_host, candidate))
                return candidate
            except OSError:
                continue
    return int(preferred_port)


def format_port_conflict_error(host: str, port: int, err: OSError) -> str:
    """Return a friendly, plain-English diagnostic when `port` cannot be bound."""
    alt_port = int(port) + 1 if int(port) < 65534 else 8080
    if is_companion_running_on_port(port):
        return (
            f"Companion is already running on http://127.0.0.1:{port} !\n"
            f"  -> Open http://127.0.0.1:{port} in your browser, or run a second instance on another port:\n"
            f"     python harness.py --web --port {alt_port}"
        )
    return (
        f"Port {port} on {host} is already in use by another program ({err}).\n"
        f"  -> Start Companion on a free port with: python harness.py --web --port {alt_port}\n"
        f"  -> Or set COMPANION_WEB_PORT={alt_port} in your .env file."
    )


def open_user_data_folder(data_dir: Optional[Path] = None) -> Path:
    """Ensure the isolated user `data/` folder exists and open it in the OS file manager."""
    target = (data_dir or settings.effective_data_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as exc:
        telemetry.log("WARN", "tray", f"Could not open user data folder `{target}`: {exc}")
    return target


def _run_win32_tray_loop(local_url: str, on_quit) -> None:
    """Run a pure-stdlib Win32 `Shell_NotifyIconW` message loop on Windows."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    wm_tray = 0x0400 + 20
    wm_destroy, wm_command = 0x0002, 0x0111
    wm_lbuttondblclk, wm_rbuttonup, wm_contextmenu = 0x0203, 0x0205, 0x007B
    nim_add, nim_delete = 0x00000000, 0x00000002
    nif_message, nif_icon, nif_tip = 0x00000001, 0x00000002, 0x00000004
    mf_string, mf_separator = 0x0000, 0x0800
    tpm_returncmd, tpm_rightbutton = 0x0100, 0x0002

    lresult_t = ctypes.c_ssize_t
    wndproc_t = ctypes.WINFUNCTYPE(lresult_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = lresult_t

    class WndClassW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", wndproc_t), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NotifyIconDataW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
        ]

    def _show_menu(hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, mf_string, 1001, "Open Companion Studio")
        user32.AppendMenuW(menu, mf_string, 1002, "Open User Data Folder")
        user32.AppendMenuW(menu, mf_separator, 0, None)
        user32.AppendMenuW(menu, mf_string, 1003, "Quit Companion")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, tpm_returncmd | tpm_rightbutton, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        if cmd == 1001:
            webbrowser.open(local_url)
        elif cmd == 1002:
            open_user_data_folder()
        elif cmd == 1003:
            user32.DestroyWindow(hwnd)

    def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == wm_tray:
            if lparam == wm_lbuttondblclk:
                webbrowser.open(local_url)
            elif lparam in (wm_rbuttonup, wm_contextmenu):
                _show_menu(hwnd)
            return 0
        if msg == wm_command and (wparam & 0xFFFF) == 1003:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == wm_destroy:
            user32.PostQuitMessage(0)
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    cb_proc = wndproc_t(_wnd_proc)
    hinst = kernel32.GetModuleHandleW(None)
    cls_name = f"CompanionTrayWnd_{os.getpid()}"
    wc = WndClassW(0, cb_proc, 0, 0, hinst, user32.LoadIconW(None, 32512), None, None, None, cls_name)
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, cls_name, "Companion AI OS", 0, 0, 0, 0, 0, None, None, hinst, None)

    nid = NotifyIconDataW(
        ctypes.sizeof(NotifyIconDataW), hwnd, 1, nif_message | nif_icon | nif_tip,
        wm_tray, user32.LoadIconW(None, 32512), f"Companion AI OS ({local_url})"[:127],
    )
    shell32.Shell_NotifyIconW(nim_add, ctypes.byref(nid))
    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        shell32.Shell_NotifyIconW(nim_delete, ctypes.byref(nid))
        on_quit()


def start_tray_app(host: str = "0.0.0.0", port: Optional[int] = None, auto_open_browser: bool = True) -> None:
    """Start Companion's Web Studio in the background and attach a desktop System Tray controller."""
    pref_port = int(port if port is not None else int(os.environ.get("COMPANION_WEB_PORT", "8000")))
    if is_companion_running_on_port(pref_port):
        existing_url = f"http://127.0.0.1:{pref_port}"
        if auto_open_browser:
            webbrowser.open(existing_url)
        print(f"Companion is already running at {existing_url} — opened browser tab.")
        return

    chosen_port = select_available_port(host=host, preferred_port=pref_port)
    srv, sess = create_web_server(host=host, port=chosen_port)
    bound_port = int(srv.server_address[1])
    local_url = f"http://127.0.0.1:{bound_port}"
    telemetry.log("INFO", "tray", f"Started Companion Tray App on {local_url} ({sess.lan_url})")

    sess.idle_watcher.start()
    srv_thread = threading.Thread(target=srv.serve_forever, name="companion-tray-http", daemon=True)
    srv_thread.start()
    if auto_open_browser:
        webbrowser.open(local_url)

    stopped = False

    def _shutdown() -> None:
        nonlocal stopped
        if stopped:
            return
        stopped = True
        sess.idle_watcher.stop()
        srv.shutdown()
        srv.server_close()

    if sys.platform == "win32":
        _run_win32_tray_loop(local_url, _shutdown)
    else:
        print(f"Companion running at {local_url} (Tray mode fallback on {sys.platform}; press Ctrl+C to stop).")
        try:
            srv_thread.join()
        except KeyboardInterrupt:
            pass
        finally:
            _shutdown()
