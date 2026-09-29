"""Errors raised by airdress-home."""

from __future__ import annotations

from .models import refusal_kind


class AirdressHomeError(Exception):
    """Base class for every error this library raises."""


class EnrollmentError(AirdressHomeError):
    """The operator refused or failed an enrollment step."""

    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class EnrollmentDenied(EnrollmentError):
    """The owner denied the enrollment."""


class EnrollmentExpired(EnrollmentError):
    """The enrollment expired before the owner decided."""


class OperatorProofError(AirdressHomeError):
    """The operator's signed answer did not verify, or was missing when required."""


class NotAuthorized(AirdressHomeError):
    """The operator refused this machine's signature or its approval has lapsed.

    ``code`` is the refusal's code; ``kind`` says what it means for good —
    ``"revoked"``, ``"lapsed"``, or ``None`` for a refusal a retry can outlast
    (see :func:`airdress_home.models.refusal_kind`).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def kind(self) -> str | None:
        """``"revoked"``, ``"lapsed"``, or ``None``."""
        return refusal_kind(self.code)


class HomeNotLinked(AirdressHomeError):
    """This machine is enrolled but not linked as a home."""


class HomeDisabled(HomeNotLinked):
    """The ``Home`` naming this machine is switched off, or its link is not
    sound (another ``Home`` claims the machine, or the machine holds grants).
    Only a long-poll can tell this from :class:`HomeNotLinked`: a refused
    WebSocket upgrade carries no body the client can read."""


class ProtocolError(AirdressHomeError):
    """A frame outside the protocol, or one that failed verification."""


class ChannelClosed(AirdressHomeError):
    """The channel ended. ``reason`` says why, for logs and statistics."""

    def __init__(self, reason: str, code: int | None = None) -> None:
        super().__init__(reason if code is None else f"{reason} ({code})")
        self.reason = reason
        self.code = code


class TransportRefused(ChannelClosed):
    """This transport could not be established here, though another may be.

    Raised when a WebSocket upgrade is refused on the way (a proxy that answers
    it with something other than ``101``, or cuts it), not when the operator
    refuses the machine: that answer is the same on every transport."""
