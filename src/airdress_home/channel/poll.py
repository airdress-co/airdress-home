"""Transport P: a streaming long-poll.

Downstream, ``POST /v1/home/poll`` returns NDJSON held open. Every ``rotate``
seconds the hub opens the next poll, naming its session and the last ``seq``
it holds, **before** it stops reading the current one; the operator then ends
the old stream and writes everything after that ``seq`` again on the new one.
The session above drops a ``seq`` it has already handled, so a rotation can
repeat a frame on the wire but never loses one.

Upstream, frames are batched into ``POST /v1/home/frames?session=<id>``,
flushed after ``flush_ms`` or ``batch`` frames, each batch led by an ``ack``.

Every poll and every batch is a machine-signed request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from ..errors import ChannelClosed, HomeNotLinked, NotAuthorized
from ..frames import parse_line
from .base import ChannelStats

if TYPE_CHECKING:
    from ..machine import MachineClient

POLL = "/v1/home/poll"
FRAMES = "/v1/home/frames"


@dataclass(frozen=True)
class _Ended:
    generation: int
    reason: str


class PollChannel:
    """A home channel over a rotated long-poll."""

    name = "poll"

    def __init__(
        self,
        client: MachineClient,
        *,
        rotate: float = 240.0,
        idle: float = 90.0,
        flush_ms: float = 50.0,
        batch: int = 32,
    ) -> None:
        self._client = client
        self._rotate = rotate
        self._idle = idle
        self._flush = flush_ms / 1000.0
        self._batch = batch
        self._session: str | None = None
        self._acked = 0
        self._generation = 0
        self._retiring: set[int] = set()
        self._queue: asyncio.Queue[dict[str, Any] | _Ended | BaseException] = asyncio.Queue()
        self._responses: dict[int, aiohttp.ClientResponse] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._outbox: list[dict[str, Any]] = []
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False
        self.stats: ChannelStats = ChannelStats()

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start_poll(self) -> int:
        """Open a poll and start reading it. Returns its generation."""
        body = json.dumps({"session": self._session, "ack": self._acked or None}).encode()
        try:
            resp = await self._client.request(
                "POST",
                POLL,
                body,
                content_type="application/json",
                client_timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=15, sock_read=self._idle
                ),
            )
        except NotAuthorized:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ChannelClosed(f"poll_{type(e).__name__}") from e
        if resp.status == 403:
            resp.release()
            raise HomeNotLinked("home_not_linked")
        if resp.status != 200:
            resp.release()
            raise ChannelClosed(f"poll_http_{resp.status}")
        self._generation += 1
        generation = self._generation
        self._responses[generation] = resp
        self._spawn(self._read(generation, resp))
        return generation

    async def _read(self, generation: int, resp: aiohttp.ClientResponse) -> None:
        reason = "eof"
        try:
            while True:
                raw = await resp.content.readline()
                if not raw:
                    break
                line = parse_line(raw)
                if line is None:
                    continue
                if generation in self._retiring:
                    self.stats.overlap_lines += 1
                await self._queue.put(line)
        except (aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError) as e:
            reason = f"read_{type(e).__name__}"
        except Exception as e:  # noqa: BLE001 - handed to the reader of lines()
            await self._queue.put(e)
            return
        finally:
            resp.release()
            self._responses.pop(generation, None)
        await self._queue.put(_Ended(generation, reason))

    async def _rotate_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._rotate)
            if self._closed:
                return
            # The operator ends the current poll as soon as it attaches the
            # next one, which can be before the next one's headers reach us:
            # its end is expected from here on, not a drop.
            retiring = self._generation
            self._retiring.add(retiring)
            try:
                await self._start_poll()
                self.stats.rotations += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - ends the channel
                self.stats.rotation_failures += 1
                self._retiring.discard(retiring)
                await self._queue.put(e)
                return

    async def open(self) -> None:
        self._closed = False
        self._retiring.clear()
        await self._start_poll()
        self.stats.opens += 1
        self._spawn(self._rotate_loop())

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._queue.get()
            if isinstance(item, dict):
                yield item
            elif isinstance(item, _Ended):
                if item.generation in self._retiring:
                    self._retiring.discard(item.generation)
                elif item.generation == self._generation:
                    raise ChannelClosed(f"poll_ended_{item.reason}")
            else:
                if isinstance(item, ChannelClosed):
                    raise item
                raise ChannelClosed(f"error_{type(item).__name__}") from item

    def bind(self, session: str) -> None:
        if session != self._session:
            self._session = session
            self._acked = 0

    def acked(self, seq: int) -> None:
        self._acked = max(self._acked, seq)

    async def send(self, frame: dict[str, Any]) -> None:
        self._outbox.append(frame)
        if len(self._outbox) >= self._batch:
            await self._flush_now()
        elif self._flusher is None or self._flusher.done():
            self._flusher = asyncio.get_running_loop().create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(self._flush)
        await self._flush_now()

    async def _flush_now(self) -> None:
        if not self._outbox or self._session is None:
            return
        frames, self._outbox = self._outbox, []
        lines = [json.dumps({"type": "ack", "seq": self._acked})]
        lines += [json.dumps(f, separators=(",", ":")) for f in frames]
        body = ("\n".join(lines) + "\n").encode()
        self.stats.upstream_batches += 1
        try:
            resp = await self._client.request(
                "POST",
                f"{FRAMES}?session={self._session}",
                body,
                content_type="application/x-ndjson",
            )
        except (aiohttp.ClientError, TimeoutError) as e:
            await self._queue.put(ChannelClosed(f"frames_{type(e).__name__}"))
            return
        status = resp.status
        resp.release()
        if status == 404:
            await self._queue.put(ChannelClosed("session_unknown"))
        elif status >= 300:
            await self._queue.put(ChannelClosed(f"frames_http_{status}"))

    def abort(self) -> None:
        for resp in list(self._responses.values()):
            resp.close()

    async def close(self) -> None:
        self._closed = True
        with contextlib.suppress(Exception):
            await self._flush_now()
        for resp in list(self._responses.values()):
            resp.close()
        for task in list(self._tasks):
            task.cancel()
        if self._flusher is not None:
            self._flusher.cancel()
        self._queue = asyncio.Queue()
