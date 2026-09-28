"""Transport negotiation: fallback, the per-network hint, demotion, re-probe."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from airdress_home.channel import (
    FileHintStore,
    Hint,
    MemoryHintStore,
    NegotiatingChannel,
    for_client,
)
from airdress_home.channel.base import ChannelStats
from airdress_home.codes import MachineKey, b64url
from airdress_home.errors import (
    ChannelClosed,
    HomeDisabled,
    HomeNotLinked,
    NotAuthorized,
    TransportRefused,
)
from airdress_home.frames import signed_bytes
from airdress_home.machine import Enrollment, MachineClient
from airdress_home.models import CLOSE_LIFETIME
from airdress_home.session import HomeSession

from .test_session import Handler

NET = "net-a"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class Fake:
    """A transport whose establishment and end are scripted per open."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.stats = ChannelStats()
        self.refuse: list[BaseException | None] = []
        """What each open raises (``None``: it opens); the last entry repeats."""
        self.opens = 0
        self.attempts = 0
        self.end = "eof"
        self.aborted = 0
        self.resuming: str | None = None
        self._ended = asyncio.Event()

    async def open(self) -> None:
        self.attempts += 1
        outcome = self.refuse[min(self.attempts, len(self.refuse)) - 1] if self.refuse else None
        if outcome is not None:
            raise outcome
        self.opens += 1
        self._ended = asyncio.Event()

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "keepalive"}
        await self._ended.wait()
        raise ChannelClosed(self.end)

    def finish(self, reason: str = "eof") -> None:
        self.end = reason
        self._ended.set()

    def bind(self, session: str) -> None:
        del session

    def acked(self, seq: int) -> None:
        del seq

    async def send(self, frame: dict[str, Any]) -> None:
        del frame

    def abort(self) -> None:
        self.aborted += 1
        self.finish("aborted")

    async def close(self) -> None:
        pass


def negotiator(
    *transports: Fake,
    store: MemoryHintStore | None = None,
    wall: Clock | None = None,
    mono: Clock | None = None,
    events: list[dict[str, Any]] | None = None,
) -> NegotiatingChannel:
    async def network() -> str:
        return NET

    return NegotiatingChannel(
        list(transports),
        network=network,
        hints=store if store is not None else MemoryHintStore(),
        clock=mono or Clock(),
        wall=wall or Clock(),
        on_event=None if events is None else events.append,
    )


async def ride(ch: NegotiatingChannel, fake: Fake, reason: str, mono: Clock, lived: float) -> None:
    """Open, read until the transport ends ``lived`` seconds later, close."""
    await ch.open()
    assert ch.active is fake
    it = ch.lines().__aiter__()
    await it.__anext__()
    mono.now += lived
    fake.finish(reason)
    with pytest.raises(ChannelClosed):
        await it.__anext__()
    await ch.close()


async def test_the_preferred_transport_is_tried_first_and_leaves_no_hint() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    store = MemoryHintStore()
    ch = negotiator(ws, poll, store=store)
    await ch.open()
    assert ch.name == "ws"
    assert (ws.opens, poll.attempts) == (1, 0)
    assert store.hints == {}


async def test_a_refused_upgrade_falls_back_and_is_remembered() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [TransportRefused("handshake_400")]
    store, wall = MemoryHintStore(), Clock()
    events: list[dict[str, Any]] = []
    ch = negotiator(ws, poll, store=store, wall=wall, events=events)
    await ch.open()
    assert ch.name == "poll"
    assert store.hints[NET] == Hint("poll", wall.now, wall.now)
    assert [e["kind"] for e in events] == ["transport_refused", "transport_fallback"]
    assert ch.negotiation.refusals == ch.negotiation.fallbacks == 1

    # The next open on this network starts where it worked.
    ws.attempts = 0
    await ch.close()
    await ch.open()
    assert (ws.attempts, ch.name) == (0, "poll")


