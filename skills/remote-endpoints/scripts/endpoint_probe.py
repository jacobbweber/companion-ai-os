"""
Ring-1 Diagnostic Helper for Remote Endpoint Reachability (`skills/remote-endpoints/scripts/endpoint_probe.py`).

Pure Python 3.12 stdlib TCP/HTTP banner probe with `--self-test` verification.
"""

import argparse
import json
import socket
import sys
import threading
import time
from typing import Any, Dict


def probe_tcp_endpoint(host: str, port: int, timeout: float = 3.0) -> Dict[str, Any]:
    """Measure TCP handshake latency and optional greeting banner without leaking credentials."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(0.4)
            try:
                banner = sock.recv(96).decode("utf-8", errors="replace").strip()
            except Exception:
                banner = ""
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {"ok": True, "host": host, "port": int(port), "latency_ms": ms, "banner": banner}
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return {"ok": False, "host": host, "port": int(port), "latency_ms": ms, "error": f"{type(exc).__name__}: {exc}"}


def _run_self_test() -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = int(srv.getsockname()[1])

    def _accept_once() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.sendall(b"SSH-2.0-CompanionSelfTest\r\n")

    t = threading.Thread(target=_accept_once, daemon=True)
    t.start()
    try:
        res = probe_tcp_endpoint("127.0.0.1", port, timeout=2.0)
        assert res["ok"] is True and "CompanionSelfTest" in res["banner"], f"Self-test failed: {res}"
        print(json.dumps({"self_test": "PASS", "probe": res}))
        return 0
    finally:
        srv.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe TCP reachability for a registered Companion endpoint.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic loopback self-test.")
    parser.add_argument("--host", default="127.0.0.1", help="Target hostname or IP.")
    parser.add_argument("--port", type=int, default=22, help="Target TCP port.")
    args = parser.parse_args()
    if args.self_test:
        return _run_self_test()
    print(json.dumps(probe_tcp_endpoint(args.host, args.port), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
