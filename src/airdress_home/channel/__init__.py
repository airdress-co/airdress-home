"""The held channel between a home hub and its operator.

The hub is multi-transport: every transport carries the same signed frames,
``seq`` and session, and the hub negotiates which one to ride.

* :class:`~airdress_home.channel.ws.WsChannel` — one WebSocket;
* :class:`~airdress_home.channel.poll.PollChannel` — a streaming long-poll,
  rotated before the relay's idle timeout, with batched upstream requests;
* :class:`~airdress_home.channel.negotiate.NegotiatingChannel` — tries them in
  order, falls back, and remembers per network what worked
  (:mod:`~airdress_home.channel.hints`).

Every one implements :class:`Channel`. The session above them
(:mod:`airdress_home.session`) verifies frames and answers calls, and never
knows which transport it runs on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import Channel, ChannelStats
from .hints import FileHintStore, Hint, HintStore, MemoryHintStore, network_key
from .negotiate import DEFAULT_ORDER, NegotiatingChannel, NegotiationStats
from .poll import PollChannel
from .ws import WsChannel

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..machine import MachineClient

__all__ = [
    "DEFAULT_ORDER",
    "Channel",
    "ChannelStats",
    "FileHintStore",
    "Hint",
    "HintStore",
    "MemoryHintStore",
    "NegotiatingChannel",
    "NegotiationStats",
    "PollChannel",
    "WsChannel",
    "for_client",
]

_TRANSPORTS: dict[str, Callable[[MachineClient], Channel]] = {
    "ws": WsChannel,
    "poll": PollChannel,
}


def for_client(
    client: MachineClient,
    *,
    hints: HintStore | None = None,
    order: Sequence[str] = DEFAULT_ORDER,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> NegotiatingChannel:
    """The channel an application should hold.

    Applications call this rather than naming a transport. It negotiates over
    every transport this library has, in ``order`` (the default order is the
    library's), and keeps what worked on each network in ``hints`` — pass a
    persistent store, or the hint lasts only as long as the process.
    """
    unknown = [name for name in order if name not in _TRANSPORTS]
    if unknown:
        raise ValueError(f"unknown transports: {unknown}")
    return NegotiatingChannel(
        [_TRANSPORTS[name](client) for name in order],
        network=lambda: network_key(client.origin),
        hints=hints,
        on_event=on_event,
    )
