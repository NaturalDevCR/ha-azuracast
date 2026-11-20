"""Repairs issues raised by the AzuraCast integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api.models import Playlist, Station
from .const import DOMAIN

NOW_PLAYING_DOCS = "https://www.azuracast.com/docs/developers/now-playing-data/"


@callback
def set_sse_unavailable(hass: HomeAssistant, entry: ConfigEntry, active: bool) -> None:
    issue_id = f"sse_unavailable_{entry.entry_id}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="sse_unavailable",
        translation_placeholders={"instance": entry.title},
        learn_more_url=NOW_PLAYING_DOCS,
    )


@callback
def set_missing_permissions(
    hass: HomeAssistant, entry: ConfigEntry, station: Station, active: bool
) -> None:
    issue_id = f"missing_permissions_{entry.entry_id}_{station.id}"
    if not active:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="missing_permissions",
        translation_placeholders={"station": station.name, "instance": entry.title},
    )


@callback
def set_conflicting_tags(
    hass: HomeAssistant, entry: ConfigEntry, station: Station, playlists: list[Playlist]
) -> None:
    issue_id = f"conflicting_tags_{entry.entry_id}_{station.id}"
    if not playlists:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="conflicting_tags",
        translation_placeholders={
            "station": station.name,
            "playlists": ", ".join(sorted({p.name for p in playlists})),
        },
    )


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete every repair issue raised for a config entry."""
    suffix = f"_{entry.entry_id}"
    infix = f"{suffix}_"
    issue_ids = [
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN and (issue_id.endswith(suffix) or infix in issue_id)
    ]
    for issue_id in issue_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
