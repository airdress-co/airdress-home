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

    async def call(
        self, action: str, targets: list[str], data: dict[str, Any] | None, function: str
    ) -> tuple[str, Any]:
        self.calls.append((action, targets))
        return "ok", None

    async def read(self, entity: str) -> tuple[str, str | None, str | None]:
        return "ok", "on", "2026-09-27T00:00:00+00:00"

    def shared(self) -> dict[str, Any]:
        return {"operate": [{"entity": "button.x"}], "observe": []}


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
