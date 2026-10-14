from __future__ import annotations

import json
from typing import Any


PROTOCOL_VERSION = 1

MAX_MESSAGE_BYTES = 64 * 1024


class ProtocolError(ValueError):
    """Raised when a sentineld message is invalid."""


def encode_message(payload: dict[str, Any]) -> bytes:
    """
    Encode a sentineld message as newline-terminated JSON.
    """

    try:
        data = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    except (TypeError, ValueError) as exc:
        raise ProtocolError("Message is not JSON serializable.") from exc

    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Message is too large.")

    return data + b"\n"


def decode_message(data: bytes) -> dict[str, Any]:
    """
    Decode and validate a sentineld JSON message.
    """

    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Message is too large.")

    try:
        payload = json.loads(data.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Invalid JSON message.") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("Message must be a JSON object.")

    version = payload.get("version")

    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"Unsupported protocol version: {version!r}")

    return payload
