"""Link a home hub such as Home Assistant to an airdress.

* :mod:`airdress_home.machine` — enroll as a machine, and sign requests;
* :mod:`airdress_home.rendezvous` — find the owner's operator through the hub;
* :mod:`airdress_home.session` and :mod:`airdress_home.channel` — the held
  channel the operator reaches the hub over.
"""

from __future__ import annotations

from .codes import MachineKey, confirmation_code
from .errors import (
    AirdressHomeError,
    ChannelClosed,
    EnrollmentDenied,
    EnrollmentError,
    EnrollmentExpired,
    HomeNotLinked,
    NotAuthorized,
    OperatorProofError,
    ProtocolError,
)
from .machine import Enrollment, MachineClient, Started, poll_until_decided, start_enrollment
from .session import Handler, HomeSession, SessionStats

__all__ = [
    "AirdressHomeError",
    "ChannelClosed",
    "Enrollment",
    "EnrollmentDenied",
    "EnrollmentError",
    "EnrollmentExpired",
    "Handler",
    "HomeNotLinked",
    "HomeSession",
    "MachineClient",
    "MachineKey",
    "NotAuthorized",
    "OperatorProofError",
    "ProtocolError",
    "SessionStats",
    "Started",
    "confirmation_code",
    "poll_until_decided",
    "start_enrollment",
]

__version__ = "0.1.0"
