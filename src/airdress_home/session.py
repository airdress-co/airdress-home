"""The hub's side of a home session, whatever the transport.

:class:`HomeSession` keeps one channel up: it opens it, verifies every
operator frame against the **pinned** operator key, enforces the session, a
strictly increasing ``seq`` and ``notAfter``, hands ``call`` and ``read``
frames to a :class:`Handler`, and sends the answers back. When the channel
ends it reconnects with jittered exponential backoff, 1 s to 60 s.

A frame whose ``seq`` was already handled is dropped (a long-poll rotation
can repeat one); a jump in ``seq`` is counted as a gap. Neither is ever
silently ignored in the statistics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from . import frames
from .errors import ChannelClosed, HomeNotLinked, NotAuthorized, ProtocolError

if TYPE_CHECKING:
    from .channel import Channel

_LOGGER = logging.getLogger(__name__)

#: How far an operator's clock may run ahead of ours before a frame is refused.
CLOCK_SKEW = 30


class Handler(Protocol):
    """What the hub does with the operator's requests."""

    async def call(
        self, action: str, targets: list[str], data: dict[str, Any] | None, function: str
    ) -> tuple[str, Any]:
        """Run ``action`` on ``targets``; return ``(outcome, response)``."""
        ...

    async def read(self, entity: str) -> tuple[str, str | None, str | None]:
        """Read ``entity``; return ``(outcome, state, last_changed)``."""
        ...

    def shared(self) -> dict[str, Any]:
        """The ``shared`` frame body: what is exposed, at which level."""
        ...


@dataclass
class SessionStats:
    """Counters and events for measurement. Carries no entity or value."""

    connects: int = 0
    hellos: int = 0
    frames: int = 0
    duplicates: int = 0
    gaps: int = 0
    gap_frames: int = 0
    calls: int = 0
    reads: int = 0
    refused: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def event(self, kind: str, **values: Any) -> None:
        """Record one event with a wall-clock timestamp."""
        self.events.append({"t": round(time.time(), 3), "kind": kind, **values})


