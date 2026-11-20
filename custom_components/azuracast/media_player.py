"""Media player for AzuraCast stations."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.models import NowPlaying
from .coordinator import NowPlayingCoordinator
from .entity import AzuraCastEntity, api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastMediaPlayer(rt.nowplaying, entry, rt)
        for rt in entry.runtime_data.stations.values()
    )


class AzuraCastMediaPlayer(AzuraCastEntity[NowPlayingCoordinator], MediaPlayerEntity):
    """What a station is playing."""

    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    _attr_media_image_remotely_accessible = True

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
    ) -> None:
        super().__init__(coordinator, entry, runtime, "media_player")
        self._attr_supported_features = (
            MediaPlayerEntityFeature.NEXT_TRACK
            if runtime.manage is not None
            else MediaPlayerEntityFeature(0)
        )
        self._attr_media_position_updated_at: datetime | None = dt_util.utcnow()

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def _np(self) -> NowPlaying | None:
        return self.coordinator.data

    @property
    def state(self) -> MediaPlayerState | None:
        np = self._np
        if np is None:
            return None
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
