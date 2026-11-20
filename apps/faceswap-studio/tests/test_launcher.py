import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app import launcher


def test_preferred_port_used_when_free():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert launcher.find_free_port("127.0.0.1", free) == free


def test_busy_port_falls_back():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        port = launcher.find_free_port("127.0.0.1", taken)
    assert port != taken
    assert launcher.can_bind("127.0.0.1", port)


def test_forbidden_port_like_winerror_10013_falls_back(monkeypatch):
    # Windows refuses ports in Hyper-V/WSL reserved ranges with WinError 10013
    real = launcher.can_bind
    monkeypatch.setattr(launcher, "can_bind", lambda h, p: p != 8000 and real(h, p))
    assert launcher.find_free_port("127.0.0.1", 8000) != 8000


def test_all_fallbacks_blocked_uses_os_port(monkeypatch):
    monkeypatch.setattr(launcher, "can_bind", lambda h, p: False)
    port = launcher.find_free_port("127.0.0.1", 8000)
    assert 0 < port < 65536


def test_browser_host():
    assert launcher.browser_host("0.0.0.0") == "127.0.0.1"
    assert launcher.browser_host("127.0.0.1") == "127.0.0.1"


def test_opens_browser_once_server_answers():
    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    opened = []
    try:
        launcher.open_browser_when_ready(url, timeout=10, opener=opened.append).join(10)
    finally:
        server.shutdown()
    assert opened == [url]


def test_browser_errors_do_not_raise():
    def boom(url):
        raise RuntimeError("no browser")

    launcher.open_browser_when_ready("http://127.0.0.1:9", timeout=0.1, opener=boom).join(5)