async def test_the_hint_survives_a_restart_in_a_file(tmp_path: Path) -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [TransportRefused("handshake_426")]
    path = tmp_path / "hints.json"
    first = NegotiatingChannel(
        [ws, poll], network=lambda: asyncio.sleep(0, NET), hints=FileHintStore(path)
    )
    await first.open()
    stored = json.loads(path.read_text())
    assert stored[NET]["transport"] == "poll"
    assert set(stored[NET]) == {"transport", "since", "preferredFailed"}

    ws2, poll2 = Fake("ws"), Fake("poll")
    second = NegotiatingChannel(
        [ws2, poll2], network=lambda: asyncio.sleep(0, NET), hints=FileHintStore(path)
    )
    await second.open()
    assert (ws2.attempts, second.name) == (0, "poll")


async def test_another_network_starts_from_the_default_order() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    store = MemoryHintStore()
    store.hints = {"elsewhere": Hint("poll", 1_000_000.0, 1_000_000.0)}
    ch = negotiator(ws, poll, store=store)
    await ch.open()
    assert ch.name == "ws"


async def test_the_preferred_transport_is_probed_again_and_confirmed() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    wall, mono, store = Clock(), Clock(), MemoryHintStore()
    events: list[dict[str, Any]] = []
    store.hints = {NET: Hint("poll", wall.now, wall.now)}
    ch = negotiator(ws, poll, store=store, wall=wall, mono=mono, events=events)

    await ch.open()
    assert ch.name == "poll"  # not due yet
    await ch.close()

    wall.now += ch.reprobe
    await ride(ch, ws, "closed_1006", mono, lived=ch.early_drop + 1)
    assert ch.negotiation.reprobes == ch.negotiation.confirmations == 1
    assert NET not in store.hints
    assert [e["kind"] for e in events][-2:] == ["transport_reprobe", "transport_confirmed"]


async def test_a_reprobe_that_drops_early_once_is_demoted_again() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    wall, mono, store = Clock(), Clock(), MemoryHintStore()
    store.hints = {NET: Hint("poll", wall.now - 10 * 3600, wall.now - 10 * 3600)}
    ch = negotiator(ws, poll, store=store, wall=wall, mono=mono)
    await ride(ch, ws, "closed_1006", mono, lived=5)
    assert store.hints[NET] == Hint("poll", wall.now, wall.now)
    await ch.open()
    assert ch.name == "poll"


async def test_a_reprobe_refused_resets_the_clock() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [TransportRefused("handshake_400")]
    wall, store = Clock(), MemoryHintStore()
    store.hints = {NET: Hint("poll", wall.now - 7 * 3600, wall.now - 7 * 3600)}
    ch = negotiator(ws, poll, store=store, wall=wall)
    await ch.open()
    assert ch.name == "poll"
    assert store.hints[NET].preferred_failed == wall.now


async def test_repeated_early_drops_demote_the_transport() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    wall, mono, store = Clock(), Clock(), MemoryHintStore()
    events: list[dict[str, Any]] = []
    ch = negotiator(ws, poll, store=store, wall=wall, mono=mono, events=events)
    for _ in range(ch.early_drops_limit - 1):
        await ride(ch, ws, "closed_1006", mono, lived=3)
    assert NET not in store.hints
    await ride(ch, ws, "closed_1006", mono, lived=3)
    assert store.hints[NET] == Hint("poll", wall.now, wall.now)
    assert events[-1]["kind"] == "transport_demoted"
    assert (events[-1]["transport"], events[-1]["to"]) == ("ws", "poll")
    await ch.open()
    assert ch.name == "poll"


async def test_a_channel_that_lived_resets_the_count() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    mono, store = Clock(), MemoryHintStore()
    ch = negotiator(ws, poll, store=store, mono=mono)
    for lived in (3, 3, ch.early_drop + 1, 3, 3):
        await ride(ch, ws, "closed_1006", mono, lived=lived)
    assert NET not in store.hints


