"""The session over a scripted channel: verification, seq, answers, reconnect."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from airdress_home.channel import ChannelStats
from airdress_home.codes import MachineKey, b64url
from airdress_home.errors import ChannelClosed, NotAuthorized, ProtocolError
from airdress_home.frames import signed_bytes
from airdress_home.models import Features, Shared, SharedEntity, Track
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
        self.tracks: list[Track] = []

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

    def track(self, track: Track) -> None:
        self.tracks.append(track)


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


async def test_operator_additions_hello_home_features_and_track() -> None:
    ch = Script(
        [
            signed("hello", 1, home="home"),
            signed(
                "features",
                2,
                home="home",
                events=[],
                observe=["sensor.washer", 7],
                observeAttributes={"sensor.washer": ["status", 1], "bad": ["x"]},
                deprecation=None,
            ),
            signed(
                "track",
                3,
                trackId="t1",
                tracker="jefe",
                lat=52.5,
                lon=13.4,
                accuracyM=None,
                function="location-to-home",
            ),
            signed("track", 4, trackId="t2", tracker="jefe", lat=91, lon=0, accuracyM=5),
            signed("track", 5, trackId="t3", tracker="jefe", lat=True, lon=0, accuracyM=5),
        ]
    )
    h = Handler()
    s = HomeSession(ch, h, OP.public, backoff_min=0.01)
    await run_until_done(s, ch)
    assert s.home == "home"
    (f,) = h.features_seen
    assert f.home == "home" and f.observe == ("sensor.washer",)
    assert f.observe_attributes == {"sensor.washer": ("status",)}
    assert h.tracks == [Track("t1", "jefe", 52.5, 13.4, None, "location-to-home")]
    assert s.stats.refused == 2
    assert not [f for f in ch.sent if f["type"] not in ("shared",)], "a track is not answered"


class Held(Script):
    """A channel that says hello, then holds until released."""

    def __init__(self, *extra: dict[str, Any]) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.pending: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.close_code: int | None = None

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        yield signed("hello", 1)
        while not self.gate.is_set():
            try:
                yield await asyncio.wait_for(self.pending.get(), 0.01)
            except TimeoutError:
                continue
        code = self.close_code
        raise ChannelClosed(f"closed_{code}" if code else "done", code)


async def _up(s: HomeSession) -> asyncio.Task[None]:
    task = asyncio.create_task(s.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.connected:
            break
    return task


async def test_notify_returns_the_operators_outcome() -> None:
    ch = Held()
    s = HomeSession(ch, Handler(), OP.public, backoff_min=5)
    with pytest.raises(ChannelClosed):
        await s.notify("before the channel")
    task = await _up(s)
    pending = asyncio.create_task(s.notify("The washer is done", title="Laundry"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if [f for f in ch.sent if f["type"] == "notify"]:
            break
    (sent,) = [f for f in ch.sent if f["type"] == "notify"]
    assert sent["text"] == "The washer is done" and sent["title"] == "Laundry"
    await ch.pending.put(
        signed("notify_result", 2, messageId=sent["messageId"], outcome="rate_limited")
    )
    assert await asyncio.wait_for(pending, 2) == "rate_limited"

    other = asyncio.create_task(s.notify("x"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len([f for f in ch.sent if f["type"] == "notify"]) == 2:
            break
    second = [f for f in ch.sent if f["type"] == "notify"][1]
    assert "title" not in second
    await ch.pending.put(signed("notify_result", 3, messageId=second["messageId"], outcome="odd"))
    assert await asyncio.wait_for(other, 2) == "failed", "an unknown outcome is a failure"

    dropped = asyncio.create_task(s.notify("lost"))
    await asyncio.sleep(0.05)
    ch.gate.set()
    with pytest.raises(ChannelClosed):
        await asyncio.wait_for(dropped, 2)
    await s.stop()
    await asyncio.wait_for(task, 2)


async def test_state_and_entity_events_stream_only_while_connected() -> None:
    from airdress_home.frames import entity_state

    ch = Held()
    s = HomeSession(ch, Handler(), OP.public, backoff_min=5)
    await s.send_state("sensor.washer", entity_state("idle", {}, "t0"))
    assert ch.sent == []
    task = await _up(s)
    await s.send_state(
        "sensor.washer",
        entity_state("running", {"status": "wash"}, "t1"),
        entity_state("idle", {}, "t0"),
    )
    await s.send_state("sensor.washer", entity_state("done", {}, "t2"))
    await s.send_entity_event("event.doorbell", "ring", {"x": 1}, "t3")
    await s.send_entity_event("event.doorbell", "ring")
    sent = [f for f in ch.sent if f["type"] != "shared"]
    assert sent == [
        {
            "type": "state",
            "entity": "sensor.washer",
            "newState": {"state": "running", "attributes": {"status": "wash"}, "lastChanged": "t1"},
            "oldState": {"state": "idle", "attributes": {}, "lastChanged": "t0"},
        },
        {
            "type": "state",
            "entity": "sensor.washer",
            "newState": {"state": "done", "attributes": {}, "lastChanged": "t2"},
        },
        {
            "type": "entity_event",
            "entity": "event.doorbell",
            "eventType": "ring",
            "attributes": {"x": 1},
            "firedAt": "t3",
        },
        {"type": "entity_event", "entity": "event.doorbell", "eventType": "ring"},
    ]
    ch.gate.set()
    await s.stop()
    await asyncio.wait_for(task, 2)


class Refusing(Held):
    """Held, and every dial after the first is answered ``401 <code>``;
    ``None`` lets it in."""

    def __init__(self, code: str | None) -> None:
        super().__init__()
        self.refuse = code

    async def open(self) -> None:
        self.opened += 1
        if self.opened > 1 and self.refuse is not None:
            raise NotAuthorized(self.refuse)


def _session(ch: Held, **callbacks: Any) -> tuple[HomeSession, list[str]]:
    heard: list[str] = []
    s = HomeSession(
        ch,
        Handler(),
        OP.public,
        backoff_min=0.01,
        on_revoked=lambda: heard.append("revoked"),
        **callbacks,
    )
    return s, heard


async def test_a_revoked_channel_is_not_reopened() -> None:
    ch = Refusing("invalid_signature")
    ch.close_code = 4003
    s, heard = _session(ch, on_lapsed=lambda: None)
    task = await _up(s)
    ch.gate.set()
    await asyncio.wait_for(task, 2)
    assert s.revoked and s.refusal == "revoked" and heard == ["revoked"]
    assert ch.opened == 2, "one dial asks which, and none after it"
    assert next(e for e in s.stats.events if e["kind"] == "refused")["refusal"] == "revoked"


async def test_a_4003_whose_dial_says_lapsed_is_handed_on_as_lapsed() -> None:
    ch = Refusing("machine_authorization_expired")
    ch.close_code = 4003
    lapsed: list[bool] = []
    s, heard = _session(ch, on_lapsed=lambda: lapsed.append(True))
    task = await _up(s)
    ch.gate.set()
    await asyncio.wait_for(task, 2)
    assert s.refusal == "lapsed" and not s.revoked
    assert lapsed == [True] and heard == []


async def test_a_lapse_reaches_on_revoked_when_nothing_listens_for_it() -> None:
    ch = Refusing("machine_authorization_expired")
    ch.close_code = 4003
    s, heard = _session(ch)
    task = await _up(s)
    ch.gate.set()
    await asyncio.wait_for(task, 2)
    assert s.refusal == "lapsed" and heard == ["revoked"]


async def test_a_4003_whose_dial_fails_otherwise_stands_as_revoked() -> None:
    class Unreachable(Held):
        async def open(self) -> None:
            self.opened += 1
            if self.opened > 1:
                raise ChannelClosed("handshake_502")

    ch = Unreachable()
    ch.close_code = 4003
    s, heard = _session(ch)
    task = await _up(s)
    ch.gate.set()
    await asyncio.wait_for(task, 2)
    assert s.refusal == "revoked" and heard == ["revoked"]


async def test_a_4003_whose_dial_is_let_in_carries_on() -> None:
    ch = Refusing(None)
    ch.close_code = 4003
    s, heard = _session(ch)
    task = await _up(s)
    ch.gate.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if s.stats.connects >= 2:
            break
    assert s.stats.connects >= 2, "the dial that asked is the channel it goes on with"
    assert heard == [] and s.refusal is None
    await s.stop()
    await asyncio.wait_for(task, 2)


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("invalid_signature", "revoked"),
        ("machine_revoked", "revoked"),
        ("machine_authorization_expired", "lapsed"),
    ],
)
async def test_a_401_on_redial_is_terminal(code: str, kind: str) -> None:
    # The phone's Disconnect: the channel ended "unlinked" (4004), and every
    # dial after it was refused 401. That used to be one more drop, retried
    # forever.
    ch = Refusing(code)
    ch.close_code = 4004
    lapsed: list[bool] = []
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01, backoff_max=0.02)
    s._on_lapsed = lambda: lapsed.append(True)
    revoked: list[bool] = []
    s._on_revoked = lambda: revoked.append(True)
    task = await _up(s)
    ch.gate.set()
    await asyncio.wait_for(task, 2)
    assert s.refusal == kind
    assert (revoked, lapsed) == (([True], []) if kind == "revoked" else ([], [True]))
    await asyncio.sleep(0.1)
    assert ch.opened == 2, "no dial after the refusal"


async def test_a_401_a_retry_can_outlast_is_retried() -> None:
    ch = Refusing("signature_clock_skew")
    ch.close_code = 4004
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01, backoff_max=0.02)
    task = await _up(s)
    ch.gate.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ch.opened >= 4:
            break
    assert ch.opened >= 4 and s.refusal is None
    await s.stop()
    await asyncio.wait_for(task, 2)


async def test_unlinked_backs_off_to_the_maximum() -> None:
    ch = Held()
    ch.close_code = 4004
    s = HomeSession(ch, Handler(), OP.public, backoff_min=0.01, backoff_max=30)
    task = await _up(s)
    ch.gate.set()
    await asyncio.sleep(0.2)
    assert ch.opened == 1, "an unlinked home is retried at the slowest pace"
    await s.stop()
    await asyncio.wait_for(task, 2)


def test_read_result_attributes_only_when_given() -> None:
    from airdress_home.frames import read_result

    assert "attributes" not in read_result("r", "ok", 1.0, "on", "t")
    assert read_result("r", "ok", 1.0, "on", "t", {"a": 1})["attributes"] == {"a": 1}
