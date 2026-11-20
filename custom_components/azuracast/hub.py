"""Now-playing hub: one SSE stream per instance with a polling fallback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import contextlib
from datetime import datetime, timedelta
import random

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .api.client import AzuraCastClient
from .api.errors import AzuraCastError
from .api.models import NowPlaying
from .api.sse import stream_nowplaying
from .const import LOGGER, POLL_FALLBACK_INTERVAL, SSE_MAX_BACKOFF
from .coordinator import NowPlayingCoordinator
from .issues import set_sse_unavailable

type StreamFactory = Callable[[aiohttp.ClientSession, str, list[str]], AsyncIterator[NowPlaying]]


class NowPlayingHub:
    """Feeds every station's NowPlayingCoordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AzuraCastClient,
        coordinators: Mapping[str, NowPlayingCoordinator],
        *,
        stream_factory: StreamFactory | None = None,
        poll_interval: timedelta = POLL_FALLBACK_INTERVAL,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinators = coordinators
        self._stream_factory: StreamFactory = stream_factory or stream_nowplaying
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None
        self._poll_failing = False
        self.sse_connected = False
        self.sse_ever_connected = False

    async def async_start(self) -> None:
        await self.async_poll()
        self._unsub_poll = async_track_time_interval(
            self._hass,
            self._async_poll_tick,
            self._poll_interval,
            name="azuracast now-playing poll",
        )
        if self._coordinators:
            self._task = self._entry.async_create_background_task(
                self._hass, self._async_run(), name=f"azuracast sse {self._entry.entry_id}"
            )

    async def async_stop(self) -> None:
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.sse_connected = False

    async def async_poll(self) -> None:
        try:
            items = await self._client.get_nowplaying()
        except AzuraCastError as err:
            if not self._poll_failing:
                LOGGER.info("AzuraCast now-playing data unavailable: %s", err)
                self._poll_failing = True
            for coordinator in self._coordinators.values():
                coordinator.async_set_update_error(err)
            return
        if self._poll_failing:
            LOGGER.info("AzuraCast now-playing data available again")
            self._poll_failing = False
        for nowplaying in items:
            self._dispatch(nowplaying)

    async def _async_poll_tick(self, _now: datetime) -> None:
        if not self.sse_connected:
            await self.async_poll()

    @callback
    def _dispatch(self, nowplaying: NowPlaying) -> None:
        coordinator = self._coordinators.get(nowplaying.station.shortcode)
        if coordinator is not None:
            coordinator.push(nowplaying)

    async def _async_run(self) -> None:
        backoff = 1.0
        shortcodes = list(self._coordinators)
        unexpected_logged = False
        while True:
            try:
                async for nowplaying in self._stream_factory(
                    self._client.session, self._client.base_url, shortcodes
                ):
                    if not self.sse_connected:
                        self.sse_connected = True
                        self.sse_ever_connected = True
                        backoff = 1.0
                        unexpected_logged = False
                        set_sse_unavailable(self._hass, self._entry, False)
                        LOGGER.debug("Now-playing stream connected")
                    self._dispatch(nowplaying)
            except AzuraCastError as err:
                LOGGER.debug("Now-playing stream unavailable: %s", err)
                if not self.sse_ever_connected:
                    set_sse_unavailable(self._hass, self._entry, True)
            except Exception:
                # Defense in depth: the reconnect loop must survive anything but
                # cancellation (CancelledError is a BaseException, so it passes).
                if not unexpected_logged:
                    LOGGER.exception("Unexpected error in the AzuraCast now-playing stream")
                    unexpected_logged = True
                else:
                    LOGGER.debug(
                        "Unexpected error in the AzuraCast now-playing stream", exc_info=True
                    )
                if not self.sse_ever_connected:
                    set_sse_unavailable(self._hass, self._entry, True)
            self.sse_connected = False
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(backoff * 2, SSE_MAX_BACKOFF)
