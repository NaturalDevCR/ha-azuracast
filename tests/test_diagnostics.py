"""Tests for diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_reports(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    data = await async_get_config_entry_diagnostics(hass, init_integration)
    assert data["entry"]["data"]["api_key"] == "**REDACTED**"
    assert data["entry"]["data"]["url"] == "**REDACTED**"
    assert data["hub"] == {"sse_connected": False, "sse_ever_connected": False}
    by_id = {s["id"]: s for s in data["stations"]}
    assert by_id[1]["manage"] is True
    assert by_id[1]["playlists"] == 5
    assert by_id[1]["selectors"]["ambiente"] == {
        "options": ["chill", "fiesta", "lounge"],
        "state": "chill",
        "reason": None,
        "conflicts": [],
    }
    assert by_id[2]["manage"] is False
    assert "selectors" not in by_id[2]
