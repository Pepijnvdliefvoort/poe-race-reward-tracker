from __future__ import annotations

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from admin_service import (
    POLLER_LOG_PATH,
    SERVER_LOG_PATH,
    admin_authorized,
    build_admin_session_set_cookie,
    csv_download_headers,
    get_client_ip,
    record_site_visit,
    should_issue_admin_session_cookie,
    tail_log_file,
    visitor_map_payload,
)
from data_service import WEB_DIR, fetch_listing_preview, load_config, load_price_data, save_config


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), DashboardHandler)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _send_admin_unauthorized_page(self) -> None:
        body = (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"/>"
            "<title>Admin — Authentication required</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:2rem auto;padding:0 1.5rem;"
            "line-height:1.5;color:#1a1a1a;}code{background:#eee;padding:0.15em 0.4em;border-radius:4px;}"
            "</style></head><body>"
            "<h1>Authentication required</h1>"
            "<p>Visit <code>/admin?token=…</code> <strong>once</strong> using the same value as "
            "<code>ADMIN_TOKEN</code> on the server (e.g. your GitHub Actions secret). "
            "The server sets an HttpOnly cookie; then <code>/admin</code> works without the query string.</p>"
            "<p>If you already did that, check that the service loads <code>/etc/poe-market-flips/secrets.env</code> "
            "and restart <code>poe-market-server</code> after deploy.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress logging for static assets; only log API requests."""
        # args[0] contains the request line like 'GET /path HTTP/1.1'
        if args and "/api/" in str(args[0]):
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        req_path = parsed.path or "/"
        params = parse_qs(parsed.query)
        auth_header = self.headers.get("Authorization")
        token_param = params.get("token", [None])[0]
        cookie_header = self.headers.get("Cookie")

        if req_path in {"/admin", "/admin/"} and should_issue_admin_session_cookie(token_param):
            set_cookie = build_admin_session_set_cookie(self.headers.get("X-Forwarded-Proto"))
            if set_cookie:
                self.send_response(302)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", set_cookie)
                self.end_headers()
                return

        if req_path in {"/admin", "/admin/"} and os.environ.get("ADMIN_TOKEN", "").strip():
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._send_admin_unauthorized_page()
                return

        if req_path.startswith("/api/admin/"):
            if not admin_authorized(auth_header, token_param, cookie_header):
                body = json.dumps({"error": "Forbidden"}).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/logs":
                stream = params.get("stream", ["server"])[0]
                log_path = POLLER_LOG_PATH if stream == "poller" else SERVER_LOG_PATH
                text = tail_log_file(log_path)
                body = text.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/visitor-map":
                payload = visitor_map_payload()
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/download/price_poll.csv":
                filename, csv_path = csv_download_headers()
                if not csv_path.exists():
                    self.send_error(404)
                    return
                data = csv_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            body = json.dumps({"error": "Unknown admin endpoint"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if req_path in {"/", "/index.html"}:
            record_site_visit(
                get_client_ip(self.headers.get("X-Forwarded-For"), self.client_address[0]),
                req_path,
            )

        if parsed.path == "/api/prices":
            payload = load_price_data()
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/config":
            payload = load_config()
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/listings":
            params = parse_qs(parsed.query)
            query_id = params.get("queryId", [""])[0].strip()
            if not query_id:
                body = json.dumps({"error": "Missing queryId parameter"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            try:
                payload = fetch_listing_preview(query_id)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if parsed.path == "/admin.html":
            loc = "/admin"
            if parsed.query:
                loc += f"?{parsed.query}"
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
            return

        if parsed.path in {"/admin", "/admin/"}:
            q = parsed.query
            self.path = "/admin.html" + (f"?{q}" if q else "")

        if parsed.path in {"/", ""}:
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
                save_config(data)
                self.send_response(204)
                self.end_headers()
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()
