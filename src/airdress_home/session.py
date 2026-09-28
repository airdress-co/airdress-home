"""The hub's side of a home session, whatever the transport.

:class:`HomeSession` keeps one channel up: it opens it, verifies every
operator frame against the **pinned** operator key, enforces the session, a
strictly increasing ``seq`` and ``notAfter``, hands ``call`` and ``read``
frames to a :class:`Handler`, and sends the answers back. When the channel
ends it reconnects with jittered exponential backoff, 1 s to 60 s.

A frame whose ``seq`` was already handled is dropped (a long-poll rotation
can repeat one); a jump in ``seq`` is counted as a gap. Neither is ever
silently ignored in the statistics.

The hub keeps its own ceilings, which the operator cannot raise: at most
``max_calls_per_minute`` calls are run (the rest are answered
``rate_limited``) and ``max_emits_per_minute`` emits handed on (the rest are
dropped and counted).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from . import frames, models
from .errors import ChannelClosed, HomeNotLinked, NotAuthorized, ProtocolError
from .models import Features, Shared, Track

if TYPE_CHECKING:
    from .channel import Channel

_LOGGER = logging.getLogger(__name__)

#: How far an operator's clock may run ahead of ours before a frame is refused.
CLOCK_SKEW = 30

#: How many measurement events :class:`SessionStats` keeps.
MAX_EVENTS = 256


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

    def shared(self) -> Shared:
        """What is exposed, at which level."""
        ...

    def features(self, features: Features) -> None:
        """The operator declared what its ``Home`` offers the hub."""
        ...

    def emit(self, event: str, event_type: str, data: dict[str, Any] | None, function: str) -> None:
        """The operator emitted ``event_type`` on the declared ``event``."""
        ...

    def track(self, track: Track) -> None:
        """A function sent the owner's position for a declared tracker."""
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
    emits: int = 0
    refused: int = 0
    rate_limited: int = 0
    emits_dropped: int = 0
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    """The most recent events, newest last; older ones are discarded."""

    def event(self, kind: str, **values: Any) -> None:
        """Record one event with a wall-clock timestamp."""
        self.events.append({"t": round(time.time(), 3), "kind": kind, **values})