class HomeSession:
    """One home's channel to its operator, kept up."""

    def __init__(
        self,
        channel: Channel,
        handler: Handler,
        operator_key: bytes,
        *,
        backoff_min: float = 1.0,
        backoff_max: float = 60.0,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if len(operator_key) != 32:
            raise ValueError("the pinned operator key is 32 bytes")
        self.channel = channel
        self.handler = handler
        self.operator_key = operator_key
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.stats = SessionStats()
        self._on_event = on_event
        self._session: str | None = None
        self._last_seq = 0
        self._dropped_at: float | None = None
        self._since_drop_first_call = False
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Whether the channel is up."""
        return self._connected.is_set()

    def _event(self, kind: str, **values: Any) -> None:
        self.stats.event(kind, **values)
        if self._on_event is not None:
            self._on_event(self.stats.events[-1])

    async def run(self) -> None:
        """Keep the channel up until :meth:`stop`."""
        delay = self.backoff_min
        while not self._stop.is_set():
            opened_at = time.monotonic()
            reason = await self._run_once()
            self._connected.clear()
            if self._stop.is_set():
                break
            if self._dropped_at is None:
                self._dropped_at = time.monotonic()
                self._since_drop_first_call = False
            lived = time.monotonic() - opened_at
            self._event("drop", transport=self.channel.name, reason=reason, lived_s=round(lived, 1))
            if lived > 30:
                delay = self.backoff_min
            wait = random.uniform(delay / 2, delay)  # noqa: S311 - jitter, not crypto
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), wait)
            delay = min(self.backoff_max, delay * 2)

    async def _run_once(self) -> str:
        try:
            await self.channel.open()
        except HomeNotLinked:
            return "home_not_linked"
        except NotAuthorized as e:
            return f"not_authorized_{e.code}"
        except ChannelClosed as e:
            return e.reason
        except Exception as e:  # noqa: BLE001 - any failure to connect is retried
            return f"open_{type(e).__name__}"
        self.stats.connects += 1
        self._connected.set()
        if self._dropped_at is not None:
            self._event(
                "ready",
                transport=self.channel.name,
                since_drop_ms=round((time.monotonic() - self._dropped_at) * 1000, 1),
            )
        try:
            async for line in self.channel.lines():
                await self._on_line(line)
                if self._stop.is_set():
                    return "stopped"
        except ChannelClosed as e:
            return e.reason
        except ProtocolError as e:
            _LOGGER.warning("closing the channel: %s", e)
            return "protocol"
        except Exception as e:  # noqa: BLE001 - a dropped channel is reconnected
            return f"error_{type(e).__name__}"
        finally:
            with contextlib.suppress(Exception):
                await self.channel.close()
        return "ended"

    async def _on_line(self, line: dict[str, Any]) -> None:
        kind = line.get("type")
        if kind in frames.CONTROL_TYPES:
            return
        frame = frames.verify(line, self.operator_key)
        now = int(time.time())
        if frame.type == "hello":
            if frame.body.get("protocol") != frames.PROTOCOL_VERSION:
                raise ProtocolError("an unsupported protocol version")
            if frame.session != self._session:
                self._session = frame.session
                self._last_seq = 0
            self.channel.bind(frame.session)
            self.stats.hellos += 1
            self._event("hello", transport=self.channel.name)
        elif frame.session != self._session:
            raise ProtocolError("a frame for another session")
        if frame.seq <= self._last_seq:
            self.stats.duplicates += 1
            return
        if frame.seq > self._last_seq + 1 and frame.type != "hello":
            self.stats.gaps += 1
            self.stats.gap_frames += frame.seq - self._last_seq - 1
            self._event("gap", transport=self.channel.name, missing=frame.seq - self._last_seq - 1)
        self._last_seq = frame.seq
        self.stats.frames += 1
        self.channel.acked(frame.seq)
        if frame.type == "hello":
            await self.channel.send({"type": "shared", **self.handler.shared()})
            return
        if frame.not_after + CLOCK_SKEW < now:
            self.stats.refused += 1
            await self._answer(frame, "expired", 0.0)
            return
        if frame.type == "call":
            await self._call(frame)
        elif frame.type == "read":
            await self._read(frame)

    async def _answer(self, frame: frames.OperatorFrame, outcome: str, ms: float) -> None:
        if frame.type == "call":
            await self.channel.send(frames.result(str(frame.body["callId"]), outcome, ms))
        elif frame.type == "read":
            await self.channel.send(frames.read_result(str(frame.body["readId"]), outcome, ms))

    def _mark_first_call(self) -> None:
        if self._dropped_at is not None and not self._since_drop_first_call:
            self._since_drop_first_call = True
            self._event(
                "first_call",
                transport=self.channel.name,
                since_drop_ms=round((time.monotonic() - self._dropped_at) * 1000, 1),
            )
            self._dropped_at = None

    async def _call(self, frame: frames.OperatorFrame) -> None:
        body = frame.body
        targets = body.get("targets")
        action = body.get("action")
        if not isinstance(action, str) or not isinstance(targets, list) or not targets:
            await self._answer(frame, "rejected", 0.0)
            return
        t0 = time.monotonic()
        try:
            outcome, response = await self.handler.call(
                action, [str(t) for t in targets], body.get("data"), str(body.get("function", ""))
            )
        except Exception:  # noqa: BLE001 - reported to the operator, never raised
            outcome, response = "failed", None
        ms = (time.monotonic() - t0) * 1000
        self.stats.calls += 1
        await self.channel.send(frames.result(str(body["callId"]), outcome, ms, response))
        self._mark_first_call()

    async def _read(self, frame: frames.OperatorFrame) -> None:
        entity = frame.body.get("entity")
        t0 = time.monotonic()
        if not isinstance(entity, str):
            outcome, state, changed = "rejected", None, None
        else:
            try:
                outcome, state, changed = await self.handler.read(entity)
            except Exception:  # noqa: BLE001 - reported to the operator
                outcome, state, changed = "failed", None, None
        ms = (time.monotonic() - t0) * 1000
        self.stats.reads += 1
        await self.channel.send(
            frames.read_result(str(frame.body["readId"]), outcome, ms, state, changed)
        )

    def force_drop(self) -> None:
        """Cut the transport as a network failure would; the session reconnects."""
        self._dropped_at = time.monotonic()
        self._since_drop_first_call = False
        self._event("forced_drop", transport=self.channel.name)
        self.channel.abort()

    async def stop(self) -> None:
        """Stop, and close the channel."""
        self._stop.set()
        with contextlib.suppress(Exception):
            await self.channel.close()
