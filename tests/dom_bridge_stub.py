"""A canned dom bridge for the smoke test.

Speaks the protocol in docs/dom_bridge.md over loopback and logs every
op it accepts, so the test can assert that a bridge-driven run actually
delivered `open`, `click` and `emit` — and that the emitted value
reached the host.

    python3 tests/dom_bridge_stub.py PORT SNAPSHOT.json VIEW.png OPS.jsonl

Refuses any request whose bearer token is not `testtoken`.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT = int(sys.argv[1])
SNAPSHOT = json.loads(Path(sys.argv[2]).read_text())
VIEW_PNG = sys.argv[3]
OPS_LOG = Path(sys.argv[4])

OPS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Authorization") != "Bearer testtoken":
            resp = {"ok": False, "error": "bad token"}
        else:
            OPS.append(body)
            OPS_LOG.write_text(
                "\n".join(json.dumps(o) for o in OPS) + "\n")
            op = body.get("op")
            if op == "snapshot":
                resp = {"ok": True, **SNAPSHOT}
            elif op == "screenshot":
                resp = {"ok": True, "path": VIEW_PNG, "w": 0, "h": 0}
            elif op == "stable":
                resp = {"ok": True, "stable": True}
            elif op in ("open", "click", "scroll", "fill", "press",
                        "emit"):
                resp = {"ok": True, "matched": 1}
            else:
                resp = {"ok": False, "error": f"unknown op {op!r}"}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
