"""Healthy standby endpoint for staging a regional migration safely.

BOT_STANDBY=1 runs this without reading or using any bot credentials. It does
not poll Telegram; the existing production instance keeps handling updates.
"""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class StandbyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_error(404)
            return
        data = b"standby"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if self.path not in ("/", "/health"):
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), StandbyHandler).serve_forever()
