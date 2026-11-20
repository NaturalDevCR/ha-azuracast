"""Tests for the dynamic entity helper."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.const import DOMAIN
from custom_components.azuracast.entity import track_dynamic_entities

PREFIX = "E_1_playlist_"


class FakeCoordinator:
    """Just enough of a coordinator: data plus listeners we can fire."""

    def __init__(self, data: set[str]) -> None:
        self.data = data
        self.listeners: list[Callable[[], None]] = []

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(update_callback)
        return lambda: self.listeners.remove(update_callback)

    def update(self, data: set[str]) -> None:
        self.data = data
        for listener in list(self.listeners):
            listener()


def _register(
    registry: er.EntityRegistry,
    entry: MockConfigEntry,
    unique_id: str,
    domain: str = "switch",
) -> str:
    return registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry).entity_id


def _entity(key: str) -> MagicMock:
    return MagicMock(key=key, unique_id=f"{PREFIX}{key}")


def _track(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    coordinator: FakeCoordinator,
    create: Callable[[str], MagicMock] = _entity,
) -> MagicMock:
    add_entities = MagicMock()
    track_dynamic_entities(
        hass,
        entry,  # type: ignore[arg-type]
        coordinator,  # type: ignore[arg-type]
        platform="switch",
        unique_id_prefix=PREFIX,
        keys=lambda data: set(data),
        create=create,
        add_entities=add_entities,
    )
    return add_entities


def _added_keys(add_entities: MagicMock) -> list[set[Any]]:
    return [{entity.key for entity in call.args[0]} for call in add_entities.call_args_list]


async def test_new_keys_add_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    coordinator = FakeCoordinator({"a", "b"})
    add_entities = _track(hass, mock_config_entry, coordinator)
    assert _added_keys(add_entities) == [{"a", "b"}]

    coordinator.update({"a", "b", "c"})
    assert _added_keys(add_entities) == [{"a", "b"}, {"c"}]

    coordinator.update({"a", "b", "c"})
    assert add_entities.call_count == 2


async def test_vanished_key_is_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    a_id = _register(registry, mock_config_entry, f"{PREFIX}a")
    b_id = _register(registry, mock_config_entry, f"{PREFIX}b")
    coordinator = FakeCoordinator({"a", "b"})
    add_entities = _track(hass, mock_config_entry, coordinator)

    coordinator.update({"a"})
    assert registry.async_get(a_id) is not None
    assert registry.async_get(b_id) is None

    coordinator.update({"a", "b"})
    assert _added_keys(add_entities) == [{"a", "b"}, {"b"}]


async def test_orphan_from_before_setup_is_removed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    kept = _register(registry, mock_config_entry, f"{PREFIX}a")
    orphan = _register(registry, mock_config_entry, f"{PREFIX}gone")
    _track(hass, mock_config_entry, FakeCoordinator({"a"}))
    assert registry.async_get(kept) is not None
    assert registry.async_get(orphan) is None


async def test_unrelated_entries_untouched(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(domain=DOMAIN, unique_id="other")
    other_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    untouched = [
        _register(registry, mock_config_entry, "E_11_playlist_gone"),
        _register(registry, mock_config_entry, "E_1_tag_gone"),
        _register(registry, mock_config_entry, f"{PREFIX}gone", domain="sensor"),
        _register(registry, other_entry, f"{PREFIX}gone"),
    ]
    _track(hass, mock_config_entry, FakeCoordinator({"a"}))
    for entity_id in untouched:
        assert registry.async_get(entity_id) is not None


async def test_unique_id_mismatch_raises(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    with pytest.raises(ValueError, match="E_1_playlist_a"):
        _track(
            hass,
            mock_config_entry,
            FakeCoordinator({"a"}),
            create=lambda key: MagicMock(key=key, unique_id=f"E_1_tag_{key}"),
        )
