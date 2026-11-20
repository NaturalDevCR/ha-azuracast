"""Base entity and helpers for AzuraCast entities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Hashable
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
from .coordinator import ManageCoordinator, ManageData
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


def track_dynamic_entities[K: Hashable](
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    coordinator: ManageCoordinator,
    *,
    platform: str,
    keys: Callable[[ManageData], set[K]],
    create: Callable[[K], AzuraCastEntity[ManageCoordinator]],
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for new keys and drop registry entries of vanished keys.

    Every entity returned by ``create(key)`` must set ``self.dynamic_key = key``.
    """
    known: dict[K, str] = {}

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        current = keys(coordinator.data)
        new_entities = [create(key) for key in current - known.keys()]
        for entity in new_entities:
            known[entity.dynamic_key] = str(entity.unique_id)  # type: ignore[attr-defined]
        if new_entities:
            add_entities(new_entities)
        registry = er.async_get(hass)
        for key in known.keys() - current:
            unique_id = known.pop(key)
            if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
                registry.async_remove(entity_id)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
