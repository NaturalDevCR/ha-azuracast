"""Centrifugo SSE stream for AzuraCast now-playing updates."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp.http_exceptions import HttpProcessingError

from .errors import AzuraCastConnectionError, error_for_status
from .models import NowPlaying

_LOGGER = logging.getLogger(__name__)

# Centrifugo's connect reply carries every station's payload on one line, which
# can exceed aiohttp's default line limit (2 x 256 KiB); allow lines up to 2 MiB.
_READ_BUFSIZE = 2**20


def build_sse_url(base_url: str, shortcodes: Iterable[str]) -> str:
    """Return the SSE URL subscribed to every given station."""
    subs = {f"station:{code}": {"recover": True} for code in shortcodes}
    connect = json.dumps({"subs": subs}, separators=(",", ":"))
    return f"{base_url.rstrip('/')}/api/live/nowplaying/sse?cf_connect={quote(connect, safe='')}"


def _connect_rows(connect: Any) -> list[Any]:
    """Return the publication rows carried by a Centrifugo connect reply."""
    if not isinstance(connect, dict):
        return []
    if "data" in connect:
        data = connect["data"]
        return list(data) if isinstance(data, list) else []
    subs = connect.get("subs")
    if not isinstance(subs, dict):
        return []
    rows: list[Any] = []
    for sub in subs.values():
        publications = sub.get("publications") if isinstance(sub, dict) else None
        if isinstance(publications, list):
            rows.extend(publications)
    return rows


def parse_sse_message(raw: str) -> list[dict[str, Any]]:
    """Extract now-playing payloads from one Centrifugo SSE message."""
    try:
        data = json.loads(raw)
    except ValueError:
        _LOGGER.debug("Ignoring non-JSON SSE message: %.80s", raw)
        return []
    if not isinstance(data, dict):
        return []
    if "connect" in data:
        rows = _connect_rows(data["connect"])
    elif "pub" in data:
        rows = [data["pub"]]
    else:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("data") if isinstance(row, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("np"), dict):
            result.append(payload["np"])
    return result


async def iter_sse_data(lines: AsyncIterable[bytes]) -> AsyncIterator[str]:
    """Yield the data of each server-sent event."""
    buffer: list[str] = []
    async for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            buffer.append(value[1:] if value.startswith(" ") else value)
    if buffer:
        yield "\n".join(buffer)


async def stream_nowplaying(
    session: aiohttp.ClientSession,
    base_url: str,
    shortcodes: list[str],
    *,
    read_timeout: float = 90.0,
) -> AsyncIterator[NowPlaying]:
    """Yield now-playing updates until the stream ends or fails."""
    url = build_sse_url(base_url, shortcodes)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=read_timeout)
    try:
        async with session.get(
            url,
            headers={"Accept": "text/event-stream"},
            timeout=timeout,
            read_bufsize=_READ_BUFSIZE,
        ) as resp:
            if resp.status >= 400:
                raise error_for_status(resp.status, "")
            async for message in iter_sse_data(resp.content):
                parsed: list[NowPlaying] = []
                for row in parse_sse_message(message):
                    try:
                        parsed.append(NowPlaying.from_dict(row))
                    except KeyError, TypeError, ValueError, AttributeError:
                        _LOGGER.debug("Skipping malformed now-playing payload")
                for nowplaying in parsed:
                    yield nowplaying
    except (aiohttp.ClientError, HttpProcessingError, TimeoutError) as err:
        raise AzuraCastConnectionError(f"Now-playing stream failed: {err!r}") from err
