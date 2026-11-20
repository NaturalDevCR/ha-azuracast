"""Typed models for AzuraCast API payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, TypeGuard


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _opt_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return 0


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _has_song(data: Any) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(data, dict)
        and isinstance(data.get("song"), dict)
        and bool(data["song"].get("id"))
    )


@dataclass(frozen=True, slots=True)
class Station:
    """A station as listed by /api/stations and inside now-playing payloads."""

    id: int
    shortcode: str
    name: str
    timezone: str | None
    is_public: bool
    public_player_url: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Station:
        shortcode = str(data["shortcode"])
        return cls(
            id=int(data["id"]),
            shortcode=shortcode,
            name=_str(data.get("name")) or shortcode,
            timezone=_opt_str(data.get("timezone")),
            is_public=bool(data.get("is_public", True)),
            public_player_url=_opt_str(data.get("public_player_url")),
        )


@dataclass(frozen=True, slots=True)
class Song:
    """A song."""

    id: str
    text: str
    artist: str
    title: str
    album: str
    art: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Song:
        return cls(
            id=_str(data.get("id")),
            text=_str(data.get("text")),
            artist=_str(data.get("artist")),
            title=_str(data.get("title")),
            album=_str(data.get("album")),
            art=_opt_str(data.get("art")),
        )


@dataclass(frozen=True, slots=True)
class CurrentTrack:
    """The track on air."""

    song: Song
    playlist: str | None
    duration: int
    elapsed: int
    played_at: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CurrentTrack:
        return cls(
            song=Song.from_dict(data["song"]),
            playlist=_opt_str(data.get("playlist")),
            duration=_int(data.get("duration")),
            elapsed=_int(data.get("elapsed")),
            played_at=_int(data.get("played_at")),
        )


@dataclass(frozen=True, slots=True)
class UpcomingTrack:
    """The next track."""

    song: Song
    playlist: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpcomingTrack:
        return cls(song=Song.from_dict(data["song"]), playlist=_opt_str(data.get("playlist")))


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """Now-playing state of one station."""

    station: Station
    is_online: bool
    listeners_current: int
    listeners_unique: int
    listeners_total: int
    is_live: bool
    streamer_name: str | None
    current: CurrentTrack | None
    upcoming: UpcomingTrack | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NowPlaying:
        listeners = data.get("listeners") or {}
        live = data.get("live") or {}
        now = data.get("now_playing")
        nxt = data.get("playing_next")
        return cls(
            station=Station.from_dict(data["station"]),
            is_online=bool(data.get("is_online")),
            listeners_current=_int(listeners.get("current")),
            listeners_unique=_int(listeners.get("unique")),
            listeners_total=_int(listeners.get("total")),
            is_live=bool(live.get("is_live")),
            streamer_name=_opt_str(live.get("streamer_name")),
            current=CurrentTrack.from_dict(now) if _has_song(now) else None,
            upcoming=UpcomingTrack.from_dict(nxt) if _has_song(nxt) else None,
        )


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    """One schedule window of a playlist. Times are HHMM integers (930 = 09:30)."""

    start_time: int
    end_time: int
    days: tuple[int, ...]
    start_date: date | None
    end_date: date | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleItem:
        return cls(
            start_time=_int(data.get("start_time")),
            end_time=_int(data.get("end_time")),
            days=tuple(sorted(_int(day) for day in data.get("days") or [])),
            start_date=_date(data.get("start_date")),
            end_date=_date(data.get("end_date")),
        )

    def as_dict(self) -> dict[str, Any]:
        def hhmm(value: int) -> str:
            return f"{value // 100:02d}:{value % 100:02d}"

        return {
            "start": hhmm(self.start_time),
            "end": hhmm(self.end_time),
            "days": list(self.days),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
        }


@dataclass(frozen=True, slots=True)
class Playlist:
    """A station playlist."""

    id: int
    name: str
    description: str
    type: str
    order: str
    weight: int
    is_enabled: bool
    is_jingle: bool
    schedule_items: tuple[ScheduleItem, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Playlist:
        return cls(
            id=int(data["id"]),
            name=_str(data.get("name")),
            description=_str(data.get("description")),
            type=_str(data.get("type")) or "default",
            order=_str(data.get("order")),
            weight=_int(data.get("weight")),
            is_enabled=bool(data.get("is_enabled")),
            is_jingle=bool(data.get("is_jingle")),
            schedule_items=tuple(
                ScheduleItem.from_dict(item) for item in data.get("schedule_items") or []
            ),
        )


@dataclass(frozen=True, slots=True)
class QueueItem:
    """An upcoming AutoDJ queue entry."""

    id: int
    song: Song | None
    playlist: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueItem:
        song = data.get("song")
        return cls(
            id=int(data["id"]),
            song=Song.from_dict(song) if isinstance(song, dict) else None,
            playlist=_opt_str(data.get("playlist")),
        )
