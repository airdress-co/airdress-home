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
    HomeDisabled,
    HomeNotLinked,
    NotAuthorized,
    OperatorProofError,
    ProtocolError,
    TransportRefused,
)
from .machine import (
    PURPOSE_HOME_ASSISTANT,
    Enrollment,
    MachineClient,
    Started,
    confirm_operator,
    poll_until_decided,
    start_enrollment,
)
from .models import EventDeclaration, Features, Shared, SharedEntity, Track
from .sensitive import is_sensitive
from .session import Handler, HomeSession, SessionStats

__all__ = [
    "PURPOSE_HOME_ASSISTANT",
    "AirdressHomeError",
    "ChannelClosed",
    "Enrollment",
    "EnrollmentDenied",
    "EnrollmentError",
    "EnrollmentExpired",
    "EventDeclaration",
    "Features",
    "Handler",
    "HomeDisabled",
    "HomeNotLinked",
    "HomeSession",
    "MachineClient",
    "MachineKey",
    "NotAuthorized",
    "OperatorProofError",
    "ProtocolError",
    "SessionStats",
    "Shared",
    "SharedEntity",
    "Started",
    "Track",
    "TransportRefused",
    "confirm_operator",
    "confirmation_code",
    "is_sensitive",
    "poll_until_decided",
    "start_enrollment",
]

__version__ = "0.1.0b1"
