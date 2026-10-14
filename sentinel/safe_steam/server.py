"""Loopback policy, extension distribution and write-only activity ingestion, owned by sentineld.

This module no longer launches an independent settings server. The daemon
binds the port before accepting RPC clients and refuses a competing listener.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from sentinel.daemon.operations import browser_ops
from sentinel.activity.collectors import browser_event
from sentinel.activity.constants import ACTIVITY_EXTENSION_ID
from sentinel.extension_catalog.catalog import CATALOG_ENDPOINT, applied_catalog, catalog_extension_id
from sentinel.settings.paths import CATALOG_DISTRIBUTION_DIR, NEWTAB_DISTRIBUTION_DIR
from sentinel.settings.paths import EXTENSIONS_DIR
from sentinel.dns.storage import protect_directory
from sentinel.settings.paths import SAFE_STEAM_DISTRIBUTION_DIR, SAFE_CRUNCHYROLL_DISTRIBUTION_DIR

HOST = "127.0.0.1"
PORT = 8765
ACTIVITY_ORIGINS = {
    f"chrome-extension://{ACTIVITY_EXTENSION_ID}": "browser",
    "chrome-extension://jdfobdpedcmlbmolbdfaoaidldkjjfnh": "safe_steam",
    "chrome-extension://njcmpekfomicmkpnaihnkohgpoeooeoa": "safe_crunchyroll",
}
FILES = {
    "/extensions/newtab/updates.xml": (NEWTAB_DISTRIBUTION_DIR / "updates.xml", "application/xml"),
    "/extensions/newtab/sentinel_newtab.crx": (NEWTAB_DISTRIBUTION_DIR / "sentinel_newtab.crx", "application/x-chrome-extension"),
    "/extensions/catalog/updates.xml": (CATALOG_DISTRIBUTION_DIR / "updates.xml", "application/xml"),
    "/extensions/catalog/sentinel_extensions.crx": (CATALOG_DISTRIBUTION_DIR / "sentinel_extensions.crx", "application/x-chrome-extension"),
    "/extensions/activity/updates.xml": (EXTENSIONS_DIR / "activity" / "updates.xml", "application/xml"),
    "/extensions/activity/sentinel_activity.crx": (EXTENSIONS_DIR / "activity" / "sentinel_activity.crx", "application/x-chrome-extension"),
    "/extensions/safe-steam/updates.xml": (SAFE_STEAM_DISTRIBUTION_DIR / "updates.xml", "application/xml"),
    "/extensions/safe-steam/browser_extension.crx": (SAFE_STEAM_DISTRIBUTION_DIR / "browser_extension.crx", "application/x-chrome-extension"),
    "/extensions/safe-crunchyroll/updates.xml": (SAFE_CRUNCHYROLL_DISTRIBUTION_DIR / "updates.xml", "application/xml"),
    "/extensions/safe-crunchyroll/safe_crunchyroll.crx": (SAFE_CRUNCHYROLL_DISTRIBUTION_DIR / "safe_crunchyroll.crx", "application/x-chrome-extension"),
}


def read_asset(path: Path) -> bytes:
    protect_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f"Unprotected extension asset: {path}")
        return stream.read()


class SentinelExtensionHandler(BaseHTTPRequestHandler):
    def handle(self):
        self.connection.settimeout(3.0)
        super().handle()

    def activity_origin(self):
        origin = self.headers.get("Origin")
        if (self.headers.get("Host") != f"{HOST}:{self.server.server_port}"
                or self.path != "/api/activity" or origin not in ACTIVITY_ORIGINS):
            self.send_error(403, "Activity origin denied")
            return None
        return origin

    def do_OPTIONS(self):
        if self.path in {CATALOG_ENDPOINT + "/approve", CATALOG_ENDPOINT + "/revoke"}:
            origin = self.catalog_origin()
            if not origin:
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        origin = self.activity_origin()
        if not origin:
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path in {CATALOG_ENDPOINT + "/approve", CATALOG_ENDPOINT + "/revoke"}:
            self.catalog_mutation()
            return
        origin = self.activity_origin()
        if not origin:
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                raise ValueError("Expected JSON with Content-Length")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384:
                raise ValueError("Activity request is too large or empty")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an activity object")
            result = browser_event(data, ACTIVITY_ORIGINS[origin])
        except (ValueError, UnicodeError):
            self.send_error(400, "Invalid activity event")
            return
        except OSError:
            self.send_error(503, "Activity storage unavailable")
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def catalog_origin(self):
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            self.send_error(403, "Use the loopback IP address")
            return None
        try:
            expected = f"chrome-extension://{catalog_extension_id()}"
        except (OSError, ValueError):
            self.send_error(503, "Sentinel Extensions is unavailable")
            return None
        if self.headers.get("Origin") != expected:
            self.send_error(403, "Catalog origin denied")
            return None
        return expected

    def catalog_mutation(self):
        origin = self.catalog_origin()
        if not origin:
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                raise ValueError("Expected JSON")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or set(data) != {"extension_id", "guardian_pin"}:
                raise ValueError("Invalid approval request")
        except (ValueError, UnicodeError):
            self.send_error(400, "Invalid extension approval request")
            return
        # Use the same serialized authentication/mutation path as Unix RPC.
        # This is a narrow adapter, never a general HTTP gateway to the daemon.
        from sentinel.daemon.rpc.request_handler import _handle_request
        operation = self.path.rsplit("/", 1)[-1]
        response = _handle_request({"request_id": secrets.token_hex(16),
            "action": f"browser.extension.{operation}", "params": data}, uid=-1, pid=-1)
        status = 200
        if not response["ok"]:
            code = response["error"]["code"]
            status = 403 if code == "permission_denied" else 409 if code == "daemon_busy" else 503
            if status == 503:
                response["error"]["message"] = "Could not update extension approval. Try Refresh."
        body = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Reject DNS rebinding names. GET exposes policy and signed assets only.
        if self.headers.get("Host") != f"{HOST}:{self.server.server_port}":
            self.send_error(403, "Use the loopback IP address")
            return
        path = urlparse(self.path).path
        if path == CATALOG_ENDPOINT:
            try:
                response = applied_catalog()
            except (OSError, ValueError, TypeError, AttributeError):
                self.send_error(503, "Approved extensions are unavailable")
                return
            self.send_body(json.dumps(response).encode(), "application/json", cors=True)
            return
        settings_key = {"/api/safe-steam": "safe_steam",
                        "/api/safe-crunchyroll": "safe_crunchyroll"}.get(path)
        if settings_key or path == "/api/protections":
            try:
                settings = browser_ops.get_settings()
                if settings_key == "safe_steam":
                    from sentinel.safe_steam.status import observe
                    observe(self.headers.get("Origin"))
                response = {settings_key: settings[settings_key]} if settings_key else settings
            except (OSError, ValueError):
                self.send_error(503, "Sentinel policy is unavailable")
                return
            self.send_body(json.dumps(response).encode(), "application/json", cors=True)
            return
        if path in FILES:
            asset, content_type = FILES[path]
            try:
                body = read_asset(asset)
            except FileNotFoundError:
                self.send_error(404, "Extension asset not installed")
                return
            except (OSError, ValueError):
                self.send_error(503, "Extension asset is unavailable")
                return
            self.send_body(body, content_type)
            return
        self.send_error(404, "Not Found")

    def send_body(self, body: bytes, content_type: str, *, cors: bool = False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid logging page URLs or browser history.
        pass


class ExtensionHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, address):
        super().__init__(address, SentinelExtensionHandler)
        self._slots = threading.BoundedSemaphore(16)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return

        def serve():
            try:
                self.finish_request(request, client_address)
            except (OSError, ValueError):
                pass
            finally:
                self.shutdown_request(request)
                self._slots.release()
        try:
            threading.Thread(target=serve, name="sentineld-extension-request", daemon=True).start()
        except Exception:
            self._slots.release()
            self.shutdown_request(request)
            raise


def start_server() -> tuple[ExtensionHTTPServer, threading.Thread]:
    server = ExtensionHTTPServer((HOST, PORT))
    thread = threading.Thread(target=server.serve_forever, name="sentineld-extensions", daemon=True)
    try:
        thread.start()
    except Exception:
        server.server_close()
        raise
    return server, thread
