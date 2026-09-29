"""The package reports the version it was installed as."""

from importlib.metadata import version

import airdress_home


def test_version_is_the_distribution_version() -> None:
    assert airdress_home.__version__ == version("airdress-home")
