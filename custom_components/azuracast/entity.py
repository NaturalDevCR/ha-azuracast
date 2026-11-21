"""Base entity and helpers for AzuraCast entities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Collection, Hashable
from contextlib import asynccontextmanager
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api.client import AzuraCastClient
from .api.errors import AzuraCastError
from .api.models import Station
from .const import DOMAIN
from .coordinator import ManageCoordinator, ManageData, NowPlayingCoordinator
from .runtime import AzuraCastConfigEntry, StationRuntime


@asynccontextmanager
async def api_errors() -> AsyncIterator[None]:
    """Turn client errors into translated Home Assistant errors."""
    try:
        yield
    except AzuraCastError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err


class AzuraCastEntity[CoordinatorT: DataUpdateCoordinator[Any]](CoordinatorEntity[CoordinatorT]):
    """An entity that belongs to one station device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoordinatorT,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.device_key}_{key}"
        client = entry.runtime_data.client
        device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.device_key)},
            name=runtime.station.name,
            manufacturer="AzuraCast",
            model="Station",
            configuration_url=f"{client.base_url}/station/{runtime.station.id}/profile",
        )
        # HA 2026.10 deprecates the `via_device` identifier tuple in favour of the
        # registry id of the parent device (the instance device made at setup).
        instance = dr.async_get(coordinator.hass).async_get_device_by_identifier(
            (DOMAIN, entry.entry_id), entry.entry_id
        )
        if instance is not None:
            device_info["via_device_id"] = instance.id
        self._attr_device_info = device_info

    @property
    def client(self) -> AzuraCastClient:
        return self._entry.runtime_data.client

    @property
    def station(self) -> Station:
        return self._runtime.station


class AzuraCastNowPlayingEntity(AzuraCastEntity[NowPlayingCoordinator]):
    """A station entity fed by the now-playing coordinator."""

    # Entities that show "off" for a stopped station instead of unavailable.
    _available_off_air = False

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.coordinator.data is not None:
            return True
        return self._available_off_air and self.coordinator.off_air


@callback
def _remove_registry_entries(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    *,
    platform: str,
    remove: Callable[[str], bool],
) -> None:
    """Remove this entry's ``platform`` registry entries whose unique id matches ``remove``."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == platform
            and reg_entry.platform == DOMAIN
            and remove(reg_entry.unique_id)
        ):
            registry.async_remove(reg_entry.entity_id)


@callback
def remove_entities(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    *,
    platform: str,
    unique_ids: Collection[str],
) -> None:
    """Remove this entry's ``platform`` registry entries with exactly these unique ids."""
    _remove_registry_entries(
        hass, entry, platform=platform, remove=lambda unique_id: unique_id in unique_ids
    )


@callback
def remove_dynamic_entities(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    *,
    platform: str,
    unique_id_prefix: str,
) -> None:
    """Remove every ``platform`` registry entry of this entry under the prefix.

    For a station that no longer gets a management coordinator (lost
    permission or API key), whose dynamic entities would otherwise never be
    synced again and would stay orphaned in the registry.
    """
    _remove_registry_entries(
        hass,
        entry,
        platform=platform,
        remove=lambda unique_id: unique_id.startswith(unique_id_prefix),
    )


def track_dynamic_entities[K: Hashable](
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    coordinator: ManageCoordinator,
    *,
    platform: str,
    unique_id_prefix: str,
    keys: Callable[[ManageData], set[K]],
    create: Callable[[K], AzuraCastEntity[ManageCoordinator]],
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for new keys and drop registry entries of keys that are gone.

    The entity made by ``create(key)`` must have the unique id
    ``f"{unique_id_prefix}{key}"``, or a ValueError is raised. Every sync
    removes this entry's ``platform`` registry entries under the prefix that
    match no current key, which also clears orphans left from before setup
    (removed while HA was down or between reloads).
    """
    # The prefix must end with a separator, so "E_1_playlist_" never matches "E_11_...".
    known: set[K] = set()

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        current = keys(coordinator.data)
        keep = {f"{unique_id_prefix}{key}" for key in current}
        _remove_registry_entries(
            hass,
            entry,
            platform=platform,
            remove=lambda unique_id: (
                unique_id.startswith(unique_id_prefix) and unique_id not in keep
            ),
        )
        known.intersection_update(current)
        if new_keys := current - known:
            entities = []
            for key in new_keys:
                entity = create(key)
                expected = f"{unique_id_prefix}{key}"
                if entity.unique_id != expected:
                    # A wrong id would make every sync delete live registry entries.
                    raise ValueError(
                        f"Dynamic entity for key {key!r} has unique id "
                        f"{entity.unique_id!r}, expected {expected!r}"
                    )
                entities.append(entity)
            known.update(new_keys)
            add_entities(entities)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
