"""Tag-based playlist selectors.

A playlist joins a selector through a token in its description:
``ha:<selector>=<value>``. Choosing a value enables that value's playlists and
disables the playlists of the selector's other values. Untagged playlists are
never touched.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re

from .api.client import AzuraCastClient
from .api.models import Playlist
from .schedule import playlist_playable_now

# Explicit ASCII classes without IGNORECASE: case-folding would let non-ASCII
# letters (the long s, the Kelvin sign, the dotted capital I) match a-z, and
# re.ASCII would stop \S from treating a no-break space as whitespace.
TAG_PATTERN = re.compile(r"(?<!\S)[Hh][Aa]:([A-Za-z0-9_]+)=([A-Za-z0-9_-]+)(?!\S)")
REASON_NONE_ENABLED = "none_enabled"
REASON_MIXED = "mixed"


def parse_tags(description: str | None) -> dict[str, set[str]]:
    """Return selector -> values found in a description."""
    tags: dict[str, set[str]] = {}
    for key, value in TAG_PATTERN.findall(description or ""):
        tags.setdefault(key.lower(), set()).add(value.lower())
    return tags


def playlist_tags(playlist: Playlist) -> dict[str, str]:
    """Return the playlist's unambiguous selector values."""
    return {
        key: next(iter(values))
        for key, values in parse_tags(playlist.description).items()
        if len(values) == 1
    }


@dataclass(frozen=True, slots=True)
class Selector:
    """All playlists of one station that share a selector key."""

    key: str
    members: Mapping[str, tuple[Playlist, ...]]
    conflicts: tuple[Playlist, ...] = ()

    @property
    def options(self) -> list[str]:
        return sorted(self.members)

    def playlist_names(self, value: str) -> set[str]:
        return {playlist.name for playlist in self.members.get(value, ())}


def build_selectors(playlists: Iterable[Playlist]) -> dict[str, Selector]:
    """Group playlists by selector key and value."""
    members: dict[str, dict[str, list[Playlist]]] = {}
    conflicts: dict[str, list[Playlist]] = {}
    for playlist in playlists:
        for key, values in parse_tags(playlist.description).items():
            if len(values) > 1:
                conflicts.setdefault(key, []).append(playlist)
                continue
            (value,) = values
            members.setdefault(key, {}).setdefault(value, []).append(playlist)
    return {
        key: Selector(
            key=key,
            members={value: tuple(items) for value, items in members.get(key, {}).items()},
            conflicts=tuple(conflicts.get(key, ())),
        )
        for key in sorted(set(members) | set(conflicts))
    }


@dataclass(frozen=True, slots=True)
class SelectorState:
    """Derived state of a selector."""

    value: str | None
    reason: str | None


def selector_state(selector: Selector) -> SelectorState:
    """Return the single enabled value, or why there is none."""
    enabled = sorted(
        value
        for value, items in selector.members.items()
        if any(playlist.is_enabled for playlist in items)
    )
    if len(enabled) == 1:
        return SelectorState(enabled[0], None)
    if not enabled:
        return SelectorState(None, REASON_NONE_ENABLED)
    return SelectorState(None, REASON_MIXED)


def available_values(selector: Selector, moment: datetime) -> list[str]:
    """Return values with at least one playlist that can play at this moment."""
    return [
        value
        for value in selector.options
        if any(playlist_playable_now(playlist, moment) for playlist in selector.members[value])
    ]


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    """Playlist changes needed to apply a value."""

    enable: tuple[int, ...]
    disable: tuple[int, ...]

    @property
    def is_empty(self) -> bool:
        return not self.enable and not self.disable


def plan_apply(selector: Selector, value: str) -> ApplyPlan:
    """Compute the playlist changes for choosing a value."""
    if value not in selector.members:
        raise ValueError(f"Unknown value {value!r} for selector {selector.key!r}")
    enable = tuple(p.id for p in selector.members[value] if not p.is_enabled)
    disable = tuple(
        p.id
        for other, items in selector.members.items()
        if other != value
        for p in items
        if p.is_enabled
    )
    return ApplyPlan(enable=enable, disable=disable)


async def execute_plan(
    client: AzuraCastClient, station_id: int, plan: ApplyPlan, *, clear_queue: bool
) -> int:
    """Enable first, then disable, then optionally clear the queue."""
    for playlist_id in plan.enable:
        await client.set_playlist_enabled(station_id, playlist_id, True)
    for playlist_id in plan.disable:
        await client.set_playlist_enabled(station_id, playlist_id, False)
    if not clear_queue:
        return 0
    return await client.clear_queue(station_id)
