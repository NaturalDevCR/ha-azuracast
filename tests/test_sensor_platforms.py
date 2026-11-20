"""Tests for media_player, sensor and binary_sensor platforms."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.media_player import (
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_TITLE,
    SERVICE_MEDIA_NEXT_TRACK,
    MediaPlayerEntityFeature,
)
from homeassistant.components.media_player import (
    DOMAIN as MP_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_ENTITY_PICTURE,
    ATTR_SUPPORTED_FEATURES,
    STATE_IDLE,
    STATE_OFF,
    STATE_ON,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastResponseError
from custom_components.azuracast.api.models import NowPlaying
from custom_components.azuracast.const import DOMAIN

from .common import BASE_URL, make_nowplaying, nowplaying_raw


async def test_media_player(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    state = hass.states.get("media_player.ambientes")
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Bossa Breeze"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Trio Mar"
    assert state.attributes[ATTR_MEDIA_ALBUM_NAME] == "Costa"
    assert state.attributes[ATTR_MEDIA_DURATION] == 240
    assert state.attributes[ATTR_MEDIA_POSITION] == 60
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == MediaPlayerEntityFeature.NEXT_TRACK

    party = hass.states.get("media_player.party_time")
    assert party.attributes[ATTR_SUPPORTED_FEATURES] == 0

    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_MEDIA_NEXT_TRACK,
        {ATTR_ENTITY_ID: "media_player.ambientes"},
        blocking=True,
    )
    mock_client.skip_song.assert_awaited_once_with(1)


async def test_media_player_skip_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.skip_song.side_effect = AzuraCastResponseError(500, "boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_MEDIA_NEXT_TRACK,
            {ATTR_ENTITY_ID: "media_player.ambientes"},
            blocking=True,
        )


async def test_media_player_offline(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    init_integration.runtime_data.stations[1].nowplaying.push(make_nowplaying(0, is_online=False))
    await hass.async_block_till_done()
    assert hass.states.get("media_player.ambientes").state == STATE_OFF
    assert hass.states.get("binary_sensor.ambientes_online").state == STATE_OFF


async def test_sensors(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    listeners = hass.states.get("sensor.ambientes_listeners")
    assert listeners.state == "3"
    assert listeners.attributes["unique"] == 3
    assert hass.states.get("sensor.ambientes_current_song").state == "Trio Mar - Bossa Breeze"
    playlist = hass.states.get("sensor.ambientes_current_playlist")
    assert playlist.state == "tarde-chill"
    assert playlist.attributes["tags"] == {"ambiente": "chill"}
    assert hass.states.get("sensor.ambientes_next_song").state == "Trio Mar - Slow Tide"
    assert hass.states.get("sensor.party_time_live_streamer").state == "DJ Sol"
    assert hass.states.get("binary_sensor.party_time_live_dj").state == STATE_ON
    assert hass.states.get("binary_sensor.ambientes_live_dj").state == STATE_OFF


async def test_entities_unavailable_when_poll_fails(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_nowplaying.side_effect = AzuraCastConnectionError("down")
    await init_integration.runtime_data.hub.async_poll()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ambientes_listeners").state == STATE_UNAVAILABLE
    assert hass.states.get("media_player.ambientes").state == STATE_UNAVAILABLE


async def test_station_device(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    entry_id = init_integration.entry_id
    devices = dr.async_get(hass)
    instance = devices.async_get_device_by_identifier((DOMAIN, entry_id), entry_id)
    station = devices.async_get_device_by_identifier((DOMAIN, f"{entry_id}_1"), entry_id)
    assert instance is not None
    assert station is not None
    assert station.name == "Ambientes"
    assert station.manufacturer == "AzuraCast"
    assert station.configuration_url == f"{BASE_URL}/station/1/profile"
    assert station.via_device_id == instance.id


async def test_media_player_unique_id(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    entity = er.async_get(hass).async_get("media_player.ambientes")
    assert entity is not None
    assert entity.unique_id == f"{init_integration.entry_id}_1_media_player"


async def test_media_player_idle_without_song(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    raw = nowplaying_raw(0)
    raw["now_playing"] = None
    init_integration.runtime_data.stations[1].nowplaying.push(NowPlaying.from_dict(raw))
    await hass.async_block_till_done()
    state = hass.states.get("media_player.ambientes")
    assert state.state == STATE_IDLE
    assert ATTR_MEDIA_TITLE not in state.attributes


async def test_media_player_artwork_is_proxied(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    state = hass.states.get("media_player.ambientes")
    assert state.attributes[ATTR_ENTITY_PICTURE].startswith(
        "/api/media_player_proxy/media_player.ambientes?"
    )


async def test_read_only_playlist_has_no_tags(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    playlist = hass.states.get("sensor.party_time_current_playlist")
    assert playlist.state == "party-mix"
    assert "tags" not in playlist.attributes


async def test_live_streamer_unknown_when_not_live(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    assert hass.states.get("sensor.ambientes_live_streamer").state == STATE_UNKNOWN


async def test_string_states_are_truncated(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    raw = nowplaying_raw(1)
    raw["now_playing"]["playlist"] = "p" * 300
    raw["live"]["streamer_name"] = "s" * 300
    init_integration.runtime_data.stations[2].nowplaying.push(NowPlaying.from_dict(raw))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.party_time_current_playlist").state == "p" * 255
    assert hass.states.get("sensor.party_time_live_streamer").state == "s" * 255
