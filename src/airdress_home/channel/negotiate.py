"""Transport negotiation: one channel over whichever transport works here.

The hub is multi-transport. Every transport carries the same signed frames,
``seq`` and session registry, so which one a channel rides is a property of
the network between the hub and its operator, not of the protocol.
:class:`NegotiatingChannel` holds the transports in their default order and:

* **tries the preferred transport first**, and falls back to the next when its
  establishment is refused on the way (:class:`TransportRefused`: a proxy that
  answers a WebSocket upgrade with something other than ``101``, or cuts it);
* **demotes a transport that keeps dropping early**: ``early_drops`` channels
  in a row that ended within ``early_drop`` seconds of opening, for a reason
  that is not the operator's own (a lifetime close, a revoke, an unlink, a
  displacement) and not a cut the hub forced;
* **remembers per network what worked**, in a small :class:`HintStore`, so the
  next start on that network does not pay for the refusal again;
* **re-probes the preferred transport** once ``reprobe`` seconds have passed
  since it last failed on that network. The operator closes every channel
  within an hour, so a re-probe is never more than an hour late. A re-probed
  transport that drops early once is demoted again at once.

A refusal that is the operator's answer — the machine is not authorized, the
``Home`` is switched off — is the same on every transport and is raised at
once. A WebSocket refused with ``403`` is the exception: a refused upgrade
carries no body, so it cannot tell "not linked" from a proxy's ``403``, and the
next transport is asked; its answer stands.

The session above (:class:`~airdress_home.session.HomeSession`) sees one
channel. Its ``name`` is the transport in use, which is also what the
operator reports as the ``Home``'s transport.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import models
from ..errors import ChannelClosed, HomeDisabled, HomeNotLinked, TransportRefused
from .base import ChannelStats
from .hints import Hint, HintStore, MemoryHintStore

if TYPE_CHECKING:
    from .base import Channel

#: The default order, preferred first. **Provisional:** the transport spike
#: decides the order (and the thresholds below); it does not remove either.
DEFAULT_ORDER: tuple[str, ...] = ("ws", "poll")

#: A channel that ends sooner than this after opening dropped early.
EARLY_DROP = 60.0
#: This many early drops in a row demote a transport on this network.
EARLY_DROPS = 3
#: Seconds after the preferred transport failed on a network before it is
#: tried first there again.
REPROBE = 6 * 3600.0

#: Close reasons that are the operator's decision, not the transport's failure.
_OPERATOR_CLOSES = frozenset(
    f"closed_{code}"
    for code in (
        models.CLOSE_LIFETIME,
        models.CLOSE_REVOKED,
        models.CLOSE_UNLINKED,
        models.CLOSE_DISPLACED,
        models.CLOSE_PROTOCOL,
    )
)


@dataclass
class NegotiationStats:
    """What negotiation did, for logs and measurement."""

    refusals: int = 0
    """Transports refused at establishment."""
    fallbacks: int = 0
    """Channels opened on a transport other than the first one tried."""
    early_drops: int = 0
    demotions: int = 0
    """Transports demoted on a network after repeated early drops."""
    reprobes: int = 0
    """Opens that tried the preferred transport again despite a hint."""
    confirmations: int = 0
    """Re-probes whose channel lived past the early-drop threshold."""


class NegotiatingChannel:
    """A channel that picks its transport, and remembers what worked where."""

    def __init__(
        self,
        transports: Sequence[Channel],
        *,
        network: Callable[[], Awaitable[str]],
        hints: HintStore | None = None,
        early_drop: float = EARLY_DROP,
        early_drops: int = EARLY_DROPS,
        reprobe: float = REPROBE,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        if not transports:
            raise ValueError("at least one transport")
        names = [t.name for t in transports]
        if len(set(names)) != len(names):
            raise ValueError("transport names must be distinct")
        self.transports = list(transports)
        self._network = network
        self._store: HintStore = hints if hints is not None else MemoryHintStore()
        self._hints: dict[str, Hint] = {}
        self._loaded = False
        self.early_drop = early_drop
        self.early_drops_limit = early_drops
        self.reprobe = reprobe
        self._on_event = on_event
        self._clock = clock
        self._wall = wall
        self.negotiation = NegotiationStats()
        self.network: str | None = None
        """The key of the network the last open ran on."""
        self._active: Channel | None = None
        self._opened_at = 0.0
        self._reprobing = False
        self._aborted = False
        self._settled = True
        self._early: dict[str, int] = {}

    # -- what the session sees ------------------------------------------------

    @property
    def preferred(self) -> Channel:
        """The first transport of the default order."""
        return self.transports[0]

    @property
    def active(self) -> Channel | None:
        """The transport the channel last opened on."""
        return self._active

    @property
    def name(self) -> str:
        """The transport in use (the preferred one before the first open)."""
        return (self._active or self.preferred).name

    @property
    def stats(self) -> ChannelStats:
        """The statistics of the transport in use."""
        return (self._active or self.preferred).stats

    def hint(self, network: str | None = None) -> Hint | None:
        """The hint held for ``network`` (default: the last one opened on)."""
        key = network if network is not None else self.network
        if key is None:
            return None
        return self._hints.get(key)

    # -- negotiation ----------------------------------------------------------

    def _event(self, kind: str, **values: Any) -> None:
        if self._on_event is not None:
            self._on_event({"t": round(self._wall(), 3), "kind": kind, **values})

    def _by_name(self, name: str) -> Channel | None:
        return next((t for t in self.transports if t.name == name), None)

    def _plan(self, net: str) -> tuple[list[Channel], bool]:
        """The transports to try, in order, and whether this is a re-probe."""
        hint = self._hints.get(net)
        chosen = self._by_name(hint.transport) if hint is not None else None
        if hint is None or chosen is None or chosen is self.preferred:
            return list(self.transports), False
        failed = hint.preferred_failed
        if failed is None or self._wall() - failed >= self.reprobe:
            return list(self.transports), True
        return [chosen, *(t for t in self.transports if t is not chosen)], False

    async def _remember(self, net: str, hint: Hint | None) -> None:
        if hint is None:
            if self._hints.pop(net, None) is None:
                return
        else:
            self._hints[net] = hint
        await self._store.save(dict(self._hints))

    async def open(self) -> None:
        """Open the channel on the first transport that can be established."""
        if not self._loaded:
            self._hints = await self._store.load()
            self._loaded = True
        net = await self._network()
        self.network = net
        plan, reprobing = self._plan(net)
        if reprobing:
            self.negotiation.reprobes += 1
            self._event("transport_reprobe", transport=self.preferred.name)
        not_linked: HomeNotLinked | None = None
        refused: list[str] = []
        for channel in plan:
            try:
                await channel.open()
            except HomeDisabled:
                raise
            except HomeNotLinked as e:
                # Authoritative from a transport that can read the refusal's
                # body; from a refused upgrade, possibly a proxy's. Ask the next.
                not_linked = e
                refused.append(channel.name)
                continue
            except TransportRefused as e:
                self.negotiation.refusals += 1
                refused.append(channel.name)
                self._event("transport_refused", transport=channel.name, reason=e.reason)
                continue
            await self._opened(net, channel, refused, reprobing)
            return
        if not_linked is not None:
            raise not_linked
        raise TransportRefused("no_transport")

    async def _opened(
        self, net: str, channel: Channel, refused: list[str], reprobing: bool
    ) -> None:
        self._active = channel
        self._opened_at = self._clock()
        self._aborted = False
        self._settled = False
        self._reprobing = reprobing and channel is self.preferred
        if not refused:
            return
        self.negotiation.fallbacks += 1
        self._event("transport_fallback", transport=channel.name, refused=refused)
        if channel is self.preferred:
            await self._remember(net, None)
            return
        old = self.hint(net)
        preferred_failed = (
            self._wall()
            if self.preferred.name in refused
            else (old.preferred_failed if old is not None else None)
        )
        await self._remember(net, Hint(channel.name, self._wall(), preferred_failed))

    async def _settle(self, reason: str | None) -> None:
        """The channel ended (``reason``), or was closed by the hub (``None``)."""
        channel, net = self._active, self.network
        if self._settled or channel is None or net is None:
            return
        self._settled = True
        lived = self._clock() - self._opened_at
        if lived >= self.early_drop:
            self._early[channel.name] = 0
            if self._reprobing:
                # The preferred transport works here again: forget the hint.
                self._reprobing = False
                self.negotiation.confirmations += 1
                self._event("transport_confirmed", transport=channel.name)
                await self._remember(net, None)
            return
        if reason is None or self._aborted or reason in _OPERATOR_CLOSES:
            return
        self.negotiation.early_drops += 1
        count = self._early.get(channel.name, 0) + 1
        self._early[channel.name] = count
        limit = 1 if self._reprobing else self.early_drops_limit
        if count < limit or len(self.transports) == 1:
            return
        self._early[channel.name] = 0
        self._reprobing = False
        following = self.transports[(self.transports.index(channel) + 1) % len(self.transports)]
        self.negotiation.demotions += 1
        self._event(
            "transport_demoted",
            transport=channel.name,
            to=following.name,
            early_drops=count,
            reason=reason,
        )
        if following is self.preferred:
            await self._remember(net, None)
            return
        old = self.hint(net)
        preferred_failed = (
            self._wall()
            if channel is self.preferred
            else (old.preferred_failed if old is not None else None)
        )
        await self._remember(net, Hint(following.name, self._wall(), preferred_failed))

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        channel = self._active
        if channel is None:
            raise ChannelClosed("not_open")
        try:
            async for line in channel.lines():
                yield line
        except ChannelClosed as e:
            await self._settle(e.reason)
            raise

    def bind(self, session: str) -> None:
        if self._active is not None:
            self._active.bind(session)

    def acked(self, seq: int) -> None:
        if self._active is not None:
            self._active.acked(seq)

    async def send(self, frame: dict[str, Any]) -> None:
        if self._active is None:
            raise ChannelClosed("not_open")
        await self._active.send(frame)

    def abort(self) -> None:
        self._aborted = True
        if self._active is not None:
            self._active.abort()

    async def close(self) -> None:
        if self._active is not None:
            await self._settle(None)
            await self._active.close()
