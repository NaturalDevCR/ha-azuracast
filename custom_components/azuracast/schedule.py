"""Evaluate AzuraCast playlist schedules."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api.models import Playlist, ScheduleItem

# AzuraCast treats start == end as "play once": it may play within 15 minutes of start.
PLAY_ONCE_MINUTES = 15


def _minutes(hhmm: int) -> int:
    return hhmm // 100 * 60 + hhmm % 100


def schedule_item_active(item: ScheduleItem, moment: datetime) -> bool:
    """Return whether a schedule window covers the moment (station local time)."""
    now = moment.hour * 100 + moment.minute
    start, end = item.start_time, item.end_time
    if start == end:
        since_start = (_minutes(now) - _minutes(start)) % (24 * 60)
        if since_start >= PLAY_ONCE_MINUTES:
            return False
        day = (moment - timedelta(minutes=since_start)).date()
    elif start < end:
        if not start <= now < end:
            return False
        day = moment.date()
    elif now >= start:
        day = moment.date()
    elif now < end:
        day = moment.date() - timedelta(days=1)
    else:
        return False
    if item.days and day.isoweekday() not in item.days:
        return False
    if item.start_date and day < item.start_date:
        return False
    return not (item.end_date and day > item.end_date)


def playlist_playable_now(playlist: Playlist, moment: datetime) -> bool:
    """Return whether the playlist could be picked now, ignoring is_enabled."""
    if not playlist.schedule_items:
        return True
    return any(schedule_item_active(item, moment) for item in playlist.schedule_items)


def station_time(timezone: str | None, utc_now: datetime, fallback: tzinfo) -> datetime:
    """Convert a UTC moment to the station's timezone."""
    zone: tzinfo = fallback
    if timezone:
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError, ValueError, OSError:
            zone = fallback
    return utc_now.astimezone(zone)
