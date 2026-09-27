"""Keys, fingerprints, kids and the enrollment codes.

Every construction here matches the operator's byte for byte; the test
vectors in ``tests/vectors`` pin that.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ENROLL_PROOF_DOMAIN = b"airdress-machine-enroll-v1\x00"
ANSWER_DOMAIN = b"airdress-machine-enroll-answer-v1\x00"
CODE_DOMAIN = b"airdress-machine-confirm-v1\x00"
_SEPARATOR = b"\x1f"
_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def b64url(data: bytes) -> str:
    """Base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode base64url with or without padding."""
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_bytes(key: Ed25519PublicKey) -> bytes:
    """The raw 32 bytes of an Ed25519 public key."""
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def fingerprint(public_key: bytes) -> str:
    """``SHA256:`` and the unpadded base64 of the key's SHA-256, as OpenSSH prints one."""
    digest = hashlib.sha256(public_key).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


def derive_kid(public_key: bytes) -> str:
    """``k-`` and the first eight bytes of the key's SHA-256, in hex."""
    return "k-" + hashlib.sha256(public_key).digest()[:8].hex()


def pairing_code(payload: bytes) -> str:
    """The first 100 bits of SHA-256, base32, in five groups of four."""
    digest = int.from_bytes(hashlib.sha256(payload).digest(), "big")
    bits = digest >> (256 - 100)
    chars = [_BASE32[(bits >> (95 - 5 * i)) & 0x1F] for i in range(20)]
    return "-".join("".join(chars[i : i + 4]) for i in range(0, 20, 4))


def normalize_origin(origin: str) -> str:
    """``https://Op.Example/`` becomes ``https://op.example``."""
    return origin.strip().rstrip("/").lower()


def answer_message(origin: str, user_code: str, machine_key: bytes) -> bytes:
    """The bytes an operator signs when it answers an enrollment."""
    return (
        ANSWER_DOMAIN
        + normalize_origin(origin).encode()
        + _SEPARATOR
        + user_code.encode()
        + _SEPARATOR
        + machine_key
    )


def verify_answer(
    operator_key: bytes, proof: str, origin: str, user_code: str, machine_key: bytes
) -> bool:
    """Whether ``proof`` is the operator's signature over its answer."""
    try:
        Ed25519PublicKey.from_public_bytes(operator_key).verify(
            b64url_decode(proof), answer_message(origin, user_code, machine_key)
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def confirmation_code(operator_key: bytes, machine_key: bytes, user_code: str) -> str:
    """The code both ends show, which the owner compares."""
    return pairing_code(CODE_DOMAIN + operator_key + machine_key + user_code.encode())


def code_matches(given: str, code: str) -> bool:
    """Compare codes forgiving case, spaces and hyphens."""

    def norm(s: str) -> str:
        return "".join(c for c in s if c not in "- ").upper()

    return bool(norm(given)) and norm(given) == norm(code)


class MachineKey:
    """A machine's Ed25519 key."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("an Ed25519 seed is 32 bytes")
        self._seed = seed
        self._private = Ed25519PrivateKey.from_private_bytes(seed)
        self.public = public_bytes(self._private.public_key())

    @classmethod
    def generate(cls) -> MachineKey:
        """A fresh key."""
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_b64(cls, text: str) -> MachineKey:
        """A key from its base64url seed, as the operator's key file stores it."""
        return cls(b64url_decode(text))

    def seed_b64(self) -> str:
        """The seed, base64url. Secret."""
        return b64url(self._seed)

    @property
    def public_b64(self) -> str:
        """The public key, base64url, as enrollment sends it."""
        return b64url(self.public)

    @property
    def fingerprint(self) -> str:
        """The fingerprint the owner compares."""
        return fingerprint(self.public)

    @property
    def kid(self) -> str:
        """The kid the operator registers this key under."""
        return derive_kid(self.public)

    def sign(self, message: bytes) -> bytes:
        """An Ed25519 signature."""
        return self._private.sign(message)

    def proof(self, device_code: str) -> str:
        """The signature that proves possession while polling an enrollment."""
        return b64url(self.sign(ENROLL_PROOF_DOMAIN + device_code.encode()))

    def __repr__(self) -> str:
        return f"MachineKey(fingerprint={self.fingerprint!r})"
