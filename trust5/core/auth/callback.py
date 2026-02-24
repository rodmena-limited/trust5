from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP request handler that captures OAuth authorization codes from redirects."""

    def do_GET(self) -> None:
        query = self.path.split("?", 1)[-1] if "?" in self.path else ""
        params = parse_qs(query)
        server = self.server
        assert isinstance(server, OAuthCallbackServer)
        server.auth_code = params.get("code", [None])[0]
        server.auth_error = params.get("error", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorization complete.</h2>"
            b"<p>You can close this window.</p>"
            b"<script>setTimeout(()=>window.close(),2000)</script>"
            b"</body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class OAuthCallbackServer(HTTPServer):
    """Local HTTP server that receives OAuth callback with authorization code."""

    auth_code: str | None = None
    auth_error: str | None = None


def run_callback_server(port: int = 8585, timeout: float = 120.0) -> tuple[str | None, str | None]:
    server = OAuthCallbackServer(("127.0.0.1", port), OAuthCallbackHandler)
    server.timeout = timeout

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    server.server_close()
    return server.auth_code, server.auth_error
