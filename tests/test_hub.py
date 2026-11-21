"""Tests for the now-playing hub."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.azuracast import hub as hub_module
from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastNotFoundError
from custom_components.azuracast.api.models import NowPlaying
from custom_components.azuracast.const import DOMAIN, SSE_MAX_BACKOFF
from custom_components.azuracast.coordinator import NowPlayingCoordinator
from custom_components.azuracast.hub import NowPlayingHub

from .common import BASE_URL, FakeStream, make_nowplaying, nowplaying_list, stations, wait_for

UNAVAILABLE = "AzuraCast now-playing data unavailable"
AVAILABLE_AGAIN = "AzuraCast now-playing data available again"
UNEXPECTED_STREAM = "Unexpected error in the AzuraCast now-playing stream"


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "SSE_INITIAL_BACKOFF", 0.01)


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


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()


def _messages(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(message)]


def _gate(result: list[NowPlaying] | Exception) -> tuple[asyncio.Event, AsyncMock]:
    """A get_nowplaying stand-in that waits for the event before answering."""
    release = asyncio.Event()

    async def _get() -> list[NowPlaying]:
        await release.wait()
        if isinstance(result, Exception):
            raise result
        return result

    return release, AsyncMock(side_effect=_get)


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
    await wait_for(lambda: hub.sse_connected)
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
    await wait_for(lambda: hub.sse_connected)
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


async def test_unexpected_poll_error_marks_unavailable_without_raising(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client.get_nowplaying.side_effect = KeyError("station")
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    assert all(not c.last_update_success for c in coordinators.values())
    assert all(isinstance(c.last_exception, KeyError) for c in coordinators.values())
    await _tick(hass)
    records = _messages(caplog, UNAVAILABLE)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].exc_info is not None
    await hub.async_stop()


async def test_availability_logging_survives_sse_reconnect(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client.get_nowplaying.side_effect = AzuraCastConnectionError("down")
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    assert len(_messages(caplog, UNAVAILABLE)) == 1

    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected)
    assert len(_messages(caplog, AVAILABLE_AGAIN)) == 1

    await stream.queue.put(AzuraCastConnectionError("dropped"))
    await wait_for(lambda: not hub.sse_connected)
    await _tick(hass)
    assert len(_messages(caplog, UNAVAILABLE)) == 2
    await hub.async_stop()


async def test_second_unexpected_stream_error_logs_at_debug(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.azuracast")
    stream = FakeStream()
    await stream.queue.put(RuntimeError("boom 1"))
    await stream.queue.put(RuntimeError("boom 2"))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    await wait_for(lambda: stream.calls == 3)
    levels = [r.levelno for r in _messages(caplog, UNEXPECTED_STREAM)]
    assert levels == [logging.ERROR, logging.DEBUG]
    await hub.async_stop()


async def test_failure_after_connection_does_not_raise_issue(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, issue_registry: ir.IssueRegistry
) -> None:
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected)
    await stream.queue.put(AzuraCastConnectionError("dropped"))
    await stream.queue.put(RuntimeError("boom"))
    await wait_for(lambda: stream.calls == 3)
    assert issue_registry.async_get_issue(DOMAIN, f"sse_unavailable_{entry.entry_id}") is None
    await hub.async_stop()


async def test_polling_resumes_after_sse_drops(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected)
    client.get_nowplaying.reset_mock()
    await _tick(hass)
    client.get_nowplaying.assert_not_called()

    await stream.queue.put(AzuraCastConnectionError("dropped"))
    await wait_for(lambda: not hub.sse_connected)
    await _tick(hass)
    client.get_nowplaying.assert_awaited_once()
    await hub.async_stop()


async def test_unknown_shortcode_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    only_ambientes = {"ambientes": coordinators["ambientes"]}
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, only_ambientes, stream_factory=stream)
    await hub.async_start()
    assert coordinators["ambientes"].data.current.song.id == "a1"
    assert coordinators["party_time"].data is None
    await stream.queue.put(make_nowplaying(1))
    await stream.queue.put(make_nowplaying(0, song_id="a9"))
    await wait_for(lambda: coordinators["ambientes"].data.current.song.id == "a9")
    assert coordinators["party_time"].data is None
    await hub.async_stop()


@pytest.mark.parametrize(
    "result", [[make_nowplaying(0, song_id="late")], AzuraCastConnectionError("late")]
)
async def test_poll_result_after_stop_is_ignored(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    result: list[NowPlaying] | Exception,
) -> None:
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    release, client.get_nowplaying = _gate(result)
    poll = asyncio.create_task(hub.async_poll())
    await wait_for(lambda: client.get_nowplaying.await_count == 1)
    await hub.async_stop()
    release.set()
    await poll
    assert coordinators["ambientes"].data.current.song.id == "a1"
    assert all(c.last_update_success for c in coordinators.values())


async def test_in_flight_poll_does_not_overwrite_sse_data(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=stream)
    await hub.async_start()
    release, client.get_nowplaying = _gate([make_nowplaying(0, song_id="stale")])
    poll = asyncio.create_task(hub.async_poll())
    await wait_for(lambda: client.get_nowplaying.await_count == 1)
    await stream.queue.put(make_nowplaying(0, song_id="a9"))
    await wait_for(lambda: hub.sse_connected)
    release.set()
    await poll
    assert coordinators["ambientes"].data.current.song.id == "a9"
    await hub.async_stop()


def test_backoff_delay_with_jitter_is_capped(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hub_module.random, "uniform", lambda _low, high: high)
    hub = NowPlayingHub(hass, entry, client, {}, stream_factory=FakeStream())
    delays = [hub._next_backoff_delay() for _ in range(20)]
    assert delays[0] == pytest.approx(0.015)
    assert max(delays) == SSE_MAX_BACKOFF
