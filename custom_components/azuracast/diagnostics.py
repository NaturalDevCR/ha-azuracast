"""Diagnostics for AzuraCast."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import HomeAssistant

from .runtime import AzuraCastConfigEntry, StationRuntime
from .selectors import selector_state

TO_REDACT = {CONF_API_KEY, CONF_URL}


def _station(runtime: StationRuntime) -> dict[str, Any]:
    np = runtime.nowplaying.data
    result: dict[str, Any] = {
        "id": runtime.station.id,
        "shortcode": runtime.station.shortcode,
        "name": runtime.station.name,
        "timezone": runtime.station.timezone,
        "manage": runtime.manage is not None,
        "can_broadcast": runtime.can_broadcast,
        "clear_queue_on_change": runtime.clear_queue_on_change,
        "change_mode": runtime.change_mode.value,
        "frontend": runtime.station.frontend,
        "reject_unavailable_values": runtime.reject_unavailable_values,
        "nowplaying_ok": runtime.nowplaying.last_update_success,
        "online": np.is_online if np else None,
        "off_air": runtime.nowplaying.off_air,
    }
    if runtime.manage is not None and runtime.manage.data is not None:
        data = runtime.manage.data
        result["playlists"] = len(data.playlists)
        result["queue"] = len(data.queue)
        result["selectors"] = {
            key: {
                "options": selector.options,
                "state": selector_state(selector).value,
                "reason": selector_state(selector).reason,
                "conflicts": [p.name for p in selector.conflicts],
            }
            for key, selector in data.selectors.items()
        }
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AzuraCastConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "hub": {
            "sse_connected": runtime.hub.sse_connected,
            "sse_ever_connected": runtime.hub.sse_ever_connected,
        },
        "stations": [_station(rt) for rt in runtime.stations.values()],
    }
