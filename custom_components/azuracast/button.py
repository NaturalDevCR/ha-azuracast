"""Buttons for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.client import AzuraCastClient
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, api_errors, remove_entities
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
    entities: list[AzuraCastButton] = []
    for rt in entry.runtime_data.stations.values():
        # Skip and clear need the Broadcasting permission; it is read from the
        # first refresh, so a later permission change needs a reload.
        if rt.manage is None or not rt.can_broadcast:
            remove_entities(
                hass,
                entry,
                platform=BUTTON_DOMAIN,
                unique_ids={f"{rt.device_key}_{description.key}" for description in BUTTONS},
            )
            continue
        entities.extend(
            AzuraCastButton(rt.manage, entry, rt, description) for description in BUTTONS
        )
    async_add_entities(entities)


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
