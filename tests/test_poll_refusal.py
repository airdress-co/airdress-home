"""A long-poll refused at establishment says why."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from airdress_home.channel import PollChannel
from airdress_home.codes import MachineKey
from airdress_home.errors import HomeDisabled, HomeNotLinked
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
