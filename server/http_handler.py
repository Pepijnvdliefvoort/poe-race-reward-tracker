from __future__ import annotations

import json
import os
import requests
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from pathlib import Path
from datetime import datetime, timezone

from .admin_service import (
    POLLER_LOG_PATH,
    SERVER_LOG_PATH,
    admin_authorized,
    admin_credential_material_present,
    admin_lockout_retry_after_seconds,
    admin_note_auth_failure,
    admin_note_auth_success,
    admin_security_enabled,
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
from .data_service import (
    ROOT_DIR,
    WEB_DIR,
    fetch_listing_preview,
    load_config,
    load_price_data,
    save_config,
)
from .db_admin_service import db_overview, er_schema, list_tables, preview_table, run_query, table_details
from .recommendation_service import RecommendationInputError, recommend_investments

from storage.db import Database
from storage.service import StorageService
from .stats_service import system_stats_payload
from server.storage_service import ServerStorage
from poller.db_export import DbExportConfig, export_db_to_discord_now

_ADMIN_UNAUTHORIZED_HTML = WEB_DIR / "admin-unauthorized.html"
_ERROR_HTML = WEB_DIR / "error.html"
_ERROR_COPY: dict[int, tuple[str, str]] = {
    400: ("Bad request", "The request could not be understood."),
    401: ("Access denied", "You don't have permission to view this page."),
    403: ("Forbidden", "You don't have permission to view this resource."),
    404: ("Page not found", "The page or resource you requested does not exist."),
    405: ("Method not allowed", "This route does not support the requested HTTP method."),
    410: ("Gone", "This resource is no longer available."),
    429: ("Too many requests", "Too many failed attempts. Try again later."),
    500: ("Server error", "Something went wrong on the server."),
    502: ("Bad gateway", "The upstream service did not return a usable response."),
}


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), DashboardHandler)


