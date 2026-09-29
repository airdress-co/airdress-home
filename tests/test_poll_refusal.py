"""A refused or ended channel says why: a long-poll refused at establishment,
a long-poll the operator ended, and a WebSocket upgrade refused ``401``."""

from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from airdress_home.channel import PollChannel, WsChannel
from airdress_home.codes import MachineKey
from airdress_home.errors import ChannelClosed, HomeDisabled, HomeNotLinked, NotAuthorized
from airdress_home.machine import Enrollment, MachineClient


@pytest.mark.parametrize(
    ("body", "error", "code"),
    [
        ({"error": {"code": "home_disabled", "message": "off"}}, HomeDisabled, "home_disabled"),
        ({"error": {"code": "home_not_linked", "message": "no"}}, HomeNotLinked, "home_not_linked"),
        ({"error": "home_disabled"}, HomeDisabled, "home_disabled"),
        (None, HomeNotLinked, "home_not_linked"),
    ],
)
async def test_refusals(body: object, error: type[Exception], code: str) -> None:
    async def handler(request: web.Request) -> web.Response:
        if body is None:
            return web.Response(status=403, text="nope")
        return web.json_response(body, status=403)

    app = web.Application()
    app.router.add_post("/v1/home/poll", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            channel = PollChannel(MachineClient(http, MachineKey(bytes(32)), enrollment))
            with pytest.raises(error) as raised:
                await channel.open()
            assert type(raised.value) is error
            assert str(raised.value) == code
    finally:
        await server.close()


async def _serve(app: web.Application) -> TestServer:
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.parametrize(("code", "reason"), [(4003, "closed_4003"), (4004, "closed_4004")])
async def test_a_poll_ended_by_the_operator_says_why(code: int, reason: str) -> None:
    async def poll(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        await resp.write(b'{"type":"keepalive","ts":1}\n')
        await resp.write(
            json.dumps({"type": "closed", "code": code, "reason": "revoked"}).encode() + b"\n"
        )
        return resp

    app = web.Application()
    app.router.add_post("/v1/home/poll", poll)
    server = await _serve(app)
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            channel = PollChannel(MachineClient(http, MachineKey(bytes(32)), enrollment))
            await channel.open()
            seen = []
            with pytest.raises(ChannelClosed) as raised:
                async for line in channel.lines():
                    seen.append(line["type"])
            assert seen == ["keepalive"], "the closed line is the end, not a line"
            assert raised.value.reason == reason and raised.value.code == code
            await channel.close()
    finally:
        await server.close()


@pytest.mark.parametrize(
    ("probe_status", "code", "kind"),
    [
        (401, "invalid_signature", "revoked"),
        (401, "machine_authorization_expired", "lapsed"),
        (401, "signature_clock_skew", None),
        (404, "unauthorized", None),
    ],
)
async def test_a_refused_websocket_asks_why(probe_status: int, code: str, kind: str | None) -> None:
    probes: list[str] = []

    async def session(request: web.Request) -> web.Response:
        return web.Response(status=401)

    async def frames(request: web.Request) -> web.Response:
        probes.append(request.query["session"])
        assert await request.read() == b""
        return web.json_response({"error": {"code": code, "message": "x"}}, status=probe_status)

    app = web.Application()
    app.router.add_get("/v1/home/session", session)
    app.router.add_post("/v1/home/frames", frames)
    server = await _serve(app)
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            channel = WsChannel(MachineClient(http, MachineKey(bytes(32)), enrollment))
            with pytest.raises(NotAuthorized) as raised:
                await channel.open()
            assert raised.value.code == code
            assert raised.value.kind == kind
            assert probes == ["00000000-0000-0000-0000-000000000000"]
    finally:
        await server.close()


async def test_a_refused_websocket_whose_probe_cannot_be_sent_is_not_terminal() -> None:
    async def session(request: web.Request) -> web.Response:
        return web.Response(status=401)

    async def frames(request: web.Request) -> web.Response:
        raise ConnectionResetError

    app = web.Application()
    app.router.add_get("/v1/home/session", session)
    app.router.add_post("/v1/home/frames", frames)
    server = await _serve(app)
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment(str(server.make_url("")).rstrip("/"), "m", "k")
            channel = WsChannel(MachineClient(http, MachineKey(bytes(32)), enrollment))
            with pytest.raises(NotAuthorized) as raised:
                await channel.open()
            assert raised.value.kind is None
    finally:
        await server.close()
