"""Tests for setting up the integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
    AzuraCastNotFoundError,
    AzuraCastPermissionError,
    AzuraCastResponseError,
)
from custom_components.azuracast.const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_IMMEDIATE_CHANGE_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    CONF_RELOAD_CHANGE_STATIONS,
    DOMAIN,
    ChangeMode,
)
from custom_components.azuracast.issues import async_delete_stale_station_issues

from .common import playlists


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
            # Reloading wins when a station is listed for both.
            CONF_IMMEDIATE_CHANGE_STATIONS: ["1"],
            CONF_RELOAD_CHANGE_STATIONS: ["1"],
            "scan_interval": 120,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    stations = mock_config_entry.runtime_data.stations
    assert set(stations) == {1}
    assert stations[1].clear_queue_on_change is False
    assert stations[1].reject_unavailable_values is False
    assert stations[1].change_mode is ChangeMode.RELOAD
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


async def test_playlist_probe_connection_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_playlists.side_effect = AzuraCastConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    "error",
    [AzuraCastResponseError(400, "Bad Request"), AzuraCastNotFoundError(404, "")],
)
async def test_playlist_probe_other_client_error_is_read_only(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
) -> None:
    """A station that answers playlists with another 4xx (no Media feature) is read-only."""

    def _probe(station_id: int) -> object:
        if station_id == 2:
            raise error
        return playlists()

    mock_client.get_playlists.side_effect = _probe
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    stations = mock_config_entry.runtime_data.stations
    assert stations[1].manage is not None
    assert stations[2].manage is None
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_playlist_probe_server_error_retries(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_playlists.side_effect = AzuraCastResponseError(500, "Internal Server Error")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (AzuraCastPermissionError(403, "Forbidden"), ConfigEntryState.SETUP_ERROR),
        (AzuraCastAuthError(401, "Unauthorized"), ConfigEntryState.SETUP_ERROR),
        (AzuraCastConnectionError("down"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_account_check_errors(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    """The key is checked against the account endpoint before any station probe."""
    mock_client.get_account.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is state
    mock_client.get_playlists.assert_not_called()


async def test_no_api_key_skips_account_check(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.has_api_key = False
    mock_client.get_account.side_effect = AzuraCastPermissionError(403, "Forbidden")
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_account.assert_not_called()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


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


async def test_issues_of_vanished_stations_removed_at_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Issues of a station deleted in AzuraCast (or while HA was down) do not linger."""
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id
    stale = [*_seed_station_issues(hass, entry_id, 99), *_seed_station_issues(hass, entry_id, 12)]
    other = _seed_station_issues(hass, "otherentry", 99)
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    for issue_id in stale:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    for issue_id in other:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_delete_stale_station_issues_matches_full_ids(
    hass: HomeAssistant, issue_registry: ir.IssueRegistry
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, entry_id="E")
    kept = [
        *_seed_station_issues(hass, "E", 1),
        # Another entry whose id starts with "E_5", and one whose id merely starts with "E".
        *_seed_station_issues(hass, "E_5", 1),
        *_seed_station_issues(hass, "E2", 12),
        "sse_unavailable_E",
    ]
    ir.async_create_issue(
        hass,
        DOMAIN,
        "sse_unavailable_E",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="sse_unavailable",
    )
    gone = [*_seed_station_issues(hass, "E", 12), *_seed_station_issues(hass, "E", 11)]
    async_delete_stale_station_issues(hass, entry, {1})
    for issue_id in kept:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None, issue_id
    for issue_id in gone:
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None, issue_id


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


async def test_rejected_key_at_setup_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    """AzuraCast treats a revoked key as anonymous: the account endpoint answers 403."""
    mock_client.get_account.side_effect = AzuraCastPermissionError(403, "Forbidden")
    # Public endpoints keep working and every station probe is 403 for an anonymous user.
    mock_client.get_playlists.side_effect = AzuraCastPermissionError(403, "Forbidden")
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id
    managed = [
        entity_registry.async_get_or_create(
            platform, DOMAIN, unique_id, config_entry=mock_config_entry
        ).entity_id
        for platform, unique_id in (
            (SWITCH_DOMAIN, f"{entry_id}_1_playlist_10"),
            (SELECT_DOMAIN, f"{entry_id}_1_selector_ambiente"),
            (BUTTON_DOMAIN, f"{entry_id}_1_skip_song"),
        )
    ]
    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(
        f["context"]["source"] == SOURCE_REAUTH and f["context"]["entry_id"] == entry_id
        for f in flows
    )
    for entity_id in managed:
        assert entity_registry.async_get(entity_id) is not None
