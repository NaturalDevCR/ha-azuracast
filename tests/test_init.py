"""Tests for setting up the integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastConnectionError
from custom_components.azuracast.const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    DOMAIN,
)


async def test_setup_and_unload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert set(runtime.stations) == {1, 2}
    ambientes, party = runtime.stations[1], runtime.stations[2]
    assert ambientes.manage is not None
    assert party.manage is None
    assert ambientes.clear_queue_on_change is True
    assert ambientes.reject_unavailable_values is True
    assert ambientes.nowplaying.data.current.song.id == "a1"
    assert ambientes.device_key == f"{entry.entry_id}_1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_instance_device_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, init_integration.entry_id), init_integration.entry_id
    )
    assert device is not None
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_options_shape_runtime(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_EXCLUDED_STATIONS: ["2"],
            CONF_KEEP_QUEUE_STATIONS: ["1"],
            CONF_ALLOW_UNAVAILABLE_STATIONS: ["1"],
            "scan_interval": 120,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    stations = mock_config_entry.runtime_data.stations
    assert set(stations) == {1}
    assert stations[1].clear_queue_on_change is False
    assert stations[1].reject_unavailable_values is False
    assert stations[1].manage.update_interval.total_seconds() == 120
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_no_api_key_is_read_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.has_api_key = False
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert all(rt.manage is None for rt in mock_config_entry.runtime_data.stations.values())
    mock_client.get_playlists.assert_not_called()
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_stations.side_effect = AzuraCastConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_stale_station_device_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    stale = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_99")},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert device_registry.async_get(stale.id) is None
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
