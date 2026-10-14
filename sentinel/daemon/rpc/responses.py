"""Construct protocol responses shared by the server and request router."""
from __future__ import annotations

from typing import Any

from sentinel.daemon.rpc.protocol import PROTOCOL_VERSION


def _success(
    request_id: str | None,
    result: dict[str, Any],
) -> dict[str, Any]:

    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


def _error(
    request_id: str | None,
    code: str,
    message: str,
) -> dict[str, Any]:

    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
