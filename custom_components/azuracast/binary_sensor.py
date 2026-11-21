"""Binary sensors for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import NowPlaying
from .coordinator import NowPlayingCoordinator
from .entity import AzuraCastNowPlayingEntity
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AzuraCastBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an AzuraCast binary sensor."""

    value_fn: Callable[[NowPlaying], bool]
    off_when_off_air: bool = False


BINARY_SENSORS: tuple[AzuraCastBinarySensorDescription, ...] = (
    AzuraCastBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda np: np.is_online,
        off_when_off_air=True,
    ),
    AzuraCastBinarySensorDescription(
        key="live",
        translation_key="live",
        value_fn=lambda np: np.is_live,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastBinarySensor(rt.nowplaying, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        for description in BINARY_SENSORS
    )


class AzuraCastBinarySensor(AzuraCastNowPlayingEntity, BinarySensorEntity):
    """A now-playing binary sensor."""

    entity_description: AzuraCastBinarySensorDescription

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description
        self._available_off_air = description.off_when_off_air

    @property
    def is_on(self) -> bool | None:
        np = self.coordinator.data
        if np is None:
            return False if self.coordinator.off_air else None
        return self.entity_description.value_fn(np)
