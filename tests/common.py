"""Test helpers shared across test modules."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from custom_components.azuracast.api.models import NowPlaying, Playlist, QueueItem, Station

BASE_URL = "https://radio.example.com"
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


def stations() -> list[Station]:
    return [Station.from_dict(row) for row in load_fixture("stations.json")]


def nowplaying_raw(index: int) -> dict[str, Any]:
    return copy.deepcopy(load_fixture("nowplaying.json")[index])


def nowplaying_list() -> list[NowPlaying]:
    return [NowPlaying.from_dict(row) for row in load_fixture("nowplaying.json")]


def make_nowplaying(
    index: int = 0,
    *,
    song_id: str | None = None,
    title: str | None = None,
    playlist: str | None = None,
    is_online: bool | None = None,
) -> NowPlaying:
    """Build a NowPlaying from the fixture with a few fields replaced."""
    raw = nowplaying_raw(index)
    if song_id is not None:
        raw["now_playing"]["song"]["id"] = song_id
    if title is not None:
        raw["now_playing"]["song"]["title"] = title
        raw["now_playing"]["song"]["text"] = f"{raw['now_playing']['song']['artist']} - {title}"
    if playlist is not None:
        raw["now_playing"]["playlist"] = playlist
    if is_online is not None:
        raw["is_online"] = is_online
    return NowPlaying.from_dict(raw)


def playlists() -> list[Playlist]:
    return [Playlist.from_dict(row) for row in load_fixture("playlists_1.json")]


def queue() -> list[QueueItem]:
    return [QueueItem.from_dict(row) for row in load_fixture("queue_1.json")]
