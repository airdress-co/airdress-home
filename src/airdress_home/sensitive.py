"""Entities that are sensitive to operate: entry points and alarms.

A lock, an alarm panel, and a cover that is a garage door, a door, a gate or a
window — or a cover that says nothing about what it is — are **sensitive**.
They are refused at operate unless the owner opted each one in, on the hub
**and** on the operator. The two lists are independent: neither side relies on
the other being honest. Observing a sensitive entity is never affected.

The set is closed and versioned with the protocol. Widening it is a protocol
change and a library release; narrowing it would weaken a default, so it is
not done.
"""

from __future__ import annotations

SENSITIVE_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel"})
"""Every entity of these domains is sensitive."""

SENSITIVE_COVER_CLASSES: frozenset[str | None] = frozenset(
    {"garage", "door", "gate", "window", None}
)
"""Cover device classes that are sensitive. ``None`` — no class — fails closed."""

SENSITIVE_CANDIDATE_DOMAINS: frozenset[str] = SENSITIVE_DOMAINS | {"cover"}
"""The domains in which a sensitive entity can be found at all."""


def is_sensitive(entity_id: str, device_class: str | None) -> bool:
    """Whether operating ``entity_id`` needs both sides' opt-in.

    ``device_class`` is what the hub reports for the entity; a cover without
    one is sensitive.
    """
    domain = entity_id.split(".", 1)[0]
    if domain in SENSITIVE_DOMAINS:
        return True
    return domain == "cover" and device_class in SENSITIVE_COVER_CLASSES
