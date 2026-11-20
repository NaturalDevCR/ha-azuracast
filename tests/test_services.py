"""Tests for integration services."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
import probatio
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastResponseError
from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.const import DOMAIN

from .common import playlists


def _device(device_registry: dr.DeviceRegistry, entry: MockConfigEntry, station_id: int) -> str:
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_{station_id}"), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_skip_song_by_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "skip_song",
        {ATTR_DEVICE_ID: _device(device_registry, init_integration, 1)},
        blocking=True,
    )
    mock_client.skip_song.assert_awaited_once_with(1)


async def test_clear_queue_by_entity(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.services.async_call(
        DOMAIN, "clear_queue", {ATTR_ENTITY_ID: "sensor.ambientes_listeners"}, blocking=True
    )
    mock_client.clear_queue.assert_awaited_once_with(1)


async def test_read_only_station_rejected(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "skip_song",
            {ATTR_DEVICE_ID: _device(device_registry, init_integration, 2)},
            blocking=True,
        )
    assert info.value.translation_key == "station_not_managed"
    mock_client.skip_song.assert_not_called()


async def test_set_playlists_enabled(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_playlists_enabled",
        {
            ATTR_ENTITY_ID: "media_player.ambientes",
            "playlists": ["tarde-lounge", "13"],
            "enabled": True,
        },
        blocking=True,
    )
    assert [c.args for c in mock_client.set_playlist_enabled.await_args_list] == [
        (1, 12, True),
        (1, 13, True),
    ]


async def test_unknown_playlist(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "set_playlists_enabled",
            {ATTR_ENTITY_ID: "media_player.ambientes", "playlists": ["nope"], "enabled": False},
            blocking=True,
        )
    assert info.value.translation_key == "unknown_playlist"
    mock_client.set_playlist_enabled.assert_not_called()


async def test_reshuffle(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "reshuffle_playlist",
        {ATTR_ENTITY_ID: "media_player.ambientes", "playlist": "10"},
        blocking=True,
    )
    mock_client.reshuffle_playlist.assert_awaited_once_with(1, 10)


async def test_no_target(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN, "skip_song", {ATTR_ENTITY_ID: "sun.sun"}, blocking=True
        )
    assert info.value.translation_key == "no_station_targeted"


async def _setup(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    rows: dict[int, list[Playlist]],
) -> None:
    """Set up the integration with the given playlists per manageable station."""
    client.get_playlists.side_effect = lambda station_id: rows[station_id]
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _numeric_named() -> list[Playlist]:
    base = playlists()
    # id 13 comes first in API order, so a first-match-on-name-or-id lookup would pick it.
    return [replace(base[4], id=13, name="x"), *base[:3], replace(base[3], id=5, name="13")]


async def test_numeric_name_wins_over_id(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await _setup(hass, mock_config_entry, mock_client, {1: _numeric_named(), 2: playlists()})
    await hass.services.async_call(
        DOMAIN,
        "set_playlists_enabled",
        {ATTR_ENTITY_ID: "media_player.ambientes", "playlists": ["13"], "enabled": True},
        blocking=True,
    )
    mock_client.set_playlist_enabled.assert_awaited_once_with(1, 5, True)


async def test_id_fallback(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await _setup(hass, mock_config_entry, mock_client, {1: _numeric_named(), 2: playlists()})
    await hass.services.async_call(
        DOMAIN,
        "reshuffle_playlist",
        {ATTR_ENTITY_ID: "media_player.ambientes", "playlist": "11"},
        blocking=True,
    )
    mock_client.reshuffle_playlist.assert_awaited_once_with(1, 11)


async def test_duplicate_name_is_ambiguous(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    base = playlists()
    rows = [*base[:4], replace(base[4], name="tarde-chill")]
    await _setup(hass, mock_config_entry, mock_client, {1: rows, 2: playlists()})
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "reshuffle_playlist",
            {ATTR_ENTITY_ID: "media_player.ambientes", "playlist": "tarde-chill"},
            blocking=True,
        )
    assert info.value.translation_key == "ambiguous_playlist"
    assert info.value.translation_placeholders == {
        "playlist": "tarde-chill",
        "station": "Ambientes",
    }
    mock_client.reshuffle_playlist.assert_not_called()


async def test_empty_playlists_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_playlists_enabled",
            {ATTR_ENTITY_ID: "media_player.ambientes", "playlists": [], "enabled": True},
            blocking=True,
        )
    mock_client.set_playlist_enabled.assert_not_called()


async def test_instance_device_only(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, init_integration.entry_id), init_integration.entry_id
    )
    assert device is not None
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN, "skip_song", {ATTR_DEVICE_ID: device.id}, blocking=True
        )
    assert info.value.translation_key == "no_station_targeted"
    mock_client.skip_song.assert_not_called()


async def test_unknown_on_second_station_is_all_or_nothing(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    second = [p for p in playlists() if p.name != "tarde-lounge"]
    await _setup(hass, mock_config_entry, mock_client, {1: playlists(), 2: second})
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "set_playlists_enabled",
            {
                ATTR_ENTITY_ID: ["media_player.ambientes", "media_player.party_time"],
                "playlists": ["tarde-lounge"],
                "enabled": True,
            },
            blocking=True,
        )
    assert info.value.translation_key == "unknown_playlist"
    assert info.value.translation_placeholders == {
        "playlist": "tarde-lounge",
        "station": "Party Time",
    }
    mock_client.set_playlist_enabled.assert_not_called()


async def test_partial_api_failure_still_refreshes(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.set_playlist_enabled.side_effect = [None, AzuraCastResponseError(500, "boom")]
    mock_client.get_playlists.reset_mock()
    with pytest.raises(HomeAssistantError) as info:
        await hass.services.async_call(
            DOMAIN,
            "set_playlists_enabled",
            {
                ATTR_ENTITY_ID: "media_player.ambientes",
                "playlists": ["tarde-lounge", "noche-fiesta"],
                "enabled": True,
            },
            blocking=True,
        )
    assert info.value.translation_key == "api_error"
    assert mock_client.set_playlist_enabled.await_count == 2
    mock_client.get_playlists.assert_awaited_with(1)


async def test_reshuffle_by_name(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "reshuffle_playlist",
        {ATTR_ENTITY_ID: "media_player.ambientes", "playlist": "noche-chill"},
        blocking=True,
    )
    mock_client.reshuffle_playlist.assert_awaited_once_with(1, 11)
