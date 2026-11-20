"""Startup helpers: pick a port Windows actually lets us bind, open the browser when ready."""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.request
import webbrowser

log = logging.getLogger("image_studio.launcher")

DEFAULT_PORT = 8000
# Ports tried after the preferred one. On Windows, Hyper-V / WSL / Docker reserve whole port
# ranges (see `netsh interface ipv4 show excludedportrange protocol=tcp`), and binding inside one
# fails with WinError 10013 even though nothing is listening there.
FALLBACK_PORTS = (8001, 8002, 8080, 8888, 7860, 5000, 3000)


def can_bind(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: fail if anyone else listens
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str, preferred: int = DEFAULT_PORT) -> int:
    """`preferred` if it can be bound, else the first usable fallback, else any port the OS gives."""
    for port in (preferred, *(p for p in FALLBACK_PORTS if p != preferred)):
        if can_bind(host, port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def browser_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "", "::"} else host


def open_browser_when_ready(url: str, timeout: float = 300.0, opener=webbrowser.open) -> threading.Thread:
    """Poll /api/health in the background and open `url` once the server answers."""

    def wait_and_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/api/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.5)
        try:
            opener(url)
        except Exception:  # noqa: BLE001 - a missing browser must not stop the server
            log.warning("Не удалось открыть браузер, откройте вручную: %s", url)

    thread = threading.Thread(target=wait_and_open, name="open-browser", daemon=True)
    thread.start()
    return thread
