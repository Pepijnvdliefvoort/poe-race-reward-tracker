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
    admin_credential_material_present,
    admin_lockout_retry_after_seconds,
    admin_note_auth_failure,
    admin_note_auth_success,
    admin_security_enabled,
    maybe_record_admin_login_attempt,
    build_admin_session_set_cookie,
    clear_market_data,
    csv_download_headers,
    get_client_ip,
    record_site_visit,
    restart_poller,
    stop_poller,
    should_issue_admin_session_cookie,
    tail_log_file,
    query_log_entries,
    visitor_map_payload,
)
from data_service import WEB_DIR, fetch_listing_preview, load_config, load_price_data, save_config
from data_service import CSV_PATH, LISTINGS_CACHE_PATH
from stats_service import system_stats_payload

_ADMIN_UNAUTHORIZED_HTML = WEB_DIR / "admin-unauthorized.html"


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), DashboardHandler)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def _client_ip(self) -> str:
        return get_client_ip(self.headers.get("X-Forwarded-For"), self.client_address[0])

    def _reject_if_admin_ip_locked(self, *, want_json: bool) -> bool:
        if not admin_security_enabled():
            return False
        retry_after = admin_lockout_retry_after_seconds(self._client_ip())
        if retry_after <= 0:
            return False
        payload = {
            "error": "Too many failed authentication attempts. Try again later.",
            "retryAfterSeconds": retry_after,
        }
        if want_json:
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        html = (
            "<!DOCTYPE html><html><head><meta charset=utf-8><title>429</title></head>"
            "<body><p>Too many failed authentication attempts. Try again later.</p></body></html>"
        )
        body = html.encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _note_failed_admin_auth_if_applicable(
        self,
        auth_header: str | None,
        query_token: str | None,
        cookie_header: str | None,
    ) -> None:
        if not admin_security_enabled():
            return
        if admin_authorized(auth_header, query_token, cookie_header):
            return
        if not admin_credential_material_present(auth_header, query_token, cookie_header):
            return
        admin_note_auth_failure(self._client_ip())

    def _note_admin_auth_success(self) -> None:
        if admin_security_enabled():
            admin_note_auth_success(self._client_ip())

    def _maybe_record_admin_login_attempt(
        self,
        auth_header: str | None,
        query_token: str | None,
        cookie_header: str | None,
    ) -> None:
        maybe_record_admin_login_attempt(
            self._client_ip(), auth_header, query_token, cookie_header
        )

    def _send_admin_unauthorized_page(self) -> None:
        if _ADMIN_UNAUTHORIZED_HTML.is_file():
            body = _ADMIN_UNAUTHORIZED_HTML.read_bytes()
        else:
            body = b"<!DOCTYPE html><html><head><meta charset=utf-8><title>401</title></head>"
            body += b"<body><p>Unauthorized</p></body></html>"
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress logging for static assets; only log API requests."""
        # args[0] contains the request line like 'GET /path HTTP/1.1'
        if not args:
            return
        request_line = str(args[0])
        if "/api/" not in request_line:
            return
        # Avoid the admin log viewer causing a self-reinforcing log flood.
        if "/api/admin/logs" in request_line:
            return
        if "/api/admin/visitor-map" in request_line:
            return
        # Use stdout so it stays INFO-level in captured logs.
        #
        # BaseHTTPRequestHandler calls log_message('"%s" %s %s', requestline, code, size)
        # where size is '-' when unknown. We omit that trailing dash for cleaner logs,
        # and we also omit the (always-local) client IP prefix.
        try:
            if len(args) >= 3 and format.strip() == '"%s" %s %s':
                req, status, size = (str(args[0]), str(args[1]), str(args[2]))
                message = f'{req} {status}'
                if size and size != "-":
                    message += f" {size}"
            else:
                message = str(format % args)
                if message.endswith(" -"):
                    message = message[: -len(" -")]
        except Exception:
            message = request_line

        print(message, flush=True)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        req_path = parsed.path or "/"
        params = parse_qs(parsed.query)
        auth_header = self.headers.get("Authorization")
        token_param = params.get("token", [None])[0]
        cookie_header = self.headers.get("Cookie")

        if admin_security_enabled() and (
            req_path in {"/admin", "/admin/"} or req_path.startswith("/api/admin/")
        ):
            self._maybe_record_admin_login_attempt(auth_header, token_param, cookie_header)
            if self._reject_if_admin_ip_locked(want_json=req_path.startswith("/api/admin/")):
                return

        if req_path in {"/admin", "/admin/"} and should_issue_admin_session_cookie(token_param):
            set_cookie = build_admin_session_set_cookie(self.headers.get("X-Forwarded-Proto"))
            if set_cookie:
                self._note_admin_auth_success()
                self.send_response(302)
                self.send_header("Location", "/admin")
                self.send_header("Set-Cookie", set_cookie)
                self.end_headers()
                return

        if req_path in {"/admin", "/admin/"} and os.environ.get("ADMIN_TOKEN", "").strip():
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                self._send_admin_unauthorized_page()
                return

        if req_path.startswith("/api/admin/"):
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                body = json.dumps({"error": "Forbidden"}).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._note_admin_auth_success()

            if req_path.rstrip("/").endswith("/stats"):
                payload = system_stats_payload()
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/logs":
                stream = params.get("stream", ["server"])[0]
                log_path = POLLER_LOG_PATH if stream == "poller" else SERVER_LOG_PATH
                fmt = (params.get("format", ["text"])[0] or "text").strip().lower()
                if fmt == "json":
                    level = params.get("level", ["all"])[0]
                    q = params.get("q", [""])[0]
                    since = params.get("since", ["session"])[0]
                    limit_raw = params.get("limit", ["2000"])[0]
                    cursor_raw = params.get("cursor", [None])[0]
                    counts_raw = params.get("counts", ["1"])[0]
                    try:
                        limit = int(limit_raw)
                    except ValueError:
                        limit = 2000
                    cursor = None
                    if cursor_raw is not None:
                        try:
                            cursor = int(cursor_raw)
                        except ValueError:
                            cursor = None
                    include_counts = str(counts_raw).strip() not in {"0", "false", "no"}
                    payload = query_log_entries(
                        log_path,
                        limit=limit,
                        level=level,
                        q=q,
                        cursor=cursor,
                        include_counts=include_counts,
                        since=since,
                    )
                    body = json.dumps(payload, allow_nan=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
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

        if parsed.path == "/api/stats":
            # Moved to /api/admin/stats (admin-protected).
            body = json.dumps({"error": "Not found"}).encode("utf-8")
            self.send_response(404)
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
            self._note_admin_auth_success()
            q = parsed.query
            self.path = "/admin.html" + (f"?{q}" if q else "")

        if parsed.path in {"/", ""}:
            self.path = "/index.html"

        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        auth_header = self.headers.get("Authorization")
        token_param = params.get("token", [None])[0]
        cookie_header = self.headers.get("Cookie")

        if parsed.path.startswith("/api/admin/"):
            if admin_security_enabled():
                self._maybe_record_admin_login_attempt(auth_header, token_param, cookie_header)
                if self._reject_if_admin_ip_locked(want_json=True):
                    return
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                body = json.dumps({"error": "Forbidden"}).encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._note_admin_auth_success()

            if parsed.path == "/api/admin/clear-data":
                payload = clear_market_data(
                    listings_cache_path=LISTINGS_CACHE_PATH,
                    csv_path=CSV_PATH,
                )
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/restart-poller":
                try:
                    payload = restart_poller()
                    body = json.dumps(payload, allow_nan=False).encode("utf-8")
                    self.send_response(200 if payload.get("ok") else 500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return

            if parsed.path == "/api/admin/stop-poller":
                try:
                    payload = stop_poller()
                    body = json.dumps(payload, allow_nan=False).encode("utf-8")
                    self.send_response(200 if payload.get("ok") else 500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                return

            body = json.dumps({"error": "Unknown admin endpoint"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/config":
            if admin_security_enabled():
                self._maybe_record_admin_login_attempt(auth_header, token_param, cookie_header)
                if self._reject_if_admin_ip_locked(want_json=True):
                    return
                if not admin_authorized(auth_header, token_param, cookie_header):
                    self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                    body = json.dumps({"error": "Forbidden"}).encode("utf-8")
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._note_admin_auth_success()
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
