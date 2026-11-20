"""Async REST client for the AzuraCast API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .errors import (
    AzuraCastConnectionError,
    AzuraCastError,
    AzuraCastResponseError,
    error_for_status,
)
from .models import NowPlaying, Playlist, QueueItem, Station


def normalize_url(url: str) -> str:
    """Return the instance base URL without trailing slash or /api suffix."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid AzuraCast URL: {url!r}")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return f"{parts.scheme}://{parts.netloc}{path}"


async def _error_message(resp: aiohttp.ClientResponse) -> str:
    try:
        data = await resp.json(content_type=None)
    except ValueError, aiohttp.ClientError:
        return ""
    if isinstance(data, dict):
        return str(data.get("formatted_message") or data.get("message") or "")
    return ""


class AzuraCastClient:
    """Talks to one AzuraCast instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None = None,
        *,
        request_timeout: float = 10.0,
    ) -> None:
        self._session = session
        self._base_url = normalize_url(base_url)
        self._api_key = api_key or None
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_api_key(self) -> bool:
        return self._api_key is not None

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(self, method: str, path: str, *, payload: Any = None) -> Any:
        url = f"{self._base_url}/api{path}"
        try:
            async with self._session.request(
                method, url, json=payload, headers=self._headers(), timeout=self._timeout
            ) as resp:
                if resp.status >= 400:
                    raise error_for_status(resp.status, await _error_message(resp))
                status = resp.status
                text = await resp.text()
        except AzuraCastError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AzuraCastConnectionError(f"Error talking to {url}: {err!r}") from err
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise AzuraCastResponseError(status, "Response is not JSON") from err

    async def get_status(self) -> bool:
        data = await self._request("GET", "/status")
        return bool(isinstance(data, dict) and data.get("online"))

    async def get_account(self) -> dict[str, Any]:
        data = await self._request("GET", "/frontend/account/me")
        return data if isinstance(data, dict) else {}

    async def get_stations(self) -> list[Station]:
        data = await self._request("GET", "/stations")
        return [Station.from_dict(row) for row in data or []]

    async def get_nowplaying(self) -> list[NowPlaying]:
        data = await self._request("GET", "/nowplaying")
        return [NowPlaying.from_dict(row) for row in data or []]

    async def get_playlists(self, station_id: int) -> list[Playlist]:
        data = await self._request("GET", f"/station/{station_id}/playlists")
        return [Playlist.from_dict(row) for row in data or []]

    async def set_playlist_enabled(self, station_id: int, playlist_id: int, enabled: bool) -> None:
        await self._request(
            "PUT", f"/station/{station_id}/playlist/{playlist_id}", payload={"is_enabled": enabled}
        )

    async def reshuffle_playlist(self, station_id: int, playlist_id: int) -> None:
        await self._request("PUT", f"/station/{station_id}/playlist/{playlist_id}/reshuffle")

    async def get_queue(self, station_id: int) -> list[QueueItem]:
        data = await self._request("GET", f"/station/{station_id}/queue")
        return [QueueItem.from_dict(row) for row in data or []]

    async def delete_queue_item(self, station_id: int, item_id: int) -> None:
        await self._request("DELETE", f"/station/{station_id}/queue/{item_id}")

    async def clear_queue(self, station_id: int) -> int:
        items = await self.get_queue(station_id)
        for item in items:
            await self.delete_queue_item(station_id, item.id)
        return len(items)

    async def skip_song(self, station_id: int) -> None:
        await self._request("POST", f"/station/{station_id}/backend/skip")
