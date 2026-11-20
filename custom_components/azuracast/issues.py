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


STATION_ISSUE_PREFIXES = ("missing_permissions", "conflicting_tags")


@callback
def async_delete_stale_station_issues(
    hass: HomeAssistant, entry: ConfigEntry, station_ids: set[int]
) -> None:
    """Delete this entry's per-station issues of stations that no longer exist.

    Only ids of the exact form ``{prefix}_{entry_id}_{station_id}`` count, so
    another entry's issues and a station id that merely starts with a kept one
    (12 vs 1) are told apart.
    """
    keep = {str(station_id) for station_id in station_ids}
    heads = [f"{prefix}_{entry.entry_id}_" for prefix in STATION_ISSUE_PREFIXES]
    stale = [
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if domain == DOMAIN
        for head in heads
        if issue_id.startswith(head)
        and (station := issue_id.removeprefix(head)).isascii()
        and station.isdigit()
        and station not in keep
    ]
    for issue_id in stale:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
