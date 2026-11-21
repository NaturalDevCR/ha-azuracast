"""Selector select entities for AzuraCast stations."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.select import SelectEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api.errors import AzuraCastError
from .api.models import NowPlaying
from .const import (
    DOMAIN,
    EVENT_SELECTOR_APPLIED,
    LOGGER,
    MAX_STALE_SKIPS,
    PENDING_TIMEOUT,
    RELOAD_QUEUE_ATTEMPTS,
    RELOAD_QUEUE_POLL,
    RELOAD_SONG_END_LEAD,
    ChangeMode,
)
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, remove_dynamic_entities, track_dynamic_entities
from .runtime import AzuraCastConfigEntry, StationRuntime
from .schedule import station_time
from .selectors import Selector, available_values, execute_plan, plan_apply, selector_state

PARALLEL_UPDATES = 1


def seconds_until_song_end(np: NowPlaying | None, now: float) -> float | None:
    """Seconds from a POSIX time until the reload before the current song ends.

    None when the song's length is unknown.
    """
    if np is None or np.current is None or np.current.duration <= 0:
        return None
    end = np.current.played_at + np.current.duration
    return end - RELOAD_SONG_END_LEAD - now


def _unique_id_prefix(runtime: StationRuntime) -> str:
    return f"{runtime.device_key}_selector_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for runtime in entry.runtime_data.stations.values():
        if (coordinator := runtime.manage) is None:
            # No management access: drop selects left from when it had some.
            remove_dynamic_entities(
                hass, entry, platform=SELECT_DOMAIN, unique_id_prefix=_unique_id_prefix(runtime)
            )
            continue
        track_dynamic_entities(
            hass,
            entry,
            coordinator,
            platform=SELECT_DOMAIN,
            unique_id_prefix=_unique_id_prefix(runtime),
            keys=lambda data: {key for key, selector in data.selectors.items() if selector.members},
            create=partial(SelectorSelect, coordinator, entry, runtime),
            add_entities=async_add_entities,
        )


class SelectorSelect(AzuraCastEntity[ManageCoordinator], SelectEntity):
    """Chooses which value of a tag selector is enabled."""

    _attr_translation_key = "selector"

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry, runtime, f"selector_{key}")
        self.selector_key = key
        self._attr_translation_placeholders = {"selector": key}
        self._pending: str | None = None
        self._pending_song_id: str | None = None
        self._cancel_pending: CALLBACK_TYPE | None = None
        self._removed = False
        # The skip mode skips songs from the previous value that Liquidsoap
        # already held, as they start (see _handle_nowplaying).
        self._skip_stale = False
        self._stale_skips = 0
        self._last_skipped: tuple[str, int] | None = None
        self._reload_task: asyncio.Task[None] | None = None
        # Set when a reload after the song may go ahead (see _async_song_end).
        self._song_ending: asyncio.Event | None = None

    @property
    def _selector(self) -> Selector | None:
        return self.coordinator.data.selectors.get(self.selector_key)

    def _now(self) -> datetime:
        return station_time(
            self.station.timezone, dt_util.utcnow(), dt_util.get_default_time_zone()
        )

    @property
    def available(self) -> bool:
        selector = self._selector
        return super().available and selector is not None and bool(selector.members)

    @property
    def options(self) -> list[str]:
        selector = self._selector
        return selector.options if selector else []

    @property
    def current_option(self) -> str | None:
        selector = self._selector
        return selector_state(selector).value if selector else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        selector = self._selector
        if selector is None:
            return None
        return {
            "reason": selector_state(selector).reason,
            "available_now": available_values(selector, self._now()),
            "pending_value": self._pending,
            "playlists": {
                value: sorted(p.name for p in items) for value, items in selector.members.items()
            },
        }

    async def async_added_to_hass(self) -> None:
        # HA re-adds this same instance after an entity_id rename.
        self._removed = False
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.nowplaying.async_add_listener(self._handle_nowplaying))
        self.async_on_remove(self._drop_pending_on_remove)

    @callback
    def _drop_pending_on_remove(self) -> None:
        """Drop pending feedback without writing state; a re-add starts clean."""
        self._removed = True
        self._clear_pending_timer()
        self._pending = None
        self._pending_song_id = None
        self._skip_stale = False
        self._cancel_reload()

    async def async_select_option(self, option: str) -> None:
        # Plan from fresh playlist state: changes made in the AzuraCast UI since
        # the last poll would otherwise be planned against, or missed.
        await self.coordinator.async_refresh()
        if not self.coordinator.last_update_success:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="selector_apply_failed",
                translation_placeholders={
                    "selector": self.selector_key,
                    "value": option,
                    "error": str(self.coordinator.last_exception),
                },
            )
        selector = self._selector
        if selector is None or option not in selector.members:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_option",
                translation_placeholders={"option": option, "selector": self.selector_key},
            )
        previous = selector_state(selector).value
        plan = plan_apply(selector, option)
        # Re-selecting the current value only repairs it: enabling all of its
        # playlists lets it carry into the next time block. It never clears the
        # queue and is never rejected, so pressing the same button twice is safe.
        repair = previous == option
        if repair and plan.is_empty:
            return
        if (
            not repair
            and self._runtime.reject_unavailable_values
            and option not in available_values(selector, self._now())
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="value_not_available_now",
                translation_placeholders={"value": option, "selector": self.selector_key},
            )
        # Without the Broadcasting permission the playlists still switch; the
        # queue is left alone and the event reports it was not cleared.
        clear_queue = (
            not repair and self._runtime.clear_queue_on_change and self._runtime.can_broadcast
        )
        try:
            await execute_plan(self.client, self.station.id, plan, clear_queue=clear_queue)
        except AzuraCastError as err:
            self._reset_pending()
            await self.coordinator.async_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="selector_apply_failed",
                translation_placeholders={
                    "selector": self.selector_key,
                    "value": option,
                    "error": str(err),
                },
            ) from err
        if repair:
            await self.coordinator.async_refresh()
            return
        mode = self._change_mode(clear_queue)
        self._set_pending(option, skip_stale=mode is ChangeMode.SKIP)
        if mode is ChangeMode.SKIP:
            await self._async_skip("the current song")
        elif mode in (ChangeMode.RELOAD, ChangeMode.RELOAD_AFTER_SONG):
            self._start_reload(after_song=mode is ChangeMode.RELOAD_AFTER_SONG)
        self.hass.bus.async_fire(
            EVENT_SELECTOR_APPLIED,
            {
                "station_id": self.station.id,
                "selector": self.selector_key,
                "value": option,
                "previous_value": previous,
                "cleared_queue": clear_queue,
                "change_mode": mode.value,
            },
        )
        await self.coordinator.async_refresh()

    def _change_mode(self, clear_queue: bool) -> ChangeMode:
        """How this change is made heard (see ChangeMode)."""
        np = self._runtime.nowplaying.data
        # Keeping the queue keeps old songs on purpose; an offline station has
        # nothing to cut; a live DJ would be disconnected by a reload, and a
        # skip has no song of ours to skip.
        if not clear_queue or np is None or not np.is_online or np.is_live:
            return ChangeMode.WAIT
        mode = self._runtime.change_mode
        if not self.station.supports_soft_reload:
            # Without a soft reload AzuraCast restarts everything and listeners
            # drop: skipping is the next quickest change, waiting the next
            # way to let the current song finish.
            if mode is ChangeMode.RELOAD:
                return ChangeMode.SKIP
            if mode is ChangeMode.RELOAD_AFTER_SONG:
                return ChangeMode.WAIT
        return mode

    async def _async_skip(self, what: str) -> None:
        """Skip the playing song; a failure is logged, never raised."""
        try:
            await self.client.skip_song(self.station.id)
        except AzuraCastError as err:
            LOGGER.warning(
                "%s: Could not skip %s after changing %s: %s",
                self.station.name,
                what,
                self.selector_key,
                err,
            )

    @callback
    def _start_reload(self, *, after_song: bool) -> None:
        self._cancel_reload()
        self._reload_task = self._entry.async_create_background_task(
            self.hass,
            self._async_reload(after_song=after_song),
            name=f"azuracast reload station {self.station.id}",
        )

    @callback
    def _cancel_reload(self) -> None:
        if self._reload_task is not None:
            self._reload_task.cancel()
            self._reload_task = None

    async def _async_reload(self, *, after_song: bool) -> None:
        """Soft-reload once AzuraCast has the next song ready.

        The reload drops the songs Liquidsoap held, and Liquidsoap asks for its
        next song right away; with the queue still empty after clearing it,
        it would retry only after ten seconds of silence.
        """
        station_id = self.station.id
        try:
            if after_song:
                await self._async_song_end()
            for attempt in range(RELOAD_QUEUE_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(RELOAD_QUEUE_POLL)
                queue = await self.client.get_queue(station_id)
                if any(not item.sent_to_autodj for item in queue):
                    await self.client.reload_station(station_id)
                    return
            LOGGER.warning(
                "%s: No next song was ready after changing %s; not reloading",
                self.station.name,
                self.selector_key,
            )
        except AzuraCastError as err:
            LOGGER.warning(
                "%s: Could not reload after changing %s: %s",
                self.station.name,
                self.selector_key,
                err,
            )
        finally:
            if self._reload_task is asyncio.current_task():
                self._reload_task = None

    async def _async_song_end(self) -> None:
        """Return shortly before the current song ends, or once another starts.

        A song from the new value starting first resets the pending value,
        which cancels the reload; a song from the previous value is cut at once.
        """
        delay = seconds_until_song_end(self._runtime.nowplaying.data, dt_util.utcnow().timestamp())
        if delay is not None and delay <= 0:
            return
        ending = self._song_ending = asyncio.Event()
        cancel_timer = (
            async_call_later(self.hass, delay, callback(lambda _now: ending.set()))
            if delay is not None
            else None
        )
        try:
            await ending.wait()
        finally:
            if cancel_timer is not None:
                cancel_timer()
            if self._song_ending is ending:
                self._song_ending = None

    @callback
    def _set_pending(self, value: str, *, skip_stale: bool) -> None:
        if self._removed:
            return
        self._clear_pending_timer()
        np = self._runtime.nowplaying.data
        self._pending = value
        self._pending_song_id = np.current.song.id if np and np.current else None
        self._skip_stale = skip_stale
        self._stale_skips = 0
        self._cancel_pending = async_call_later(self.hass, PENDING_TIMEOUT, self._pending_expired)
        self.async_write_ha_state()

    @callback
    def _reset_pending(self) -> None:
        self._clear_pending_timer()
        # A reload still waiting for the song to end is no longer needed.
        if self._song_ending is not None:
            self._cancel_reload()
        if self._pending is None:
            return
        self._pending = None
        self._pending_song_id = None
        self._skip_stale = False
        if not self._removed:
            self.async_write_ha_state()

    @callback
    def _clear_pending_timer(self) -> None:
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None

    @callback
    def _pending_expired(self, _now: datetime) -> None:
        self._cancel_pending = None
        LOGGER.warning(
            "%s: %s=%s was applied but no song from it started within %s",
            self.station.name,
            self.selector_key,
            self._pending,
            PENDING_TIMEOUT,
        )
        self._reset_pending()

    @callback
    def _handle_nowplaying(self) -> None:
        np = self._runtime.nowplaying.data
        selector = self._selector
        if self._pending is None or np is None or np.current is None or selector is None:
            return
        if np.current.song.id == self._pending_song_id:
            return
        if self._song_ending is not None:
            self._song_ending.set()
        if np.current.playlist in selector.playlist_names(self._pending):
            self._reset_pending()
            return
        if self._is_stale(selector, np.current.playlist):
            self._skip_stale_song((np.current.song.id, np.current.played_at))

    def _is_stale(self, selector: Selector, playlist: str | None) -> bool:
        """Whether a playlist belongs to another value of this selector."""
        return any(
            playlist in selector.playlist_names(value)
            for value in selector.members
            if value != self._pending
        )

    @callback
    def _skip_stale_song(self, song: tuple[str, int]) -> None:
        if (
            not self._skip_stale
            or not self._runtime.can_broadcast
            or song == self._last_skipped
            or self._stale_skips >= MAX_STALE_SKIPS
        ):
            return
        self._last_skipped = song
        self._stale_skips += 1
        self._entry.async_create_background_task(
            self.hass,
            self._async_skip("a song from the previous value"),
            name=f"azuracast skip stale song {self.station.id}",
        )
