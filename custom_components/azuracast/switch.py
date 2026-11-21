"""Playlist switches for AzuraCast stations."""

from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import Playlist
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, api_errors, remove_dynamic_entities, track_dynamic_entities
from .runtime import AzuraCastConfigEntry, StationRuntime
from .selectors import playlist_tags

PARALLEL_UPDATES = 1


def _unique_id_prefix(runtime: StationRuntime) -> str:
    return f"{runtime.device_key}_playlist_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for runtime in entry.runtime_data.stations.values():
        if (coordinator := runtime.manage) is None:
            # No management access: drop switches left from when it had some.
            remove_dynamic_entities(
                hass, entry, platform=SWITCH_DOMAIN, unique_id_prefix=_unique_id_prefix(runtime)
            )
            continue
        track_dynamic_entities(
            hass,
            entry,
            coordinator,
            platform=SWITCH_DOMAIN,
            unique_id_prefix=_unique_id_prefix(runtime),
            keys=lambda data: {p.id for p in data.playlists},
            create=partial(PlaylistSwitch, coordinator, entry, runtime),
            add_entities=async_add_entities,
        )


class PlaylistSwitch(AzuraCastEntity[ManageCoordinator], SwitchEntity):
    """Whether a playlist is enabled."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "playlist"

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        playlist_id: int,
    ) -> None:
        super().__init__(coordinator, entry, runtime, f"playlist_{playlist_id}")
        self.playlist_id = playlist_id
        playlist = self._playlist
        self._attr_translation_placeholders = {
            "name": playlist.name if playlist else str(playlist_id)
        }

    @property
    def _playlist(self) -> Playlist | None:
        return next((p for p in self.coordinator.data.playlists if p.id == self.playlist_id), None)

    @property
    def available(self) -> bool:
        return super().available and self._playlist is not None

    @property
    def is_on(self) -> bool:
        playlist = self._playlist
        return bool(playlist and playlist.is_enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        playlist = self._playlist
        if playlist is None:
            return None
        return {
            "playlist_id": playlist.id,
            "type": playlist.type,
            "order": playlist.order,
            "weight": playlist.weight,
            "schedule": [item.as_dict() for item in playlist.schedule_items],
            "tags": playlist_tags(playlist),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        async with api_errors():
            await self.client.set_playlist_enabled(self.station.id, self.playlist_id, enabled)
        await self.coordinator.async_refresh()
