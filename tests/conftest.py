"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockPlatform,
    mock_config_flow,
    mock_platform,
)

from custom_components.azuracast.api.errors import AzuraCastPermissionError
from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.const import DOMAIN

from .common import BASE_URL, FakeStream, nowplaying_list, playlists, queue, stations


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom integrations in every test."""


@pytest.fixture(autouse=True)
def fake_stream() -> Generator[FakeStream]:
    """Replace the SSE stream so no test opens a real connection."""
    stream = FakeStream()
    with patch("custom_components.azuracast.hub.stream_nowplaying", stream):
        yield stream


def _playlists_for(station_id: int) -> list[Playlist]:
    if station_id == 2:
        raise AzuraCastPermissionError(403, "Forbidden")
    return playlists()


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the client used by setup."""
    with patch("custom_components.azuracast.AzuraCastClient", autospec=True) as client_cls:
        client = client_cls.return_value
        client.base_url = BASE_URL
        client.has_api_key = True
        client.session = MagicMock()
        client.get_status.return_value = True
        client.get_account.return_value = {"id": 1}
        client.get_stations.return_value = stations()
        client.get_nowplaying.return_value = nowplaying_list()
        client.get_playlists.side_effect = _playlists_for
        client.get_queue.return_value = queue()
        client.clear_queue.return_value = 2
        yield client


class _StubConfigFlow(ConfigFlow):
    """Placeholder flow handler; it never runs a step (and is not auto-registered)."""

    VERSION = 1


@pytest.fixture
async def config_flow_stub(hass: HomeAssistant) -> AsyncGenerator[None]:
    """Stand in for config_flow.py, which Task 10 adds.

    Home Assistant imports an integration's config_flow platform and looks up its flow
    handler (to migrate the entry) before it sets up any of its config entries.
    Remove this fixture once config_flow.py exists.
    """
    await async_get_integration(hass, DOMAIN)
    mock_platform(hass, f"{DOMAIN}.config_flow", MockPlatform())
    with mock_config_flow(DOMAIN, _StubConfigFlow):
        yield


@pytest.fixture
def mock_config_entry(config_flow_stub: None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="radio.example.com",
        unique_id=BASE_URL,
        data={CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration and unload it after the test."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_config_entry
    if mock_config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