@pytest.mark.parametrize("reason", [f"closed_{CLOSE_LIFETIME}", "closed_4003", "closed_4008"])
async def test_the_operators_own_closes_are_not_early_drops(reason: str) -> None:
    ws, poll = Fake("ws"), Fake("poll")
    mono, store = Clock(), MemoryHintStore()
    ch = negotiator(ws, poll, store=store, mono=mono)
    for _ in range(5):
        await ride(ch, ws, reason, mono, lived=1)
    assert ch.negotiation.early_drops == 0
    assert NET not in store.hints


async def test_a_forced_cut_is_not_an_early_drop() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    mono = Clock()
    ch = negotiator(ws, poll, mono=mono)
    for _ in range(5):
        await ch.open()
        it = ch.lines().__aiter__()
        await it.__anext__()
        ch.abort()
        with pytest.raises(ChannelClosed):
            await it.__anext__()
        await ch.close()
    assert ch.negotiation.early_drops == 0


async def test_a_reattached_poll_is_timed_from_its_sessions_hello() -> None:
    """The operator ends a long-poll session an hour after its hello with a bare
    end of stream; a poll that re-attached shortly before is not an early drop."""
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [TransportRefused("handshake_400")]
    mono, store = Clock(), MemoryHintStore()
    ch = negotiator(ws, poll, store=store, mono=mono)
    await ch.open()
    ch.bind("s1")  # the operator's hello
    poll.resuming = "s1"
    mono.now += 3000  # the session is 50 minutes old
    await ch.close()
    for _ in range(5):
        # A drop, then a re-attach of the same session: no new hello.
        await ride(ch, poll, "poll_ended_eof", mono, lived=30)
    assert ch.negotiation.early_drops == 0
    assert store.hints[NET].transport == "poll"


async def test_a_new_session_restarts_the_clock() -> None:
    """A re-attach the operator answers with a new session is timed from that
    session's hello."""
    ws, poll = Fake("ws"), Fake("poll")
    mono = Clock()
    ch = negotiator(ws, poll, mono=mono)
    await ch.open()
    ch.bind("s1")
    mono.now += 3600
    await ch.close()
    for _ in range(ch.early_drops_limit):
        await ch.open()
        ch.bind(f"s-{mono.now}")  # a new session every time
        it = ch.lines().__aiter__()
        await it.__anext__()
        mono.now += 5
        ws.finish("closed_1006")
        with pytest.raises(ChannelClosed):
            await it.__anext__()
        await ch.close()
    assert ch.negotiation.early_drops == ch.early_drops_limit
    assert ch.negotiation.demotions == 1


async def test_a_channel_that_never_said_hello_is_timed_from_its_opening() -> None:
    """A transport that resumes nothing, ending before any hello, dropped early."""
    ws, poll = Fake("ws"), Fake("poll")
    mono = Clock()
    ch = negotiator(ws, poll, mono=mono)
    await ch.open()
    ch.bind("s1")
    mono.now += 3600
    await ch.close()
    await ride(ch, ws, "closed_1006", mono, lived=5)
    assert ch.negotiation.early_drops == 1


@pytest.mark.parametrize("refusal", [NotAuthorized("key_revoked"), HomeDisabled("home_disabled")])
async def test_the_operators_refusal_is_raised_at_once(refusal: Exception) -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [refusal]
    ch = negotiator(ws, poll)
    with pytest.raises(type(refusal)):
        await ch.open()
    assert poll.attempts == 0


async def test_an_unreachable_operator_is_not_a_refused_transport() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [aiohttp.ClientConnectionError("down")]
    store = MemoryHintStore()
    ch = negotiator(ws, poll, store=store)
    with pytest.raises(aiohttp.ClientConnectionError):
        await ch.open()
    assert poll.attempts == 0
    assert store.hints == {}


async def test_a_bodiless_403_asks_the_next_transport_whose_answer_stands() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [HomeNotLinked("Invalid response status")]
    poll.refuse = [HomeDisabled("home_disabled")]
    ch = negotiator(ws, poll)
    with pytest.raises(HomeDisabled):
        await ch.open()

    ws2, poll2 = Fake("ws"), Fake("poll")
    ws2.refuse = [HomeNotLinked("Invalid response status")]
    poll2.refuse = [HomeNotLinked("home_not_linked")]
    ch2 = negotiator(ws2, poll2)
    with pytest.raises(HomeNotLinked) as raised:
        await ch2.open()
    assert str(raised.value) == "home_not_linked"


