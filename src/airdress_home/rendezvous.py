"""The hub rendezvous: finding the owner's operator without typing its address.

The hub (``airdress.co``) only introduces. It never approves a machine and
never sees a machine key:

1. :func:`start` — ``POST /api/link/start``. Show the owner the
   ``verification_uri`` and ``user_code``.
2. The owner signs in at the hub, confirms the code and picks an airdress.
3. :func:`poll` — ``POST /api/link/poll`` until it answers the operator's
   ``origin``.
4. The machine enrolls **directly with that operator**
   (:func:`airdress_home.machine.start_enrollment`), so the confirmation code
   comes from the real origin.
5. :func:`enrolled` — ``POST /api/link/enrolled`` with the operator's user
   code, so the hub can send the owner's browser on to the operator's approve
   page. The hub builds that redirect itself from the origin it bound.

The origin the hub answers is a hint: the enrollment's operator proof and the
confirmation code the owner compares are what tie it to the owner's operator.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

from .errors import EnrollmentDenied, EnrollmentError, EnrollmentExpired

DEFAULT_HUB = "https://airdress.co"


@dataclass(frozen=True)
class LinkStarted:
    """What the hub answered to ``start``."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: int
    expires_in: int


def _hub(hub: str, path: str) -> str:
    return hub.rstrip("/") + path


async def _error(resp: aiohttp.ClientResponse) -> EnrollmentError:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return EnrollmentError(
        str(body.get("error", f"http_{resp.status}")), str(body.get("error_description", ""))
    )


def check_origin(origin: str) -> str:
    """Accept only a bare ``https`` origin: no path, query, fragment or userinfo."""
    parts = urlsplit(origin.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise EnrollmentError("bad_origin", "the hub answered something that is not an origin")
    port = f":{parts.port}" if parts.port else ""
    return f"https://{parts.hostname.lower()}{port}"


async def start(http: aiohttp.ClientSession, *, hub: str = DEFAULT_HUB) -> LinkStarted:
    """Begin a rendezvous."""
    async with http.post(_hub(hub, "/api/link/start"), json={}) as resp:
        if resp.status >= 300:
            raise await _error(resp)
        raw = await resp.json()
    return LinkStarted(
        device_code=raw["device_code"],
        user_code=raw["user_code"],
        verification_uri=raw["verification_uri"],
        verification_uri_complete=raw.get("verification_uri_complete"),
        interval=int(raw.get("interval", 5)),
        expires_in=int(raw.get("expires_in", 600)),
    )


async def poll(http: aiohttp.ClientSession, started: LinkStarted, *, hub: str = DEFAULT_HUB) -> str:
    """Poll until the owner binds an airdress; return its operator's origin."""
    deadline = time.monotonic() + started.expires_in
    interval = max(1, started.interval)
    while True:
        await asyncio.sleep(interval)
        async with http.post(
            _hub(hub, "/api/link/poll"), json={"device_code": started.device_code}
        ) as resp:
            if resp.status < 300:
                raw = await resp.json()
                return check_origin(str(raw["origin"]))
            err = await _error(resp)
        if err.code == "slow_down":
            interval += 5
        elif err.code in ("access_denied", "denied"):
            raise EnrollmentDenied(err.code, err.description)
        elif err.code in ("expired_token", "expired"):
            raise EnrollmentExpired(err.code, err.description)
        elif err.code not in ("authorization_pending", "pending"):
            raise err
        if time.monotonic() >= deadline:
            raise EnrollmentExpired("expired_token", "the owner did not link in time")


async def enrolled(
    http: aiohttp.ClientSession,
    started: LinkStarted,
    operator_user_code: str,
    *,
    hub: str = DEFAULT_HUB,
) -> None:
    """Tell the hub which enrollment to send the owner's browser on to."""
    async with http.post(
        _hub(hub, "/api/link/enrolled"),
        json={"device_code": started.device_code, "operator_user_code": operator_user_code},
    ) as resp:
        if resp.status >= 300:
            raise await _error(resp)
