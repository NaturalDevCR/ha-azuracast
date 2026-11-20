"""Buttons for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.client import AzuraCastClient
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AzuraCastButtonDescription(ButtonEntityDescription):
    """Describes an AzuraCast button."""

    press_fn: Callable[[AzuraCastClient, int], Awaitable[Any]]


BUTTONS: tuple[AzuraCastButtonDescription, ...] = (
    AzuraCastButtonDescription(
        key="skip_song", translation_key="skip_song", press_fn=lambda c, sid: c.skip_song(sid)
    ),
    AzuraCastButtonDescription(
        key="clear_queue", translation_key="clear_queue", press_fn=lambda c, sid: c.clear_queue(sid)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastButton(rt.manage, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        if rt.manage is not None
        for description in BUTTONS
    )


class AzuraCastButton(AzuraCastEntity[ManageCoordinator], ButtonEntity):
    """A station action."""

    entity_description: AzuraCastButtonDescription

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastButtonDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        async with api_errors():
            await self.entity_description.press_fn(self.client, self.station.id)
        await self.coordinator.async_refresh()
