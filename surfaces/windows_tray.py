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


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]


class _NotifyIconDataW(ctypes.Structure):
    """Full Windows 10/11 `NOTIFYICONDATAW` layout (valid `cbSize` on 64-bit Windows)."""

    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _Guid), ("hBalloonIcon", wintypes.HICON),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _IconInfo(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP),
    ]


def _create_companion_hicon(user32, gdi32, shell32, hinst: int) -> int:
    """Build a crisp 32x32 ARGB emerald Companion tray icon (falling back to the Python executable icon)."""
    try:
        gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(_BitmapInfoHeader), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        gdi32.CreateBitmap.restype = wintypes.HBITMAP
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        user32.CreateIconIndirect.argtypes = [ctypes.POINTER(_IconInfo)]
        user32.CreateIconIndirect.restype = wintypes.HICON

        bmi = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), 32, -32, 1, 32, 0, 32 * 32 * 4, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbm_color = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if hbm_color and bits.value:
            px = (ctypes.c_uint32 * (32 * 32)).from_address(bits.value)
            for y in range(32):
                for x in range(32):
                    dx, dy = x - 15.5, y - 15.5
                    d2 = dx * dx + dy * dy
                    if d2 <= 210:
                        if 68 <= d2 <= 132 and dx < 7.5:
                            px[y * 32 + x] = 0xFF34D399  # Emerald "C" arc
                        elif d2 <= 18:
                            px[y * 32 + x] = 0xFF38BDF8  # Cyan core spark
                        else:
                            px[y * 32 + x] = 0xFF0F172A  # Deep slate orb
                    else:
                        px[y * 32 + x] = 0x00000000
            hbm_mask = gdi32.CreateBitmap(32, 32, 1, 1, None)
            ii = _IconInfo(True, 0, 0, hbm_mask, hbm_color)
            hicon = user32.CreateIconIndirect(ctypes.byref(ii))
            gdi32.DeleteObject(hbm_color)
            gdi32.DeleteObject(hbm_mask)
            if hicon:
                return int(hicon)
    except Exception:
        pass
    shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
    shell32.ExtractIconW.restype = wintypes.HICON
    ico = shell32.ExtractIconW(hinst, sys.executable, 0)
    if ico:
        return int(ico)
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    return int(user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR)))


def _bind_win32_apis(user32, shell32, kernel32) -> None:
    """Configure 64-bit-safe `ctypes` prototypes for Win32 window and tray functions."""
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NotifyIconDataW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def _run_win32_tray_loop(local_url: str, on_quit) -> None:
    """Run a pure-stdlib 64-bit Win32 `Shell_NotifyIconW` message loop on Windows."""
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
    _bind_win32_apis(user32, shell32, kernel32)

    wm_tray, wm_destroy, wm_command = 0x0414, 0x0002, 0x0111
    wm_lbuttonup, wm_lbuttondblclk, wm_rbuttonup, wm_contextmenu = 0x0202, 0x0203, 0x0205, 0x007B
    wm_taskbar_created = int(user32.RegisterWindowMessageW("TaskbarCreated") or 0)
    wndproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WndClassW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT), ("lpfnWndProc", wndproc_t), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ]

    nid = _NotifyIconDataW()

    def _show_menu(hwnd: int) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0x0000, 1001, "Open Companion Studio")
        user32.AppendMenuW(menu, 0x0000, 1002, "Open User Data Folder")
        user32.AppendMenuW(menu, 0x0800, 0, None)
        user32.AppendMenuW(menu, 0x0000, 1003, "Quit Companion")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = int(user32.TrackPopupMenu(menu, 0x0102, pt.x, pt.y, 0, hwnd, None))
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == 1001:
            webbrowser.open(local_url)
        elif cmd == 1002:
            open_user_data_folder()
        elif cmd == 1003:
            user32.DestroyWindow(hwnd)

    def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if wm_taskbar_created and msg == wm_taskbar_created:
            shell32.Shell_NotifyIconW(0, ctypes.byref(nid))
            return 0
        if msg == wm_tray:
            ev = int(lparam) & 0xFFFF
            if ev == wm_lbuttondblclk:
                webbrowser.open(local_url)
            elif ev in (wm_rbuttonup, wm_contextmenu, wm_lbuttonup):
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
    hinst = int(kernel32.GetModuleHandleW(None))
    hicon = _create_companion_hicon(user32, gdi32, shell32, hinst)
    cls_name = f"CompanionTrayWnd_{os.getpid()}"
    wc = WndClassW(0, cb_proc, 0, 0, hinst, hicon, None, None, None, cls_name)
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClassW)]
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = int(user32.CreateWindowExW(0, cls_name, "Companion AI OS", 0, 0, 0, 0, 0, None, None, hinst, None))

    nid.cbSize = ctypes.sizeof(_NotifyIconDataW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = 0x00000001 | 0x00000002 | 0x00000004
    nid.uCallbackMessage = wm_tray
    nid.hIcon = hicon
    nid.szTip = f"Companion AI OS ({local_url})"[:127]

    added = False
    for _ in range(10):
        if bool(shell32.Shell_NotifyIconW(0, ctypes.byref(nid))):
            added = True
            break
        threading.Event().wait(0.5)
    telemetry.log("INFO" if added else "WARN", "tray", f"Win32 tray icon registered={added} ({local_url})")
    try:
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
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
