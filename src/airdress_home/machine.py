"""The machine's side of an airdress operator: enrollment and signed requests.

A home hub is an enrolled machine. It generates an Ed25519 key, asks the
operator to enroll it, and waits while the owner compares a code and approves.
It never receives a bearer or a secret: it signs every later request with its
own key.

The operator signs its enrollment answer with its answer-signing key. The
machine verifies that proof, computes the confirmation code the owner compares
from the key whose proof verified, and **pins** the key: every frame the
operator later sends on the channel must verify under it.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp

from .codes import MachineKey, b64url_decode, confirmation_code, verify_answer
from .errors import (
    EnrollmentDenied,
    EnrollmentError,
    EnrollmentExpired,
    NotAuthorized,
    OperatorProofError,
)
from .httpsig import keyid_for, sign_request


@dataclass(frozen=True)
class Enrollment:
    """Which operator and machine a key belongs to. Nothing in it is secret.

    The field names match the operator's own enrollment record, so a record
    written by either side can be read by the other.
    """

    operator: str
    machine_id: str
    kid: str
    authorized_until: str | None = None
    operator_key: str | None = None
    """The operator's answer-signing key, base64url: the pinned key."""

    def to_json(self) -> str:
        """The record as JSON, omitting what is unset."""
        return json.dumps({k: v for k, v in asdict(self).items() if v is not None})

    @classmethod
    def from_json(cls, text: str) -> Enrollment:
        """A record from JSON."""
        raw = json.loads(text)
        return cls(
            operator=raw["operator"],
            machine_id=raw["machine_id"],
            kid=raw["kid"],
            authorized_until=raw.get("authorized_until"),
            operator_key=raw.get("operator_key"),
        )

    @property
    def pinned_key(self) -> bytes | None:
        """The pinned operator key's 32 bytes, if one was confirmed."""
        return b64url_decode(self.operator_key) if self.operator_key else None


@dataclass
class Started:
    """What the operator answered to an enrollment request."""

    device_code: str
    user_code: str
    expires_in: int
    interval: int
    fingerprint: str
    verification_uri: str | None = None
    verification_uri_complete: str | None = None
    operator_key: str | None = None
    operator_kid: str | None = None
    operator_proof: str | None = None
    confirmation_code: str | None = field(default=None)
    """Computed here from the key whose proof verified, never read from the answer."""


def _endpoint(operator: str, path: str) -> str:
    return operator.rstrip("/") + path


async def _error_of(resp: aiohttp.ClientResponse) -> EnrollmentError:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        body = {}
    code = str(body.get("error", f"http_{resp.status}")) if isinstance(body, dict) else ""
    description = str(body.get("error_description", "")) if isinstance(body, dict) else ""
    return EnrollmentError(code or f"http_{resp.status}", description)


def confirm_operator(
    started: Started, operator: str, key: MachineKey, *, require_proof: bool = False
) -> None:
    """Verify the operator's proof on ``started`` and compute the confirmation code.

    Raises :class:`OperatorProofError` when the proof does not verify, or is
    missing while ``require_proof`` (an operator found by discovery rather than
    typed by the owner).
    """
    if started.operator_key is None or started.operator_proof is None:
        if require_proof:
            raise OperatorProofError("the operator did not sign its answer")
        return
    try:
        op_pub = b64url_decode(started.operator_key)
    except ValueError as e:
        raise OperatorProofError("the operator's key is malformed") from e
    if len(op_pub) != 32 or not verify_answer(
        op_pub, started.operator_proof, operator, started.user_code, key.public
    ):
        raise OperatorProofError("the operator's proof does not verify")
    started.confirmation_code = confirmation_code(op_pub, key.public, started.user_code)


