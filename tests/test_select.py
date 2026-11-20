"""Tests for selector select entities."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import logging
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
from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.const import DOMAIN, EVENT_SELECTOR_APPLIED

from .common import make_nowplaying, playlists

ENTITY = "select.ambientes_ambiente"
TIMEOUT_WARNING = "was applied but no song from it started"


@pytest.fixture(autouse=True)
def afternoon(freezer: FrozenDateTimeFactory) -> None:
    """14:00 in Costa Rica: tarde-* playlists are scheduled."""
    freezer.move_to("2026-11-20 20:00:00+00:00")


async def _set_playlists(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: MagicMock,
    rows: list[Playlist],
) -> None:
    mock_client.get_playlists.side_effect = None
    mock_client.get_playlists.return_value = rows
    await entry.runtime_data.stations[1].manage.async_refresh()
    await hass.async_block_till_done()


def _with_enabled(enabled: set[int]) -> list[Playlist]:
    return [replace(p, is_enabled=p.id in enabled) for p in playlists()]


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: int) -> None:
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


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


async def test_pending_not_cleared_by_song_playing_at_apply(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    # Lounge is on; song a1 from tarde-chill is still playing when chill is applied.
    await _set_playlists(hass, init_integration, mock_client, _with_enabled({12, 14}))
    assert hass.states.get(ENTITY).state == "lounge"
    await _select(hass, "chill")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "chill"
    nowplaying = init_integration.runtime_data.stations[1].nowplaying
    nowplaying.push(make_nowplaying(0, song_id="a1", playlist="tarde-chill"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] == "chill"
    nowplaying.push(make_nowplaying(0, song_id="c2", playlist="tarde-chill"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_pending_times_out(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    await _select(hass, "lounge")
    await _advance(hass, freezer, 14)
    assert TIMEOUT_WARNING not in caplog.text
    await _advance(hass, freezer, 2)
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
    assert f"ambiente=lounge {TIMEOUT_WARNING}" in caplog.text


async def test_reselect_while_pending_keeps_one_timer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    await _select(hass, "lounge")
    await _advance(hass, freezer, 10)
    await _select(hass, "fiesta")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "fiesta"
    await _advance(hass, freezer, 6)
    assert TIMEOUT_WARNING not in caplog.text
    assert hass.states.get(ENTITY).attributes["pending_value"] == "fiesta"
    await _advance(hass, freezer, 10)
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
    assert caplog.text.count(TIMEOUT_WARNING) == 1
    assert f"ambiente=fiesta {TIMEOUT_WARNING}" in caplog.text


async def test_selector_removed_while_pending(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    await _select(hass, "lounge")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"
    untagged = [replace(p, description=None) for p in playlists()]
    await _set_playlists(hass, init_integration, mock_client, untagged)
    assert entity_registry.async_get(ENTITY) is None
    await _advance(hass, freezer, 16)
    assert TIMEOUT_WARNING not in caplog.text


async def test_selector_removed_during_apply(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The selector disappears while its plan runs: no pending timer is armed."""
    caplog.set_level(logging.WARNING)
    untagged = [replace(p, description=None) for p in playlists()]

    async def _remove_selector(*_args: object) -> int:
        await _set_playlists(hass, init_integration, mock_client, untagged)
        return 0

    mock_client.clear_queue.side_effect = _remove_selector
    await _select(hass, "lounge")
    assert entity_registry.async_get(ENTITY) is None
    await _advance(hass, freezer, 16)
    assert TIMEOUT_WARNING not in caplog.text


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


async def test_same_value_repairs_disabled_playlists(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Re-selecting the current value only re-enables its disabled playlists."""
    await _set_playlists(hass, init_integration, mock_client, _with_enabled({10, 14}))
    assert hass.states.get(ENTITY).state == "chill"
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    mock_client.get_playlists.reset_mock()
    await _select(hass, "chill")
    assert mock_client.set_playlist_enabled.await_args_list == [call(1, 11, True)]
    mock_client.clear_queue.assert_not_called()
    assert events == []
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
    assert mock_client.get_playlists.await_count >= 1


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


async def test_partial_failure_clears_earlier_pending(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    await _select(hass, "fiesta")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "fiesta"
    mock_client.set_playlist_enabled.reset_mock()
    mock_client.set_playlist_enabled.side_effect = [None, AzuraCastResponseError(500, "x")]
    mock_client.get_playlists.reset_mock()
    with pytest.raises(HomeAssistantError) as info:
        await _select(hass, "lounge")
    assert info.value.translation_key == "selector_apply_failed"
    assert mock_client.set_playlist_enabled.await_count == 2
    assert mock_client.get_playlists.await_count >= 1
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
    await _advance(hass, freezer, 16)
    assert TIMEOUT_WARNING not in caplog.text


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


async def test_pending_works_after_entity_rename(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """HA re-adds the same instance on rename; pending feedback must survive it."""
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    await _select(hass, "lounge")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"
    entity_registry.async_update_entity(ENTITY, new_entity_id="select.lobby_mood")
    await hass.async_block_till_done()
    renamed = hass.states.get("select.lobby_mood")
    assert renamed is not None
    assert renamed.attributes["pending_value"] is None
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: "select.lobby_mood", ATTR_OPTION: "fiesta"},
        blocking=True,
    )
    assert hass.states.get("select.lobby_mood").attributes["pending_value"] == "fiesta"


async def test_same_value_never_rejected_when_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """08:00 in Costa Rica: nothing tagged is scheduled, chill is still on."""
    freezer.move_to("2026-11-20 14:00:00+00:00")
    assert hass.states.get(ENTITY).state == "chill"
    await _select(hass, "chill")
    mock_client.set_playlist_enabled.assert_not_called()
    mock_client.clear_queue.assert_not_called()


async def test_same_value_repairs_when_unavailable(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _set_playlists(hass, init_integration, mock_client, _with_enabled({10, 14}))
    freezer.move_to("2026-11-20 14:00:00+00:00")
    await _select(hass, "chill")
    assert mock_client.set_playlist_enabled.await_args_list == [call(1, 11, True)]
    mock_client.clear_queue.assert_not_called()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_pending_without_song_at_apply_clears_on_first_match(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """With no song known at apply time, any push from the value's playlists clears it."""
    nowplaying = init_integration.runtime_data.stations[1].nowplaying
    nowplaying.async_set_updated_data(None)
    await _select(hass, "lounge")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"
    nowplaying.push(make_nowplaying(0, song_id="a1", playlist="tarde-lounge"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
