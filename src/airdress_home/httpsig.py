"""The RFC 9421 request-signing profile an airdress machine signs with.

Covered components are ``@method``, ``@target-uri`` and ``content-digest``
(RFC 9530, ``sha-256``), plus ``content-type`` and ``authorization`` when the
request carries them. Parameters are ``created``, ``expires``, a 128-bit random
``nonce``, ``alg="ed25519"``, ``keyid`` and ``tag="airdress-machine"``,
serialized in that order under the label ``sig1``.

``@target-uri`` is the operator's public ``https`` origin plus the path and
query, even for a WebSocket upgrade sent to ``wss://``.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Mapping

from .codes import MachineKey, b64url

MACHINE_TAG = "airdress-machine"
LABEL = "sig1"
DEFAULT_LIFETIME = 60


def content_digest(body: bytes) -> str:
    """``sha-256=:<base64>:`` over ``body``."""
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii") + ":"


def keyid_for(machine_id: str, kid: str) -> str:
    """The keyid a machine signs with: ``machine:<uuid>#<kid>``."""
    return f"machine:{machine_id}#{kid}"


def signature_base(
    method: str,
    target_uri: str,
    headers: Mapping[str, str],
    components: list[str],
    params: str,
) -> bytes:
    """The RFC 9421 signature base for these components and parameters."""
    lines = []
    lower = {k.lower(): v for k, v in headers.items()}
    for c in components:
        if c == "@method":
            value = method.upper()
        elif c == "@target-uri":
            value = target_uri
        else:
            value = lower[c].strip()
        lines.append(f'"{c}": {value}')
    lines.append(f'"@signature-params": {params}')
    return "\n".join(lines).encode()


def sign_request(
    key: MachineKey,
    keyid: str,
    method: str,
    target_uri: str,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
    *,
    created: int | None = None,
    nonce: str | None = None,
    lifetime: int = DEFAULT_LIFETIME,
) -> dict[str, str]:
    """The headers to send: the caller's, with ``Content-Digest``,
    ``Signature-Input`` and ``Signature`` set (and any caller copy replaced)."""
    out = {
        k: v
        for k, v in (headers or {}).items()
        if k.lower() not in {"signature", "signature-input", "content-digest"}
    }
    out["Content-Digest"] = content_digest(body)
    lower = {k.lower() for k in out}
    components = ["@method", "@target-uri", "content-digest"]
    if "content-type" in lower:
        components.append("content-type")
    if "authorization" in lower:
        components.append("authorization")
    created = int(time.time()) if created is None else created
    nonce = b64url(secrets.token_bytes(16)) if nonce is None else nonce
    inner = " ".join(f'"{c}"' for c in components)
    params = (
        f"({inner});created={created};expires={created + lifetime}"
        f';nonce="{nonce}";alg="ed25519";keyid="{keyid}";tag="{MACHINE_TAG}"'
    )
    base = signature_base(method, target_uri, out, components, params)
    signature = base64.b64encode(key.sign(base)).decode("ascii")
    out["Signature-Input"] = f"{LABEL}={params}"
    out["Signature"] = f"{LABEL}=:{signature}:"
    return out
