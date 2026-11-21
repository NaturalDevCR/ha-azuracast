"""Tests for selector select entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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

from custom_components.azuracast import select as select_module
from custom_components.azuracast.api.errors import AzuraCastResponseError
from custom_components.azuracast.api.models import NowPlaying, Playlist
from custom_components.azuracast.const import (
    DOMAIN,
    EVENT_SELECTOR_APPLIED,
    MAX_STALE_SKIPS,
    PENDING_TIMEOUT,
    ChangeMode,
)

from .common import make_nowplaying, nowplaying_raw, playlists, queue

ENTITY = "select.ambientes_ambiente"
TIMEOUT_WARNING = "was applied but no song from it started"
PENDING_MINUTES = int(PENDING_TIMEOUT.total_seconds() // 60)


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
        "change_mode": "wait",
    }
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"
    mock_client.skip_song.assert_not_called()


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
    await _advance(hass, freezer, PENDING_MINUTES - 1)
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
    await _advance(hass, freezer, PENDING_MINUTES - 5)
    await _select(hass, "fiesta")
    assert hass.states.get(ENTITY).attributes["pending_value"] == "fiesta"
    await _advance(hass, freezer, 6)
    assert TIMEOUT_WARNING not in caplog.text
    assert hass.states.get(ENTITY).attributes["pending_value"] == "fiesta"
    await _advance(hass, freezer, PENDING_MINUTES - 5)
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
    await _advance(hass, freezer, PENDING_MINUTES + 1)
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
    await _advance(hass, freezer, PENDING_MINUTES + 1)
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
    await _advance(hass, freezer, PENDING_MINUTES + 1)
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


async def test_select_without_broadcast_keeps_queue(
    hass: HomeAssistant, init_no_broadcast: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Playlists still switch; the queue clear is skipped and reported as not done."""
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert mock_client.set_playlist_enabled.await_args_list == [
        call(1, 12, True),
        call(1, 10, False),
        call(1, 11, False),
    ]
    mock_client.clear_queue.assert_not_called()
    assert events[0].data["cleared_queue"] is False


