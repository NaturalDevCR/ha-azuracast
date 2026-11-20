"""Tests for integration services."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.const import DOMAIN


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
