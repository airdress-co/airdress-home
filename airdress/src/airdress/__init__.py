"""The Airdress Python packages.

This package is a small, working meta-package: installing it installs
``airdress-home``, and ``import airdress.home`` is that package.

* ``airdress-home`` (``import airdress_home``) — link a home hub such as Home
  Assistant to an airdress.
"""

from __future__ import annotations

from . import home

__all__ = ["home"]
__version__ = "0.0.0"
