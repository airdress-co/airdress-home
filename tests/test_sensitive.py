"""The sensitive set: every domain and class, and a cover without a class."""

from __future__ import annotations

import pytest

from airdress_home.sensitive import is_sensitive


@pytest.mark.parametrize(
    ("entity", "device_class", "expected"),
    [
        ("lock.front_door", None, True),
        ("lock.front_door", "anything", True),
        ("alarm_control_panel.house", None, True),
        ("cover.garage", "garage", True),
        ("cover.front", "door", True),
        ("cover.drive", "gate", True),
        ("cover.skylight", "window", True),
        ("cover.unknown", None, True),
        ("cover.living_room", "blind", False),
        ("cover.patio", "awning", False),
        ("cover.bedroom", "curtain", False),
        ("cover.vent", "damper", False),
        ("cover.office", "shade", False),
        ("cover.kitchen", "shutter", False),
        ("button.office_pc", None, False),
        ("script.wake", None, False),
        ("switch.garage_door", None, False),
    ],
)
def test_the_sensitive_set(entity: str, device_class: str | None, expected: bool) -> None:
    assert is_sensitive(entity, device_class) is expected
