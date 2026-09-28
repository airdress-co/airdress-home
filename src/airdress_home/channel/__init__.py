"""The held channel between a home hub and its operator.

Two transports carry the same signed frames until one is chosen:

* :class:`~airdress_home.channel.ws.WsChannel` — one WebSocket;
* :class:`~airdress_home.channel.poll.PollChannel` — a streaming long-poll,
  rotated before the relay's idle timeout, with batched upstream requests.

Both implement :class:`Channel`. The session above them
(:mod:`airdress_home.session`) verifies frames and answers calls, and never
knows which transport it runs on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Channel, ChannelStats
from .poll import PollChannel
from .ws import WsChannel

if TYPE_CHECKING:
    from ..machine import MachineClient

__all__ = ["Channel", "ChannelStats", "PollChannel", "WsChannel", "for_client"]


def for_client(client: MachineClient) -> Channel:
    """The channel an application should hold.

    Applications call this rather than naming a transport, so the choice
    lives here. **Provisional:** until the measurement between the two
    candidates is decided this is the WebSocket; afterwards it is the winner,
    and the other transport is removed before the first release.
    """
    return WsChannel(client)