async def start_enrollment(
    http: aiohttp.ClientSession,
    operator: str,
    key: MachineKey,
    name: str,
    *,
    preauth_key: str | None = None,
    require_proof: bool = False,
) -> Started:
    """Ask ``operator`` to enroll ``key`` as ``name``."""
    body: dict[str, Any] = {"public_key": key.public_b64, "name": name}
    if preauth_key is not None:
        if not operator.startswith("https://"):
            raise EnrollmentError("insecure_origin", "a pre-auth key is sent only over https")
        body["preauth_key"] = preauth_key.strip()
    async with http.post(_endpoint(operator, "/v1/machines/enroll"), json=body) as resp:
        if resp.status == 404:
            raise EnrollmentError("not_enabled", "this operator does not enroll machines")
        if resp.status >= 400:
            raise await _error_of(resp)
        raw = await resp.json()
    op_key = raw.get("operator_key") or {}
    started = Started(
        device_code=raw["device_code"],
        user_code=raw["user_code"],
        expires_in=int(raw["expires_in"]),
        interval=int(raw["interval"]),
        fingerprint=raw["fingerprint"],
        verification_uri=raw.get("verification_uri"),
        verification_uri_complete=raw.get("verification_uri_complete"),
        operator_key=op_key.get("public_key"),
        operator_kid=op_key.get("kid"),
        operator_proof=raw.get("operator_proof"),
    )
    if started.fingerprint != key.fingerprint:
        raise EnrollmentError("fingerprint_mismatch", "the operator reported another key")
    confirm_operator(started, operator, key, require_proof=require_proof)
    return started


async def poll_until_decided(
    http: aiohttp.ClientSession, operator: str, key: MachineKey, started: Started
) -> Enrollment:
    """Poll until the owner decides; the approved enrollment pins the operator key."""
    deadline = time.monotonic() + started.expires_in
    interval = max(1, started.interval)
    body = {"device_code": started.device_code, "proof": key.proof(started.device_code)}
    while True:
        await asyncio.sleep(interval)
        async with http.post(_endpoint(operator, "/v1/machines/enroll/poll"), json=body) as resp:
            if resp.status < 300:
                raw = await resp.json()
                return Enrollment(
                    operator=operator.rstrip("/"),
                    machine_id=str(raw["machine_id"]),
                    kid=raw["kid"],
                    authorized_until=raw.get("authorized_until"),
                    operator_key=started.operator_key,
                )
            err = await _error_of(resp)
        if err.code == "slow_down":
            interval += 5
        elif err.code == "access_denied":
            raise EnrollmentDenied(err.code, err.description)
        elif err.code == "expired_token":
            raise EnrollmentExpired(err.code, err.description)
        elif err.code != "authorization_pending":
            raise err
        if time.monotonic() >= deadline:
            raise EnrollmentExpired("expired_token", "the owner did not decide in time")


class MachineClient:
    """Signed requests as an enrolled machine."""

    def __init__(
        self, http: aiohttp.ClientSession, key: MachineKey, enrollment: Enrollment
    ) -> None:
        self.http = http
        self.key = key
        self.enrollment = enrollment
        self.signed_requests = 0
        """How many requests this client has signed: one replay-cache slot each."""

    @property
    def origin(self) -> str:
        """The operator's ``https`` origin."""
        return self.enrollment.operator.rstrip("/")

    def sign(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Headers signing ``method path`` with ``body``."""
        self.signed_requests += 1
        return sign_request(
            self.key,
            keyid_for(self.enrollment.machine_id, self.enrollment.kid),
            method,
            self.origin + path,
            headers,
            body,
        )

    async def request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        content_type: str | None = None,
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> aiohttp.ClientResponse:
        """Send a signed request. The caller reads and releases the response."""
        headers = {"Content-Type": content_type} if content_type else {}
        signed = self.sign(method, path, body, headers)
        resp = await self.http.request(
            method,
            self.origin + path,
            data=body,
            headers=signed,
            timeout=client_timeout or aiohttp.ClientTimeout(total=30),
        )
        if resp.status == 401:
            try:
                raw = await resp.json(content_type=None)
                code = str(raw["error"]["code"])
            except (ValueError, KeyError, TypeError, aiohttp.ContentTypeError):
                code = "unauthorized"
            resp.release()
            raise NotAuthorized(code)
        return resp
