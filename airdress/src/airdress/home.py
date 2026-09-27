"""``airdress.home`` is ``airdress_home``: the same module, not a copy."""

import sys

import airdress_home

sys.modules[__name__] = airdress_home
