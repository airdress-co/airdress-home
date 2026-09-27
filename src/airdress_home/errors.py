"""Errors raised by airdress-home."""

from __future__ import annotations


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
    """The operator refused this machine's signature or its approval has lapsed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HomeNotLinked(AirdressHomeError):
    """This machine is enrolled but not linked as a home."""


class ProtocolError(AirdressHomeError):
    """A frame outside the protocol, or one that failed verification."""


class ChannelClosed(AirdressHomeError):
    """The channel ended. ``reason`` says why, for logs and statistics."""

    def __init__(self, reason: str, code: int | None = None) -> None:
        super().__init__(reason if code is None else f"{reason} ({code})")
        self.reason = reason
        self.code = code
