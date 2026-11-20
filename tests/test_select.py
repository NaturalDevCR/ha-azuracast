"""Tests for selector select entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, call

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.azuracast.api.errors import AzuraCastResponseError
from custom_components.azuracast.const import DOMAIN, EVENT_SELECTOR_APPLIED

from .common import make_nowplaying

ENTITY = "select.ambientes_ambiente"


@pytest.fixture(autouse=True)
def afternoon(freezer: FrozenDateTimeFactory) -> None:
    """14:00 in Costa Rica: tarde-* playlists are scheduled."""
    freezer.move_to("2026-11-20 20:00:00+00:00")


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: ENTITY, ATTR_OPTION: option},
        blocking=True,
    )


async def test_state_and_attributes(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    state = hass.states.get(ENTITY)
    assert state.state == "chill"
    assert state.attributes["options"] == ["chill", "fiesta", "lounge"]
    assert state.attributes["available_now"] == ["chill", "lounge"]
    assert state.attributes["reason"] is None
    assert state.attributes["pending_value"] is None
    assert state.attributes["playlists"]["chill"] == ["noche-chill", "tarde-chill"]
    assert hass.states.get("select.party_time_ambiente") is None


async def test_select_applies_plan(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert mock_client.set_playlist_enabled.await_args_list == [
        call(1, 12, True),
        call(1, 10, False),
        call(1, 11, False),
    ]
    mock_client.clear_queue.assert_awaited_once_with(1)
    assert events[0].data == {
        "station_id": 1,
        "selector": "ambiente",
        "value": "lounge",
        "previous_value": "chill",
        "cleared_queue": True,
    }
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"


async def test_pending_clears_when_song_from_value_starts(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    await _select(hass, "lounge")
    nowplaying = init_integration.runtime_data.stations[1].nowplaying
    nowplaying.push(make_nowplaying(0, song_id="l1", playlist="tarde-lounge"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_pending_times_out(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await _select(hass, "lounge")
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_unavailable_value_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await _select(hass, "fiesta")
    assert info.value.translation_key == "value_not_available_now"
    mock_client.set_playlist_enabled.assert_not_called()


async def test_unavailable_value_allowed_when_configured(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    await _select(hass, "fiesta")
    mock_client.set_playlist_enabled.assert_any_await(1, 13, True)


async def test_same_value_is_noop(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await _select(hass, "chill")
    mock_client.set_playlist_enabled.assert_not_called()
    mock_client.clear_queue.assert_not_called()


async def test_keep_queue_option(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    init_integration.runtime_data.stations[1].clear_queue_on_change = False
    await _select(hass, "lounge")
    mock_client.clear_queue.assert_not_called()


async def test_api_failure_refreshes_and_raises(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.set_playlist_enabled.side_effect = AzuraCastResponseError(500, "boom")
    mock_client.get_playlists.reset_mock()
    with pytest.raises(HomeAssistantError) as info:
        await _select(hass, "lounge")
    assert info.value.translation_key == "selector_apply_failed"
    assert mock_client.get_playlists.await_count >= 1
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_read_only_station_orphans_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id
    orphan = entity_registry.async_get_or_create(
        SELECT_DOMAIN, DOMAIN, f"{entry_id}_2_selector_ambiente", config_entry=mock_config_entry
    ).entity_id
    kept = entity_registry.async_get_or_create(
        SELECT_DOMAIN, DOMAIN, f"{entry_id}_1_selector_ambiente", config_entry=mock_config_entry
    ).entity_id
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(orphan) is None
    assert entity_registry.async_get(kept) is not None
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
