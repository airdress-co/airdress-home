"""The session over a scripted channel: verification, seq, answers, reconnect."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from airdress_home.channel import ChannelStats
from airdress_home.codes import MachineKey, b64url
from airdress_home.errors import ChannelClosed, ProtocolError
from airdress_home.frames import signed_bytes
from airdress_home.models import Features, Shared, SharedEntity
from airdress_home.session import HomeSession

OP = MachineKey(bytes([9]) * 32)
SESSION = "0a0b0c0d-0000-4000-8000-000000000002"


def signed(
    t: str, seq: int, session: str = SESSION, key: MachineKey = OP, **body: Any
) -> dict[str, Any]:
    inner = {"session": session, "seq": seq, "notAfter": 4_000_000_000, **body}
    if t == "hello":
        inner["protocol"] = 1
    text = json.dumps(inner)
    return {"type": t, "frame": text, "sig": b64url(key.sign(signed_bytes(t, text)))}


class Script:
    """A channel that replays scripted lines, then ends."""

    name = "script"

    def __init__(self, *runs: list[dict[str, Any]]) -> None:
        self.runs = list(runs)
        self.sent: list[dict[str, Any]] = []
        self.bound: list[str] = []
        self.acks: list[int] = []
        self.stats = ChannelStats()
        self.opened = 0

    async def open(self) -> None:
        self.opened += 1

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        run = self.runs.pop(0) if self.runs else []
        for line in run:
            yield line
            await asyncio.sleep(0)
        raise ChannelClosed("script_end")

    def bind(self, session: str) -> None:
        self.bound.append(session)

    def acked(self, seq: int) -> None:
        self.acks.append(seq)

    async def send(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)

    def abort(self) -> None:
        pass

    async def close(self) -> None:
        pass


class Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.features_seen: list[Features] = []
        self.emitted: list[tuple[str, str, dict[str, Any] | None, str]] = []

    async def call(
        self, action: str, targets: list[str], data: dict[str, Any] | None, function: str
    ) -> tuple[str, Any]:
        self.calls.append((action, targets))
        return "ok", None

    async def read(self, entity: str) -> tuple[str, str | None, str | None]:
        return "ok", "on", "2026-09-27T00:00:00+00:00"

    def shared(self) -> Shared:
        return Shared("1", "2026.9.0", operate=(SharedEntity("button.x"),))

    def features(self, features: Features) -> None:
        self.features_seen.append(features)

    def emit(self, event: str, event_type: str, data: dict[str, Any] | None, function: str) -> None:
        self.emitted.append((event, event_type, data, function))


async def run_until_done(s: HomeSession, ch: Script) -> None:
    task = asyncio.create_task(s.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not ch.runs:
            break
    await asyncio.sleep(0.05)
    await s.stop()
    await asyncio.wait_for(task, 2)


async def test_a_call_is_answered_and_duplicates_are_dropped() -> None:
    ch = Script(
        [
            signed("hello", 1),
            signed("call", 2, callId="c1", action="button.press", targets=["button.x"]),
            signed("call", 2, callId="c1", action="button.press", targets=["button.x"]),
            signed("read", 4, readId="r1", entity="button.x"),
        ]
    )
    h = Handler()
    s = HomeSession(ch, h, OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    assert ch.bound == [SESSION]
    assert ch.sent[0]["type"] == "shared", "the hub shares first"
    results = [f for f in ch.sent if f["type"] == "result"]
    assert [r["callId"] for r in results] == ["c1"], "a repeated seq is not run twice"
    assert h.calls == [("button.press", ["button.x"])]
    assert s.stats.duplicates == 1
    assert s.stats.gaps == 1 and s.stats.gap_frames == 1
    assert [f["readId"] for f in ch.sent if f["type"] == "read_result"] == ["r1"]
    assert ch.acks[-1] == 4


async def test_a_frame_under_another_key_ends_the_channel() -> None:
    rogue = MachineKey(bytes([1]) * 32)
    ch = Script([signed("hello", 1, key=rogue)])
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    assert s.stats.hellos == 0
    assert any(e["kind"] == "drop" and e["reason"] == "protocol" for e in s.stats.events)


async def test_a_new_hello_resets_the_seq_and_a_reconnect_is_timed() -> None:
    ch = Script(
        [signed("hello", 1)],
        [
            signed("hello", 1, session="11111111-0000-4000-8000-000000000003"),
            signed(
                "call",
                2,
                session="11111111-0000-4000-8000-000000000003",
                callId="c2",
                action="button.press",
                targets=["button.x"],
            ),
        ],
    )
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    kinds = [e["kind"] for e in s.stats.events]
    assert "ready" in kinds and "first_call" in kinds
    assert s.stats.hellos == 2
    assert s.stats.calls == 1


async def test_a_frame_for_another_session_is_a_protocol_error() -> None:
    from airdress_home.frames import verify

    frame = verify(signed("call", 2, session="other"), OP.public)
    assert frame.session == "other"
    ch = Script(
        [
            signed("hello", 1),
            signed("call", 2, session="other", callId="x", action="a.b", targets=["a.c"]),
        ]
    )
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    assert s.stats.calls == 0


def test_the_pinned_key_must_be_a_key() -> None:
    with pytest.raises(ValueError):
        HomeSession(Script(), Handler(), b"short")
    assert issubclass(ProtocolError, Exception)


async def test_features_and_emits_reach_the_handler() -> None:
    ch = Script(
        [
            signed("hello", 1),
            signed(
                "features",
                2,
                events=[
                    {"name": "arrived", "types": ["home", "work", "home"]},
                    {"name": "Bad Name", "types": ["x"]},
                    {"name": "empty", "types": []},
                ],
                notify={"enabled": True},
                trackers=[{"name": "jefe"}],
            ),
            signed(
                "emit",
                3,
                emitId="e1",
                event="arrived",
                eventType="home",
                data={"k": 1},
                function="wake",
            ),
            signed("emit", 4, emitId="e2", event="arrived", eventType="Not Valid", data=None),
            signed("emit", 5, emitId="e3", event="arrived", eventType="work", data=[1]),
        ]
    )
    h = Handler()
    s = HomeSession(ch, h, OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    (f,) = h.features_seen
    assert [(e.name, e.types) for e in f.events] == [("arrived", ("home", "work"))]
    assert f.dropped == 2 and f.notify and f.trackers == ("jefe",)
    assert h.emitted == [("arrived", "home", {"k": 1}, "wake")]
    assert s.stats.emits == 1 and s.stats.emits_dropped == 2


async def test_the_hub_ceilings_hold_whatever_the_operator_sends() -> None:
    ch = Script(
        [
            signed("hello", 1),
            *[
                signed("call", 2 + i, callId=f"c{i}", action="button.press", targets=["button.x"])
                for i in range(3)
            ],
            *[signed("emit", 5 + i, event="arrived", eventType="home") for i in range(3)],
        ]
    )
    h = Handler()
    s = HomeSession(
        ch, h, OP.public, backoff_min=0.01, max_calls_per_minute=2, max_emits_per_minute=1
    )
    await run_until_done(s, ch)
    outcomes = [f["outcome"] for f in ch.sent if f["type"] == "result"]
    assert outcomes == ["ok", "ok", "rate_limited"]
    assert len(h.calls) == 2 and s.stats.rate_limited == 1
    assert len(h.emitted) == 1 and s.stats.emits_dropped == 2


async def test_a_call_with_malformed_fields_is_rejected_unrun() -> None:
    ch = Script(
        [
            signed("hello", 1),
            signed("call", 2, callId="c1", action="button.press", targets=[1]),
            signed("call", 3, callId="c2", action="button.press", targets=["button.x"], data=[]),
        ]
    )
    h = Handler()
    s = HomeSession(ch, h, OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    assert [f["outcome"] for f in ch.sent if f["type"] == "result"] == ["rejected", "rejected"]
    assert h.calls == []


async def test_connection_is_reported_on_hello_and_on_drop() -> None:
    ch = Script([signed("hello", 1)])
    seen: list[bool] = []
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01, on_connection=seen.append)
    await run_until_done(s, ch)
    assert seen[:2] == [True, False]


async def test_open_first_then_run_uses_that_channel_and_share_resends() -> None:
    ch = Script([signed("hello", 1)])
    h = Handler()
    s = HomeSession(ch, h, OP.public, backoff_min=5)
    await s.open()
    assert ch.opened == 1
    await s.share()
    assert ch.sent == [], "nothing is sent before a hello"
    task = asyncio.create_task(s.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.stats.hellos:
            break
    assert ch.opened == 1, "run carries on with the channel open() opened"
    await s.stop()
    await asyncio.wait_for(task, 2)
    shared = [f for f in ch.sent if f["type"] == "shared"]
    assert shared[0]["operate"] == [{"entity": "button.x"}]
    assert shared[0]["observe"] == [{"entity": "button.x"}], "operate implies observe"


async def test_share_sends_while_connected() -> None:
    gate = asyncio.Event()

    class Held(Script):
        async def lines(self) -> AsyncIterator[dict[str, Any]]:
            yield signed("hello", 1)
            await gate.wait()
            raise ChannelClosed("done")

    ch = Held()
    s = HomeSession(ch, Handler(), OP.public, backoff_min=5)
    task = asyncio.create_task(s.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.connected:
            break
    await s.share()
    assert [f["type"] for f in ch.sent] == ["shared", "shared"]
    gate.set()
    await s.stop()
    await asyncio.wait_for(task, 2)


def test_measurement_events_are_bounded() -> None:
    s = HomeSession(Script(), Handler(), OP.public)
    for _ in range(1000):
        s.stats.event("x")
    assert len(s.stats.events) == 256
