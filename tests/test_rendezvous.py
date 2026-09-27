"""The rendezvous client against a stand-in hub."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from airdress_home import rendezvous
from airdress_home.errors import EnrollmentError

STATE: web.AppKey[dict[str, Any]] = web.AppKey("state", dict)


async def hub_app() -> web.Application:
    state: dict[str, Any] = {"polls": 0, "enrolled": None}

    async def start(request: web.Request) -> web.Response:
        state["client_name"] = (await request.json()).get("client_name")
        return web.json_response(
            {
                "device_code": "dc",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://airdress.co/link",
                "verification_uri_complete": "https://airdress.co/link?user_code=WDJB-MJHT",
                "interval": 1,
                "expires_in": 30,
            }
        )

    async def poll(request: web.Request) -> web.Response:
        state["polls"] += 1
        if state["polls"] == 1:
            return web.json_response({"status": "pending"})
        if state["polls"] == 2:
            return web.json_response({"error": "temporarily_unavailable"}, status=503)
        return web.json_response({"status": "bound", "origin": "https://Op.Example/"})

    async def enrolled(request: web.Request) -> web.Response:
        state["enrolled"] = await request.json()
        return web.Response(status=204)

    app = web.Application()
    app[STATE] = state
    app.router.add_post("/api/link/start", start)
    app.router.add_post("/api/link/poll", poll)
    app.router.add_post("/api/link/enrolled", enrolled)
    return app


async def test_the_rendezvous_answers_an_origin_and_never_a_key() -> None:
    app = await hub_app()
    async with TestServer(app) as server, ClientSession() as http:
        hub = str(server.make_url("")).rstrip("/")
        started = await rendezvous.start(http, hub=hub, client_name="Home Assistant")
        assert started.user_code == "WDJB-MJHT"
        origin = await rendezvous.poll(http, started, hub=hub)
        assert origin == "https://op.example"
        await rendezvous.enrolled(http, started, "BCDF-GHJK", hub=hub)
        assert app[STATE]["client_name"] == "Home Assistant"
        assert app[STATE]["enrolled"] == {"device_code": "dc", "operator_user_code": "BCDF-GHJK"}


@pytest.mark.parametrize(
    "bad",
    [
        "http://op.example",
        "https://op.example/path",
        "https://u:p@op.example",
        "https://op.example?x=1",
        "javascript:alert(1)",
    ],
)
def test_only_a_bare_https_origin_is_accepted(bad: str) -> None:
    with pytest.raises(EnrollmentError):
        rendezvous.check_origin(bad)


async def test_a_denial_at_the_hub_is_an_error() -> None:
    from airdress_home.errors import EnrollmentDenied

    async def poll(request: web.Request) -> web.Response:
        return web.json_response({"status": "denied"})

    app = web.Application()
    app.router.add_post("/api/link/poll", poll)
    async with TestServer(app) as server, ClientSession() as http:
        started = rendezvous.LinkStarted("dc", "WDJB-MJHT", "https://x/link", None, 1, 30)
        with pytest.raises(EnrollmentDenied):
            await rendezvous.poll(http, started, hub=str(server.make_url("")).rstrip("/"))
