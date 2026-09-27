"""The meta-package installs and re-exports airdress-home."""

import importlib


def test_airdress_home_is_airdress_home() -> None:
    import airdress.home

    import airdress
    import airdress_home

    assert airdress.home is airdress_home
    assert importlib.import_module("airdress.home").MachineKey is airdress_home.MachineKey
