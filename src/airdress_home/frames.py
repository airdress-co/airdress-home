"""The closed frame set, and the verification of the operator's frames.

Operator frames (signed):

* ``hello`` — ``session``, ``protocol``, ``transport``, ``operatorKid``;
* ``features`` — ``events: [{name, types: [..]}]``, ``notify: {enabled}``,
  ``trackers: [{name}]``: what the operator's ``Home`` declares;
* ``call`` — ``callId``, ``action`` (``domain.service``), ``targets`` (entity
  ids), ``data`` (an object or null), ``function``; answered by ``result``;
* ``read`` — ``readId``, ``entity``, ``function``; answered by ``read_result``;
* ``emit`` — ``emitId``, ``event``, ``eventType``, ``data`` (an object or
  null), ``function``; not answered;
* ``track`` and ``notify_result``.

Hub frames (unsigned; the channel is machine-signed): ``shared``, ``result``,
``read_result``, ``state``, ``entity_event``, ``notify``.

Every operator frame also carries ``session``, ``seq`` and ``notAfter``.

The operator sends every frame as a signed envelope::

    {"type": "call", "frame": "<the frame as JSON text>", "sig": "<base64url>"}

``sig`` is Ed25519 by the pinned operator key over
``b"airdress-home-" + type + b"-v1\\x00" + frame``. The frame text is verified
exactly as it arrived and only then parsed, so no canonical JSON is needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .codes import b64url_decode
from .errors import ProtocolError
from .models import (
    MAX_EVENT_TYPES,
    MAX_EVENTS,
    EventDeclaration,
    Features,
    Shared,
    valid_name,
)

SUBPROTOCOL = "airdress.home.v1"
PROTOCOL_VERSION = 1

OPERATOR_TYPES = frozenset({"hello", "features", "call", "emit", "read", "track", "notify_result"})
HUB_TYPES = frozenset({"shared", "result", "read_result", "state", "entity_event", "notify"})
CONTROL_TYPES = frozenset({"keepalive", "ack"})


def signed_bytes(frame_type: str, frame: str) -> bytes:
    """The bytes a frame signature covers."""
    return b"airdress-home-" + frame_type.encode() + b"-v1\x00" + frame.encode()


@dataclass(frozen=True)
class OperatorFrame:
    """A verified operator frame."""

    type: str
    body: dict[str, Any]

    @property
    def seq(self) -> int:
        return int(self.body["seq"])

    @property
    def session(self) -> str:
        return str(self.body["session"])

    @property
    def not_after(self) -> int:
        return int(self.body["notAfter"])


def parse_line(line: str | bytes) -> dict[str, Any] | None:
    """One line as JSON; ``None`` for a blank line."""
    text = line.decode() if isinstance(line, bytes) else line
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError as e:
        raise ProtocolError("a line that is not JSON") from e
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise ProtocolError("a line without a type")
    return value


def verify(envelope: dict[str, Any], operator_key: bytes) -> OperatorFrame:
    """Verify a signed envelope against the pinned key, and parse its frame."""
    frame_type = envelope.get("type")
    frame = envelope.get("frame")
    sig = envelope.get("sig")
    if frame_type not in OPERATOR_TYPES:
        raise ProtocolError(f"not an operator frame type: {frame_type!r}")
    if not isinstance(frame, str) or not isinstance(sig, str):
        raise ProtocolError("an unsigned operator frame")
    try:
        Ed25519PublicKey.from_public_bytes(operator_key).verify(
            b64url_decode(sig), signed_bytes(frame_type, frame)
        )
    except (InvalidSignature, ValueError) as e:
        raise ProtocolError("an operator frame whose signature does not verify") from e
    body = json.loads(frame)
    if not isinstance(body, dict) or not isinstance(body.get("seq"), int):
        raise ProtocolError("an operator frame without a seq")
    return OperatorFrame(type=frame_type, body=body)


def result(call_id: str, outcome: str, ha_ms: float, response: Any = None) -> dict[str, Any]:
    """A ``result`` frame."""
    return {
        "type": "result",
        "callId": call_id,
        "outcome": outcome,
        "response": response,
        "haMs": round(ha_ms, 3),
    }


def read_result(
    read_id: str,
    outcome: str,
    ha_ms: float,
    state: str | None = None,
    last_changed: str | None = None,
) -> dict[str, Any]:
    """A ``read_result`` frame."""
    return {
        "type": "read_result",
        "readId": read_id,
        "outcome": outcome,
        "state": state,
        "lastChanged": last_changed,
        "haMs": round(ha_ms, 3),
    }


def shared(value: Shared) -> dict[str, Any]:
    """A ``shared`` frame. Operate implies observe, so every operate entity is
    listed at observe too."""
    observe = {e.entity: e for e in value.observe}
    for e in value.operate:
        observe.setdefault(e.entity, e)
    return {
        "type": "shared",
        "integrationVersion": value.integration_version,
        "haVersion": value.hub_version,
        "operate": [e.to_wire() for e in value.operate],
        "observe": [e.to_wire() for e in observe.values()],
    }


def features(body: dict[str, Any]) -> Features:
    """The declarations of a verified ``features`` frame.

    A malformed declaration is left out and counted, never raised: the frame
    is signed by the operator, and one bad entry must not end the channel.
    """
    dropped = 0
    events: list[EventDeclaration] = []
    seen: set[str] = set()
    raw_events = body.get("events")
    for raw in raw_events if isinstance(raw_events, list) else []:
        name = raw.get("name") if isinstance(raw, dict) else None
        types = raw.get("types") if isinstance(raw, dict) else None
        if (
            not valid_name(name)
            or name in seen
            or not isinstance(types, list)
            or not types
            or len(types) > MAX_EVENT_TYPES
            or not all(valid_name(t) for t in types)
            or len(events) >= MAX_EVENTS
        ):
            dropped += 1
            continue
        seen.add(name)
        events.append(EventDeclaration(name=name, types=tuple(dict.fromkeys(types))))
    notify = body.get("notify")
    trackers = body.get("trackers")
    return Features(
        events=tuple(events),
        notify=isinstance(notify, dict) and notify.get("enabled") is True,
        trackers=tuple(
            t["name"]
            for t in (trackers if isinstance(trackers, list) else [])
            if isinstance(t, dict) and valid_name(t.get("name"))
        ),
        dropped=dropped,
    )
