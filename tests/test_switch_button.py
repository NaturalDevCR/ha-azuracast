"""Tests for playlist switches and buttons."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import (
    AzuraCastPermissionError,
    AzuraCastResponseError,
)
from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.const import DOMAIN

from .common import playlists


async def test_playlist_switches(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    chill = hass.states.get("switch.ambientes_playlist_tarde_chill")
    assert chill.state == STATE_ON
    assert chill.attributes["playlist_id"] == 10
    assert chill.attributes["tags"] == {"ambiente": "chill"}
    assert chill.attributes["schedule"] == [
        {"start": "12:00", "end": "18:00", "days": [], "start_date": None, "end_date": None}
    ]
    assert hass.states.get("switch.ambientes_playlist_tarde_lounge").state == STATE_OFF
    assert hass.states.get("switch.party_time_playlist_party_mix") is None

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_lounge"},
        blocking=True,
    )
    mock_client.set_playlist_enabled.assert_awaited_with(1, 12, True)
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_chill"},
        blocking=True,
    )
    mock_client.set_playlist_enabled.assert_awaited_with(1, 10, False)


async def test_switch_error(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.set_playlist_enabled.side_effect = AzuraCastResponseError(500, "boom")
    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_lounge"},
            blocking=True,
        )
    # ServiceNotFound is also a HomeAssistantError, so pin the translated API error.
    assert exc_info.value.translation_key == "api_error"
    mock_client.set_playlist_enabled.assert_awaited_once_with(1, 12, True)


async def test_playlists_added_and_removed(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    new = Playlist.from_dict(
        {"id": 15, "name": "manana-cafe", "description": "ha:ambiente=cafe", "is_enabled": False}
    )
    mock_client.get_playlists.side_effect = None
    mock_client.get_playlists.return_value = [p for p in playlists() if p.id != 14] + [new]
    await init_integration.runtime_data.stations[1].manage.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.ambientes_playlist_manana_cafe") is not None
    assert entity_registry.async_get("switch.ambientes_playlist_respaldo") is None


async def test_read_only_station_orphans_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id
    orphan = entity_registry.async_get_or_create(
        SWITCH_DOMAIN, DOMAIN, f"{entry_id}_2_playlist_99", config_entry=mock_config_entry
    ).entity_id
    kept = entity_registry.async_get_or_create(
        SWITCH_DOMAIN, DOMAIN, f"{entry_id}_1_playlist_10", config_entry=mock_config_entry
    ).entity_id
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(orphan) is None
    assert entity_registry.async_get(kept) is not None
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()


async def test_buttons(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: "button.ambientes_skip_song"}, blocking=True
    )
    mock_client.skip_song.assert_awaited_once_with(1)
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.ambientes_clear_queue"},
        blocking=True,
    )
    mock_client.clear_queue.assert_awaited_once_with(1)
    assert hass.states.get("button.party_time_skip_song") is None


async def test_no_broadcast_station_has_no_buttons(
    hass: HomeAssistant, init_no_broadcast: MockConfigEntry
) -> None:
    """Media permission alone keeps playlists and selectors, but not skip or clear."""
    assert hass.states.get("button.ambientes_skip_song") is None
    assert hass.states.get("button.ambientes_clear_queue") is None
    assert hass.states.get("switch.ambientes_playlist_tarde_chill") is not None
    assert hass.states.get("select.ambientes_ambiente") is not None


@pytest.mark.parametrize("no_broadcast", [False, True])
async def test_orphan_buttons_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    entity_registry: er.EntityRegistry,
    no_broadcast: bool,
) -> None:
    """Exact skip/clear unique ids go for stations that cannot broadcast; others stay."""
    if no_broadcast:
        mock_client.get_queue.side_effect = AzuraCastPermissionError(403, "Forbidden")
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id

    def _register(unique_id: str) -> str:
        return entity_registry.async_get_or_create(
            BUTTON_DOMAIN, DOMAIN, unique_id, config_entry=mock_config_entry
        ).entity_id

    read_only = [_register(f"{entry_id}_2_skip_song"), _register(f"{entry_id}_2_clear_queue")]
    station_1 = [_register(f"{entry_id}_1_skip_song"), _register(f"{entry_id}_1_clear_queue")]
    unrelated = _register(f"{entry_id}_2_skip_song_extra")
    assert await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()
    for entity_id in read_only:
        assert entity_registry.async_get(entity_id) is None
    for entity_id in station_1:
        assert (entity_registry.async_get(entity_id) is None) is no_broadcast
    assert entity_registry.async_get(unrelated) is not None
    assert await hass.config_entries.async_unload(entry_id)
    await hass.async_block_till_done()
