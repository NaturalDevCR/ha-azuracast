"""The AzuraCast integration."""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from homeassistant.const import (
    CONF_API_KEY,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api.client import AzuraCastClient
from .api.errors import (
    AzuraCastAuthError,
    AzuraCastError,
    AzuraCastPermissionError,
    is_client_error,
)
from .api.models import Station
from .const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
)
from .coordinator import ManageCoordinator, NowPlayingCoordinator, tags_for_playlist
from .hub import NowPlayingHub
from .issues import (
    async_delete_entry_issues,
    async_delete_stale_station_issues,
    set_conflicting_tags,
    set_missing_permissions,
)
from .runtime import AzuraCastConfigEntry, AzuraCastRuntime, StationRuntime, station_device_key
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for the domain."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AzuraCastConfigEntry) -> bool:
    """Set up one AzuraCast instance."""
    session = async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL])
    client = AzuraCastClient(session, entry.data[CONF_URL], entry.data.get(CONF_API_KEY))
    try:
        stations = await client.get_stations()
    except AzuraCastAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except AzuraCastError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    if client.has_api_key:
        await _async_check_api_key(client)

    options = entry.options
    excluded = set(options.get(CONF_EXCLUDED_STATIONS, []))
    keep_queue = set(options.get(CONF_KEEP_QUEUE_STATIONS, []))
    allow_unavailable = set(options.get(CONF_ALLOW_UNAVAILABLE_STATIONS, []))
    interval = timedelta(seconds=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AzuraCast",
        model="AzuraCast instance",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=client.base_url,
    )

    runtimes: dict[int, StationRuntime] = {}
    for station in stations:
        key = str(station.id)
        if key in excluded:
            _clear_station_issues(hass, entry, station)
            continue
        manage = await _async_manage_coordinator(hass, entry, client, station, interval)
        if manage is None:
            _clear_station_issues(hass, entry, station)
        runtimes[station.id] = StationRuntime(
            station=station,
            device_key=station_device_key(entry.entry_id, station.id),
            nowplaying=NowPlayingCoordinator(
                hass, entry, station, partial(tags_for_playlist, manage)
            ),
            manage=manage,
            clear_queue_on_change=key not in keep_queue,
            reject_unavailable_values=key not in allow_unavailable,
        )

    _remove_stale_devices(device_registry, entry, runtimes)
    async_delete_stale_station_issues(hass, entry, {station.id for station in stations})

    hub = NowPlayingHub(
        hass, entry, client, {rt.station.shortcode: rt.nowplaying for rt in runtimes.values()}
    )
    entry.runtime_data = AzuraCastRuntime(client=client, hub=hub, stations=runtimes)
    await hub.async_start()
    entry.async_on_unload(hub.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AzuraCastConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AzuraCastConfigEntry) -> None:
    """Delete the repair issues of a removed config entry."""
    async_delete_entry_issues(hass, entry)


def _clear_station_issues(
    hass: HomeAssistant, entry: AzuraCastConfigEntry, station: Station
) -> None:
    """Drop issues only a ManageCoordinator maintains, for a station without one."""
    set_missing_permissions(hass, entry, station, False)
    set_conflicting_tags(hass, entry, station, [])


async def _async_check_api_key(client: AzuraCastClient) -> None:
    """Fail setup with reauth when AzuraCast no longer accepts the key.

    AzuraCast treats an unknown or revoked key as an anonymous user, so it
    answers 403 (not 401) on protected endpoints. The account endpoint tells a
    rejected key apart from a key that only lacks permission on a station.
    """
    try:
        await client.get_account()
    except (AzuraCastAuthError, AzuraCastPermissionError) as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except AzuraCastError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err


async def _async_manage_coordinator(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    client: AzuraCastClient,
    station: Station,
    interval: timedelta,
) -> ManageCoordinator | None:
    if not client.has_api_key:
        return None
    try:
        await client.get_playlists(station.id)
    except AzuraCastAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except AzuraCastError as err:
        # 403 is a missing permission; another 4xx can be a station without the
        # Media feature. Either way, retrying setup would not help.
        if is_client_error(err):
            LOGGER.info(
                "Station %s cannot be managed (playlists answered %s); it will be read-only",
                station.name,
                err.status,
            )
            return None
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    coordinator = ManageCoordinator(hass, entry, client, station, interval)
    await coordinator.async_config_entry_first_refresh()
    return coordinator


def _remove_stale_devices(
    device_registry: dr.DeviceRegistry,
    entry: AzuraCastConfigEntry,
    runtimes: dict[int, StationRuntime],
) -> None:
    keep = {entry.entry_id} | {rt.device_key for rt in runtimes.values()}
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        identifiers = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if identifiers and not identifiers & keep:
            device_registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
