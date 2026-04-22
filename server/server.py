from __future__ import annotations

from .structured_logging import install_structured_logging

install_structured_logging("server", "server.log")

from .http_handler import create_server
from . import http_handler as _http_handler_mod
from .storage_service import ServerStorage

HOST = "127.0.0.1"
PORT = 8080


def main() -> None:
    server = create_server(HOST, PORT)
    print(f"Using handler module: {_http_handler_mod.__file__}")
    print(f"Serving dashboard at http://{HOST}:{PORT}")
    print(f"Reading live data from: {ServerStorage().db_path}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
