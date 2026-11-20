"""Tests for setting up the integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
)
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
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_no_api_key_is_read_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.has_api_key = False
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert all(rt.manage is None for rt in mock_config_entry.runtime_data.stations.values())
    mock_client.get_playlists.assert_not_called()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


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
    kept = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_1")},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert device_registry.async_get(stale.id) is None
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{mock_config_entry.entry_id}_1"), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.id == kept.id
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_excluded_station_device_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_EXCLUDED_STATIONS: ["2"]}
    )
    excluded = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_2")},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert device_registry.async_get(excluded.id) is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (AzuraCastAuthError(401, "Unauthorized"), ConfigEntryState.SETUP_ERROR),
        (AzuraCastConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_playlist_probe_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    mock_client.get_playlists.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is state


def _seed_station_issues(hass: HomeAssistant, entry_id: str, station_id: int) -> list[str]:
    issue_ids = [
        f"missing_permissions_{entry_id}_{station_id}",
        f"conflicting_tags_{entry_id}_{station_id}",
    ]
    for issue_id in issue_ids:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="conflicting_tags",
        )
    return issue_ids


@pytest.mark.parametrize("options", [{CONF_EXCLUDED_STATIONS: ["2"]}, {}])
async def test_station_issues_cleared_without_manage_coordinator(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    issue_registry: ir.IssueRegistry,
    options: dict[str, list[str]],
) -> None:
    """Excluded and read-only (403) stations do not keep stale per-station issues."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options=options)
    issue_ids = _seed_station_issues(hass, mock_config_entry.entry_id, 2)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for issue_id in issue_ids:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_station_issues_cleared_without_api_key(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    mock_client.has_api_key = False
    mock_config_entry.add_to_hass(hass)
    issue_ids = _seed_station_issues(hass, mock_config_entry.entry_id, 1)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for issue_id in issue_ids:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_remove_entry_deletes_its_issues(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    entry_id = init_integration.entry_id
    ours = [f"sse_unavailable_{entry_id}", *_seed_station_issues(hass, entry_id, 1)]
    ir.async_create_issue(
        hass,
        DOMAIN,
        ours[0],
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="sse_unavailable",
    )
    other = _seed_station_issues(hass, "otherentry", 1)
    assert await hass.config_entries.async_remove(entry_id)
    await hass.async_block_till_done()
    for issue_id in ours:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    for issue_id in other:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None


async def test_setup_auth_error_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_stations.side_effect = AzuraCastAuthError(401, "")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)
