"""Tests for the coordinators and issues."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
    AzuraCastPermissionError,
)
from custom_components.azuracast.const import DOMAIN, EVENT_SONG_CHANGED
from custom_components.azuracast.coordinator import (
    ManageCoordinator,
    NowPlayingCoordinator,
    tags_for_playlist,
)

from .common import conflict_playlists, make_nowplaying, playlists, queue, stations


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="radio.example.com", data={})
    entry.add_to_hass(hass)
    return entry


def _manage(hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock) -> ManageCoordinator:
    return ManageCoordinator(hass, entry, client, stations()[0], timedelta(seconds=60))


async def test_manage_coordinator_builds_selectors(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    client = AsyncMock()
    client.get_playlists.return_value = playlists()
    client.get_queue.return_value = queue()
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert [p.id for p in coordinator.data.playlists] == [10, 11, 12, 13, 14]
    assert list(coordinator.data.selectors) == ["ambiente"]
    assert coordinator.data.playlist_by_name("tarde-chill").id == 10
    assert coordinator.data.playlist_by_name(None) is None
    assert tags_for_playlist(coordinator, "tarde-chill") == {"ambiente": "chill"}
    assert tags_for_playlist(coordinator, "missing") == {}
    assert tags_for_playlist(None, "tarde-chill") == {}


async def test_manage_coordinator_permission_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastPermissionError(403, "Forbidden")
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    issue_id = f"missing_permissions_{entry.entry_id}_1"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    client.get_playlists.side_effect = None
    client.get_playlists.return_value = playlists()
    client.get_queue.return_value = []
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_manage_coordinator_auth_error(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastAuthError(401, "")
    coordinator = _manage(hass, entry, client)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_manage_coordinator_connection_error(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastConnectionError("down")
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_conflicting_tags_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    client = AsyncMock()
    client.get_playlists.return_value = conflict_playlists()
    client.get_queue.return_value = []
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    issue = issue_registry.async_get_issue(DOMAIN, f"conflicting_tags_{entry.entry_id}_1")
    assert issue is not None
    assert issue.translation_placeholders == {"station": "Ambientes", "playlists": "both"}


async def test_nowplaying_push_fires_song_changed_only_on_change(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_SONG_CHANGED)
    coordinator = NowPlayingCoordinator(
        hass, entry, stations()[0], lambda name: {"ambiente": "chill"}
    )
    coordinator.push(make_nowplaying(0))
    coordinator.push(make_nowplaying(0))
    await hass.async_block_till_done()
    assert events == []
    assert coordinator.data.current.song.id == "a1"

    coordinator.push(make_nowplaying(0, song_id="a2", title="Slow Tide"))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {
        "station_id": 1,
        "station_shortcode": "ambientes",
        "song": {
            "id": "a2",
            "artist": "Trio Mar",
            "title": "Slow Tide",
            "album": "Costa",
            "art": "https://radio.example.com/api/station/ambientes/art/a1.jpg",
        },
        "playlist": "tarde-chill",
        "tags": {"ambiente": "chill"},
    }
