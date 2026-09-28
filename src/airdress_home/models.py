"""The values frames carry, as typed objects.

Nothing here holds a state value, a friendly name or any other data about the
home: only entity ids, device classes and the names the owner gave a ``Home``'s
events on the operator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, TypeIs

# Outcomes of a ``call`` or ``read``, as the ``result`` / ``read_result``
# frame carries them.
OK: Final = "ok"
"""The action ran; the state was read."""
REJECTED: Final = "rejected"
"""The request was malformed, or asked for something outside the protocol."""
NOT_EXPOSED: Final = "not_exposed"
"""A target is not shared with the operator at the level the request needs."""
SENSITIVE_REFUSED: Final = "sensitive_refused"
"""A target is sensitive and the hub's own opt-in does not cover it."""
NOT_FOUND: Final = "not_found"
"""A target does not exist on the hub."""
FAILED: Final = "failed"
"""The hub tried and its action failed."""
RATE_LIMITED: Final = "rate_limited"
"""The hub's own ceiling refused the request."""
EXPIRED: Final = "expired"
"""The frame's ``notAfter`` had passed when it arrived; it was not run."""

MAX_EVENTS: Final = 32
"""How many event declarations a ``features`` frame may carry."""
MAX_EVENT_TYPES: Final = 32
"""How many types one event declaration may carry."""

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")


def valid_name(name: object) -> TypeIs[str]:
    """A name the operator may give an event or an event type: a DNS-label-like
    word of lowercase letters, digits, ``_`` and ``-``."""
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class SharedEntity:
    """One entity the hub shares. ``device_class`` decides whether a cover is
    sensitive; it is never a friendly name."""

    entity: str
    device_class: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The entity as a ``shared`` frame lists it."""
        wire: dict[str, Any] = {"entity": self.entity}
        if self.device_class is not None:
            wire["deviceClass"] = self.device_class
        return wire


@dataclass(frozen=True)
class Shared:
    """What the hub shares, and at which level. Operate implies observe: an
    entity listed at ``operate`` is also listed at ``observe``."""

    integration_version: str
    hub_version: str
    operate: tuple[SharedEntity, ...] = ()
    observe: tuple[SharedEntity, ...] = ()


@dataclass(frozen=True)
class EventDeclaration:
    """An event the operator may emit to the hub: its name and its types."""

    name: str
    types: tuple[str, ...]


@dataclass(frozen=True)
class Features:
    """What the operator's ``Home`` declares for the hub."""

    events: tuple[EventDeclaration, ...] = ()
    notify: bool = False
    trackers: tuple[str, ...] = field(default_factory=tuple)
    dropped: int = 0
    """Declarations that were malformed and left out."""
