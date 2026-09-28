"""What worked where: the small, persisted transport hint.

A hub remembers, per network, which transport last carried its channel, so a
hub on a network that refuses WebSocket upgrades does not pay for a refused
upgrade on every reconnect. A hint is only a starting point: the preferred
transport is probed again once the hint is old enough (see
:class:`~airdress_home.channel.negotiate.NegotiatingChannel`).

A network is named by :func:`network_key`: a digest of the operator's origin
and the prefix of the local address the hub reaches it from. The hint file
therefore holds no address, and moving the hub to another network (or the
operator to another origin) starts from the default order again.

Stores are asynchronous so that an application whose event loop must not
block (Home Assistant's) can keep the hint in its own storage.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

#: How many networks a store remembers; the least recently confirmed go first.
MAX_NETWORKS = 16


@dataclass(frozen=True)
class Hint:
    """The transport that last worked on one network.

    ``since`` is the wall-clock time (seconds) the hint was last confirmed or
    changed, and ``preferred_failed`` the time the preferred transport last
    failed here (``None`` if it never has, or has since recovered).
    """

    transport: str
    since: float
    preferred_failed: float | None = None

    def to_json(self) -> dict[str, Any]:
        """The hint as stored."""
        out: dict[str, Any] = {"transport": self.transport, "since": round(self.since, 3)}
        if self.preferred_failed is not None:
            out["preferredFailed"] = round(self.preferred_failed, 3)
        return out

    @classmethod
    def from_json(cls, raw: object) -> Hint | None:
        """A stored hint, or ``None`` for anything malformed."""
        if not isinstance(raw, dict):
            return None
        transport, since = raw.get("transport"), raw.get("since")
        failed = raw.get("preferredFailed")
        if not isinstance(transport, str) or not isinstance(since, int | float):
            return None
        if failed is not None and not isinstance(failed, int | float):
            return None
        return cls(transport, float(since), None if failed is None else float(failed))


class HintStore(Protocol):
    """Where hints live between runs."""

    async def load(self) -> dict[str, Hint]:
        """Every stored hint, by network key. Never raises for a missing store."""
        ...

    async def save(self, hints: dict[str, Hint]) -> None:
        """Replace the stored hints."""
        ...


def _bounded(hints: dict[str, Hint]) -> dict[str, Hint]:
    newest = sorted(hints.items(), key=lambda kv: kv[1].since, reverse=True)
    return dict(newest[:MAX_NETWORKS])


class MemoryHintStore:
    """Hints for this process only."""

    def __init__(self) -> None:
        self.hints: dict[str, Hint] = {}

    async def load(self) -> dict[str, Hint]:
        return dict(self.hints)

    async def save(self, hints: dict[str, Hint]) -> None:
        self.hints = _bounded(hints)


class FileHintStore:
    """Hints in one small JSON file, written atomically off the event loop."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Hint]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Hint] = {}
        for key, value in raw.items():
            if isinstance(key, str) and (hint := Hint.from_json(value)) is not None:
                out[key] = hint
        return out

    def _write(self, hints: dict[str, Hint]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({k: v.to_json() for k, v in _bounded(hints).items()}, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    async def load(self) -> dict[str, Hint]:
        return await asyncio.to_thread(self._read)

    async def save(self, hints: dict[str, Hint]) -> None:
        await asyncio.to_thread(self._write, hints)


def _local_prefix(host: str, port: int) -> str:
    """The prefix of the local address that routes to ``host``: the /24 of an
    IPv4 address, the /64 of an IPv6 one. A UDP ``connect`` sends nothing."""
    for family, _, _, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM):
        with contextlib.suppress(OSError), socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            local = ipaddress.ip_address(s.getsockname()[0])
            bits = 24 if local.version == 4 else 64
            return str(ipaddress.ip_network(f"{local}/{bits}", strict=False))
    return "unknown"


async def network_key(origin: str) -> str:
    """A key for "this network, towards this operator", holding no address.

    Falls back to a key for the origin alone when the route cannot be read.
    """
    parts = urlsplit(origin)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        prefix = await asyncio.to_thread(_local_prefix, host, port)
    except OSError:
        prefix = "unknown"
    digest = hashlib.sha256(f"{origin}\x1f{prefix}".encode()).hexdigest()
    return digest[:16]
