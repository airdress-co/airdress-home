"""The channel interface both transports implement."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ChannelStats:
    """What a channel counts, for measurement."""

    opens: int = 0
    rotations: int = 0
    rotation_failures: int = 0
    upstream_batches: int = 0
    overlap_lines: int = 0
    """Lines read from a replaced long-poll after its successor opened."""


class Channel(Protocol):
    """A transport for the frames of one home session."""

    @property
    def name(self) -> str:
        """The transport's name, as the operator reports it (``ws``, ``poll``)."""
        ...

    @property
    def stats(self) -> ChannelStats:
        """What the transport counted."""
        ...

    async def open(self) -> None:
        """Establish the channel (signed). Raises on refusal."""

    def lines(self) -> AsyncIterator[dict[str, Any]]:
        """Parsed lines from the operator until the channel ends, then
        :class:`~airdress_home.errors.ChannelClosed`."""
        ...

    def bind(self, session: str) -> None:
        """The operator named this session in its ``hello``."""

    def acked(self, seq: int) -> None:
        """Every frame up to ``seq`` has been handled."""

    async def send(self, frame: dict[str, Any]) -> None:
        """Send one hub frame."""

    def abort(self) -> None:
        """Cut the transport at once, as a network failure would."""

    async def close(self) -> None:
        """Close cleanly."""