async def test_every_transport_refused() -> None:
    ws, poll = Fake("ws"), Fake("poll")
    ws.refuse = [TransportRefused("handshake_400")]
    poll.refuse = [TransportRefused("nope")]
    with pytest.raises(TransportRefused) as raised:
        await negotiator(ws, poll).open()
    assert raised.value.reason == "no_transport"


# -- against a real server: W's upgrade refused, P takes over ---------------

OP = MachineKey(bytes([7]) * 32)
SESSION = "0a0b0c0d-0000-4000-8000-00000000000a"


def _hello() -> str:
    inner = {"session": SESSION, "seq": 1, "notAfter": 4_000_000_000, "protocol": 1}
    text = json.dumps(inner)
    return json.dumps(
        {"type": "hello", "frame": text, "sig": b64url(OP.sign(signed_bytes("hello", text)))}
    )


async def _serve(ws_status: int) -> tuple[TestServer, dict[str, Any]]:
    seen: dict[str, Any] = {"session_gets": 0, "polls": 0, "frames": []}

    async def session(request: web.Request) -> web.StreamResponse:
        seen["session_gets"] += 1
        # What a proxy that does not pass upgrades answers.
        return web.Response(status=ws_status, text="no upgrade here")

    async def poll(request: web.Request) -> web.StreamResponse:
        seen["polls"] += 1
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        await resp.write((_hello() + "\n").encode())
        with pytest.raises((asyncio.CancelledError, ConnectionResetError)):
            await asyncio.sleep(30)
        return resp

    async def frames(request: web.Request) -> web.Response:
        seen["frames"].extend(json.loads(x) for x in (await request.text()).splitlines() if x)
        return web.Response(status=204)

    app = web.Application()
    app.router.add_get("/v1/home/session", session)
    app.router.add_post("/v1/home/poll", poll)
    app.router.add_post("/v1/home/frames", frames)
    server = TestServer(app)
    await server.start_server()
    return server, seen


async def test_a_refused_websocket_upgrade_hands_the_session_to_the_long_poll() -> None:
    server, seen = await _serve(ws_status=400)
    events: list[dict[str, Any]] = []
    store = MemoryHintStore()
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            client = MachineClient(http, MachineKey(bytes(32)), enrollment)
            channel = for_client(client, hints=store, on_event=events.append)
            session = HomeSession(channel, Handler(), OP.public)
            await session.open()
            task = asyncio.create_task(session.run())
            for _ in range(200):
                if session.connected and seen["frames"]:
                    break
                await asyncio.sleep(0.01)
            assert session.connected
            assert channel.name == "poll"
            assert seen["session_gets"] == 1
            assert seen["polls"] == 1
            assert [f["type"] for f in seen["frames"]] == ["ack", "shared"]
            assert [e["kind"] for e in events] == ["transport_refused", "transport_fallback"]
            assert events[0]["reason"] == "handshake_400"
            assert [e["transport"] for e in session.stats.events if e["kind"] == "hello"] == [
                "poll"
            ]
            (hint,) = store.hints.values()
            assert hint.transport == "poll"
            await session.stop()
            await asyncio.wait_for(task, 5)
    finally:
        await server.close()


async def test_an_operator_in_trouble_is_not_a_refused_upgrade() -> None:
    server, seen = await _serve(ws_status=503)
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            client = MachineClient(http, MachineKey(bytes(32)), enrollment)
            channel = for_client(client)
            with pytest.raises(ChannelClosed) as raised:
                await channel.open()
            assert not isinstance(raised.value, TransportRefused)
            assert raised.value.reason == "handshake_503"
            assert seen["polls"] == 0
    finally:
        await server.close()
