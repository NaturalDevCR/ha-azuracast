"""Tests for API models."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.azuracast.api.models import NowPlaying, Playlist, ScheduleItem, Station

from .common import load_fixture, nowplaying_raw, playlists, queue, stations


def test_station_from_dict() -> None:
    first, second = stations()
    assert first.id == 1
    assert first.shortcode == "ambientes"
    assert first.name == "Ambientes"
    assert first.timezone == "America/Costa_Rica"
    assert first.is_public is True
    assert first.frontend == "icecast"
    assert first.supports_soft_reload is True
    assert second.shortcode == "party_time"


def test_nowplaying_from_dict() -> None:
    np = NowPlaying.from_dict(load_fixture("nowplaying.json")[0])
    assert np.station.shortcode == "ambientes"
    assert np.is_online is True
    assert (np.listeners_current, np.listeners_unique, np.listeners_total) == (3, 3, 3)
    assert np.is_live is False
    assert np.streamer_name is None
    assert np.current is not None
    assert np.current.song.id == "a1"
    assert np.current.song.title == "Bossa Breeze"
    assert np.current.playlist == "tarde-chill"
    assert (np.current.duration, np.current.elapsed) == (240, 60)
    assert np.upcoming is not None
    assert np.upcoming.song.title == "Slow Tide"


def test_nowplaying_live_and_no_next() -> None:
    np = NowPlaying.from_dict(load_fixture("nowplaying.json")[1])
    assert np.is_live is True
    assert np.streamer_name == "DJ Sol"
    assert np.upcoming is None


def test_nowplaying_offline_without_song() -> None:
    raw = nowplaying_raw(0)
    raw["is_online"] = False
    raw["now_playing"] = None
    np = NowPlaying.from_dict(raw)
    assert np.is_online is False
    assert np.current is None


def test_nowplaying_empty_song_id_means_no_current() -> None:
    raw = nowplaying_raw(0)
    raw["now_playing"]["song"]["id"] = ""
    assert NowPlaying.from_dict(raw).current is None


def test_playlist_from_dict() -> None:
    by_id = {p.id: p for p in playlists()}
    chill = by_id[10]
    assert chill.name == "tarde-chill"
    assert chill.description == "Afternoon chill ha:ambiente=chill"
    assert chill.is_enabled is True
    assert chill.schedule_items == (
        ScheduleItem(start_time=1200, end_time=1800, days=(), start_date=None, end_date=None),
    )
    assert by_id[14].schedule_items == ()


def test_schedule_item_dates_days_and_as_dict() -> None:
    item = ScheduleItem.from_dict(
        {
            "start_time": 930,
            "end_time": 2215,
            "days": [5, 1],
            "start_date": "2026-12-01",
            "end_date": None,
        }
    )
    assert item.days == (1, 5)
    assert item.start_date == date(2026, 12, 1)
    assert item.as_dict() == {
        "start": "09:30",
        "end": "22:15",
        "days": [1, 5],
        "start_date": "2026-12-01",
        "end_date": None,
    }


def test_playlist_missing_description_is_empty() -> None:
    playlist = Playlist.from_dict({"id": 1, "name": "x", "is_enabled": True})
    assert playlist.description == ""
    assert playlist.schedule_items == ()


def test_queue_from_dict() -> None:
    items = queue()
    assert [i.sent_to_autodj for i in items] == [True, False]
    assert items[0].song is not None
    assert items[0].song.title == "Slow Tide"
    assert items[0].playlist == "tarde-chill"


@pytest.mark.parametrize(
    ("frontend", "expected"),
    [("icecast", True), ("rsas", True), ("shoutcast2", False), ("remote", False), (None, False)],
)
def test_station_soft_reload_support(frontend: str | None, expected: bool) -> None:
    raw = {"id": 1, "shortcode": "x", "name": "X"}
    if frontend is not None:
        raw["frontend"] = frontend
    assert Station.from_dict(raw).supports_soft_reload is expected