class _Window:
    """A sliding one-minute window of at most ``limit`` admissions."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._times: deque[float] = deque()

    def admit(self) -> bool:
        now = time.monotonic()
        while self._times and now - self._times[0] >= 60:
            self._times.popleft()
        if len(self._times) >= self.limit:
            return False
        self._times.append(now)
        return True


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
        max_calls_per_minute: int = 60,
        max_emits_per_minute: int = 60,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_connection: Callable[[bool], None] | None = None,
        on_revoked: Callable[[], None] | None = None,
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
        self._on_connection = on_connection
        self._on_revoked = on_revoked
        self.home: str | None = None
        """The ``Home``'s name as the last ``hello`` gave it."""
        self.revoked = False
        """The operator closed the channel as revoked; it is not reopened."""
        self._notifies: dict[str, asyncio.Future[str]] = {}
        self._calls = _Window(max_calls_per_minute)
        self._emits = _Window(max_emits_per_minute)
        self._opened = False
        self._session: str | None = None
        self._last_seq = 0
        self._dropped_at: float | None = None
        self._since_drop_first_call = False
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        """Whether the channel is up and the operator has said ``hello``."""
        return self._connected.is_set()

    def _set_connected(self, up: bool) -> None:
        if not up:
            for future in self._notifies.values():
                if not future.done():
                    future.set_exception(ChannelClosed("dropped"))
        if up == self._connected.is_set():
            return
        if up:
            self._connected.set()
        else:
            self._connected.clear()
        if self._on_connection is not None:
            self._on_connection(up)

    async def open(self) -> None:
        """Open the channel once, raising why it could not be opened.

        For a caller that must know the channel works before it goes on (a
        setup that tests its connection). :meth:`run` then carries on with the
        channel this opened, and reopens it itself from then on.

        Raises :class:`HomeNotLinked`, :class:`NotAuthorized`,
        :class:`ChannelClosed`, or the transport's own error.
        """
        await self.channel.open()
        self._opened = True

    async def share(self) -> None:
        """Send what is shared again, after it changed. Does nothing while the
        channel is down: every ``hello`` is answered with it anyway."""
        if self.connected and self._session is not None:
            await self.channel.send(frames.shared(self.handler.shared()))

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
            self._set_connected(False)
            if self._stop.is_set():
                break
            if self._dropped_at is None:
                self._dropped_at = time.monotonic()
                self._since_drop_first_call = False
            lived = time.monotonic() - opened_at
            self._event("drop", transport=self.channel.name, reason=reason, lived_s=round(lived, 1))
            if reason == f"closed_{models.CLOSE_REVOKED}":
                self.revoked = True
                if self._on_revoked is not None:
                    self._on_revoked()
                break
            if lived > 30:
                delay = self.backoff_min
            if reason in (
                f"closed_{models.CLOSE_UNLINKED}",
                f"closed_{models.CLOSE_DISPLACED}",
                "home_not_linked",
            ):
                # Nothing on this side changes that by retrying sooner.
                delay = self.backoff_max
            wait = random.uniform(delay / 2, delay)  # noqa: S311 - jitter, not crypto
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), wait)
            delay = min(self.backoff_max, delay * 2)

    async def _run_once(self) -> str:
        try:
            if self._opened:
                self._opened = False
            else:
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
            home = frame.body.get("home")
            self.home = home if isinstance(home, str) else None
            self.channel.bind(frame.session)
            self.stats.hellos += 1
            self._event("hello", transport=self.channel.name)
            self._set_connected(True)
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
            await self.channel.send(frames.shared(self.handler.shared()))
            return
        if frame.not_after + CLOCK_SKEW < now:
            self.stats.refused += 1
            await self._answer(frame, models.EXPIRED, 0.0)
            return
        if frame.type == "call":
            await self._call(frame)
        elif frame.type == "read":
            await self._read(frame)
        elif frame.type == "emit":
            self._emit(frame)
        elif frame.type == "features":
            self.handler.features(frames.features(frame.body))
        elif frame.type == "track":
            self._track(frame)
        elif frame.type == "notify_result":
            self._notify_result(frame)

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
        data = body.get("data")
        if (
            not isinstance(action, str)
            or not isinstance(targets, list)
            or not targets
            or not all(isinstance(t, str) for t in targets)
            or not (data is None or isinstance(data, dict))
        ):
            await self._answer(frame, models.REJECTED, 0.0)
            return
        if not self._calls.admit():
            self.stats.rate_limited += 1
            await self._answer(frame, models.RATE_LIMITED, 0.0)
            return
        t0 = time.monotonic()
        try:
            outcome, response = await self.handler.call(
                action, list(targets), data, str(body.get("function", ""))
            )
        except Exception:
            _LOGGER.exception("the handler failed a call")
            outcome, response = models.FAILED, None
        ms = (time.monotonic() - t0) * 1000
        self.stats.calls += 1
        await self.channel.send(frames.result(str(body["callId"]), outcome, ms, response))
        self._mark_first_call()

    async def _read(self, frame: frames.OperatorFrame) -> None:
        entity = frame.body.get("entity")
        t0 = time.monotonic()
        if not isinstance(entity, str):
            outcome, state, changed = models.REJECTED, None, None
        else:
            try:
                outcome, state, changed = await self.handler.read(entity)
            except Exception:
                _LOGGER.exception("the handler failed a read")
                outcome, state, changed = models.FAILED, None, None
        ms = (time.monotonic() - t0) * 1000
        self.stats.reads += 1
        await self.channel.send(
            frames.read_result(str(frame.body["readId"]), outcome, ms, state, changed)
        )

    def _emit(self, frame: frames.OperatorFrame) -> None:
        body = frame.body
        event, event_type, data = body.get("event"), body.get("eventType"), body.get("data")
        if (
            not models.valid_name(event)
            or not models.valid_name(event_type)
            or not (data is None or isinstance(data, dict))
            or not self._emits.admit()
        ):
            self.stats.emits_dropped += 1
            return
        self.stats.emits += 1
        try:
            self.handler.emit(event, event_type, data, str(body.get("function", "")))
        except Exception:
            _LOGGER.exception("the handler failed an emit")

    def _track(self, frame: frames.OperatorFrame) -> None:
        track = frames.track(frame.body)
        if track is None:
            self.stats.refused += 1
            return
        try:
            self.handler.track(track)
        except Exception:
            _LOGGER.exception("the handler failed a track")

    def _notify_result(self, frame: frames.OperatorFrame) -> None:
        message_id = frame.body.get("messageId")
        outcome = frame.body.get("outcome")
        future = self._notifies.pop(str(message_id), None)
        if future is not None and not future.done():
            future.set_result(outcome if outcome in models.NOTIFY_OUTCOMES else models.FAILED)

    async def notify(self, text: str, title: str | None = None) -> str:
        """Send a message for the owner's Home conversation; return the
        operator's outcome (``delivered``, ``rate_limited``, ``disabled``,
        ``too_long``, ``rejected`` or ``failed``).

        Raises :class:`ChannelClosed` while the channel is down, or when it
        drops before the answer. Never queued; the caller bounds the wait.
        """
        if not self.connected:
            raise ChannelClosed("not_connected")
        message_id = str(uuid.uuid4())
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._notifies[message_id] = future
        try:
            await self.channel.send(frames.notify(message_id, text, title))
            return await future
        finally:
            self._notifies.pop(message_id, None)

    async def send_state(
        self, entity: str, new_state: dict[str, Any], old_state: dict[str, Any] | None = None
    ) -> None:
        """Stream an observed entity's change. Dropped while the channel is down."""
        if self.connected:
            await self.channel.send(frames.state(entity, new_state, old_state))

    async def send_entity_event(
        self,
        entity: str,
        event_type: str,
        attributes: dict[str, Any] | None = None,
        fired_at: str | None = None,
    ) -> None:
        """Stream an observed entity's event. Dropped while the channel is down."""
        if self.connected:
            await self.channel.send(frames.entity_event(entity, event_type, attributes, fired_at))

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
