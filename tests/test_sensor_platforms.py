"""Tests for media_player, sensor and binary_sensor platforms."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.media_player import (
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
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastResponseError

from .common import make_nowplaying


async def test_media_player(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    state = hass.states.get("media_player.ambientes")
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Bossa Breeze"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Trio Mar"
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