async def test_select_plans_from_fresh_playlists(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Lounge was switched on in the AzuraCast UI since the last poll; chill must still apply."""
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    assert hass.states.get(ENTITY).state == "chill"
    mock_client.get_playlists.side_effect = None
    mock_client.get_playlists.return_value = _with_enabled({12, 14})
    await _select(hass, "chill")
    assert mock_client.set_playlist_enabled.await_args_list == [
        call(1, 10, True),
        call(1, 11, True),
        call(1, 12, False),
    ]
    mock_client.clear_queue.assert_awaited_once_with(1)
    assert events[0].data["previous_value"] == "lounge"


async def test_select_refresh_failure_changes_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_playlists.side_effect = AzuraCastResponseError(500, "boom")
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    with pytest.raises(HomeAssistantError) as info:
        await _select(hass, "lounge")
    assert info.value.translation_key == "selector_apply_failed"
    assert "boom" in info.value.translation_placeholders["error"]
    mock_client.set_playlist_enabled.assert_not_called()
    mock_client.clear_queue.assert_not_called()
    assert events == []


async def _play(hass: HomeAssistant, entry: MockConfigEntry, song_id: str, playlist: str) -> None:
    entry.runtime_data.stations[1].nowplaying.push(
        make_nowplaying(0, song_id=song_id, playlist=playlist)
    )
    await hass.async_block_till_done()


def _immediate(entry: MockConfigEntry) -> None:
    entry.runtime_data.stations[1].change_mode = ChangeMode.SKIP


async def test_default_change_skips_nothing(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Songs Liquidsoap already holds play out: smooth, at the cost of a delay."""
    await _select(hass, "lounge")
    await _play(hass, init_integration, "old1", "tarde-chill")
    await _play(hass, init_integration, "old2", "tarde-chill")
    mock_client.skip_song.assert_not_called()
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"


async def test_immediate_change_skips_current_then_stale_songs(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_integration)
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert mock_client.mock_calls.index(call.clear_queue(1)) < mock_client.mock_calls.index(
        call.skip_song(1)
    )
    assert events[0].data["change_mode"] == "skip"
    # The song playing at apply time was skipped already; it is not skipped again.
    await _play(hass, init_integration, "a1", "tarde-chill")
    assert mock_client.skip_song.await_count == 1
    # A song from the previous value that Liquidsoap had loaded is skipped once.
    await _play(hass, init_integration, "old1", "tarde-chill")
    assert mock_client.skip_song.await_count == 2
    await _play(hass, init_integration, "old1", "tarde-chill")
    assert mock_client.skip_song.await_count == 2
    await _play(hass, init_integration, "new1", "tarde-lounge")
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
    # Once the change has landed, songs from other values are left alone.
    await _play(hass, init_integration, "old2", "tarde-chill")
    assert mock_client.skip_song.await_count == 2


async def test_immediate_stale_skips_are_capped(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_integration)
    await _select(hass, "lounge")
    for index in range(MAX_STALE_SKIPS + 2):
        await _play(hass, init_integration, f"old{index}", "tarde-chill")
    assert mock_client.skip_song.await_count == 1 + MAX_STALE_SKIPS


async def test_immediate_leaves_untagged_songs_and_requests(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_integration)
    await _select(hass, "lounge")
    await _play(hass, init_integration, "u1", "respaldo")
    await _play(hass, init_integration, "r1", "")
    assert mock_client.skip_song.await_count == 1
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"


async def test_immediate_stale_skip_failure_is_logged(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _immediate(init_integration)
    await _select(hass, "lounge")
    mock_client.skip_song.side_effect = AzuraCastResponseError(500, "boom")
    with caplog.at_level(logging.WARNING):
        await _play(hass, init_integration, "old1", "tarde-chill")
    assert "Could not skip a song from the previous value" in caplog.text


async def test_immediate_skip_failure_keeps_selection(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_integration)
    mock_client.skip_song.side_effect = AzuraCastResponseError(500, "boom")
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert events[0].data["change_mode"] == "skip"
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"


async def test_immediate_not_used_for_repair(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_integration)
    await _set_playlists(hass, init_integration, mock_client, _with_enabled({10}))
    await _select(hass, "chill")
    mock_client.skip_song.assert_not_called()


async def test_immediate_skips_nothing_when_queue_kept(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Keeping the queue keeps old songs on purpose; skipping them would fight it."""
    _immediate(init_integration)
    init_integration.runtime_data.stations[1].clear_queue_on_change = False
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    await _play(hass, init_integration, "old1", "tarde-chill")
    mock_client.skip_song.assert_not_called()
    assert events[0].data["change_mode"] == "wait"


async def test_immediate_needs_broadcast(
    hass: HomeAssistant, init_no_broadcast: MockConfigEntry, mock_client: MagicMock
) -> None:
    _immediate(init_no_broadcast)
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    await _play(hass, init_no_broadcast, "old1", "tarde-chill")
    mock_client.skip_song.assert_not_called()
    assert events[0].data["change_mode"] == "wait"


def _reload_mode(entry: MockConfigEntry) -> None:
    entry.runtime_data.stations[1].change_mode = ChangeMode.RELOAD


@pytest.fixture
def fast_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    # Time is frozen in these tests, so polls must not wait on the clock.
    monkeypatch.setattr(select_module, "RELOAD_QUEUE_POLL", 0)
    monkeypatch.setattr(select_module, "RELOAD_QUEUE_ATTEMPTS", 50)


async def _spin(hass: HomeAssistant, until: Callable[[], bool] | None = None) -> None:
    """Let background tasks run without advancing the frozen clock."""
    for _ in range(200):
        if until is not None and until():
            return
        await asyncio.sleep(0)
    await hass.async_block_till_done()
    if until is not None:
        assert until()


@pytest.mark.usefixtures("fast_reload")
async def test_reload_mode_reloads_once_next_song_is_ready(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Reloading before AzuraCast refills the queue would leave Liquidsoap silent."""
    _reload_mode(init_integration)
    mock_client.get_queue.return_value = []
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert events[0].data["change_mode"] == "reload"
    for _ in range(10):
        await asyncio.sleep(0)
    mock_client.reload_station.assert_not_called()
    mock_client.get_queue.return_value = queue()
    await _spin(hass, lambda: mock_client.reload_station.await_count == 1)
    mock_client.reload_station.assert_awaited_once_with(1)
    mock_client.skip_song.assert_not_called()
    # The reload drops what Liquidsoap held, so nothing is skipped afterwards.
    await _play(hass, init_integration, "old1", "tarde-chill")
    mock_client.skip_song.assert_not_called()


@pytest.mark.usefixtures("fast_reload")
async def test_reload_mode_gives_up_without_next_song(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reload_mode(init_integration)
    # Only songs already handed to Liquidsoap: nothing new to play after a reload.
    mock_client.get_queue.return_value = [i for i in queue() if i.sent_to_autodj]
    with caplog.at_level(logging.WARNING):
        await _select(hass, "lounge")
        await _spin(hass)
    mock_client.reload_station.assert_not_called()
    assert "not reloading" in caplog.text


@pytest.mark.usefixtures("fast_reload")
async def test_reload_failure_is_logged(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _reload_mode(init_integration)
    mock_client.reload_station.side_effect = AzuraCastResponseError(500, "boom")
    with caplog.at_level(logging.WARNING):
        await _select(hass, "lounge")
        await _spin(hass, lambda: mock_client.reload_station.await_count == 1)
    assert "Could not reload" in caplog.text


@pytest.mark.usefixtures("fast_reload")
async def test_new_selection_cancels_pending_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    _reload_mode(init_integration)
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    mock_client.get_queue.return_value = []
    await _select(hass, "lounge")
    await _select(hass, "fiesta")
    mock_client.get_queue.return_value = queue()
    await _spin(hass, lambda: mock_client.reload_station.await_count == 1)
    await _spin(hass)
    assert mock_client.reload_station.await_count == 1


async def test_reload_mode_skips_when_station_cannot_soft_reload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Without Icecast a reload restarts everything and drops listeners."""
    _reload_mode(init_integration)
    runtime = init_integration.runtime_data.stations[1]
    runtime.station = replace(runtime.station, frontend="shoutcast2")
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    await hass.async_block_till_done()
    mock_client.reload_station.assert_not_called()
    mock_client.skip_song.assert_awaited_once_with(1)
    assert events[0].data["change_mode"] == "skip"


@pytest.mark.parametrize("mode", [ChangeMode.SKIP, ChangeMode.RELOAD])
async def test_immediate_modes_leave_live_dj_alone(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, mode: ChangeMode
) -> None:
    """A reload would disconnect the DJ and a skip has nothing of ours to skip."""
    init_integration.runtime_data.stations[1].change_mode = mode
    raw = nowplaying_raw(0)
    raw["live"]["is_live"] = True
    init_integration.runtime_data.stations[1].nowplaying.push(NowPlaying.from_dict(raw))
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    await hass.async_block_till_done()
    mock_client.reload_station.assert_not_called()
    mock_client.skip_song.assert_not_called()
    assert events[0].data["change_mode"] == "wait"


@pytest.mark.parametrize("mode", [ChangeMode.SKIP, ChangeMode.RELOAD])
async def test_immediate_modes_wait_when_station_offline(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, mode: ChangeMode
) -> None:
    init_integration.runtime_data.stations[1].change_mode = mode
    init_integration.runtime_data.stations[1].nowplaying.push(make_nowplaying(0, is_online=False))
    await _select(hass, "lounge")
    await hass.async_block_till_done()
    mock_client.reload_station.assert_not_called()
    mock_client.skip_song.assert_not_called()