def _load_discord_db_export_webhook_url_from_env() -> str:
    for env_name in ("DISCORD_WEBHOOK_URL_DB_EXPORT", "POE_DISCORD_WEBHOOK_URL_DB_EXPORT"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Client disconnected mid-response (refresh/navigation/proxy timeout).
            # Treat as a normal cancellation and avoid emitting a traceback.
            return

    def _client_ip(self) -> str:
        return get_client_ip(self.headers.get("X-Forwarded-For"), self.client_address[0])

    def _send_error_page(
        self,
        status_code: int,
        *,
        title: str | None = None,
        message: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        default_title, default_message = _ERROR_COPY.get(status_code, ("Request failed", "The request failed."))
        page_title = title or default_title
        page_message = message or default_message
        if _ERROR_HTML.is_file():
            html = _ERROR_HTML.read_text(encoding="utf-8")
            html = html.replace("{{status_code}}", escape(str(status_code)))
            html = html.replace("{{title}}", escape(page_title))
            html = html.replace("{{message}}", escape(page_message))
            body = html.encode("utf-8")
        else:
            fallback = (
                "<!DOCTYPE html><html><head><meta charset=utf-8>"
                f"<title>{escape(str(status_code))}</title></head><body>"
                f"<h1>{escape(page_title)}</h1><p>{escape(page_message)}</p></body></html>"
            )
            body = fallback.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Render SimpleHTTPRequestHandler fallback errors with the app error page."""
        self._send_error_page(code)

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
        self._send_error_page(
            429,
            message="Too many failed authentication attempts. Try again later.",
            extra_headers={"Retry-After": str(retry_after)},
        )
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
            req_path in {"/admin", "/admin/", "/admin/db", "/admin/db/"} or req_path.startswith("/api/admin/")
        ):
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

        if req_path in {"/admin/db", "/admin/db/"} and os.environ.get("ADMIN_TOKEN", "").strip():
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                self._send_admin_unauthorized_page()
                return

        # Prevent direct access to the underlying admin HTML file.
        # The canonical route is /admin/db (which is auth-protected).
        if req_path in {"/db.html"} and os.environ.get("ADMIN_TOKEN", "").strip():
            if not admin_authorized(auth_header, token_param, cookie_header):
                self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                self._send_admin_unauthorized_page()
                return
            q = parsed.query
            loc = "/admin/db" + (f"?{q}" if q else "")
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
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

            if req_path == "/api/admin/db/overview":
                payload = db_overview(root_dir=ROOT_DIR)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/db/tables":
                payload = list_tables(root_dir=ROOT_DIR)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/db/er":
                payload = er_schema(root_dir=ROOT_DIR)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/db/table":
                name = (params.get("name", [""])[0] or "").strip()
                payload = table_details(root_dir=ROOT_DIR, name=name)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/db/preview":
                name = (params.get("name", [""])[0] or "").strip()
                limit_raw = params.get("limit", ["100"])[0]
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 100
                payload = preview_table(root_dir=ROOT_DIR, name=name, limit=limit)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/market/variants-sales":
                # List item variants with non-reverted sales counts (for admin tooling).
                try:
                    Database(ROOT_DIR).ensure_initialized()
                except Exception:
                    pass
                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    rows = con.execute(
                        """
                        SELECT
                          v.id AS variant_id,
                          v.display_name AS display_name,
                          v.mode AS mode,
                          i.name AS base_item_name,
                          (
                            SELECT COUNT(*)
                            FROM sales s
                            WHERE s.item_variant_id = v.id
                              AND s.reverted_at_utc IS NULL
                          ) AS sales_count
                        FROM item_variants v
                        JOIN items i ON i.id = v.item_id
                        ORDER BY v.display_name ASC, v.mode ASC
                        """
                    ).fetchall()
                    variants = []
                    for r in rows:
                        variants.append(
                            {
                                "variantId": int(r["variant_id"]),
                                "displayName": str(r["display_name"] or ""),
                                "mode": str(r["mode"] or ""),
                                "baseItemName": str(r["base_item_name"] or ""),
                                "salesCount": int(r["sales_count"] or 0),
                            }
                        )
                finally:
                    con.close()
                body = json.dumps({"ok": True, "variants": variants}, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/app-config":
                # List available app_config keys.
                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    rows = con.execute("SELECT key, updated_at_utc FROM app_config ORDER BY key ASC").fetchall()
                    items = [{"key": str(r["key"]), "updated_at_utc": str(r["updated_at_utc"] or "")} for r in rows]
                finally:
                    con.close()
                body = json.dumps({"ok": True, "items": items}, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/app-config/get":
                key = (params.get("key", [""])[0] or "").strip()
                if not key:
                    body = json.dumps({"ok": False, "error": "Missing key"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    row = con.execute(
                        "SELECT key, value_json, updated_at_utc FROM app_config WHERE key = ?",
                        (key,),
                    ).fetchone()
                finally:
                    con.close()
                if not row:
                    # Fresh clones won't have any app_config rows yet. For the primary "market"
                    # config, fall back to the same defaults as `/api/config`, and persist them
                    # so the admin editor can load immediately.
                    if key == "market":
                        try:
                            _ = load_config()
                        except Exception:
                            pass
                        con2 = storage.connect()
                        try:
                            row = con2.execute(
                                "SELECT key, value_json, updated_at_utc FROM app_config WHERE key = ?",
                                (key,),
                            ).fetchone()
                        finally:
                            con2.close()
                        if row:
                            raw = str(row["value_json"] or "")
                            try:
                                parsed = json.loads(raw)
                                raw = json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2)
                            except Exception:
                                pass
                            payload = {
                                "ok": True,
                                "key": str(row["key"]),
                                "value_json": raw,
                                "updated_at_utc": str(row["updated_at_utc"] or ""),
                            }
                            body = json.dumps(payload, allow_nan=False).encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                    body = json.dumps({"ok": False, "error": "Not found"}).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                raw = str(row["value_json"] or "")
                # Pretty-print JSON when possible (so the editor has readable defaults).
                try:
                    parsed = json.loads(raw)
                    raw = json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2)
                except Exception:
                    pass
                payload = {"ok": True, "key": str(row["key"]), "value_json": raw, "updated_at_utc": str(row["updated_at_utc"] or "")}
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path.rstrip("/").endswith("/stats"):
                payload = system_stats_payload()
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                # Avoid surfacing a 500 for expected local-dev conditions (e.g. missing psutil).
                # The UI can still render a helpful error message from the JSON payload.
                self.send_response(200)
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
                body = json.dumps({"error": "CSV export has been removed (SQLite is the source of truth)."}).encode(
                    "utf-8"
                )
                self.send_response(410)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if req_path == "/api/admin/download/market.db":
                db_path = ServerStorage(ROOT_DIR).db_path
                if not db_path.is_file():
                    body = json.dumps({"error": "DB file not found"}).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                try:
                    size = db_path.stat().st_size
                except OSError:
                    size = 0

                # Use a stable filename so users can overwrite local copies easily.
                filename = "market.db"
                self.send_response(200)
                self.send_header("Content-Type", "application/x-sqlite3")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                if size:
                    self.send_header("Content-Length", str(size))
                self.end_headers()
                with db_path.open("rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
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

        if parsed.path == "/api/companion/auth":
            if admin_security_enabled():
                if self._reject_if_admin_ip_locked(want_json=True):
                    return
                authorized = admin_authorized(auth_header, token_param, cookie_header)
                if authorized:
                    self._note_admin_auth_success()
            else:
                authorized = True
            payload = {"ok": True, "authenticated": authorized}
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(200)
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

        if parsed.path in {"/error", "/error.html"}:
            try:
                status_code = int(params.get("status", ["404"])[0])
            except ValueError:
                status_code = 404
            if status_code not in _ERROR_COPY:
                status_code = 404
            self._send_error_page(status_code)
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

        if parsed.path in {"/admin/db", "/admin/db/"}:
            self._note_admin_auth_success()
            q = parsed.query
            self.path = "/db.html" + (f"?{q}" if q else "")

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

            if parsed.path == "/api/admin/db/query":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                sql = (data.get("sql") if isinstance(data, dict) else "") or ""
                limit_raw = (data.get("limit") if isinstance(data, dict) else None) or 200
                try:
                    limit = int(limit_raw)
                except Exception:
                    limit = 200
                payload = run_query(root_dir=ROOT_DIR, sql=str(sql), limit=limit)
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200 if payload.get("ok") else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/app-config/set":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                key = str(data.get("key") or "").strip()
                value_raw = data.get("value_json")
                value_raw = str(value_raw or "").strip()
                if not key:
                    body = json.dumps({"ok": False, "error": "Missing key"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not value_raw:
                    body = json.dumps({"ok": False, "error": "Missing value_json"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    parsed_json = json.loads(value_raw)
                    normalized = json.dumps(parsed_json, ensure_ascii=False, sort_keys=True, indent=2)
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"ok": False, "error": f"Invalid JSON: {exc}"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                updated_at = datetime.now(timezone.utc).isoformat()
                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    con.execute(
                        """
                        INSERT INTO app_config(key, value_json, updated_at_utc)
                        VALUES(?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                          value_json=excluded.value_json,
                          updated_at_utc=excluded.updated_at_utc
                        """,
                        (key, normalized, updated_at),
                    )
                    con.commit()
                finally:
                    con.close()
                body = json.dumps({"ok": True, "key": key, "value_json": normalized, "updated_at_utc": updated_at}, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/clear-data":
                # SQLite is authoritative. Clear it first.
                try:
                    Database(ROOT_DIR).ensure_initialized()
                except Exception:
                    pass
                payload = clear_market_data(
                    listings_cache_path=Path("web") / "listings_cache.json",
                    csv_path=Path("price_poll.csv"),
                )
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/sales/delete":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}

                scope = str(data.get("scope") or "variant").strip().lower()
                vid_raw = data.get("variantId")
                try:
                    variant_id = int(vid_raw)
                except Exception:
                    variant_id = 0

                if variant_id <= 0:
                    body = json.dumps({"ok": False, "error": "variantId is required"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                try:
                    Database(ROOT_DIR).ensure_initialized()
                except Exception:
                    pass

                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    variant_ids: list[int]
                    if scope == "item":
                        row = con.execute("SELECT item_id FROM item_variants WHERE id = ?", (variant_id,)).fetchone()
                        if not row:
                            body = json.dumps({"ok": False, "error": "Variant not found"}).encode("utf-8")
                            self.send_response(404)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        item_id = int(row["item_id"])
                        ids = con.execute("SELECT id FROM item_variants WHERE item_id = ?", (item_id,)).fetchall()
                        variant_ids = [int(r["id"]) for r in ids]
                    else:
                        variant_ids = [variant_id]

                    deleted_total = 0
                    deleted_by_variant: dict[str, int] = {}

                    for vid in variant_ids:
                        cnt_row = con.execute(
                            "SELECT COUNT(*) AS n FROM sales WHERE item_variant_id = ? AND reverted_at_utc IS NULL",
                            (vid,),
                        ).fetchone()
                        before = int(cnt_row["n"] or 0) if cnt_row else 0
                        con.execute("DELETE FROM sales WHERE item_variant_id = ?", (vid,))
                        deleted_by_variant[str(vid)] = before
                        deleted_total += before

                    con.commit()
                finally:
                    con.close()

                body = json.dumps(
                    {"ok": True, "scope": scope, "variantIds": variant_ids, "deleted": deleted_total, "deletedByVariantId": deleted_by_variant},
                    allow_nan=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/inference/reset-counters":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}

                vid_raw = data.get("variantId")
                try:
                    variant_id = int(vid_raw)
                except Exception:
                    variant_id = 0

                if variant_id <= 0:
                    body = json.dumps({"ok": False, "error": "variantId is required"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                try:
                    Database(ROOT_DIR).ensure_initialized()
                except Exception:
                    pass

                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    row = con.execute(
                        "SELECT COUNT(*) AS n FROM item_polls WHERE item_variant_id = ?",
                        (variant_id,),
                    ).fetchone()
                    polls = int(row["n"] or 0) if row else 0
                    con.execute(
                        """
                        UPDATE item_polls
                        SET
                          inf_confirmed_transfer = 0,
                          inf_likely_instant_sale = 0,
                          inf_likely_non_instant_online = 0,
                          inf_relist_same_seller = 0,
                          inf_non_instant_removed = 0,
                          inf_reprice_same_seller = 0,
                          inf_multi_seller_same_fingerprint = 0,
                          inf_new_listing_rows = 0
                        WHERE item_variant_id = ?
                        """,
                        (variant_id,),
                    )
                    con.commit()
                finally:
                    con.close()

                body = json.dumps({"ok": True, "variantId": variant_id, "pollsUpdated": polls}, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/admin/market/wipe-variant":
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                try:
                    data = json.loads(raw or b"{}")
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}

                scope = str(data.get("scope") or "variant").strip().lower()
                vid_raw = data.get("variantId")
                try:
                    variant_id = int(vid_raw)
                except Exception:
                    variant_id = 0

                if variant_id <= 0:
                    body = json.dumps({"ok": False, "error": "variantId is required"}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                try:
                    Database(ROOT_DIR).ensure_initialized()
                except Exception:
                    pass

                storage = ServerStorage(ROOT_DIR)
                con = storage.connect()
                try:
                    if scope == "item":
                        row = con.execute("SELECT item_id FROM item_variants WHERE id = ?", (variant_id,)).fetchone()
                        if not row:
                            body = json.dumps({"ok": False, "error": "Variant not found"}).encode("utf-8")
                            self.send_response(404)
                            self.send_header("Content-Type", "application/json; charset=utf-8")
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        item_id = int(row["item_id"])
                        ids = con.execute("SELECT id FROM item_variants WHERE item_id = ?", (item_id,)).fetchall()
                        variant_ids = [int(r["id"]) for r in ids]
                    else:
                        variant_ids = [variant_id]

                    deleted_sales = 0
                    deleted_listing_snapshots = 0
                    deleted_inference_events = 0
                    deleted_inf_pending = 0
                    deleted_inf_signals = 0
                    polls_reset = 0

                    for vid in variant_ids:
                        # Polls for this variant (used to delete per-poll fingerprint tables).
                        poll_rows = con.execute("SELECT id FROM item_polls WHERE item_variant_id = ?", (vid,)).fetchall()
                        poll_ids = [int(r["id"]) for r in poll_rows]

                        if poll_ids:
                            qmarks = ",".join(["?"] * len(poll_ids))
                            cur = con.execute(f"DELETE FROM listing_snapshots WHERE item_poll_id IN ({qmarks})", poll_ids)
                            deleted_listing_snapshots += int(cur.rowcount or 0)
                            cur = con.execute(f"DELETE FROM inference_events WHERE item_poll_id IN ({qmarks})", poll_ids)
                            deleted_inference_events += int(cur.rowcount or 0)

                        # Delete recorded sales for this variant.
                        cur = con.execute("DELETE FROM sales WHERE item_variant_id = ?", (vid,))
                        deleted_sales += int(cur.rowcount or 0)

                        # Clear persisted inference fingerprint state for this variant.
                        cur = con.execute("DELETE FROM inference_state_pending WHERE item_variant_id = ?", (vid,))
                        deleted_inf_pending += int(cur.rowcount or 0)
                        cur = con.execute("DELETE FROM inference_state_signals WHERE item_variant_id = ?", (vid,))
                        deleted_inf_signals += int(cur.rowcount or 0)

                        # Reset inferred counters (used by “Est. sold”) but keep price history.
                        row = con.execute("SELECT COUNT(*) AS n FROM item_polls WHERE item_variant_id = ?", (vid,)).fetchone()
                        polls = int(row["n"] or 0) if row else 0
                        polls_reset += polls
                        con.execute(
                            """
                            UPDATE item_polls
                            SET
                              inf_confirmed_transfer = 0,
                              inf_likely_instant_sale = 0,
                              inf_likely_non_instant_online = 0,
                              inf_relist_same_seller = 0,
                              inf_non_instant_removed = 0,
                              inf_reprice_same_seller = 0,
                              inf_multi_seller_same_fingerprint = 0,
                              inf_new_listing_rows = 0
                            WHERE item_variant_id = ?
                            """,
                            (vid,),
                        )

                    con.commit()
                finally:
                    con.close()

                body = json.dumps(
                    {
                        "ok": True,
                        "scope": scope,
                        "variantIds": variant_ids,
                        "deleted": {
                            "sales": deleted_sales,
                            "listingSnapshots": deleted_listing_snapshots,
                            "inferenceEvents": deleted_inference_events,
                            "inferencePending": deleted_inf_pending,
                            "inferenceSignals": deleted_inf_signals,
                        },
                        "updated": {"pollsReset": polls_reset},
                    },
                    allow_nan=False,
                ).encode("utf-8")
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

            if parsed.path == "/api/admin/run-db-export":
                webhook_url = _load_discord_db_export_webhook_url_from_env()
                if not webhook_url:
                    body = json.dumps(
                        {
                            "ok": False,
                            "error": "DB export webhook is not configured. Set DISCORD_WEBHOOK_URL_DB_EXPORT.",
                        },
                        allow_nan=False,
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                try:
                    storage = StorageService(root_dir=ROOT_DIR)
                    storage.ensure_initialized()
                    with requests.Session() as session:
                        payload = export_db_to_discord_now(
                            storage=storage,
                            session=session,
                            cfg=DbExportConfig(webhook_url=webhook_url, tz_offset_minutes=120, schedule_hour=12, schedule_minute=0),
                            log=lambda *_args, **_kwargs: None,
                        )
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

        if parsed.path == "/api/companion/recommend":
            if admin_security_enabled():
                if self._reject_if_admin_ip_locked(want_json=True):
                    return
                if not admin_authorized(auth_header, token_param, cookie_header):
                    self._note_failed_admin_auth_if_applicable(auth_header, token_param, cookie_header)
                    body = json.dumps({"ok": False, "error": "Forbidden"}).encode("utf-8")
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
                data = json.loads(raw or b"{}")
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            try:
                payload = recommend_investments(data, root_dir=ROOT_DIR)
                status = 200
            except RecommendationInputError as exc:
                payload = {"ok": False, "error": str(exc)}
                status = 400
            except Exception as exc:  # noqa: BLE001
                payload = {"ok": False, "error": str(exc)}
                status = 500
            body = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/config":
            if admin_security_enabled():
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
