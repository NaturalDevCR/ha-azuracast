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

SSE_INITIAL_BACKOFF = 1.0
"""First reconnect delay in seconds; read at runtime so tests can shrink it."""

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
        self._stopped = False
        self._backoff = SSE_INITIAL_BACKOFF
        self._unexpected_logged = False
        self.sse_connected = False
        self.sse_ever_connected = False

    async def async_start(self) -> None:
        self._stopped = False
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
        self._stopped = True
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
            self._poll_failed(err)
            return
        except Exception as err:  # a bad payload must not escape a start or a tick
            self._poll_failed(err, unexpected=True)
            return
        if self._poll_superseded():
            return
        self._mark_available()
        for nowplaying in items:
            self._dispatch(nowplaying)

    def _poll_superseded(self) -> bool:
        """True when a poll result is stale: the hub stopped or SSE took over."""
        return self._stopped or self.sse_connected

    @callback
    def _poll_failed(self, err: Exception, *, unexpected: bool = False) -> None:
        if self._poll_superseded():
            return
        if not self._poll_failing:
            self._poll_failing = True
            if unexpected:
                LOGGER.exception("AzuraCast now-playing data unavailable: %s", err)
            else:
                LOGGER.info("AzuraCast now-playing data unavailable: %s", err)
        for coordinator in self._coordinators.values():
            coordinator.async_set_update_error(err)

    @callback
    def _mark_available(self) -> None:
        if self._poll_failing:
            LOGGER.info("AzuraCast now-playing data available again")
            self._poll_failing = False

    async def _async_poll_tick(self, _now: datetime) -> None:
        if not self.sse_connected:
            await self.async_poll()

    @callback
    def _dispatch(self, nowplaying: NowPlaying) -> None:
        coordinator = self._coordinators.get(nowplaying.station.shortcode)
        if coordinator is not None:
            coordinator.push(nowplaying)

    async def _async_run(self) -> None:
        shortcodes = list(self._coordinators)
        self._backoff = SSE_INITIAL_BACKOFF
        self._unexpected_logged = False
        while True:
            try:
                await self._async_consume_stream(shortcodes)
            except AzuraCastError as err:
                LOGGER.debug("Now-playing stream unavailable: %s", err)
                self._stream_failed()
            except Exception:
                # Defense in depth: the reconnect loop must survive anything but
                # cancellation (CancelledError is a BaseException, so it passes).
                self._log_unexpected_stream_error()
                self._stream_failed()
            self.sse_connected = False
            await asyncio.sleep(self._next_backoff_delay())

    async def _async_consume_stream(self, shortcodes: list[str]) -> None:
        async for nowplaying in self._stream_factory(
            self._client.session, self._client.base_url, shortcodes
        ):
            if not self.sse_connected:
                self._on_stream_connected()
            self._dispatch(nowplaying)

    @callback
    def _on_stream_connected(self) -> None:
        self.sse_connected = True
        self.sse_ever_connected = True
        self._backoff = SSE_INITIAL_BACKOFF
        self._unexpected_logged = False
        set_sse_unavailable(self._hass, self._entry, False)
        LOGGER.debug("Now-playing stream connected")
        self._mark_available()

    @callback
    def _stream_failed(self) -> None:
        if not self.sse_ever_connected:
            set_sse_unavailable(self._hass, self._entry, True)

    def _log_unexpected_stream_error(self) -> None:
        if self._unexpected_logged:
            LOGGER.debug("Unexpected error in the AzuraCast now-playing stream", exc_info=True)
            return
        LOGGER.exception("Unexpected error in the AzuraCast now-playing stream")
        self._unexpected_logged = True

    def _next_backoff_delay(self) -> float:
        """Return the next reconnect delay (jitter included, capped) and grow the base."""
        delay = min(self._backoff + random.uniform(0, self._backoff / 2), SSE_MAX_BACKOFF)
        self._backoff = min(self._backoff * 2, SSE_MAX_BACKOFF)
        return delay
