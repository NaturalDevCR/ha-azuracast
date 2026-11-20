"""Services of the AzuraCast integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
import probatio

from .api.client import AzuraCastClient
from .api.models import Playlist
from .const import DOMAIN
from .coordinator import ManageCoordinator
from .entity import api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

SERVICE_SKIP_SONG = "skip_song"
SERVICE_CLEAR_QUEUE = "clear_queue"
SERVICE_SET_PLAYLISTS_ENABLED = "set_playlists_enabled"
SERVICE_RESHUFFLE_PLAYLIST = "reshuffle_playlist"
ATTR_PLAYLISTS = "playlists"
ATTR_PLAYLIST = "playlist"
ATTR_ENABLED = "enabled"

TARGET_SCHEMA = cv.make_entity_service_schema({})
SET_PLAYLISTS_SCHEMA = cv.make_entity_service_schema(
    {
        probatio.Required(ATTR_PLAYLISTS): probatio.All(
            cv.ensure_list, [cv.string], probatio.Length(min=1)
        ),
        probatio.Required(ATTR_ENABLED): cv.boolean,
    }
)
RESHUFFLE_SCHEMA = cv.make_entity_service_schema({probatio.Required(ATTR_PLAYLIST): cv.string})

type Target = tuple[AzuraCastClient, StationRuntime, ManageCoordinator]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""
    hass.services.async_register(DOMAIN, SERVICE_SKIP_SONG, _async_skip_song, schema=TARGET_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_QUEUE, _async_clear_queue, schema=TARGET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PLAYLISTS_ENABLED,
        _async_set_playlists_enabled,
        schema=SET_PLAYLISTS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESHUFFLE_PLAYLIST, _async_reshuffle_playlist, schema=RESHUFFLE_SCHEMA
    )


def _targets(hass: HomeAssistant, call: ServiceCall) -> list[Target]:
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device_ids = set(selected.referenced_devices)
    for entity_id in selected.referenced | selected.indirectly_referenced:
        entity = entity_registry.async_get(entity_id)
        if entity is not None and entity.platform == DOMAIN and entity.device_id:
            device_ids.add(entity.device_id)
    keys = {
        ident
        for device_id in device_ids
        if (device := device_registry.async_get(device_id)) is not None
        for domain, ident in device.identifiers
        if domain == DOMAIN
    }
    targets: list[Target] = []
    entries: list[AzuraCastConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    for entry in entries:
        for runtime in entry.runtime_data.stations.values():
            if runtime.device_key not in keys:
                continue
            if runtime.manage is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="station_not_managed",
                    translation_placeholders={"station": runtime.station.name},
                )
            targets.append((entry.runtime_data.client, runtime, runtime.manage))
    if not targets:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_station_targeted"
        )
    return targets


def _find_playlist(runtime: StationRuntime, manage: ManageCoordinator, ref: str) -> Playlist:
    """Resolve a playlist reference: an exact name first, then a numeric id."""
    placeholders = {"playlist": ref, "station": runtime.station.name}
    named = [playlist for playlist in manage.data.playlists if playlist.name == ref]
    if len(named) == 1:
        return named[0]
    if named:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ambiguous_playlist",
            translation_placeholders=placeholders,
        )
    for playlist in manage.data.playlists:
        if str(playlist.id) == ref:
            return playlist
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_playlist",
        translation_placeholders=placeholders,
    )


async def _async_skip_song(call: ServiceCall) -> None:
    for client, runtime, _ in _targets(call.hass, call):
        async with api_errors():
            await client.skip_song(runtime.station.id)


async def _async_clear_queue(call: ServiceCall) -> None:
    for client, runtime, manage in _targets(call.hass, call):
        try:
            async with api_errors():
                await client.clear_queue(runtime.station.id)
        finally:
            await manage.async_refresh()


async def _async_set_playlists_enabled(call: ServiceCall) -> None:
    work = [
        (
            client,
            runtime,
            manage,
            [_find_playlist(runtime, manage, ref) for ref in call.data[ATTR_PLAYLISTS]],
        )
        for client, runtime, manage in _targets(call.hass, call)
    ]
    for client, runtime, manage, playlists in work:
        try:
            async with api_errors():
                for playlist in playlists:
                    await client.set_playlist_enabled(
                        runtime.station.id, playlist.id, call.data[ATTR_ENABLED]
                    )
        finally:
            await manage.async_refresh()


async def _async_reshuffle_playlist(call: ServiceCall) -> None:
    work = [
        (client, runtime, manage, _find_playlist(runtime, manage, call.data[ATTR_PLAYLIST]))
        for client, runtime, manage in _targets(call.hass, call)
    ]
    for client, runtime, manage, playlist in work:
        try:
            async with api_errors():
                await client.reshuffle_playlist(runtime.station.id, playlist.id)
        finally:
            await manage.async_refresh()
