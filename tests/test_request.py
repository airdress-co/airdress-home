"""Signed requests as they reach a server: what is sent is what was signed."""

from __future__ import annotations

import base64
import functools
import json
import re
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from airdress_home import machine
from airdress_home.codes import MachineKey
from airdress_home.httpsig import content_digest, sign_request, signature_base
from airdress_home.machine import Enrollment, MachineClient

V: dict[str, Any] = json.loads((Path(__file__).parent / "vectors" / "vectors.json").read_text())
KEY = MachineKey.from_b64(V["machine"]["seed"])
MACHINE_ID, KID = V["machine"]["keyid"].removeprefix("machine:").split("#")


def verify_as_received(request: web.Request, body: bytes, origin: str) -> None:
    """Verify the signature from the headers the server actually got."""
    params = request.headers["Signature-Input"].removeprefix("sig1=")
    components = re.findall(r'"([^"]+)"', params.split(")")[0])
    assert request.headers["Content-Digest"] == content_digest(body)
    if "Content-Type" in request.headers:
        assert "content-type" in components, "a Content-Type the signature does not cover"
    base = signature_base(
        request.method, origin + request.path_qs, dict(request.headers), components, params
    )
    sig = base64.b64decode(request.headers["Signature"].removeprefix("sig1=:").rstrip(":"))
    Ed25519PublicKey.from_public_bytes(KEY.public).verify(sig, base)


async def _serve(handler: Any) -> TestServer:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.mark.parametrize(
    ("method", "body", "content_type"),
    [("GET", b"", None), ("POST", b"", None), ("POST", b'{"a":1}', "application/json")],
)
async def test_what_is_sent_is_what_was_signed(
    method: str, body: bytes, content_type: str | None
) -> None:
    got: list[tuple[web.Request, bytes]] = []

    async def handler(request: web.Request) -> web.Response:
        got.append((request, await request.read()))
        return web.json_response({})

    server = await _serve(handler)
    origin = str(server.make_url("")).rstrip("/")
    try:
        async with aiohttp.ClientSession() as http:
            client = MachineClient(http, KEY, Enrollment(origin, MACHINE_ID, KID))
            resp = await client.request(method, "/v1/x?y=1", body, content_type=content_type)
            resp.release()
    finally:
        await server.close()
    ((request, received),) = got
    assert received == body
    if content_type is None:
        assert "Content-Type" not in request.headers
    verify_as_received(request, received, origin)


async def test_a_bodiless_get_matches_the_shared_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    case = next(c for c in V["httpsig"] if c["method"] == "GET")
    monkeypatch.setattr(
        machine,
        "sign_request",
        functools.partial(
            sign_request, created=case["created"], nonce=case["nonce"], lifetime=case["lifetime"]
        ),
    )
    got: list[web.Request] = []

    async def handler(request: web.Request) -> web.Response:
        got.append(request)
        return web.json_response({})

    server = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as http:
            enrollment = Enrollment("https://op.example", MACHINE_ID, KID)
            client = MachineClient(http, KEY, enrollment)
            signed = client.sign("GET", "/v1/home/session")
            assert signed == case["headers"]
            # Send it for real, to a server standing in for op.example.
            client.enrollment = Enrollment(str(server.make_url("")).rstrip("/"), MACHINE_ID, KID)
            resp = await client.request("GET", "/v1/home/session")
            resp.release()
    finally:
        await server.close()
    (request,) = got
    assert "Content-Type" not in request.headers
    assert request.headers["Content-Digest"] == case["headers"]["Content-Digest"]
