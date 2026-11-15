import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from snapshotkit.notify import send_failure

PAYLOAD = {"project": "p", "image_digest": "sha256:" + "a" * 64, "reason": "x", "pipeline_run_url": "u"}


def test_posts_json():
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        send_failure(f"http://127.0.0.1:{srv.server_port}/", PAYLOAD)
    finally:
        srv.shutdown()
    assert got["body"] == PAYLOAD


def test_unreachable_returns_quickly():
    srv = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = srv.server_port
    srv.server_close()  # closed port
    start = time.monotonic()
    send_failure(f"http://127.0.0.1:{port}/", PAYLOAD)
    assert time.monotonic() - start < 6


def test_none_url_is_noop():
    send_failure(None, PAYLOAD)


def test_malformed_url_never_raises():
    send_failure("not-a-url", PAYLOAD)
    send_failure("http://[::1", PAYLOAD)
