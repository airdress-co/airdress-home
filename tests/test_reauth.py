"""A machine whose approval lapsed asks to be approved again."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from airdress_home import start_reauth
from airdress_home.codes import MachineKey, answer_message, b64url
from airdress_home.errors import EnrollmentError, NotAuthorized, OperatorProofError
from airdress_home.machine import Enrollment, MachineClient

OP = MachineKey(bytes([5]) * 32)
OTHER = MachineKey(bytes([6]) * 32)
KEY = MachineKey(bytes([4]) * 32)


def _answer(origin: str, signer: MachineKey = OP) -> dict[str, Any]:
    return {
        "device_code": "dc",
        "user_code": "WXYZ-2345",
        "expires_in": 600,
        "interval": 5,
        "fingerprint": KEY.fingerprint,
        "verification_uri": f"{origin}/machines/approve",
        "operator_key": {"public_key": b64url(signer.public), "kid": "k-op"},
        "operator_proof": b64url(signer.sign(answer_message(origin, "WXYZ-2345", KEY.public))),
    }


async def _reauth(respond: Callable[[str], web.Response]) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        seen["signature"] = request.headers.get("Signature")
        seen["body"] = await request.read()
        return respond(str(request.url.origin()))

    app = web.Application()
    app.router.add_post("/v1/machines/reauth", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as http:
            origin = str(server.make_url("")).rstrip("/")
            enrollment = Enrollment(origin, "m-1", KEY.kid, operator_key=b64url(OP.public))
            try:
                return await start_reauth(MachineClient(http, KEY, enrollment)), seen
            except Exception as e:  # noqa: BLE001 - returned for the assertion
                return e, seen
    finally:
        await server.close()


async def test_a_signed_reauth_starts_an_approval_from_the_pinned_operator() -> None:
    started, seen = await _reauth(lambda origin: web.json_response(_answer(origin)))
    assert seen["signature"] and seen["body"] == b""
    assert started.user_code == "WXYZ-2345"
    assert started.confirmation_code  # computed from the verified proof


async def test_an_answer_from_another_operator_key_is_refused() -> None:
    result, _ = await _reauth(lambda origin: web.json_response(_answer(origin, OTHER)))
    assert isinstance(result, OperatorProofError)


async def test_a_revoked_machine_is_refused() -> None:
    result, _ = await _reauth(
        lambda origin: web.json_response(
            {"error": "invalid_client", "error_description": "not enrolled"}, status=401
        )
    )
    assert isinstance(result, NotAuthorized)


@pytest.mark.parametrize(("status", "code"), [(404, "not_enabled"), (429, "slow_down")])
async def test_other_refusals_say_why(status: int, code: str) -> None:
    result, _ = await _reauth(
        lambda origin: web.json_response({"error": code, "error_description": "x"}, status=status)
    )
    assert isinstance(result, EnrollmentError)
    assert result.code == code
