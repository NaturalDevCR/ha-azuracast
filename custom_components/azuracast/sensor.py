"""Sensors for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api.models import NowPlaying, Song
from .coordinator import NowPlayingCoordinator, tags_for_playlist
from .entity import AzuraCastNowPlayingEntity
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 0
MAX_STATE = 255


def _truncate(value: str | None) -> str | None:
    """Fit a string into a state."""
    return value[:MAX_STATE] if value is not None else None


def _song_attrs(song: Song) -> dict[str, Any]:
    return {"artist": song.artist, "title": song.title, "album": song.album, "art": song.art}


@dataclass(frozen=True, kw_only=True)
class AzuraCastSensorDescription(SensorEntityDescription):
    """Describes an AzuraCast sensor."""

    value_fn: Callable[[NowPlaying], StateType]
    attrs_fn: Callable[[NowPlaying, StationRuntime], dict[str, Any] | None] = lambda np, rt: None


SENSORS: tuple[AzuraCastSensorDescription, ...] = (
    AzuraCastSensorDescription(
        key="listeners",
        translation_key="listeners",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda np: np.listeners_current,
        attrs_fn=lambda np, rt: {"unique": np.listeners_unique, "total": np.listeners_total},
    ),
    AzuraCastSensorDescription(
        key="current_song",
        translation_key="current_song",
        value_fn=lambda np: _truncate(np.current.song.text) if np.current else None,
        attrs_fn=lambda np, rt: (
            {
                **_song_attrs(np.current.song),
                "playlist": np.current.playlist,
                "duration": np.current.duration,
            }
            if np.current
            else None
        ),
    ),
    AzuraCastSensorDescription(
        key="current_playlist",
        translation_key="current_playlist",
        value_fn=lambda np: _truncate(np.current.playlist) if np.current else None,
        attrs_fn=lambda np, rt: (
            {"tags": tags_for_playlist(rt.manage, np.current.playlist if np.current else None)}
            if rt.manage is not None
            else None
        ),
    ),
    AzuraCastSensorDescription(
        key="next_song",
        translation_key="next_song",
        value_fn=lambda np: _truncate(np.upcoming.song.text) if np.upcoming else None,
        attrs_fn=lambda np, rt: (
            {**_song_attrs(np.upcoming.song), "playlist": np.upcoming.playlist}
            if np.upcoming
            else None
        ),
    ),
    AzuraCastSensorDescription(
        key="live_streamer",
        translation_key="live_streamer",
        value_fn=lambda np: _truncate(np.streamer_name) if np.is_live else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastSensor(rt.nowplaying, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        for description in SENSORS
    )


class AzuraCastSensor(AzuraCastNowPlayingEntity, SensorEntity):
    """A now-playing sensor."""

    entity_description: AzuraCastSensorDescription

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        np = self.coordinator.data
        return self.entity_description.value_fn(np) if np else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        np = self.coordinator.data
        return self.entity_description.attrs_fn(np, self._runtime) if np else None
