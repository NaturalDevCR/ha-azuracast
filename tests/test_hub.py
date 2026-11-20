"""Tests for the now-playing hub."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastNotFoundError
from custom_components.azuracast.const import DOMAIN
from custom_components.azuracast.coordinator import NowPlayingCoordinator
from custom_components.azuracast.hub import NowPlayingHub

from .common import BASE_URL, FakeStream, make_nowplaying, nowplaying_list, stations, wait_for


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="radio.example.com", data={})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    client.base_url = BASE_URL
    client.session = MagicMock()
    client.get_nowplaying = AsyncMock(return_value=nowplaying_list())
    return client


def _coordinators(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, NowPlayingCoordinator]:
    return {s.shortcode: NowPlayingCoordinator(hass, entry, s, lambda _: {}) for s in stations()}


async def test_start_polls_once_and_dispatches(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    assert coordinators["ambientes"].data.current.song.id == "a1"
    assert coordinators["party_time"].data.is_live is True
    await hub.async_stop()


async def test_sse_updates_and_suppresses_polling(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    stream = FakeStream()
    hub = NowPlayingHub(
        hass,
        entry,
        client,
        coordinators,
        stream_factory=stream,
        poll_interval=timedelta(seconds=30),
    )
    await hub.async_start()
    await stream.queue.put(make_nowplaying(0, song_id="a9", title="New"))
    await wait_for(lambda: hub.sse_connected)
    assert stream.shortcodes == ["ambientes", "party_time"]
    assert coordinators["ambientes"].data.current.song.id == "a9"

    client.get_nowplaying.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    client.get_nowplaying.assert_not_called()
    await hub.async_stop()
    assert hub.sse_connected is False


async def test_sse_missing_raises_issue_and_keeps_polling(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, issue_registry: ir.IssueRegistry
) -> None:
    stream = FakeStream()
    await stream.queue.put(AzuraCastNotFoundError(404, ""))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    issue_id = f"sse_unavailable_{entry.entry_id}"
    await wait_for(lambda: issue_registry.async_get_issue(DOMAIN, issue_id) is not None)

    client.get_nowplaying.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    client.get_nowplaying.assert_awaited_once()
    await hub.async_stop()


async def test_issue_cleared_once_sse_connects(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, issue_registry: ir.IssueRegistry
) -> None:
    stream = FakeStream()
    await stream.queue.put(AzuraCastConnectionError("refused"))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    issue_id = f"sse_unavailable_{entry.entry_id}"
    await wait_for(lambda: issue_registry.async_get_issue(DOMAIN, issue_id) is not None)
    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected, attempts=300)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    await hub.async_stop()


async def test_unexpected_stream_error_does_not_kill_the_hub(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    issue_registry: ir.IssueRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream = FakeStream()
    await stream.queue.put(RuntimeError("boom"))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    issue_id = f"sse_unavailable_{entry.entry_id}"
    await wait_for(lambda: issue_registry.async_get_issue(DOMAIN, issue_id) is not None)
    assert "Unexpected error in the AzuraCast now-playing stream" in caplog.text
    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected, attempts=300)
    assert stream.calls == 2
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    await hub.async_stop()


async def test_poll_failure_marks_coordinators_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    client.get_nowplaying.side_effect = AzuraCastConnectionError("down")
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    assert all(not c.last_update_success for c in coordinators.values())
    await hub.async_stop()
