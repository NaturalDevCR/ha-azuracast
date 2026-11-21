"""Media player for AzuraCast stations."""

from __future__ import annotations

from datetime import datetime
from http import HTTPStatus

import aiohttp
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.const import CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.models import NowPlaying
from .const import LOGGER
from .coordinator import NowPlayingCoordinator
from .entity import AzuraCastNowPlayingEntity, api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 1
ART_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastMediaPlayer(rt.nowplaying, entry, rt)
        for rt in entry.runtime_data.stations.values()
    )


class AzuraCastMediaPlayer(AzuraCastNowPlayingEntity, MediaPlayerEntity):
    """What a station is playing."""

    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    # Browsers load artwork through HA's media proxy, since instances are often
    # LAN-only; HA fetches it with _async_fetch_image below.
    _attr_media_image_remotely_accessible = False

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
    ) -> None:
        super().__init__(coordinator, entry, runtime, "media_player")
        self._attr_supported_features = (
            MediaPlayerEntityFeature.NEXT_TRACK
            if runtime.can_broadcast
            else MediaPlayerEntityFeature(0)
        )
        self._attr_media_position_updated_at: datetime | None = dt_util.utcnow()

    _available_off_air = True

    @property
    def _np(self) -> NowPlaying | None:
        return self.coordinator.data

    @property
    def state(self) -> MediaPlayerState | None:
        np = self._np
        if np is None:
            return MediaPlayerState.OFF if self.coordinator.off_air else None
        if not np.is_online:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING if np.current else MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        np = self._np
        return (np.current.song.title or np.current.song.text) if np and np.current else None

    @property
    def media_artist(self) -> str | None:
        np = self._np
        return (np.current.song.artist or None) if np and np.current else None

    @property
    def media_album_name(self) -> str | None:
        np = self._np
        return (np.current.song.album or None) if np and np.current else None

    @property
    def media_image_url(self) -> str | None:
        np = self._np
        return np.current.song.art if np and np.current else None

    @property
    def media_duration(self) -> int | None:
        np = self._np
        return (np.current.duration or None) if np and np.current else None

    @property
    def media_position(self) -> int | None:
        np = self._np
        return np.current.elapsed if np and np.current else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_media_position_updated_at = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_media_next_track(self) -> None:
        async with api_errors():
            await self.client.skip_song(self.station.id)

    async def _async_fetch_image(self, url: str) -> tuple[bytes | None, str | None]:
        """Fetch artwork honouring the entry's verify_ssl (HA's default always verifies)."""
        session = async_get_clientsession(self.hass, verify_ssl=self._entry.data[CONF_VERIFY_SSL])
        try:
            async with session.get(url, timeout=ART_FETCH_TIMEOUT) as response:
                if response.status != HTTPStatus.OK:
                    LOGGER.debug("Artwork %s returned HTTP %s", url, response.status)
                    return None, None
                content = await response.read()
                content_type = response.headers.get(aiohttp.hdrs.CONTENT_TYPE)
        except (aiohttp.ClientError, TimeoutError) as err:
            LOGGER.debug("Could not fetch artwork %s: %r", url, err)
            return None, None
        return content, content_type.split(";")[0].strip() if content_type else None
