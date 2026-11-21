"""Constants for the AzuraCast integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "azuracast"
LOGGER = logging.getLogger(__package__)

CONF_EXCLUDED_STATIONS: Final = "excluded_stations"
CONF_KEEP_QUEUE_STATIONS: Final = "keep_queue_stations"
CONF_ALLOW_UNAVAILABLE_STATIONS: Final = "allow_unavailable_stations"
CONF_IMMEDIATE_CHANGE_STATIONS: Final = "immediate_change_stations"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 300

POLL_FALLBACK_INTERVAL: Final = timedelta(seconds=30)
# By default a change is heard after the current song and the one or two
# songs Liquidsoap already holds, which can take well over ten minutes.
PENDING_TIMEOUT: Final = timedelta(minutes=30)
# Liquidsoap already holds one or two songs when a selector changes; a few
# skips cover them without risking a skip loop.
MAX_STALE_SKIPS: Final = 3
SSE_MAX_BACKOFF: Final = 60.0

EVENT_SONG_CHANGED: Final = "azuracast_song_changed"
EVENT_SELECTOR_APPLIED: Final = "azuracast_selector_applied"
