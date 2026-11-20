"""Tests for tag-based selectors."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, call
from zoneinfo import ZoneInfo

import pytest

from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.selectors import (
    REASON_MIXED,
    REASON_NONE_ENABLED,
    ApplyPlan,
    available_values,
    build_selectors,
    execute_plan,
    parse_tags,
    plan_apply,
    playlist_tags,
    selector_state,
)

from .common import playlists

CR = ZoneInfo("America/Costa_Rica")


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("", {}),
        (None, {}),
        ("Afternoon chill ha:ambiente=chill", {"ambiente": {"chill"}}),
        ("HA:Ambiente=Chill ha:zona=lobby", {"ambiente": {"chill"}, "zona": {"lobby"}}),
        ("ha:ambiente=chill ha:ambiente=lounge", {"ambiente": {"chill", "lounge"}}),
        ("xha:ambiente=chill", {}),
        ("ha:ambiente=chill,", {}),
        ("ha:ambiente=", {}),
        ("line1\nha:genre=jazz-latino\tmore", {"genre": {"jazz-latino"}}),
    ],
)
def test_parse_tags(description: str | None, expected: dict[str, set[str]]) -> None:
    assert parse_tags(description) == expected


def _pl(pid: int, description: str, enabled: bool = False) -> Playlist:
    return Playlist(
        id=pid,
        name=f"p{pid}",
        description=description,
        type="default",
        order="shuffle",
        weight=1,
        is_enabled=enabled,
        is_jingle=False,
        schedule_items=(),
    )


def test_playlist_tags_skips_ambiguous() -> None:
    assert playlist_tags(_pl(1, "ha:a=x ha:a=y ha:b=z")) == {"b": "z"}


def test_build_selectors_from_fixture() -> None:
    selectors = build_selectors(playlists())
    assert list(selectors) == ["ambiente"]
    ambiente = selectors["ambiente"]
    assert ambiente.options == ["chill", "fiesta", "lounge"]
    assert ambiente.playlist_names("chill") == {"tarde-chill", "noche-chill"}
    assert ambiente.conflicts == ()


def test_conflicting_playlist_is_excluded() -> None:
    selectors = build_selectors([_pl(1, "ha:a=x"), _pl(2, "ha:a=x ha:a=y")])
    assert selectors["a"].options == ["x"]
    assert [p.id for p in selectors["a"].conflicts] == [2]


def test_selector_with_only_conflicts_has_no_options() -> None:
    selectors = build_selectors([_pl(2, "ha:a=x ha:a=y")])
    assert selectors["a"].options == []


def test_state_single_value() -> None:
    state = selector_state(build_selectors(playlists())["ambiente"])
    assert (state.value, state.reason) == ("chill", None)


def test_state_none_and_mixed() -> None:
    none = build_selectors([_pl(1, "ha:a=x"), _pl(2, "ha:a=y")])["a"]
    assert selector_state(none).reason == REASON_NONE_ENABLED
    mixed = build_selectors([_pl(1, "ha:a=x", True), _pl(2, "ha:a=y", True)])["a"]
    assert (selector_state(mixed).value, selector_state(mixed).reason) == (None, REASON_MIXED)


def test_available_values_afternoon_and_night() -> None:
    ambiente = build_selectors(playlists())["ambiente"]
    assert available_values(ambiente, datetime(2026, 11, 20, 14, 0, tzinfo=CR)) == [
        "chill",
        "lounge",
    ]
    assert available_values(ambiente, datetime(2026, 11, 20, 21, 0, tzinfo=CR)) == [
        "chill",
        "fiesta",
    ]
    assert available_values(ambiente, datetime(2026, 11, 20, 8, 0, tzinfo=CR)) == []


def test_plan_apply_enables_then_disables_others() -> None:
    ambiente = build_selectors(playlists())["ambiente"]
    plan = plan_apply(ambiente, "lounge")
    assert plan == ApplyPlan(enable=(12,), disable=(10, 11))
    assert plan.is_empty is False


def test_plan_apply_current_value_is_empty() -> None:
    assert plan_apply(build_selectors(playlists())["ambiente"], "chill").is_empty is True


def test_plan_apply_unknown_value() -> None:
    with pytest.raises(ValueError):
        plan_apply(build_selectors(playlists())["ambiente"], "jazz")


def test_untagged_playlists_never_in_plan() -> None:
    ambiente = build_selectors(playlists())["ambiente"]
    plan = plan_apply(ambiente, "fiesta")
    assert 14 not in plan.enable + plan.disable


async def test_execute_plan_order_and_queue() -> None:
    client = AsyncMock()
    client.clear_queue.return_value = 2
    cleared = await execute_plan(
        client, 1, ApplyPlan(enable=(12,), disable=(10, 11)), clear_queue=True
    )
    assert cleared == 2
    assert client.mock_calls == [
        call.set_playlist_enabled(1, 12, True),
        call.set_playlist_enabled(1, 10, False),
        call.set_playlist_enabled(1, 11, False),
        call.clear_queue(1),
    ]


async def test_execute_plan_without_queue_clear() -> None:
    client = AsyncMock()
    assert (
        await execute_plan(client, 1, ApplyPlan(enable=(12,), disable=()), clear_queue=False) == 0
    )
    client.clear_queue.assert_not_called()


def test_disabled_values_do_not_count_for_state() -> None:
    items = [replace(p, is_enabled=False) for p in playlists()]
    assert selector_state(build_selectors(items)["ambiente"]).reason == REASON_NONE_ENABLED
