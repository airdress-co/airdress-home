"""Transport W: one WebSocket, ``GET /v1/home/session``.

The upgrade request is machine-signed. The hub pings every ``heartbeat``
seconds and the operator does the same, so neither direction of the relay's
splice is silent for its idle timeout.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import aiohttp

from ..errors import ChannelClosed, HomeNotLinked, NotAuthorized, TransportRefused
from ..frames import SUBPROTOCOL, parse_line
from .base import ChannelStats

if TYPE_CHECKING:
    from ..machine import MachineClient

PATH = "/v1/home/session"


#: A session id no session has: the probe's upstream post names it.
_NO_SESSION = "00000000-0000-0000-0000-000000000000"


async def refusal_code(client: MachineClient) -> str:
    """Why the operator refused this machine, read from a signed request.

    A refused WebSocket upgrade carries no body a client can read, so a
    ``401`` there cannot tell a revoked machine from a lapsed approval or a
    clock out of step. One signed, empty upstream post to a session that
    does not exist asks again: it changes nothing on the operator, and a
    ``401`` answer carries the code. ``"unauthorized"`` when the probe was
    not refused (the refusal did not last) or could not be sent.
    """
    try:
        resp = await client.request(
            "POST",
            f"/v1/home/frames?session={_NO_SESSION}",
            b"",
            content_type="application/x-ndjson",
        )
    except NotAuthorized as e:
        return e.code
    except (aiohttp.ClientError, TimeoutError):
        return "unauthorized"
    resp.release()
    return "unauthorized"


class WsChannel:
    """A home channel over one WebSocket."""

    name = "ws"

    def __init__(self, client: MachineClient, *, heartbeat: float = 30.0) -> None:
        self._client = client
        self._heartbeat = heartbeat
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.stats: ChannelStats = ChannelStats()

    async def open(self) -> None:
        origin = self._client.origin
        url = "wss://" + origin[8:] if origin.startswith("https://") else "ws://" + origin[7:]
        headers = self._client.sign("GET", PATH)
        try:
            self._ws = await self._client.http.ws_connect(
                url + PATH,
                headers=headers,
                protocols=(SUBPROTOCOL,),
                heartbeat=self._heartbeat,
                autoping=True,
                timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=10.0),
            )
        except aiohttp.WSServerHandshakeError as e:
            if e.status == 403:
                raise HomeNotLinked(str(e.message)) from e
            if e.status == 401:
                raise NotAuthorized(await refusal_code(self._client)) from e
            if 500 <= e.status < 600 and e.status != 501:
                # The operator or the relay in trouble: no other transport
                # would fare better, so this is not a refused upgrade.
                raise ChannelClosed(f"handshake_{e.status}") from e
            # Anything else answered instead of 101 — including a 200 from a
            # proxy that does not upgrade — refuses this transport, here.
            raise TransportRefused(f"handshake_{e.status}") from e
        except aiohttp.ClientConnectorError:
            raise  # the operator is unreachable, whatever the transport
        except aiohttp.ClientError as e:
            # Reached, then cut during the upgrade: what a filtering middlebox does.
            raise TransportRefused(f"handshake_{type(e).__name__}") from e
        self.stats.opens += 1

    async def lines(self) -> AsyncIterator[dict[str, Any]]:
        ws = self._ws
        if ws is None:
            raise ChannelClosed("not_open")
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                line = parse_line(msg.data)
                if line is not None:
                    yield line
            elif msg.type == aiohttp.WSMsgType.BINARY:
                raise ChannelClosed("binary_frame")
            else:
                break
        code = ws.close_code
        exc = ws.exception()
        reason = f"closed_{code}" if code else "closed"
        if exc is not None:
            reason = f"error_{type(exc).__name__}"
        raise ChannelClosed(reason, code)

    def bind(self, session: str) -> None:
        del session

    def acked(self, seq: int) -> None:
        del seq

    async def send(self, frame: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise ChannelClosed("not_open")
        await self._ws.send_str(json.dumps(frame, separators=(",", ":")))

    def abort(self) -> None:
        ws = self._ws
        if ws is None:
            return
        response = getattr(ws, "_response", None)
        if response is not None:
            response.close()
        else:  # pragma: no cover - aiohttp internals moved
            ws._reader.feed_eof()

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
