# AzuraCast Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `azuracast`, a generic Home Assistant custom integration that turns every station of an AzuraCast instance into a device. Each device gets now-playing sensors, a media player, playlist switches, queue and skip controls, services, events, and tag-based playlist selectors.

**Architecture:** The integration has three layers:

- **HTTP client** (`api/`, no Home Assistant imports): REST calls plus a Centrifugo SSE stream.
- **Logic** (`schedule.py`, `selectors.py`): pure functions for schedule evaluation and tag selectors.
- **Home Assistant layer**: one now-playing hub per instance (SSE with polling fallback) feeds a push-only coordinator per station. Stations that can be managed also get a polling management coordinator, and entities are built on both.

AzuraCast is the only source of truth. Home Assistant persists no state of its own about playlists or selectors.

**Tech Stack:**

- Python 3.14
- Home Assistant 2026.10 (`probatio` validation, `DataUpdateCoordinator`, `ConfigEntry.runtime_data`)
- aiohttp
- pytest-homeassistant-custom-component 0.13.375, ruff, mypy

**Spec:** `docs/superpowers/specs/2026-11-20-azuracast-integration-design.md`

## Global Constraints

- Minimum Home Assistant `2026.10.0`. Python `3.14`. Test pin: `pytest-homeassistant-custom-component==0.13.375`.
- Integration domain: `azuracast`. Repository: `NaturalDevCR/ha-azuracast`. Codeowner: `@NaturalDevCR`.
- Validation library: `import probatio` (HA 2026.10 core convention; it has the same API as voluptuous). `manifest.json` `requirements` stays `[]`.
- Nothing under `custom_components/azuracast/api/` may import `homeassistant`.
- AzuraCast authentication header: `X-API-Key: <key>`. Every API path is under `<base_url>/api`.
- Playlists are enabled or disabled only with an explicit `PUT /api/station/{id}/playlist/{pid}` body of `{"is_enabled": <bool>}`. Never call the toggle endpoint.
- Tag grammar: whitespace-delimited token `ha:<selector>=<value>`, where `<selector>` matches `[a-z0-9_]+` and `<value>` matches `[a-z0-9_-]+`. Matching is case-insensitive and both parts are lowercased.
- Unique IDs: `{entry_id}_{station_id}_{key}`. Station device identifier: `(azuracast, "{entry_id}_{station_id}")`. Instance device identifier: `(azuracast, entry_id)`.
- Event names: `azuracast_song_changed` and `azuracast_selector_applied`.
- Per-station behavior options are stored as inverse lists (`keep_queue_stations`, `allow_unavailable_stations`; both default `[]`). A station that is not listed gets the spec defaults: `clear_queue_on_change = true` and `reject_unavailable_values = true`. This way, stations added later get the defaults automatically.
- Every user-facing string goes through translations (`strings.json`, `translations/en.json`, `translations/es.json`).
- Run every command from the repository root (the folder that contains `custom_components/`).

## File Map

```
custom_components/azuracast/
  __init__.py        async_setup (services), async_setup_entry/unload, station discovery, stale devices
  manifest.json
  const.py           domain, option keys, intervals, event names, LOGGER
  runtime.py         StationRuntime, AzuraCastRuntime, AzuraCastConfigEntry, station_device_key
  api/__init__.py    package marker
  api/errors.py      exception hierarchy + error_for_status
  api/models.py      frozen dataclasses parsed from API JSON
  api/client.py      normalize_url + AzuraCastClient (REST)
  api/sse.py         SSE URL, line parser, Centrifugo message parser, stream_nowplaying
  schedule.py        schedule_item_active, playlist_playable_now, station_time
  selectors.py       tag parsing, Selector, selector_state, available_values, plan_apply, execute_plan
  issues.py          Repairs issues (sse_unavailable, missing_permissions, conflicting_tags)
  coordinator.py     ManageData, ManageCoordinator, NowPlayingCoordinator, tags_for_playlist
  hub.py             NowPlayingHub (SSE + polling fallback)
  entity.py          AzuraCastEntity, api_errors, track_dynamic_entities
  media_player.py sensor.py binary_sensor.py switch.py button.py select.py
  services.py services.yaml
  diagnostics.py
  config_flow.py
  strings.json translations/en.json translations/es.json icons.json quality_scale.yaml
tests/
  common.py conftest.py fixtures/*.json
  test_models.py test_client.py test_sse.py test_schedule.py test_selectors.py
  test_coordinator.py test_hub.py test_init.py test_config_flow.py
  test_sensor_platforms.py test_switch_button.py test_select.py test_services.py test_diagnostics.py
tools/probe.py       phase 0 probe against a real instance
docs/validation.md   phase 0 findings
```

This layout differs from section 14 of the spec in three ways:

- `issues.py` replaces `repairs.py`. Home Assistant reserves `repairs.py` for fix flows, and every issue here is informational.
- `schedule.py` is split out of `selectors.py`, so that time logic is tested on its own.
- `runtime.py` holds the runtime dataclasses, which avoids an import cycle between `__init__.py`, the platforms and the services.

---

### Task 1: Phase 0: probe a real AzuraCast instance

**Human in the loop.** This task needs the user's AzuraCast URL, an API key and a test station, so ask the user to run it or to provide the environment variables. The other tasks use hand-written fixtures and can go ahead in parallel. Task 17 reconciles the code with these findings.

**Files:**
- Create: `tools/probe.py`
- Create: `docs/validation.md`
- Create: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `docs/validation.md` with answers that later tasks may act on, and raw responses in `probe-output/`. That folder is gitignored and never committed.

- [ ] **Step 1: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
probe-output/
.env
```

- [ ] **Step 2: Write `tools/probe.py`**

```python
"""Phase 0 probe: record how a real AzuraCast instance behaves.

Environment:
  AZURACAST_URL                base URL, e.g. https://radio.example.com
  AZURACAST_API_KEY            API key of a user with rights on the test station
  AZURACAST_STATION_ID         numeric id of a TEST station
  AZURACAST_PROBE_PLAYLIST_ID  optional: playlist on that station to flip and restore
  AZURACAST_VERIFY_SSL         optional, "0" to skip certificate checks

Writes raw responses to ./probe-output/ (gitignored) and prints a summary.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import quote

import aiohttp

OUT = Path("probe-output")


def env(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if required and not value:
        sys.exit(f"Missing environment variable {name}")
    return value


def save(name: str, data: Any) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))


async def call(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any = None,
) -> tuple[int, Any]:
    async with session.request(method, url, headers=headers, json=body) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text) if text else None
        except ValueError:
            return resp.status, text


async def main() -> None:
    base = str(env("AZURACAST_URL")).rstrip("/")
    key = str(env("AZURACAST_API_KEY"))
    station_id = int(str(env("AZURACAST_STATION_ID")))
    probe_playlist = env("AZURACAST_PROBE_PLAYLIST_ID", required=False)
    verify = os.environ.get("AZURACAST_VERIFY_SSL", "1") != "0"
    headers = {"Accept": "application/json", "X-API-Key": key}
    api = f"{base}/api"
    summary: list[str] = []

    connector = aiohttp.TCPConnector(ssl=None if verify else False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for name, path in (
            ("status", "/status"),
            ("account_me", "/frontend/account/me"),
            ("stations", "/stations"),
            ("nowplaying", "/nowplaying"),
            ("playlists", f"/station/{station_id}/playlists"),
            ("queue", f"/station/{station_id}/queue"),
        ):
            status, data = await call(session, "GET", api + path, headers)
            save(f"{name}.json", {"status": status, "body": data})
            summary.append(f"- GET {path}: HTTP {status}")

        _, stations = await call(session, "GET", f"{api}/stations", headers)
        station = next(s for s in stations if s["id"] == station_id)
        summary.append(f"- station has 'timezone': {'timezone' in station} ({station.get('timezone')})")
        _, playlists = await call(session, "GET", f"{api}/station/{station_id}/playlists", headers)
        if playlists:
            summary.append(f"- playlist keys: {sorted(playlists[0])}")
            scheduled = [p for p in playlists if p.get("schedule_items")]
            if scheduled:
                summary.append(f"- schedule_items sample: {scheduled[0]['schedule_items'][0]}")

        if probe_playlist:
            url = f"{api}/station/{station_id}/playlist/{probe_playlist}"
            _, before = await call(session, "GET", url, headers)
            original = bool(before["is_enabled"])
            put_status, put_body = await call(session, "PUT", url, headers, {"is_enabled": not original})
            _, after = await call(session, "GET", url, headers)
            changed = {k for k in before if k not in ("is_enabled", "links") and before[k] != after.get(k)}
            await call(session, "PUT", url, headers, {"is_enabled": original})
            _, restored = await call(session, "GET", url, headers)
            save("partial_put.json", {"put_status": put_status, "put_body": put_body, "before": before, "after": after})
            summary.append(
                f"- partial PUT: HTTP {put_status}, flipped={after.get('is_enabled') != original}, "
                f"other fields changed={sorted(changed) or 'none'}, restored={restored['is_enabled'] == original}"
            )

        connect = json.dumps({"subs": {f"station:{station['shortcode']}": {"recover": True}}}, separators=(",", ":"))
        sse_url = f"{api}/live/nowplaying/sse?cf_connect={quote(connect, safe='')}"
        lines: list[str] = []
        try:
            timeout = aiohttp.ClientTimeout(total=25, sock_read=25)
            async with session.get(sse_url, headers={"Accept": "text/event-stream"}, timeout=timeout) as resp:
                summary.append(f"- SSE: HTTP {resp.status}")
                deadline = time.monotonic() + 20
                async for raw in resp.content:
                    lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
                    if time.monotonic() > deadline:
                        break
        except (aiohttp.ClientError, TimeoutError) as err:
            summary.append(f"- SSE error: {err!r}")
        (OUT / "sse_raw.txt").write_text("\n".join(lines))
        data_lines = [line for line in lines if line.startswith("data:")]
        summary.append(f"- SSE data lines in 20 s: {len(data_lines)}")
        if data_lines:
            first = json.loads(data_lines[0][5:].strip())
            summary.append(f"- SSE first message keys: {sorted(first)}")

    print("\n".join(summary))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Write `docs/validation.md`**

````markdown
# Phase 0 validation

Instance version: (from AzuraCast footer)
Date:

Run:

```bash
AZURACAST_URL=... AZURACAST_API_KEY=... AZURACAST_STATION_ID=... AZURACAST_PROBE_PLAYLIST_ID=... python tools/probe.py
```

Paste the printed summary here:

```
```

## Answers

1. Partial `PUT {"is_enabled": x}` works without other fields changing: yes / no
2. SSE URL and message shape match `api/sse.py` (`connect.subs[*].publications[*].data.np` and `pub.data.np`): yes / no
3. `GET /api/frontend/account/me` validates the key (200 with a key, 401 with a bad one): yes / no
4. `schedule_items` fields are `start_time`, `end_time` (HHMM ints), `days` (ISO weekdays), `start_date`, `end_date`: yes / no
5. Two enabled scheduled playlists that overlap in time: mix by weight / one wins (which?)
6. After clearing the queue, the next track comes from the newly enabled set: yes / no. How many seconds before a track ends does the next one get fetched?
7. `POST /api/station/{id}/backend/skip` skips with crossfade: yes / no
8. Station admin page URL pattern (for the device's configuration_url): `/station/{id}/profile` / other
9. Stations list includes `timezone`: yes / no
````

- [ ] **Step 4: Run the probe with the user's credentials and fill `docs/validation.md`**

Ask the user to listen to a Snapcast zone while answering 5–7: they need ears. Run:

```bash
python3.14 -m venv .venv && .venv/bin/pip install aiohttp
AZURACAST_URL=... AZURACAST_API_KEY=... AZURACAST_STATION_ID=... AZURACAST_PROBE_PLAYLIST_ID=... .venv/bin/python tools/probe.py
```

Expected: a summary with one line per check. Paste it into `docs/validation.md`, answer items 1–9, and **do not** commit `probe-output/`.

- [ ] **Step 5: Record contingencies (applied in Task 17)**

The fixes for findings that differ from the assumptions are listed in Task 17. Nothing to change now.

- [ ] **Step 6: Commit**

```bash
git add .gitignore tools/probe.py docs/validation.md
git commit -m "chore: add phase 0 probe for a real AzuraCast instance"
```

---

### Task 2: Project scaffold and API models

**Files:**
- Create: `pyproject.toml`, `requirements_test.txt`, `hacs.json`
- Create: `custom_components/azuracast/__init__.py` (temporary stub), `custom_components/azuracast/manifest.json`, `custom_components/azuracast/const.py`
- Create: `custom_components/azuracast/api/__init__.py`, `custom_components/azuracast/api/models.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/common.py`
- Create: `tests/fixtures/stations.json`, `tests/fixtures/nowplaying.json`, `tests/fixtures/playlists_1.json`, `tests/fixtures/queue_1.json`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces (`api/models.py`):
  - `Station(id: int, shortcode: str, name: str, timezone: str | None, is_public: bool, public_player_url: str | None)`
  - `Song(id: str, text: str, artist: str, title: str, album: str, art: str | None)`
  - `CurrentTrack(song: Song, playlist: str | None, duration: int, elapsed: int, played_at: int)`
  - `UpcomingTrack(song: Song, playlist: str | None)`
  - `NowPlaying(station, is_online, listeners_current, listeners_unique, listeners_total, is_live, streamer_name, current: CurrentTrack | None, upcoming: UpcomingTrack | None)`
  - `ScheduleItem(start_time: int, end_time: int, days: tuple[int, ...], start_date: date | None, end_date: date | None)` with `.as_dict()`
  - `Playlist(id, name, description, type, order, weight, is_enabled, is_jingle, schedule_items: tuple[ScheduleItem, ...])`
  - `QueueItem(id: int, song: Song | None, playlist: str | None)`
  - Every model has `classmethod from_dict(data: dict[str, Any])`.
- Produces (`const.py`): `DOMAIN`, `LOGGER`, `CONF_EXCLUDED_STATIONS`, `CONF_KEEP_QUEUE_STATIONS`, `CONF_ALLOW_UNAVAILABLE_STATIONS`, `DEFAULT_SCAN_INTERVAL`, `MIN_SCAN_INTERVAL`, `MAX_SCAN_INTERVAL`, `POLL_FALLBACK_INTERVAL`, `PENDING_TIMEOUT`, `SSE_MAX_BACKOFF`, `EVENT_SONG_CHANGED`, `EVENT_SELECTOR_APPLIED`.
- Produces (`tests/common.py`): `BASE_URL`, `load_fixture(name)`, `stations()`, `nowplaying_list()`, `nowplaying_raw(index)`, `make_nowplaying(index=0, *, song_id=None, title=None, playlist=None, is_online=None)`, `playlists()`, `queue()`.

- [ ] **Step 1: Create the project files**

`pyproject.toml`:

```toml
[project]
name = "ha-azuracast"
version = "0.1.0"
requires-python = ">=3.14"

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
filterwarnings = ["ignore::DeprecationWarning"]

[tool.ruff]
target-version = "py314"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ASYNC", "RUF"]

[tool.ruff.lint.isort]
force-sort-within-sections = true
known-first-party = ["custom_components.azuracast"]

[tool.mypy]
python_version = "3.14"
disallow_untyped_defs = true
check_untyped_defs = true
ignore_missing_imports = true
follow_imports = "silent"
warn_unused_ignores = true
```

`requirements_test.txt`:

```
pytest-homeassistant-custom-component==0.13.375
ruff
mypy
```

`hacs.json`:

```json
{
  "name": "AzuraCast",
  "homeassistant": "2026.10.0",
  "render_readme": true
}
```

`custom_components/azuracast/manifest.json`:

```json
{
  "domain": "azuracast",
  "name": "AzuraCast",
  "codeowners": ["@NaturalDevCR"],
  "config_flow": true,
  "documentation": "https://github.com/NaturalDevCR/ha-azuracast",
  "integration_type": "hub",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/NaturalDevCR/ha-azuracast/issues",
  "loggers": ["custom_components.azuracast"],
  "requirements": [],
  "version": "0.1.0"
}
```

`custom_components/azuracast/__init__.py` (stub, replaced in Task 9):

```python
"""The AzuraCast integration."""
```

`custom_components/azuracast/api/__init__.py`:

```python
"""AzuraCast API client (no Home Assistant imports)."""
```

`custom_components/azuracast/const.py`:

```python
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

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 300

POLL_FALLBACK_INTERVAL: Final = timedelta(seconds=30)
PENDING_TIMEOUT: Final = timedelta(minutes=15)
SSE_MAX_BACKOFF: Final = 60.0

EVENT_SONG_CHANGED: Final = "azuracast_song_changed"
EVENT_SELECTOR_APPLIED: Final = "azuracast_selector_applied"
```

`tests/__init__.py`:

```python
"""Tests for the AzuraCast integration."""
```

`tests/conftest.py` (extended in Task 9):

```python
"""Shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom integrations in every test."""
```

- [ ] **Step 2: Create the fixtures**

`tests/fixtures/stations.json`:

```json
[
  {
    "id": 1,
    "name": "Ambientes",
    "shortcode": "ambientes",
    "description": "",
    "frontend": "icecast",
    "backend": "liquidsoap",
    "timezone": "America/Costa_Rica",
    "listen_url": "https://radio.example.com/listen/ambientes/radio.mp3",
    "url": "",
    "public_player_url": "https://radio.example.com/public/ambientes",
    "is_public": true
  },
  {
    "id": 2,
    "name": "Party Time",
    "shortcode": "party_time",
    "description": "",
    "frontend": "icecast",
    "backend": "liquidsoap",
    "timezone": "America/Costa_Rica",
    "listen_url": "https://radio.example.com/listen/party_time/radio.mp3",
    "url": "",
    "public_player_url": "https://radio.example.com/public/party_time",
    "is_public": true
  }
]
```

`tests/fixtures/nowplaying.json`:

```json
[
  {
    "station": {
      "id": 1,
      "name": "Ambientes",
      "shortcode": "ambientes",
      "timezone": "America/Costa_Rica",
      "public_player_url": "https://radio.example.com/public/ambientes",
      "is_public": true
    },
    "listeners": {"total": 3, "unique": 3, "current": 3},
    "live": {"is_live": false, "streamer_name": "", "broadcast_start": null, "art": null},
    "now_playing": {
      "sh_id": 9001,
      "played_at": 1795204800,
      "duration": 240,
      "playlist": "tarde-chill",
      "streamer": "",
      "is_request": false,
      "song": {
        "id": "a1",
        "text": "Trio Mar - Bossa Breeze",
        "artist": "Trio Mar",
        "title": "Bossa Breeze",
        "album": "Costa",
        "genre": "",
        "isrc": "",
        "lyrics": "",
        "art": "https://radio.example.com/api/station/ambientes/art/a1.jpg",
        "custom_fields": []
      },
      "elapsed": 60,
      "remaining": 180
    },
    "playing_next": {
      "cued_at": 1795205030,
      "played_at": 1795205040,
      "duration": 200,
      "playlist": "tarde-chill",
      "is_request": false,
      "song": {
        "id": "a2",
        "text": "Trio Mar - Slow Tide",
        "artist": "Trio Mar",
        "title": "Slow Tide",
        "album": "Costa",
        "art": null
      }
    },
    "song_history": [],
    "is_online": true,
    "cache": null
  },
  {
    "station": {
      "id": 2,
      "name": "Party Time",
      "shortcode": "party_time",
      "timezone": "America/Costa_Rica",
      "public_player_url": "https://radio.example.com/public/party_time",
      "is_public": true
    },
    "listeners": {"total": 10, "unique": 8, "current": 8},
    "live": {"is_live": true, "streamer_name": "DJ Sol", "broadcast_start": 1795204000, "art": null},
    "now_playing": {
      "sh_id": 9100,
      "played_at": 1795204700,
      "duration": 300,
      "playlist": "party-mix",
      "streamer": "DJ Sol",
      "is_request": false,
      "song": {
        "id": "p1",
        "text": "Banda X - Fiesta",
        "artist": "Banda X",
        "title": "Fiesta",
        "album": "",
        "art": null
      },
      "elapsed": 100,
      "remaining": 200
    },
    "playing_next": null,
    "song_history": [],
    "is_online": true,
    "cache": null
  }
]
```

`tests/fixtures/playlists_1.json`:

```json
[
  {
    "id": 10, "name": "tarde-chill", "description": "Afternoon chill ha:ambiente=chill",
    "type": "default", "source": "songs", "order": "shuffle", "weight": 3,
    "is_enabled": true, "is_jingle": false,
    "schedule_items": [{"id": 1, "start_time": 1200, "end_time": 1800, "start_date": null, "end_date": null, "days": [], "loop_once": false}]
  },
  {
    "id": 11, "name": "noche-chill", "description": "ha:ambiente=chill",
    "type": "default", "source": "songs", "order": "shuffle", "weight": 3,
    "is_enabled": true, "is_jingle": false,
    "schedule_items": [{"id": 2, "start_time": 1800, "end_time": 200, "start_date": null, "end_date": null, "days": [], "loop_once": false}]
  },
  {
    "id": 12, "name": "tarde-lounge", "description": "ha:ambiente=lounge",
    "type": "default", "source": "songs", "order": "shuffle", "weight": 3,
    "is_enabled": false, "is_jingle": false,
    "schedule_items": [{"id": 3, "start_time": 1200, "end_time": 1800, "start_date": null, "end_date": null, "days": [], "loop_once": false}]
  },
  {
    "id": 13, "name": "noche-fiesta", "description": "ha:ambiente=fiesta",
    "type": "default", "source": "songs", "order": "random", "weight": 3,
    "is_enabled": false, "is_jingle": false,
    "schedule_items": [{"id": 4, "start_time": 1800, "end_time": 200, "start_date": null, "end_date": null, "days": [], "loop_once": false}]
  },
  {
    "id": 14, "name": "respaldo", "description": "Fallback rotation",
    "type": "default", "source": "songs", "order": "shuffle", "weight": 1,
    "is_enabled": true, "is_jingle": false,
    "schedule_items": []
  }
]
```

`tests/fixtures/queue_1.json`:

```json
[
  {
    "id": 501, "cued_at": 1795205030, "played_at": 1795205040, "duration": 200,
    "playlist": "tarde-chill", "is_request": false, "sent_to_autodj": true, "is_played": false,
    "song": {"id": "a2", "text": "Trio Mar - Slow Tide", "artist": "Trio Mar", "title": "Slow Tide", "album": "Costa", "art": null}
  },
  {
    "id": 502, "cued_at": 1795205030, "played_at": 1795205240, "duration": 210,
    "playlist": "tarde-chill", "is_request": false, "sent_to_autodj": false, "is_played": false,
    "song": {"id": "a3", "text": "Trio Mar - Sand", "artist": "Trio Mar", "title": "Sand", "album": "Costa", "art": null}
  }
]
```

`tests/common.py`:

```python
"""Test helpers shared across test modules."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from custom_components.azuracast.api.models import NowPlaying, Playlist, QueueItem, Station

BASE_URL = "https://radio.example.com"
FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


def stations() -> list[Station]:
    return [Station.from_dict(row) for row in load_fixture("stations.json")]


def nowplaying_raw(index: int) -> dict[str, Any]:
    return copy.deepcopy(load_fixture("nowplaying.json")[index])


def nowplaying_list() -> list[NowPlaying]:
    return [NowPlaying.from_dict(row) for row in load_fixture("nowplaying.json")]


def make_nowplaying(
    index: int = 0,
    *,
    song_id: str | None = None,
    title: str | None = None,
    playlist: str | None = None,
    is_online: bool | None = None,
) -> NowPlaying:
    """Build a NowPlaying from the fixture with a few fields replaced."""
    raw = nowplaying_raw(index)
    if song_id is not None:
        raw["now_playing"]["song"]["id"] = song_id
    if title is not None:
        raw["now_playing"]["song"]["title"] = title
        raw["now_playing"]["song"]["text"] = f"{raw['now_playing']['song']['artist']} - {title}"
    if playlist is not None:
        raw["now_playing"]["playlist"] = playlist
    if is_online is not None:
        raw["is_online"] = is_online
    return NowPlaying.from_dict(raw)


def playlists() -> list[Playlist]:
    return [Playlist.from_dict(row) for row in load_fixture("playlists_1.json")]


def queue() -> list[QueueItem]:
    return [QueueItem.from_dict(row) for row in load_fixture("queue_1.json")]
```

- [ ] **Step 3: Write the failing tests `tests/test_models.py`**

```python
"""Tests for API models."""

from __future__ import annotations

from datetime import date

from custom_components.azuracast.api.models import NowPlaying, Playlist, ScheduleItem

from .common import load_fixture, nowplaying_raw, playlists, queue, stations


def test_station_from_dict() -> None:
    first, second = stations()
    assert first.id == 1
    assert first.shortcode == "ambientes"
    assert first.name == "Ambientes"
    assert first.timezone == "America/Costa_Rica"
    assert first.is_public is True
    assert second.shortcode == "party_time"


def test_nowplaying_from_dict() -> None:
    np = NowPlaying.from_dict(load_fixture("nowplaying.json")[0])
    assert np.station.shortcode == "ambientes"
    assert np.is_online is True
    assert (np.listeners_current, np.listeners_unique, np.listeners_total) == (3, 3, 3)
    assert np.is_live is False
    assert np.streamer_name is None
    assert np.current is not None
    assert np.current.song.id == "a1"
    assert np.current.song.title == "Bossa Breeze"
    assert np.current.playlist == "tarde-chill"
    assert (np.current.duration, np.current.elapsed) == (240, 60)
    assert np.upcoming is not None
    assert np.upcoming.song.title == "Slow Tide"


def test_nowplaying_live_and_no_next() -> None:
    np = NowPlaying.from_dict(load_fixture("nowplaying.json")[1])
    assert np.is_live is True
    assert np.streamer_name == "DJ Sol"
    assert np.upcoming is None


def test_nowplaying_offline_without_song() -> None:
    raw = nowplaying_raw(0)
    raw["is_online"] = False
    raw["now_playing"] = None
    np = NowPlaying.from_dict(raw)
    assert np.is_online is False
    assert np.current is None


def test_nowplaying_empty_song_id_means_no_current() -> None:
    raw = nowplaying_raw(0)
    raw["now_playing"]["song"]["id"] = ""
    assert NowPlaying.from_dict(raw).current is None


def test_playlist_from_dict() -> None:
    by_id = {p.id: p for p in playlists()}
    chill = by_id[10]
    assert chill.name == "tarde-chill"
    assert chill.description == "Afternoon chill ha:ambiente=chill"
    assert chill.is_enabled is True
    assert chill.schedule_items == (
        ScheduleItem(start_time=1200, end_time=1800, days=(), start_date=None, end_date=None),
    )
    assert by_id[14].schedule_items == ()


def test_schedule_item_dates_days_and_as_dict() -> None:
    item = ScheduleItem.from_dict(
        {"start_time": 930, "end_time": 2215, "days": [5, 1], "start_date": "2026-12-01", "end_date": None}
    )
    assert item.days == (1, 5)
    assert item.start_date == date(2026, 12, 1)
    assert item.as_dict() == {
        "start": "09:30",
        "end": "22:15",
        "days": [1, 5],
        "start_date": "2026-12-01",
        "end_date": None,
    }


def test_playlist_missing_description_is_empty() -> None:
    playlist = Playlist.from_dict({"id": 1, "name": "x", "is_enabled": True})
    assert playlist.description == ""
    assert playlist.schedule_items == ()


def test_queue_from_dict() -> None:
    items = queue()
    assert [i.id for i in items] == [501, 502]
    assert items[0].song is not None
    assert items[0].song.title == "Slow Tide"
    assert items[0].playlist == "tarde-chill"
```

- [ ] **Step 4: Run them to verify they fail**

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest tests/test_models.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'custom_components.azuracast.api.models'`.

- [ ] **Step 5: Implement `custom_components/azuracast/api/models.py`**

```python
"""Typed models for AzuraCast API payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


def _str(value: Any) -> str:
    return "" if value is None else str(value)


def _opt_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _has_song(data: Any) -> bool:
    return isinstance(data, dict) and isinstance(data.get("song"), dict) and bool(data["song"].get("id"))


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
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_models.py -q
```

Expected: `9 passed`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements_test.txt hacs.json custom_components tests
git commit -m "feat: scaffold the integration and parse AzuraCast API models"
```

---

### Task 3: REST client and errors

**Files:**
- Create: `custom_components/azuracast/api/errors.py`, `custom_components/azuracast/api/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: the models from Task 2.
- Produces (`api/errors.py`): `AzuraCastError`, `AzuraCastConnectionError`, `AzuraCastResponseError(status: int, message: str)` (attributes `.status`, `.message`), `AzuraCastAuthError` (401), `AzuraCastPermissionError` (403), `AzuraCastNotFoundError` (404), and `error_for_status(status: int, message: str) -> AzuraCastResponseError`.
- Produces (`api/client.py`):
  - `normalize_url(url: str) -> str`. Raises `ValueError`.
  - `AzuraCastClient(session: aiohttp.ClientSession, base_url: str, api_key: str | None = None, *, request_timeout: float = 10.0)`.
  - Properties: `base_url: str`, `has_api_key: bool`, `session: aiohttp.ClientSession`.
  - Async methods: `get_status() -> bool`, `get_account() -> dict[str, Any]`, `get_stations() -> list[Station]`, `get_nowplaying() -> list[NowPlaying]`, `get_playlists(station_id: int) -> list[Playlist]`, `set_playlist_enabled(station_id: int, playlist_id: int, enabled: bool) -> None`, `reshuffle_playlist(station_id: int, playlist_id: int) -> None`, `get_queue(station_id: int) -> list[QueueItem]`, `delete_queue_item(station_id: int, item_id: int) -> None`, `clear_queue(station_id: int) -> int`, `skip_song(station_id: int) -> None`.

- [ ] **Step 1: Write the failing tests `tests/test_client.py`**

```python
"""Tests for the AzuraCast REST client."""

from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.azuracast.api.client import AzuraCastClient, normalize_url
from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
    AzuraCastNotFoundError,
    AzuraCastPermissionError,
    AzuraCastResponseError,
)

from .common import BASE_URL, load_fixture

API = f"{BASE_URL}/api"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://radio.example.com", "https://radio.example.com"),
        ("  https://radio.example.com/  ", "https://radio.example.com"),
        ("https://radio.example.com/api", "https://radio.example.com"),
        ("https://radio.example.com/api/", "https://radio.example.com"),
        ("http://10.0.0.5:8080/radio/", "http://10.0.0.5:8080/radio"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["radio.example.com", "ftp://radio.example.com", "https://", ""])
def test_normalize_url_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_url(raw)


def _client(hass: HomeAssistant, key: str | None = "secret") -> AzuraCastClient:
    return AzuraCastClient(async_get_clientsession(hass), f"{BASE_URL}/", key)


async def test_properties(hass: HomeAssistant) -> None:
    assert _client(hass).base_url == BASE_URL
    assert _client(hass).has_api_key is True
    assert _client(hass, None).has_api_key is False
    assert _client(hass, "").has_api_key is False


async def test_get_stations_sends_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/stations", json=load_fixture("stations.json"))
    stations = await _client(hass).get_stations()
    assert [s.shortcode for s in stations] == ["ambientes", "party_time"]
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["X-API-Key"] == "secret"


async def test_no_key_sends_no_header(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/status", json={"online": True, "timestamp": 1})
    assert await _client(hass, None).get_status() is True
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert "X-API-Key" not in headers


async def test_get_nowplaying(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/nowplaying", json=load_fixture("nowplaying.json"))
    items = await _client(hass).get_nowplaying()
    assert [i.station.id for i in items] == [1, 2]


async def test_get_account(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/frontend/account/me", json={"id": 7, "email": "ha@example.com"})
    assert (await _client(hass).get_account())["id"] == 7


async def test_playlists_and_queue(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/station/1/playlists", json=load_fixture("playlists_1.json"))
    aioclient_mock.get(f"{API}/station/1/queue", json=load_fixture("queue_1.json"))
    client = _client(hass)
    assert [p.id for p in await client.get_playlists(1)] == [10, 11, 12, 13, 14]
    assert [q.id for q in await client.get_queue(1)] == [501, 502]


async def test_set_playlist_enabled_sends_partial_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.put(f"{API}/station/1/playlist/12", json={"success": True})
    await _client(hass).set_playlist_enabled(1, 12, True)
    method, url, data, _ = aioclient_mock.mock_calls[0]
    assert method == "PUT"
    assert str(url) == f"{API}/station/1/playlist/12"
    assert data == {"is_enabled": True}


async def test_reshuffle_and_skip(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.put(f"{API}/station/1/playlist/10/reshuffle", json={"success": True})
    aioclient_mock.post(f"{API}/station/1/backend/skip", json={"success": True})
    client = _client(hass)
    await client.reshuffle_playlist(1, 10)
    await client.skip_song(1)
    assert [(c[0], str(c[1])) for c in aioclient_mock.mock_calls] == [
        ("PUT", f"{API}/station/1/playlist/10/reshuffle"),
        ("POST", f"{API}/station/1/backend/skip"),
    ]


async def test_clear_queue_deletes_each_item(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/station/1/queue", json=load_fixture("queue_1.json"))
    aioclient_mock.delete(f"{API}/station/1/queue/501", json={"success": True})
    aioclient_mock.delete(f"{API}/station/1/queue/502", json={"success": True})
    assert await _client(hass).clear_queue(1) == 2
    deletes = [str(c[1]) for c in aioclient_mock.mock_calls if c[0] == "DELETE"]
    assert deletes == [f"{API}/station/1/queue/501", f"{API}/station/1/queue/502"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AzuraCastAuthError),
        (403, AzuraCastPermissionError),
        (404, AzuraCastNotFoundError),
        (500, AzuraCastResponseError),
    ],
)
async def test_http_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, error: type[Exception]
) -> None:
    aioclient_mock.get(
        f"{API}/station/1/playlists",
        status=status,
        json={"code": status, "message": "nope", "formatted_message": "Nope.", "success": False},
    )
    with pytest.raises(error) as info:
        await _client(hass).get_playlists(1)
    assert isinstance(info.value, AzuraCastResponseError)
    assert info.value.status == status
    assert info.value.message == "Nope."


@pytest.mark.parametrize("exc", [aiohttp.ClientError(), TimeoutError()])
async def test_connection_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, exc: Exception
) -> None:
    aioclient_mock.get(f"{API}/stations", exc=exc)
    with pytest.raises(AzuraCastConnectionError):
        await _client(hass).get_stations()


async def test_invalid_json(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API}/stations", text="<html>proxy error</html>")
    with pytest.raises(AzuraCastResponseError):
        await _client(hass).get_stations()
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_client.py -q
```

Expected: collection error `ModuleNotFoundError: No module named 'custom_components.azuracast.api.client'`.

- [ ] **Step 3: Implement `custom_components/azuracast/api/errors.py`**

```python
"""Errors raised by the AzuraCast client."""

from __future__ import annotations


class AzuraCastError(Exception):
    """Base class for AzuraCast errors."""


class AzuraCastConnectionError(AzuraCastError):
    """The instance could not be reached."""


class AzuraCastResponseError(AzuraCastError):
    """The instance answered with an error or an unusable body."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status
        self.message = message


class AzuraCastAuthError(AzuraCastResponseError):
    """The API key is missing or invalid (401)."""


class AzuraCastPermissionError(AzuraCastResponseError):
    """The API key lacks permission for this resource (403)."""


class AzuraCastNotFoundError(AzuraCastResponseError):
    """The resource does not exist (404)."""


_BY_STATUS: dict[int, type[AzuraCastResponseError]] = {
    401: AzuraCastAuthError,
    403: AzuraCastPermissionError,
    404: AzuraCastNotFoundError,
}


def error_for_status(status: int, message: str) -> AzuraCastResponseError:
    """Return the error class instance that matches an HTTP status."""
    return _BY_STATUS.get(status, AzuraCastResponseError)(status, message)
```

- [ ] **Step 4: Implement `custom_components/azuracast/api/client.py`**

```python
"""Async REST client for the AzuraCast API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from .errors import (
    AzuraCastConnectionError,
    AzuraCastError,
    AzuraCastResponseError,
    error_for_status,
)
from .models import NowPlaying, Playlist, QueueItem, Station


def normalize_url(url: str) -> str:
    """Return the instance base URL without trailing slash or /api suffix."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Invalid AzuraCast URL: {url!r}")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    return f"{parts.scheme}://{parts.netloc}{path}"


async def _error_message(resp: aiohttp.ClientResponse) -> str:
    try:
        data = await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return ""
    if isinstance(data, dict):
        return str(data.get("formatted_message") or data.get("message") or "")
    return ""


class AzuraCastClient:
    """Talks to one AzuraCast instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str | None = None,
        *,
        request_timeout: float = 10.0,
    ) -> None:
        self._session = session
        self._base_url = normalize_url(base_url)
        self._api_key = api_key or None
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_api_key(self) -> bool:
        return self._api_key is not None

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(self, method: str, path: str, *, payload: Any = None) -> Any:
        url = f"{self._base_url}/api{path}"
        try:
            async with self._session.request(
                method, url, json=payload, headers=self._headers(), timeout=self._timeout
            ) as resp:
                if resp.status >= 400:
                    raise error_for_status(resp.status, await _error_message(resp))
                status = resp.status
                text = await resp.text()
        except AzuraCastError:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            raise AzuraCastConnectionError(f"Error talking to {url}: {err!r}") from err
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise AzuraCastResponseError(status, "Response is not JSON") from err

    async def get_status(self) -> bool:
        data = await self._request("GET", "/status")
        return bool(isinstance(data, dict) and data.get("online"))

    async def get_account(self) -> dict[str, Any]:
        data = await self._request("GET", "/frontend/account/me")
        return data if isinstance(data, dict) else {}

    async def get_stations(self) -> list[Station]:
        data = await self._request("GET", "/stations")
        return [Station.from_dict(row) for row in data or []]

    async def get_nowplaying(self) -> list[NowPlaying]:
        data = await self._request("GET", "/nowplaying")
        return [NowPlaying.from_dict(row) for row in data or []]

    async def get_playlists(self, station_id: int) -> list[Playlist]:
        data = await self._request("GET", f"/station/{station_id}/playlists")
        return [Playlist.from_dict(row) for row in data or []]

    async def set_playlist_enabled(self, station_id: int, playlist_id: int, enabled: bool) -> None:
        await self._request(
            "PUT", f"/station/{station_id}/playlist/{playlist_id}", payload={"is_enabled": enabled}
        )

    async def reshuffle_playlist(self, station_id: int, playlist_id: int) -> None:
        await self._request("PUT", f"/station/{station_id}/playlist/{playlist_id}/reshuffle")

    async def get_queue(self, station_id: int) -> list[QueueItem]:
        data = await self._request("GET", f"/station/{station_id}/queue")
        return [QueueItem.from_dict(row) for row in data or []]

    async def delete_queue_item(self, station_id: int, item_id: int) -> None:
        await self._request("DELETE", f"/station/{station_id}/queue/{item_id}")

    async def clear_queue(self, station_id: int) -> int:
        items = await self.get_queue(station_id)
        for item in items:
            await self.delete_queue_item(station_id, item.id)
        return len(items)

    async def skip_song(self, station_id: int) -> None:
        await self._request("POST", f"/station/{station_id}/backend/skip")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_client.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast/api tests/test_client.py
git commit -m "feat: add the AzuraCast REST client"
```

---

### Task 4: SSE now-playing stream

**Files:**
- Create: `custom_components/azuracast/api/sse.py`
- Test: `tests/test_sse.py`

**Interfaces:**
- Consumes: `NowPlaying` (Task 2), `AzuraCastConnectionError` and `error_for_status` (Task 3).
- Produces:
  - `build_sse_url(base_url: str, shortcodes: Iterable[str]) -> str`
  - `parse_sse_message(raw: str) -> list[dict[str, Any]]`, which returns the raw `np` dicts
  - `iter_sse_data(lines: AsyncIterable[bytes]) -> AsyncIterator[str]`
  - `stream_nowplaying(session: aiohttp.ClientSession, base_url: str, shortcodes: list[str], *, read_timeout: float = 90.0) -> AsyncIterator[NowPlaying]`. It raises `AzuraCastResponseError` subclasses on HTTP errors and `AzuraCastConnectionError` on network errors.

- [ ] **Step 1: Write the failing tests `tests/test_sse.py`**

```python
"""Tests for the SSE stream helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
import re
from urllib.parse import parse_qs, urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastNotFoundError
from custom_components.azuracast.api.sse import (
    build_sse_url,
    iter_sse_data,
    parse_sse_message,
    stream_nowplaying,
)

from .common import BASE_URL, nowplaying_raw

SSE_RE = re.compile(r".*/api/live/nowplaying/sse.*")


def connect_message(*nps: dict) -> str:
    subs = {
        f"station:{np['station']['shortcode']}": {"publications": [{"data": {"np": np}, "offset": 1}]}
        for np in nps
    }
    return json.dumps({"connect": {"client": "c", "subs": subs, "ping": 25, "pong": True}})


def pub_message(np: dict) -> str:
    return json.dumps({"channel": f"station:{np['station']['shortcode']}", "pub": {"data": {"np": np}}})


def test_build_sse_url() -> None:
    url = build_sse_url(BASE_URL, ["ambientes", "party_time"])
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE_URL}/api/live/nowplaying/sse"
    connect = json.loads(parse_qs(parts.query)["cf_connect"][0])
    assert connect == {
        "subs": {"station:ambientes": {"recover": True}, "station:party_time": {"recover": True}}
    }


def test_parse_connect_with_publications() -> None:
    rows = parse_sse_message(connect_message(nowplaying_raw(0), nowplaying_raw(1)))
    assert [r["station"]["shortcode"] for r in rows] == ["ambientes", "party_time"]


def test_parse_legacy_connect_data() -> None:
    raw = json.dumps({"connect": {"data": [{"data": {"np": nowplaying_raw(0)}}]}})
    assert parse_sse_message(raw)[0]["station"]["id"] == 1


def test_parse_pub() -> None:
    assert parse_sse_message(pub_message(nowplaying_raw(1)))[0]["station"]["id"] == 2


@pytest.mark.parametrize("raw", ["{}", '{"time": 1}', "not json", "[]", '{"pub": {"data": {}}}'])
def test_parse_ignores_other_messages(raw: str) -> None:
    assert parse_sse_message(raw) == []


async def _lines(*chunks: str) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk.encode()


async def test_iter_sse_data_joins_and_skips_comments() -> None:
    lines = _lines(": comment\n", "data: {\"a\":\n", "data: 1}\n", "\n", "event: x\n", "data:{}\n", "\n")
    assert [event async for event in iter_sse_data(lines)] == ['{"a":\n1}', "{}"]


async def test_iter_sse_data_flushes_last_event_without_blank_line() -> None:
    assert [e async for e in iter_sse_data(_lines("data: tail\n"))] == ["tail"]


async def test_stream_nowplaying(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    body = (
        f"data: {connect_message(nowplaying_raw(0))}\n\n"
        "data: {}\n\n"
        f"data: {pub_message(nowplaying_raw(1))}\n\n"
        "data: {broken\n\n"
    )
    aioclient_mock.get(SSE_RE, text=body)
    session = async_get_clientsession(hass)
    items = [np async for np in stream_nowplaying(session, BASE_URL, ["ambientes", "party_time"])]
    assert [np.station.shortcode for np in items] == ["ambientes", "party_time"]
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Accept"] == "text/event-stream"


async def test_stream_not_found(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(SSE_RE, status=404, text="")
    with pytest.raises(AzuraCastNotFoundError):
        async for _ in stream_nowplaying(async_get_clientsession(hass), BASE_URL, ["ambientes"]):
            pass


async def test_stream_connection_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(SSE_RE, exc=aiohttp.ClientError())
    with pytest.raises(AzuraCastConnectionError):
        async for _ in stream_nowplaying(async_get_clientsession(hass), BASE_URL, ["ambientes"]):
            pass
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_sse.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.api.sse'`.

- [ ] **Step 3: Implement `custom_components/azuracast/api/sse.py`**

```python
"""Centrifugo SSE stream for AzuraCast now-playing updates."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

from .errors import AzuraCastConnectionError, error_for_status
from .models import NowPlaying

_LOGGER = logging.getLogger(__name__)


def build_sse_url(base_url: str, shortcodes: Iterable[str]) -> str:
    """Return the SSE URL subscribed to every given station."""
    subs = {f"station:{code}": {"recover": True} for code in shortcodes}
    connect = json.dumps({"subs": subs}, separators=(",", ":"))
    return f"{base_url}/api/live/nowplaying/sse?cf_connect={quote(connect, safe='')}"


def parse_sse_message(raw: str) -> list[dict[str, Any]]:
    """Extract now-playing payloads from one Centrifugo SSE message."""
    try:
        data = json.loads(raw)
    except ValueError:
        _LOGGER.debug("Ignoring non-JSON SSE message: %.80s", raw)
        return []
    if not isinstance(data, dict):
        return []
    rows: list[Any]
    if "connect" in data:
        connect = data["connect"] or {}
        if "data" in connect:
            rows = list(connect.get("data") or [])
        else:
            rows = [
                publication
                for sub in (connect.get("subs") or {}).values()
                for publication in (sub.get("publications") or [])
            ]
    elif "pub" in data:
        rows = [data["pub"]]
    else:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("data") if isinstance(row, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("np"), dict):
            result.append(payload["np"])
    return result


async def iter_sse_data(lines: AsyncIterable[bytes]) -> AsyncIterator[str]:
    """Yield the data of each server-sent event."""
    buffer: list[str] = []
    async for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            buffer.append(value[1:] if value.startswith(" ") else value)
    if buffer:
        yield "\n".join(buffer)


async def stream_nowplaying(
    session: aiohttp.ClientSession,
    base_url: str,
    shortcodes: list[str],
    *,
    read_timeout: float = 90.0,
) -> AsyncIterator[NowPlaying]:
    """Yield now-playing updates until the stream ends or fails."""
    url = build_sse_url(base_url, shortcodes)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=read_timeout)
    try:
        async with session.get(
            url, headers={"Accept": "text/event-stream"}, timeout=timeout
        ) as resp:
            if resp.status >= 400:
                raise error_for_status(resp.status, "")
            async for message in iter_sse_data(resp.content):
                parsed: list[NowPlaying] = []
                for row in parse_sse_message(message):
                    try:
                        parsed.append(NowPlaying.from_dict(row))
                    except (KeyError, TypeError, ValueError):
                        _LOGGER.debug("Skipping malformed now-playing payload")
                for nowplaying in parsed:
                    yield nowplaying
    except (aiohttp.ClientError, TimeoutError) as err:
        raise AzuraCastConnectionError(f"Now-playing stream failed: {err!r}") from err
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_sse.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/azuracast/api/sse.py tests/test_sse.py
git commit -m "feat: stream now-playing updates over AzuraCast's SSE endpoint"
```

---

### Task 5: Schedule evaluation

**Files:**
- Create: `custom_components/azuracast/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `Playlist` and `ScheduleItem` (Task 2).
- Produces:
  - `schedule_item_active(item: ScheduleItem, moment: datetime) -> bool`
  - `playlist_playable_now(playlist: Playlist, moment: datetime) -> bool`
  - `station_time(timezone: str | None, utc_now: datetime, fallback: tzinfo) -> datetime`

  `moment` is an aware datetime that is already in the station's timezone.

- [ ] **Step 1: Write the failing tests `tests/test_schedule.py`**

```python
"""Tests for schedule evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.azuracast.api.models import Playlist, ScheduleItem
from custom_components.azuracast.schedule import (
    playlist_playable_now,
    schedule_item_active,
    station_time,
)

CR = ZoneInfo("America/Costa_Rica")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """2026-11-16 is a Monday (ISO weekday 1)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=CR)


def item(start: int, end: int, days: tuple[int, ...] = (), start_date=None, end_date=None) -> ScheduleItem:
    return ScheduleItem(start_time=start, end_time=end, days=days, start_date=start_date, end_date=end_date)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [(at(16, 11, 59), False), (at(16, 12, 0), True), (at(16, 17, 59), True), (at(16, 18, 0), False)],
)
def test_same_day_window(moment: datetime, expected: bool) -> None:
    assert schedule_item_active(item(1200, 1800), moment) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [(at(16, 17, 59), False), (at(16, 18, 0), True), (at(16, 23, 30), True), (at(17, 1, 59), True), (at(17, 2, 0), False)],
)
def test_window_wrapping_midnight(moment: datetime, expected: bool) -> None:
    assert schedule_item_active(item(1800, 200), moment) is expected


def test_equal_start_and_end_is_all_day() -> None:
    assert schedule_item_active(item(0, 0), at(16, 3)) is True


def test_days_use_the_day_the_window_started() -> None:
    monday_only = item(1800, 200, days=(1,))
    assert schedule_item_active(monday_only, at(16, 20)) is True
    assert schedule_item_active(monday_only, at(17, 1)) is True
    assert schedule_item_active(monday_only, at(17, 20)) is False


def test_date_range() -> None:
    ranged = item(0, 0, start_date=date(2026, 11, 17), end_date=date(2026, 11, 18))
    assert schedule_item_active(ranged, at(16, 12)) is False
    assert schedule_item_active(ranged, at(17, 12)) is True
    assert schedule_item_active(ranged, at(18, 12)) is True
    assert schedule_item_active(ranged, at(19, 12)) is False


def _playlist(*items: ScheduleItem) -> Playlist:
    return Playlist(
        id=1, name="p", description="", type="default", order="shuffle", weight=1,
        is_enabled=False, is_jingle=False, schedule_items=items,
    )


def test_unscheduled_playlist_is_always_playable() -> None:
    assert playlist_playable_now(_playlist(), at(16, 4)) is True


def test_playlist_with_any_active_item() -> None:
    playlist = _playlist(item(600, 900), item(1200, 1800))
    assert playlist_playable_now(playlist, at(16, 13)) is True
    assert playlist_playable_now(playlist, at(16, 10)) is False


def test_station_time_uses_station_zone() -> None:
    utc_now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    local = station_time("America/Costa_Rica", utc_now, UTC)
    assert (local.hour, local.tzinfo) == (14, ZoneInfo("America/Costa_Rica"))


@pytest.mark.parametrize("zone", [None, "", "Not/AZone"])
def test_station_time_falls_back(zone: str | None) -> None:
    utc_now = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    assert station_time(zone, utc_now, ZoneInfo("Europe/Madrid")).hour == 21
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_schedule.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.schedule'`.

- [ ] **Step 3: Implement `custom_components/azuracast/schedule.py`**

```python
"""Evaluate AzuraCast playlist schedules."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api.models import Playlist, ScheduleItem


def schedule_item_active(item: ScheduleItem, moment: datetime) -> bool:
    """Return whether a schedule window covers the moment (station local time)."""
    now = moment.hour * 100 + moment.minute
    start, end = item.start_time, item.end_time
    if start == end:
        day = moment.date()
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
        except (ZoneInfoNotFoundError, ValueError):
            zone = fallback
    return utc_now.astimezone(zone)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_schedule.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/azuracast/schedule.py tests/test_schedule.py
git commit -m "feat: evaluate playlist schedules in the station's timezone"
```

---

### Task 6: Tag-based selectors (pure logic)

**Files:**
- Create: `custom_components/azuracast/selectors.py`
- Test: `tests/test_selectors.py`

**Interfaces:**
- Consumes: `Playlist` (Task 2), `AzuraCastClient` (Task 3, used only as a type and for its methods), `playlist_playable_now` (Task 5).
- Produces:
  - `TAG_PATTERN`, `REASON_NONE_ENABLED = "none_enabled"`, `REASON_MIXED = "mixed"`
  - `parse_tags(description: str | None) -> dict[str, set[str]]`
  - `playlist_tags(playlist: Playlist) -> dict[str, str]`, which returns only unambiguous tags
  - `Selector(key: str, members: Mapping[str, tuple[Playlist, ...]], conflicts: tuple[Playlist, ...] = ())` with `.options -> list[str]` and `.playlist_names(value: str) -> set[str]`
  - `build_selectors(playlists: Iterable[Playlist]) -> dict[str, Selector]`
  - `SelectorState(value: str | None, reason: str | None)`
  - `selector_state(selector: Selector) -> SelectorState`
  - `available_values(selector: Selector, moment: datetime) -> list[str]`
  - `ApplyPlan(enable: tuple[int, ...], disable: tuple[int, ...])` with `.is_empty -> bool`
  - `plan_apply(selector: Selector, value: str) -> ApplyPlan`. Raises `ValueError` for an unknown value.
  - `async execute_plan(client: AzuraCastClient, station_id: int, plan: ApplyPlan, *, clear_queue: bool) -> int`, which returns the number of queue items cleared

- [ ] **Step 1: Write the failing tests `tests/test_selectors.py`**

```python
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
    build_selectors,
    execute_plan,
    parse_tags,
    plan_apply,
    playlist_tags,
    selector_state,
    available_values,
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
        id=pid, name=f"p{pid}", description=description, type="default", order="shuffle",
        weight=1, is_enabled=enabled, is_jingle=False, schedule_items=(),
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
    assert available_values(ambiente, datetime(2026, 11, 20, 14, 0, tzinfo=CR)) == ["chill", "lounge"]
    assert available_values(ambiente, datetime(2026, 11, 20, 21, 0, tzinfo=CR)) == ["chill", "fiesta"]
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
    cleared = await execute_plan(client, 1, ApplyPlan(enable=(12,), disable=(10, 11)), clear_queue=True)
    assert cleared == 2
    assert client.mock_calls == [
        call.set_playlist_enabled(1, 12, True),
        call.set_playlist_enabled(1, 10, False),
        call.set_playlist_enabled(1, 11, False),
        call.clear_queue(1),
    ]


async def test_execute_plan_without_queue_clear() -> None:
    client = AsyncMock()
    assert await execute_plan(client, 1, ApplyPlan(enable=(12,), disable=()), clear_queue=False) == 0
    client.clear_queue.assert_not_called()


def test_disabled_values_do_not_count_for_state() -> None:
    items = [replace(p, is_enabled=False) for p in playlists()]
    assert selector_state(build_selectors(items)["ambiente"]).reason == REASON_NONE_ENABLED
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_selectors.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.selectors'`.

- [ ] **Step 3: Implement `custom_components/azuracast/selectors.py`**

```python
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

TAG_PATTERN = re.compile(r"(?<!\S)ha:([a-z0-9_]+)=([a-z0-9_-]+)(?!\S)", re.IGNORECASE)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_selectors.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/azuracast/selectors.py tests/test_selectors.py
git commit -m "feat: add tag-based playlist selectors"
```

---

### Task 7: Repairs issues and coordinators

**Files:**
- Create: `custom_components/azuracast/issues.py`, `custom_components/azuracast/coordinator.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: the client and errors (Task 3), the models (Task 2), `build_selectors`, `playlist_tags` and `Selector` (Task 6), and the constants.
- Produces (`issues.py`): `set_sse_unavailable(hass, entry, active: bool)`, `set_missing_permissions(hass, entry, station: Station, active: bool)` and `set_conflicting_tags(hass, entry, station: Station, playlists: list[Playlist])`. Issue IDs are `sse_unavailable_{entry_id}`, `missing_permissions_{entry_id}_{station_id}` and `conflicting_tags_{entry_id}_{station_id}`.
- Produces (`coordinator.py`):
  - `ManageData(playlists: tuple[Playlist, ...], queue: tuple[QueueItem, ...], selectors: dict[str, Selector])` with `.playlist_by_name(name) -> Playlist | None`
  - `ManageCoordinator(hass, entry, client, station, interval: timedelta)` with attributes `.client` and `.station`
  - `tags_for_playlist(manage: ManageCoordinator | None, name: str | None) -> dict[str, str]`
  - `NowPlayingCoordinator(hass, entry, station, tags_for: Callable[[str | None], dict[str, str]])` with `.station` and `.push(nowplaying: NowPlaying)` (a callback)

- [ ] **Step 1: Write the failing tests `tests/test_coordinator.py`**

```python
"""Tests for the coordinators and issues."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
    AzuraCastPermissionError,
)
from custom_components.azuracast.const import DOMAIN, EVENT_SONG_CHANGED
from custom_components.azuracast.coordinator import (
    ManageCoordinator,
    NowPlayingCoordinator,
    tags_for_playlist,
)

from .common import conflict_playlists, make_nowplaying, playlists, queue, stations


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="radio.example.com", data={})
    entry.add_to_hass(hass)
    return entry


def _manage(hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock) -> ManageCoordinator:
    return ManageCoordinator(hass, entry, client, stations()[0], timedelta(seconds=60))


async def test_manage_coordinator_builds_selectors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = AsyncMock()
    client.get_playlists.return_value = playlists()
    client.get_queue.return_value = queue()
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert [p.id for p in coordinator.data.playlists] == [10, 11, 12, 13, 14]
    assert list(coordinator.data.selectors) == ["ambiente"]
    assert coordinator.data.playlist_by_name("tarde-chill").id == 10
    assert coordinator.data.playlist_by_name(None) is None
    assert tags_for_playlist(coordinator, "tarde-chill") == {"ambiente": "chill"}
    assert tags_for_playlist(coordinator, "missing") == {}
    assert tags_for_playlist(None, "tarde-chill") == {}


async def test_manage_coordinator_permission_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastPermissionError(403, "Forbidden")
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    issue_id = f"missing_permissions_{entry.entry_id}_1"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    client.get_playlists.side_effect = None
    client.get_playlists.return_value = playlists()
    client.get_queue.return_value = []
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_manage_coordinator_auth_error(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastAuthError(401, "")
    coordinator = _manage(hass, entry, client)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_manage_coordinator_connection_error(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    client = AsyncMock()
    client.get_playlists.side_effect = AzuraCastConnectionError("down")
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success


async def test_conflicting_tags_issue(
    hass: HomeAssistant, entry: MockConfigEntry, issue_registry: ir.IssueRegistry
) -> None:
    client = AsyncMock()
    client.get_playlists.return_value = conflict_playlists()
    client.get_queue.return_value = []
    coordinator = _manage(hass, entry, client)
    await coordinator.async_refresh()
    issue = issue_registry.async_get_issue(DOMAIN, f"conflicting_tags_{entry.entry_id}_1")
    assert issue is not None
    assert issue.translation_placeholders == {"station": "Ambientes", "playlists": "both"}


async def test_nowplaying_push_fires_song_changed_only_on_change(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    events = async_capture_events(hass, EVENT_SONG_CHANGED)
    coordinator = NowPlayingCoordinator(hass, entry, stations()[0], lambda name: {"ambiente": "chill"})
    coordinator.push(make_nowplaying(0))
    coordinator.push(make_nowplaying(0))
    await hass.async_block_till_done()
    assert events == []
    assert coordinator.data.current.song.id == "a1"

    coordinator.push(make_nowplaying(0, song_id="a2", title="Slow Tide"))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {
        "station_id": 1,
        "station_shortcode": "ambientes",
        "song": {
            "id": "a2",
            "artist": "Trio Mar",
            "title": "Slow Tide",
            "album": "Costa",
            "art": "https://radio.example.com/api/station/ambientes/art/a1.jpg",
        },
        "playlist": "tarde-chill",
        "tags": {"ambiente": "chill"},
    }
```

Add a helper to `tests/common.py`, under `queue()`:

```python
def conflict_playlists() -> list[Playlist]:
    return [
        Playlist.from_dict({"id": 20, "name": "both", "description": "ha:ambiente=chill ha:ambiente=lounge", "is_enabled": True}),
        Playlist.from_dict({"id": 21, "name": "ok", "description": "ha:ambiente=chill", "is_enabled": True}),
    ]
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_coordinator.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.coordinator'`.

- [ ] **Step 3: Implement `custom_components/azuracast/issues.py`**

```python
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
```

- [ ] **Step 4: Implement `custom_components/azuracast/coordinator.py`**

```python
"""Data coordinators for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import AzuraCastClient
from .api.errors import AzuraCastAuthError, AzuraCastError, AzuraCastPermissionError
from .api.models import NowPlaying, Playlist, QueueItem, Station
from .const import DOMAIN, EVENT_SONG_CHANGED, LOGGER
from .issues import set_conflicting_tags, set_missing_permissions
from .selectors import Selector, build_selectors, playlist_tags


@dataclass(frozen=True, slots=True)
class ManageData:
    """Management data of one station."""

    playlists: tuple[Playlist, ...]
    queue: tuple[QueueItem, ...]
    selectors: dict[str, Selector]

    def playlist_by_name(self, name: str | None) -> Playlist | None:
        if not name:
            return None
        return next((p for p in self.playlists if p.name == name), None)


class ManageCoordinator(DataUpdateCoordinator[ManageData]):
    """Polls playlists and queue of a station the API key can manage."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AzuraCastClient,
        station: Station,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {station.shortcode} management",
            update_interval=interval,
        )
        self.client = client
        self.station = station

    async def _async_update_data(self) -> ManageData:
        try:
            playlists = await self.client.get_playlists(self.station.id)
            queue = await self.client.get_queue(self.station.id)
        except AzuraCastAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except AzuraCastPermissionError as err:
            set_missing_permissions(self.hass, self.config_entry, self.station, True)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="missing_permissions",
                translation_placeholders={"station": self.station.name},
            ) from err
        except AzuraCastError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        set_missing_permissions(self.hass, self.config_entry, self.station, False)
        selectors = build_selectors(playlists)
        set_conflicting_tags(
            self.hass,
            self.config_entry,
            self.station,
            [playlist for selector in selectors.values() for playlist in selector.conflicts],
        )
        return ManageData(playlists=tuple(playlists), queue=tuple(queue), selectors=selectors)


def tags_for_playlist(manage: ManageCoordinator | None, name: str | None) -> dict[str, str]:
    """Return the selector tags of a playlist, looked up by name."""
    if manage is None or manage.data is None:
        return {}
    playlist = manage.data.playlist_by_name(name)
    return playlist_tags(playlist) if playlist else {}


class NowPlayingCoordinator(DataUpdateCoordinator[NowPlaying | None]):
    """Holds now-playing data pushed by the hub; it never polls by itself."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        station: Station,
        tags_for: Callable[[str | None], dict[str, str]],
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {station.shortcode} now playing",
            update_interval=None,
        )
        self.station = station
        self._tags_for = tags_for

    async def _async_update_data(self) -> NowPlaying | None:
        return self.data

    @callback
    def push(self, nowplaying: NowPlaying) -> None:
        previous = self.data
        self.async_set_updated_data(nowplaying)
        current = nowplaying.current
        if previous is None or current is None:
            return
        if previous.current is not None and previous.current.song.id == current.song.id:
            return
        self.hass.bus.async_fire(
            EVENT_SONG_CHANGED,
            {
                "station_id": self.station.id,
                "station_shortcode": self.station.shortcode,
                "song": {
                    "id": current.song.id,
                    "artist": current.song.artist,
                    "title": current.song.title,
                    "album": current.song.album,
                    "art": current.song.art,
                },
                "playlist": current.playlist,
                "tags": self._tags_for(current.playlist),
            },
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_coordinator.py -q
```

Expected: all pass. Note that `make_nowplaying(..., song_id="a2", ...)` keeps the fixture's art URL, which is why the expected `art` in the test is the `a1.jpg` URL.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast/issues.py custom_components/azuracast/coordinator.py tests
git commit -m "feat: add station coordinators and repairs issues"
```

---

### Task 8: Now-playing hub (SSE + polling fallback)

**Files:**
- Create: `custom_components/azuracast/hub.py`
- Test: `tests/test_hub.py`

**Interfaces:**
- Consumes: `stream_nowplaying` (Task 4), `NowPlayingCoordinator` (Task 7), `set_sse_unavailable` (Task 7), and `POLL_FALLBACK_INTERVAL` and `SSE_MAX_BACKOFF`.
- Produces: `NowPlayingHub(hass, entry, client, coordinators: Mapping[str, NowPlayingCoordinator], *, stream_factory: StreamFactory | None = None, poll_interval: timedelta = POLL_FALLBACK_INTERVAL)`.
  - Attributes: `sse_connected: bool`, `sse_ever_connected: bool`.
  - Methods: `async_start()`, `async_stop()`, `async_poll()`.
  - `StreamFactory = Callable[[aiohttp.ClientSession, str, list[str]], AsyncIterator[NowPlaying]]`.
  - When `stream_factory` is `None`, the hub resolves `hub.stream_nowplaying` at construction time. That is what lets tests patch `custom_components.azuracast.hub.stream_nowplaying`.

- [ ] **Step 1: Add the fake stream to `tests/common.py`**

Add these two imports to the import block at the top of `tests/common.py`:

```python
import asyncio
from collections.abc import AsyncIterator, Callable
```

Then append:

```python
class FakeStream:
    """Stand-in for stream_nowplaying, fed from a queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[NowPlaying | Exception] = asyncio.Queue()
        self.calls = 0

    def __call__(self, session: object, base_url: str, shortcodes: list[str]) -> AsyncIterator[NowPlaying]:
        self.calls += 1
        self.shortcodes = shortcodes
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[NowPlaying]:
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


async def wait_for(predicate: Callable[[], bool], attempts: int = 100) -> None:
    """Yield to the loop until the predicate is true."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")
```

- [ ] **Step 2: Write the failing tests `tests/test_hub.py`**

```python
"""Tests for the now-playing hub."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from homeassistant.util import dt as dt_util

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastNotFoundError
from custom_components.azuracast.const import DOMAIN
from custom_components.azuracast.coordinator import NowPlayingCoordinator
from custom_components.azuracast.hub import NowPlayingHub

from .common import BASE_URL, FakeStream, make_nowplaying, nowplaying_list, stations, wait_for


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="radio.example.com", data={})
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def client() -> MagicMock:
    client = MagicMock()
    client.base_url = BASE_URL
    client.session = MagicMock()
    client.get_nowplaying = AsyncMock(return_value=nowplaying_list())
    return client


def _coordinators(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, NowPlayingCoordinator]:
    return {s.shortcode: NowPlayingCoordinator(hass, entry, s, lambda _: {}) for s in stations()}


async def test_start_polls_once_and_dispatches(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    assert coordinators["ambientes"].data.current.song.id == "a1"
    assert coordinators["party_time"].data.is_live is True
    await hub.async_stop()


async def test_sse_updates_and_suppresses_polling(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    coordinators = _coordinators(hass, entry)
    stream = FakeStream()
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=stream, poll_interval=timedelta(seconds=30))
    await hub.async_start()
    await stream.queue.put(make_nowplaying(0, song_id="a9", title="New"))
    await wait_for(lambda: hub.sse_connected)
    assert stream.shortcodes == ["ambientes", "party_time"]
    assert coordinators["ambientes"].data.current.song.id == "a9"

    client.get_nowplaying.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    client.get_nowplaying.assert_not_called()
    await hub.async_stop()
    assert hub.sse_connected is False


async def test_sse_missing_raises_issue_and_keeps_polling(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, issue_registry: ir.IssueRegistry
) -> None:
    stream = FakeStream()
    await stream.queue.put(AzuraCastNotFoundError(404, ""))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    issue_id = f"sse_unavailable_{entry.entry_id}"
    await wait_for(lambda: issue_registry.async_get_issue(DOMAIN, issue_id) is not None)

    client.get_nowplaying.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    client.get_nowplaying.assert_awaited_once()
    await hub.async_stop()


async def test_issue_cleared_once_sse_connects(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, issue_registry: ir.IssueRegistry
) -> None:
    stream = FakeStream()
    await stream.queue.put(AzuraCastConnectionError("refused"))
    hub = NowPlayingHub(hass, entry, client, _coordinators(hass, entry), stream_factory=stream)
    await hub.async_start()
    issue_id = f"sse_unavailable_{entry.entry_id}"
    await wait_for(lambda: issue_registry.async_get_issue(DOMAIN, issue_id) is not None)
    await stream.queue.put(make_nowplaying(0))
    await wait_for(lambda: hub.sse_connected, attempts=300)
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    await hub.async_stop()


async def test_poll_failure_marks_coordinators_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock
) -> None:
    client.get_nowplaying.side_effect = AzuraCastConnectionError("down")
    coordinators = _coordinators(hass, entry)
    hub = NowPlayingHub(hass, entry, client, coordinators, stream_factory=FakeStream())
    await hub.async_start()
    assert all(not c.last_update_success for c in coordinators.values())
    await hub.async_stop()
```

- [ ] **Step 3: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_hub.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.hub'`.

- [ ] **Step 4: Implement `custom_components/azuracast/hub.py`**

```python
"""Now-playing hub: one SSE stream per instance with a polling fallback."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import contextlib
from datetime import datetime, timedelta
import random

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .api.client import AzuraCastClient
from .api.errors import AzuraCastError
from .api.models import NowPlaying
from .api.sse import stream_nowplaying
from .const import LOGGER, POLL_FALLBACK_INTERVAL, SSE_MAX_BACKOFF
from .coordinator import NowPlayingCoordinator
from .issues import set_sse_unavailable

type StreamFactory = Callable[[aiohttp.ClientSession, str, list[str]], AsyncIterator[NowPlaying]]


class NowPlayingHub:
    """Feeds every station's NowPlayingCoordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AzuraCastClient,
        coordinators: Mapping[str, NowPlayingCoordinator],
        *,
        stream_factory: StreamFactory | None = None,
        poll_interval: timedelta = POLL_FALLBACK_INTERVAL,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._client = client
        self._coordinators = coordinators
        self._stream_factory: StreamFactory = stream_factory or stream_nowplaying
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._unsub_poll: CALLBACK_TYPE | None = None
        self._poll_failing = False
        self.sse_connected = False
        self.sse_ever_connected = False

    async def async_start(self) -> None:
        await self.async_poll()
        self._unsub_poll = async_track_time_interval(
            self._hass, self._async_poll_tick, self._poll_interval, name="azuracast now-playing poll"
        )
        if self._coordinators:
            self._task = self._entry.async_create_background_task(
                self._hass, self._async_run(), name=f"azuracast sse {self._entry.entry_id}"
            )

    async def async_stop(self) -> None:
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.sse_connected = False

    async def async_poll(self) -> None:
        try:
            items = await self._client.get_nowplaying()
        except AzuraCastError as err:
            if not self._poll_failing:
                LOGGER.info("AzuraCast now-playing data unavailable: %s", err)
                self._poll_failing = True
            for coordinator in self._coordinators.values():
                coordinator.async_set_update_error(err)
            return
        if self._poll_failing:
            LOGGER.info("AzuraCast now-playing data available again")
            self._poll_failing = False
        for nowplaying in items:
            self._dispatch(nowplaying)

    async def _async_poll_tick(self, _now: datetime) -> None:
        if not self.sse_connected:
            await self.async_poll()

    @callback
    def _dispatch(self, nowplaying: NowPlaying) -> None:
        coordinator = self._coordinators.get(nowplaying.station.shortcode)
        if coordinator is not None:
            coordinator.push(nowplaying)

    async def _async_run(self) -> None:
        backoff = 1.0
        shortcodes = list(self._coordinators)
        while True:
            try:
                async for nowplaying in self._stream_factory(
                    self._client.session, self._client.base_url, shortcodes
                ):
                    if not self.sse_connected:
                        self.sse_connected = True
                        self.sse_ever_connected = True
                        backoff = 1.0
                        set_sse_unavailable(self._hass, self._entry, False)
                        LOGGER.debug("Now-playing stream connected")
                    self._dispatch(nowplaying)
            except AzuraCastError as err:
                LOGGER.debug("Now-playing stream unavailable: %s", err)
                if not self.sse_ever_connected:
                    set_sse_unavailable(self._hass, self._entry, True)
            self.sse_connected = False
            await asyncio.sleep(backoff + random.uniform(0, backoff / 2))
            backoff = min(backoff * 2, SSE_MAX_BACKOFF)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_hub.py -q
```

Expected: all pass. `test_issue_cleared_once_sse_connects` waits through one 1–1.5 s backoff, so it is slower.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast/hub.py tests
git commit -m "feat: add the now-playing hub with SSE and polling fallback"
```

---

### Task 9: Runtime data, integration setup and translations

**Files:**
- Create: `custom_components/azuracast/runtime.py`
- Replace: `custom_components/azuracast/__init__.py`
- Create: `custom_components/azuracast/strings.json`, `custom_components/azuracast/translations/en.json` (identical content)
- Create: `custom_components/azuracast/services.py` (stub `async_setup_services`, filled in Task 14)
- Modify: `tests/conftest.py`
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: everything from Tasks 2–8.
- Produces:
  - `runtime.station_device_key(entry_id: str, station_id: int) -> str`
  - `StationRuntime(station, device_key, nowplaying, manage, clear_queue_on_change, reject_unavailable_values)`
  - `AzuraCastRuntime(client, hub, stations: dict[int, StationRuntime])`
  - `type AzuraCastConfigEntry = ConfigEntry[AzuraCastRuntime]`
  - `__init__.PLATFORMS: list[Platform]`, which starts as `[]` and grows in Tasks 11–13
  - `services.async_setup_services(hass)`
  - Test fixtures `mock_client`, `fake_stream` (autouse), `mock_config_entry` and `init_integration`

- [ ] **Step 1: Write `custom_components/azuracast/strings.json` and copy it to `translations/en.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Connect to AzuraCast",
        "description": "Enter the address of your AzuraCast instance. The API key is optional: without it you only get now-playing data; with it you can also manage playlists, the queue and skipping songs.",
        "data": {
          "url": "URL",
          "api_key": "API key",
          "verify_ssl": "Verify SSL certificate"
        },
        "data_description": {
          "url": "Base address, for example https://radio.example.com",
          "api_key": "Create it in AzuraCast under My Account → API Keys. Use a dedicated user that only has permissions on the stations Home Assistant should manage.",
          "verify_ssl": "Turn off only for instances with a self-signed certificate."
        }
      },
      "reauth_confirm": {
        "title": "Update the AzuraCast API key",
        "description": "AzuraCast no longer accepts the API key for {name}. Enter a new one.",
        "data": {
          "api_key": "API key"
        }
      },
      "reconfigure": {
        "title": "Reconfigure AzuraCast",
        "data": {
          "url": "URL",
          "api_key": "API key",
          "verify_ssl": "Verify SSL certificate"
        },
        "data_description": {
          "url": "Base address, for example https://radio.example.com",
          "api_key": "Leave empty to use the instance read-only.",
          "verify_ssl": "Turn off only for instances with a self-signed certificate."
        }
      }
    },
    "error": {
      "cannot_connect": "Could not reach AzuraCast at this address.",
      "invalid_auth": "AzuraCast rejected the API key.",
      "invalid_url": "Enter a full address that starts with http:// or https://.",
      "unknown": "Unexpected error."
    },
    "abort": {
      "already_configured": "This AzuraCast instance is already configured.",
      "reauth_successful": "The API key was updated.",
      "reconfigure_successful": "The configuration was updated."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "AzuraCast options",
        "data": {
          "excluded_stations": "Stations to ignore",
          "keep_queue_stations": "Keep the queue when a selector changes",
          "allow_unavailable_stations": "Accept selector values with nothing scheduled now",
          "scan_interval": "Playlist refresh interval"
        },
        "data_description": {
          "excluded_stations": "These stations get no device or entities.",
          "keep_queue_stations": "By default, changing a selector clears the upcoming queue so the change is heard after the current song. Stations listed here keep their queue.",
          "allow_unavailable_stations": "By default, a selector value is rejected when none of its playlists can play right now. Stations listed here accept it anyway.",
          "scan_interval": "How often playlists and the queue are refreshed, in seconds."
        }
      }
    },
    "abort": {
      "not_loaded": "Load the integration before changing its options.",
      "cannot_connect": "Could not reach AzuraCast to list the stations."
    }
  },
  "entity": {
    "sensor": {
      "listeners": {"name": "Listeners"},
      "current_song": {"name": "Current song"},
      "current_playlist": {"name": "Current playlist"},
      "next_song": {"name": "Next song"},
      "live_streamer": {"name": "Live streamer"}
    },
    "binary_sensor": {
      "online": {"name": "Online"},
      "live": {"name": "Live DJ"}
    },
    "switch": {
      "playlist": {"name": "Playlist {name}"}
    },
    "button": {
      "skip_song": {"name": "Skip song"},
      "clear_queue": {"name": "Clear queue"}
    },
    "select": {
      "selector": {"name": "{selector}"}
    }
  },
  "exceptions": {
    "auth_failed": {"message": "AzuraCast rejected the API key."},
    "cannot_connect": {"message": "Could not connect to AzuraCast: {error}"},
    "update_failed": {"message": "Error updating AzuraCast data: {error}"},
    "missing_permissions": {"message": "The API key has no management permissions on station {station}."},
    "api_error": {"message": "AzuraCast returned an error: {error}"},
    "unknown_option": {"message": "{option} is not a value of selector {selector}."},
    "value_not_available_now": {"message": "No playlist tagged {selector}={value} can play right now."},
    "selector_apply_failed": {"message": "Could not apply {selector}={value}: {error}"},
    "no_station_targeted": {"message": "Select at least one AzuraCast station."},
    "station_not_managed": {"message": "Station {station} is read-only: the API key cannot manage it."},
    "unknown_playlist": {"message": "Station {station} has no playlist named {playlist}."}
  },
  "issues": {
    "sse_unavailable": {
      "title": "Real-time updates unavailable on {instance}",
      "description": "Home Assistant could not open AzuraCast's now-playing stream, so song changes arrive up to 30 seconds late. In AzuraCast, turn on \"Use High-Performance Now Playing Updates\" under Administration → System Settings, then reload the integration."
    },
    "missing_permissions": {
      "title": "No management permissions on {station}",
      "description": "The API key used for {instance} can no longer manage station {station}. Give the AzuraCast user permissions on that station, or exclude it in the integration options."
    },
    "conflicting_tags": {
      "title": "Conflicting tags on {station}",
      "description": "These playlists have two values for the same selector, so the selector ignores them: {playlists}. Leave one ha:selector=value tag per selector in each playlist description."
    }
  },
  "services": {
    "skip_song": {
      "name": "Skip song",
      "description": "Skips the song playing on the targeted stations."
    },
    "clear_queue": {
      "name": "Clear queue",
      "description": "Removes every upcoming song from the AutoDJ queue of the targeted stations."
    },
    "set_playlists_enabled": {
      "name": "Enable or disable playlists",
      "description": "Enables or disables several playlists of the targeted stations at once.",
      "fields": {
        "playlists": {"name": "Playlists", "description": "Playlist names or numeric IDs."},
        "enabled": {"name": "Enabled", "description": "Whether the playlists should be enabled."}
      }
    },
    "reshuffle_playlist": {
      "name": "Reshuffle playlist",
      "description": "Reshuffles the song order of one playlist.",
      "fields": {
        "playlist": {"name": "Playlist", "description": "Playlist name or numeric ID."}
      }
    }
  }
}
```

- [ ] **Step 2: Create `custom_components/azuracast/runtime.py`**

```python
"""Runtime data stored on the config entry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .api.client import AzuraCastClient
from .api.models import Station
from .coordinator import ManageCoordinator, NowPlayingCoordinator
from .hub import NowPlayingHub


def station_device_key(entry_id: str, station_id: int) -> str:
    """Return the device identifier value of a station."""
    return f"{entry_id}_{station_id}"


@dataclass(slots=True)
class StationRuntime:
    """Everything the platforms need about one station."""

    station: Station
    device_key: str
    nowplaying: NowPlayingCoordinator
    manage: ManageCoordinator | None
    clear_queue_on_change: bool
    reject_unavailable_values: bool


@dataclass(slots=True)
class AzuraCastRuntime:
    """Runtime data of one AzuraCast instance."""

    client: AzuraCastClient
    hub: NowPlayingHub
    stations: dict[int, StationRuntime]


type AzuraCastConfigEntry = ConfigEntry[AzuraCastRuntime]
```

- [ ] **Step 3: Create the stub `custom_components/azuracast/services.py`**

```python
"""Services of the AzuraCast integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""
```

- [ ] **Step 4: Extend `tests/conftest.py`**

Replace its content with:

```python
"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastPermissionError
from custom_components.azuracast.api.models import Playlist
from custom_components.azuracast.const import DOMAIN

from .common import BASE_URL, FakeStream, nowplaying_list, playlists, queue, stations


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow loading custom integrations in every test."""


@pytest.fixture(autouse=True)
def fake_stream() -> Generator[FakeStream]:
    """Replace the SSE stream so no test opens a real connection."""
    stream = FakeStream()
    with patch("custom_components.azuracast.hub.stream_nowplaying", stream):
        yield stream


def _playlists_for(station_id: int) -> list[Playlist]:
    if station_id == 2:
        raise AzuraCastPermissionError(403, "Forbidden")
    return playlists()


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the client used by setup."""
    with patch("custom_components.azuracast.AzuraCastClient", autospec=True) as client_cls:
        client = client_cls.return_value
        client.base_url = BASE_URL
        client.has_api_key = True
        client.session = MagicMock()
        client.get_status.return_value = True
        client.get_account.return_value = {"id": 1}
        client.get_stations.return_value = stations()
        client.get_nowplaying.return_value = nowplaying_list()
        client.get_playlists.side_effect = _playlists_for
        client.get_queue.return_value = queue()
        client.clear_queue.return_value = 2
        yield client


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="radio.example.com",
        unique_id=BASE_URL,
        data={CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration and unload it after the test."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_config_entry
    if mock_config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
```

Task 10 extends `mock_client` so it also patches the client used by the config flow, and adds the reauth-on-setup test. Both need the config flow, which does not exist yet.

- [ ] **Step 5: Write the failing tests `tests/test_init.py`**

```python
"""Tests for setting up the integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastConnectionError
from custom_components.azuracast.const import CONF_ALLOW_UNAVAILABLE_STATIONS, CONF_EXCLUDED_STATIONS, CONF_KEEP_QUEUE_STATIONS, DOMAIN


async def test_setup_and_unload(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert set(runtime.stations) == {1, 2}
    ambientes, party = runtime.stations[1], runtime.stations[2]
    assert ambientes.manage is not None
    assert party.manage is None
    assert ambientes.clear_queue_on_change is True
    assert ambientes.reject_unavailable_values is True
    assert ambientes.nowplaying.data.current.song.id == "a1"
    assert ambientes.device_key == f"{entry.entry_id}_1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_instance_device_created(
    hass: HomeAssistant, init_integration: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    device = device_registry.async_get_device(identifiers={(DOMAIN, init_integration.entry_id)})
    assert device is not None
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_options_shape_runtime(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_EXCLUDED_STATIONS: ["2"],
            CONF_KEEP_QUEUE_STATIONS: ["1"],
            CONF_ALLOW_UNAVAILABLE_STATIONS: ["1"],
            "scan_interval": 120,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    stations = mock_config_entry.runtime_data.stations
    assert set(stations) == {1}
    assert stations[1].clear_queue_on_change is False
    assert stations[1].reject_unavailable_values is False
    assert stations[1].manage.update_interval.total_seconds() == 120
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_no_api_key_is_read_only(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.has_api_key = False
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert all(rt.manage is None for rt in mock_config_entry.runtime_data.stations.values())
    mock_client.get_playlists.assert_not_called()
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_stations.side_effect = AzuraCastConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_stale_station_device_removed(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
    device_registry: dr.DeviceRegistry,
) -> None:
    mock_config_entry.add_to_hass(hass)
    stale = device_registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_99")},
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert device_registry.async_get(stale.id) is None
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
```

- [ ] **Step 6: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_init.py -q
```

Expected: FAIL. `async_setup_entry` is missing, so the entry state is `SETUP_ERROR` / `NOT_LOADED`.

- [ ] **Step 7: Implement `custom_components/azuracast/__init__.py`**

```python
"""The AzuraCast integration."""

from __future__ import annotations

from datetime import timedelta
from functools import partial

from homeassistant.const import (
    CONF_API_KEY,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api.client import AzuraCastClient
from .api.errors import AzuraCastAuthError, AzuraCastError, AzuraCastPermissionError
from .api.models import Station
from .const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
)
from .coordinator import ManageCoordinator, NowPlayingCoordinator, tags_for_playlist
from .hub import NowPlayingHub
from .runtime import AzuraCastConfigEntry, AzuraCastRuntime, StationRuntime, station_device_key
from .services import async_setup_services

PLATFORMS: list[Platform] = []
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for the domain."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AzuraCastConfigEntry) -> bool:
    """Set up one AzuraCast instance."""
    session = async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL])
    client = AzuraCastClient(session, entry.data[CONF_URL], entry.data.get(CONF_API_KEY))
    try:
        stations = await client.get_stations()
    except AzuraCastAuthError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except AzuraCastError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    options = entry.options
    excluded = set(options.get(CONF_EXCLUDED_STATIONS, []))
    keep_queue = set(options.get(CONF_KEEP_QUEUE_STATIONS, []))
    allow_unavailable = set(options.get(CONF_ALLOW_UNAVAILABLE_STATIONS, []))
    interval = timedelta(seconds=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)))

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="AzuraCast",
        model="AzuraCast instance",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=client.base_url,
    )

    runtimes: dict[int, StationRuntime] = {}
    for station in stations:
        key = str(station.id)
        if key in excluded:
            continue
        manage = await _async_manage_coordinator(hass, entry, client, station, interval)
        runtimes[station.id] = StationRuntime(
            station=station,
            device_key=station_device_key(entry.entry_id, station.id),
            nowplaying=NowPlayingCoordinator(hass, entry, station, partial(tags_for_playlist, manage)),
            manage=manage,
            clear_queue_on_change=key not in keep_queue,
            reject_unavailable_values=key not in allow_unavailable,
        )

    _remove_stale_devices(device_registry, entry, runtimes)

    hub = NowPlayingHub(
        hass, entry, client, {rt.station.shortcode: rt.nowplaying for rt in runtimes.values()}
    )
    entry.runtime_data = AzuraCastRuntime(client=client, hub=hub, stations=runtimes)
    await hub.async_start()
    entry.async_on_unload(hub.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AzuraCastConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_manage_coordinator(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    client: AzuraCastClient,
    station: Station,
    interval: timedelta,
) -> ManageCoordinator | None:
    if not client.has_api_key:
        return None
    try:
        await client.get_playlists(station.id)
    except AzuraCastPermissionError:
        LOGGER.info("No management permission on station %s; it will be read-only", station.name)
        return None
    except AzuraCastAuthError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
    except AzuraCastError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
    coordinator = ManageCoordinator(hass, entry, client, station, interval)
    await coordinator.async_config_entry_first_refresh()
    return coordinator


def _remove_stale_devices(
    device_registry: dr.DeviceRegistry,
    entry: AzuraCastConfigEntry,
    runtimes: dict[int, StationRuntime],
) -> None:
    keep = {entry.entry_id} | {rt.device_key for rt in runtimes.values()}
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        identifiers = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if identifiers and not identifiers & keep:
            device_registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_init.py -q
```

Expected: all pass.

- [ ] **Step 9: Run the whole suite**

```bash
.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 10: Commit**

```bash
git add custom_components/azuracast tests
git commit -m "feat: set up stations, coordinators and the hub per AzuraCast instance"
```

---

### Task 10: Config flow (user, reauth, reconfigure, options)

**Files:**
- Create: `custom_components/azuracast/config_flow.py`
- Modify: `tests/conftest.py` (`mock_client` also patches the config flow's client)
- Modify: `tests/test_init.py` (reauth started by a setup auth failure)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `normalize_url`, `AzuraCastClient` and the errors (Task 3), the `const` option keys, and `AzuraCastConfigEntry` (Task 9).
- Produces: `AzuraCastConfigFlow` (VERSION 1) and `AzuraCastOptionsFlow(OptionsFlowWithReload)`. Options keys: `excluded_stations`, `keep_queue_stations`, `allow_unavailable_stations` (lists of station-ID strings) and `scan_interval` (int).

- [ ] **Step 1: Patch the config flow's client too**

In `tests/conftest.py`, change the `mock_client` fixture header to:

```python
@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the client used by setup and the config flow."""
    with (
        patch("custom_components.azuracast.AzuraCastClient", autospec=True) as client_cls,
        patch("custom_components.azuracast.config_flow.AzuraCastClient", new=client_cls),
    ):
        client = client_cls.return_value
```

Keep the rest of the fixture body unchanged.

Append this test to `tests/test_init.py`, and add `SOURCE_REAUTH` to its `homeassistant.config_entries` import and `AzuraCastAuthError` to its errors import:

```python
async def test_setup_auth_error_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_stations.side_effect = AzuraCastAuthError(401, "")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)
```

- [ ] **Step 2: Write the failing tests `tests/test_config_flow.py`**

```python
"""Tests for the config and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastAuthError, AzuraCastConnectionError
from custom_components.azuracast.const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    DOMAIN,
)

from .common import BASE_URL


@pytest.fixture
def no_setup() -> Generator[None]:
    with patch("custom_components.azuracast.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_with_key(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://radio.example.com/api/", CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "radio.example.com"
    assert result["data"] == {CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True}
    assert result["result"].unique_id == BASE_URL
    mock_client.get_account.assert_awaited_once()


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_without_key(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] is None
    mock_client.get_account.assert_not_called()


@pytest.mark.usefixtures("no_setup")
@pytest.mark.parametrize(
    ("method", "exc", "error"),
    [
        ("get_status", AzuraCastConnectionError("down"), "cannot_connect"),
        ("get_account", AzuraCastAuthError(401, ""), "invalid_auth"),
        ("get_status", RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recover(
    hass: HomeAssistant, mock_client: MagicMock, method: str, exc: Exception, error: str
) -> None:
    getattr(mock_client, method).side_effect = exc
    result = await _start(hass)
    user_input = {CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    getattr(mock_client, method).side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_invalid_url(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "radio", CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{BASE_URL}/", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("no_setup")
async def test_reauth(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.get_account.side_effect = AzuraCastAuthError(401, "")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.get_account.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_API_KEY: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_KEY] == "new"


@pytest.mark.usefixtures("no_setup")
async def test_reconfigure(hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://cast.example.org", CONF_API_KEY: "secret", CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.unique_id == "https://cast.example.org"
    assert mock_config_entry.data[CONF_VERIFY_SSL] is False


async def test_options_flow(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXCLUDED_STATIONS: ["2"],
            CONF_KEEP_QUEUE_STATIONS: [],
            CONF_ALLOW_UNAVAILABLE_STATIONS: [],
            CONF_SCAN_INTERVAL: 120,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options[CONF_EXCLUDED_STATIONS] == ["2"]
    assert init_integration.options[CONF_SCAN_INTERVAL] == 120
    assert set(init_integration.runtime_data.stations) == {1}
```

- [ ] **Step 3: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_config_flow.py tests/test_init.py -q
```

Expected: FAIL. `mock_client` cannot patch the missing module `custom_components.azuracast.config_flow`.

- [ ] **Step 4: Implement `custom_components/azuracast/config_flow.py`**

```python
"""Config flow for AzuraCast."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import probatio

from .api.client import AzuraCastClient, normalize_url
from .api.errors import AzuraCastAuthError, AzuraCastError, AzuraCastPermissionError
from .const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .runtime import AzuraCastConfigEntry

URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): URL_SELECTOR,
        probatio.Optional(CONF_API_KEY): KEY_SELECTOR,
        probatio.Required(CONF_VERIFY_SSL, default=True): BooleanSelector(),
    }
)
REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_API_KEY): KEY_SELECTOR})


def _title(url: str) -> str:
    return urlsplit(url).hostname or url


class AzuraCastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AzuraCast."""

    VERSION = 1

    async def _validate(self, url: str, api_key: str | None, verify_ssl: bool) -> dict[str, str]:
        client = AzuraCastClient(async_get_clientsession(self.hass, verify_ssl=verify_ssl), url, api_key)
        try:
            await client.get_status()
            if api_key:
                await client.get_account()
        except (AzuraCastAuthError, AzuraCastPermissionError):
            return {"base": "invalid_auth"}
        except AzuraCastError:
            return {"base": "cannot_connect"}
        except Exception:
            LOGGER.exception("Unexpected error validating AzuraCast")
            return {"base": "unknown"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                api_key = user_input.get(CONF_API_KEY) or None
                verify_ssl = user_input[CONF_VERIFY_SSL]
                errors = await self._validate(url, api_key, verify_ssl)
                if not errors:
                    return self.async_create_entry(
                        title=_title(url),
                        data={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify_ssl},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY]
            errors = await self._validate(entry.data[CONF_URL], api_key, entry.data[CONF_VERIFY_SSL])
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: api_key})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                if url != entry.unique_id:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured()
                api_key = user_input.get(CONF_API_KEY) or None
                verify_ssl = user_input[CONF_VERIFY_SSL]
                errors = await self._validate(url, api_key, verify_ssl)
                if not errors:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=url,
                        title=_title(url),
                        data={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify_ssl},
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or entry.data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: AzuraCastConfigEntry) -> AzuraCastOptionsFlow:
        return AzuraCastOptionsFlow()


class AzuraCastOptionsFlow(OptionsFlowWithReload):
    """Options for an AzuraCast instance."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry: AzuraCastConfigEntry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                data={**user_input, CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        try:
            stations = await entry.runtime_data.client.get_stations()
        except AzuraCastError:
            return self.async_abort(reason="cannot_connect")

        station_selector = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=str(s.id), label=s.name) for s in stations],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
        options = entry.options
        schema = probatio.Schema(
            {
                probatio.Optional(
                    CONF_EXCLUDED_STATIONS, default=options.get(CONF_EXCLUDED_STATIONS, [])
                ): station_selector,
                probatio.Optional(
                    CONF_KEEP_QUEUE_STATIONS, default=options.get(CONF_KEEP_QUEUE_STATIONS, [])
                ): station_selector,
                probatio.Optional(
                    CONF_ALLOW_UNAVAILABLE_STATIONS,
                    default=options.get(CONF_ALLOW_UNAVAILABLE_STATIONS, []),
                ): station_selector,
                probatio.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=10,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_config_flow.py tests/test_init.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast/config_flow.py tests
git commit -m "feat: add config, reauth, reconfigure and options flows"
```

---

### Task 11: Base entity, media player, sensors and binary sensors

**Files:**
- Create: `custom_components/azuracast/entity.py`, `custom_components/azuracast/media_player.py`, `custom_components/azuracast/sensor.py`, `custom_components/azuracast/binary_sensor.py`
- Modify: `custom_components/azuracast/__init__.py` (the `PLATFORMS` list)
- Test: `tests/test_sensor_platforms.py`

**Interfaces:**
- Consumes: `StationRuntime`, `AzuraCastConfigEntry` and `NowPlayingCoordinator`.
- Produces (`entity.py`):
  - `AzuraCastEntity[CoordinatorT](coordinator, entry, runtime, key)` with `.client`, `.station` and `._runtime`
  - `api_errors()`, an async context manager that maps `AzuraCastError` to a translated `HomeAssistantError(api_error)`
  - `track_dynamic_entities(hass, entry, coordinator, *, platform: str, keys: Callable[[ManageData], set[K]], create: Callable[[K], AzuraCastEntity[ManageCoordinator]], add_entities)` (used in Tasks 12–13)

- [ ] **Step 1: Write the failing tests `tests/test_sensor_platforms.py`**

```python
"""Tests for media_player, sensor and binary_sensor platforms."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.media_player import (
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_TITLE,
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_NEXT_TRACK,
    MediaPlayerEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_OFF, STATE_ON, STATE_PLAYING, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastConnectionError, AzuraCastResponseError

from .common import make_nowplaying


async def test_media_player(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    state = hass.states.get("media_player.ambientes")
    assert state.state == STATE_PLAYING
    assert state.attributes[ATTR_MEDIA_TITLE] == "Bossa Breeze"
    assert state.attributes[ATTR_MEDIA_ARTIST] == "Trio Mar"
    assert state.attributes[ATTR_MEDIA_DURATION] == 240
    assert state.attributes[ATTR_MEDIA_POSITION] == 60
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == MediaPlayerEntityFeature.NEXT_TRACK

    party = hass.states.get("media_player.party_time")
    assert party.attributes[ATTR_SUPPORTED_FEATURES] == 0

    await hass.services.async_call(
        MP_DOMAIN, SERVICE_MEDIA_NEXT_TRACK, {ATTR_ENTITY_ID: "media_player.ambientes"}, blocking=True
    )
    mock_client.skip_song.assert_awaited_once_with(1)


async def test_media_player_skip_error(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    mock_client.skip_song.side_effect = AzuraCastResponseError(500, "boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            MP_DOMAIN, SERVICE_MEDIA_NEXT_TRACK, {ATTR_ENTITY_ID: "media_player.ambientes"}, blocking=True
        )


async def test_media_player_offline(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    init_integration.runtime_data.stations[1].nowplaying.push(make_nowplaying(0, is_online=False))
    await hass.async_block_till_done()
    assert hass.states.get("media_player.ambientes").state == STATE_OFF
    assert hass.states.get("binary_sensor.ambientes_online").state == STATE_OFF


async def test_sensors(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    listeners = hass.states.get("sensor.ambientes_listeners")
    assert listeners.state == "3"
    assert listeners.attributes["unique"] == 3
    assert hass.states.get("sensor.ambientes_current_song").state == "Trio Mar - Bossa Breeze"
    playlist = hass.states.get("sensor.ambientes_current_playlist")
    assert playlist.state == "tarde-chill"
    assert playlist.attributes["tags"] == {"ambiente": "chill"}
    assert hass.states.get("sensor.ambientes_next_song").state == "Trio Mar - Slow Tide"
    assert hass.states.get("sensor.party_time_live_streamer").state == "DJ Sol"
    assert hass.states.get("binary_sensor.party_time_live_dj").state == STATE_ON
    assert hass.states.get("binary_sensor.ambientes_live_dj").state == STATE_OFF


async def test_entities_unavailable_when_poll_fails(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.get_nowplaying.side_effect = AzuraCastConnectionError("down")
    await init_integration.runtime_data.hub.async_poll()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ambientes_listeners").state == STATE_UNAVAILABLE
    assert hass.states.get("media_player.ambientes").state == STATE_UNAVAILABLE
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_sensor_platforms.py -q
```

Expected: FAIL. `hass.states.get(...)` returns `None` (`AttributeError: 'NoneType' object has no attribute 'state'`).

- [ ] **Step 3: Implement `custom_components/azuracast/entity.py`**

```python
"""Base entity and helpers for AzuraCast entities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Hashable
from contextlib import asynccontextmanager
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api.client import AzuraCastClient
from .api.errors import AzuraCastError
from .api.models import Station
from .const import DOMAIN
from .coordinator import ManageCoordinator, ManageData
from .runtime import AzuraCastConfigEntry, StationRuntime


@asynccontextmanager
async def api_errors() -> AsyncIterator[None]:
    """Turn client errors into translated Home Assistant errors."""
    try:
        yield
    except AzuraCastError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err


class AzuraCastEntity[CoordinatorT: DataUpdateCoordinator[Any]](CoordinatorEntity[CoordinatorT]):
    """An entity that belongs to one station device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoordinatorT,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.device_key}_{key}"
        client = entry.runtime_data.client
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.device_key)},
            name=runtime.station.name,
            manufacturer="AzuraCast",
            model="Station",
            configuration_url=f"{client.base_url}/station/{runtime.station.id}/profile",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def client(self) -> AzuraCastClient:
        return self._entry.runtime_data.client

    @property
    def station(self) -> Station:
        return self._runtime.station


def track_dynamic_entities[K: Hashable](
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    coordinator: ManageCoordinator,
    *,
    platform: str,
    keys: Callable[[ManageData], set[K]],
    create: Callable[[K], AzuraCastEntity[ManageCoordinator]],
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add entities for new keys and drop registry entries of vanished keys.

    Every entity returned by ``create(key)`` must set ``self.dynamic_key = key``.
    """
    known: dict[K, str] = {}

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        current = keys(coordinator.data)
        new_entities = [create(key) for key in current - known.keys()]
        for entity in new_entities:
            known[entity.dynamic_key] = str(entity.unique_id)  # type: ignore[attr-defined]
        if new_entities:
            add_entities(new_entities)
        registry = er.async_get(hass)
        for key in known.keys() - current:
            unique_id = known.pop(key)
            if entity_id := registry.async_get_entity_id(platform, DOMAIN, unique_id):
                registry.async_remove(entity_id)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
```

- [ ] **Step 4: Implement `custom_components/azuracast/media_player.py`**

```python
"""Media player for AzuraCast stations."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.models import NowPlaying
from .coordinator import NowPlayingCoordinator
from .entity import AzuraCastEntity, api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastMediaPlayer(rt.nowplaying, entry, rt) for rt in entry.runtime_data.stations.values()
    )


class AzuraCastMediaPlayer(AzuraCastEntity[NowPlayingCoordinator], MediaPlayerEntity):
    """What a station is playing."""

    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    _attr_media_image_remotely_accessible = True

    def __init__(
        self, coordinator: NowPlayingCoordinator, entry: AzuraCastConfigEntry, runtime: StationRuntime
    ) -> None:
        super().__init__(coordinator, entry, runtime, "media_player")
        self._attr_supported_features = (
            MediaPlayerEntityFeature.NEXT_TRACK
            if runtime.manage is not None
            else MediaPlayerEntityFeature(0)
        )
        self._attr_media_position_updated_at: datetime | None = dt_util.utcnow()

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def _np(self) -> NowPlaying | None:
        return self.coordinator.data

    @property
    def state(self) -> MediaPlayerState | None:
        np = self._np
        if np is None:
            return None
        if not np.is_online:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING if np.current else MediaPlayerState.IDLE

    @property
    def media_title(self) -> str | None:
        np = self._np
        return (np.current.song.title or np.current.song.text) if np and np.current else None

    @property
    def media_artist(self) -> str | None:
        np = self._np
        return (np.current.song.artist or None) if np and np.current else None

    @property
    def media_album_name(self) -> str | None:
        np = self._np
        return (np.current.song.album or None) if np and np.current else None

    @property
    def media_image_url(self) -> str | None:
        np = self._np
        return np.current.song.art if np and np.current else None

    @property
    def media_duration(self) -> int | None:
        np = self._np
        return (np.current.duration or None) if np and np.current else None

    @property
    def media_position(self) -> int | None:
        np = self._np
        return np.current.elapsed if np and np.current else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_media_position_updated_at = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_media_next_track(self) -> None:
        async with api_errors():
            await self.client.skip_song(self.station.id)
```

- [ ] **Step 5: Implement `custom_components/azuracast/sensor.py`**

```python
"""Sensors for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .api.models import NowPlaying, Song
from .coordinator import NowPlayingCoordinator, tags_for_playlist
from .entity import AzuraCastEntity
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 0
MAX_STATE = 255


def _song_attrs(song: Song) -> dict[str, Any]:
    return {"artist": song.artist, "title": song.title, "album": song.album, "art": song.art}


@dataclass(frozen=True, kw_only=True)
class AzuraCastSensorDescription(SensorEntityDescription):
    """Describes an AzuraCast sensor."""

    value_fn: Callable[[NowPlaying], StateType]
    attrs_fn: Callable[[NowPlaying, StationRuntime], dict[str, Any] | None] = lambda np, rt: None


SENSORS: tuple[AzuraCastSensorDescription, ...] = (
    AzuraCastSensorDescription(
        key="listeners",
        translation_key="listeners",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda np: np.listeners_current,
        attrs_fn=lambda np, rt: {"unique": np.listeners_unique, "total": np.listeners_total},
    ),
    AzuraCastSensorDescription(
        key="current_song",
        translation_key="current_song",
        value_fn=lambda np: np.current.song.text[:MAX_STATE] if np.current else None,
        attrs_fn=lambda np, rt: (
            {**_song_attrs(np.current.song), "playlist": np.current.playlist, "duration": np.current.duration}
            if np.current
            else None
        ),
    ),
    AzuraCastSensorDescription(
        key="current_playlist",
        translation_key="current_playlist",
        value_fn=lambda np: np.current.playlist if np.current else None,
        attrs_fn=lambda np, rt: {
            "tags": tags_for_playlist(rt.manage, np.current.playlist if np.current else None)
        },
    ),
    AzuraCastSensorDescription(
        key="next_song",
        translation_key="next_song",
        value_fn=lambda np: np.upcoming.song.text[:MAX_STATE] if np.upcoming else None,
        attrs_fn=lambda np, rt: (
            {**_song_attrs(np.upcoming.song), "playlist": np.upcoming.playlist} if np.upcoming else None
        ),
    ),
    AzuraCastSensorDescription(
        key="live_streamer",
        translation_key="live_streamer",
        value_fn=lambda np: np.streamer_name if np.is_live else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastSensor(rt.nowplaying, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        for description in SENSORS
    )


class AzuraCastSensor(AzuraCastEntity[NowPlayingCoordinator], SensorEntity):
    """A now-playing sensor."""

    entity_description: AzuraCastSensorDescription

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> StateType:
        np = self.coordinator.data
        return self.entity_description.value_fn(np) if np else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        np = self.coordinator.data
        return self.entity_description.attrs_fn(np, self._runtime) if np else None
```

- [ ] **Step 6: Implement `custom_components/azuracast/binary_sensor.py`**

```python
"""Binary sensors for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import NowPlaying
from .coordinator import NowPlayingCoordinator
from .entity import AzuraCastEntity
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class AzuraCastBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an AzuraCast binary sensor."""

    value_fn: Callable[[NowPlaying], bool]


BINARY_SENSORS: tuple[AzuraCastBinarySensorDescription, ...] = (
    AzuraCastBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda np: np.is_online,
    ),
    AzuraCastBinarySensorDescription(
        key="live",
        translation_key="live",
        value_fn=lambda np: np.is_live,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastBinarySensor(rt.nowplaying, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        for description in BINARY_SENSORS
    )


class AzuraCastBinarySensor(AzuraCastEntity[NowPlayingCoordinator], BinarySensorEntity):
    """A now-playing binary sensor."""

    entity_description: AzuraCastBinarySensorDescription

    def __init__(
        self,
        coordinator: NowPlayingCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def is_on(self) -> bool | None:
        np = self.coordinator.data
        return self.entity_description.value_fn(np) if np else None
```

- [ ] **Step 7: Register the platforms in `custom_components/azuracast/__init__.py`**

Replace `PLATFORMS: list[Platform] = []` with:

```python
PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.MEDIA_PLAYER, Platform.SENSOR]
```

- [ ] **Step 8: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_sensor_platforms.py tests/test_init.py -q
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add custom_components/azuracast tests/test_sensor_platforms.py
git commit -m "feat: add media player, sensors and binary sensors per station"
```

---

### Task 12: Playlist switches and buttons

**Files:**
- Create: `custom_components/azuracast/switch.py`, `custom_components/azuracast/button.py`
- Modify: `custom_components/azuracast/__init__.py` (`PLATFORMS`)
- Test: `tests/test_switch_button.py`

**Interfaces:**
- Consumes: `AzuraCastEntity`, `api_errors` and `track_dynamic_entities` (Task 11), `ManageCoordinator` (Task 7) and `playlist_tags` (Task 6).
- Produces: `PlaylistSwitch(coordinator, entry, runtime, playlist_id: int)` (with `.dynamic_key = playlist_id`), `AzuraCastButton`, and the unique ID keys `playlist_{id}`, `skip_song` and `clear_queue`.

- [ ] **Step 1: Write the failing tests `tests/test_switch_button.py`**

```python
"""Tests for playlist switches and buttons."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import AzuraCastResponseError
from custom_components.azuracast.api.models import Playlist

from .common import playlists


async def test_playlist_switches(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    chill = hass.states.get("switch.ambientes_playlist_tarde_chill")
    assert chill.state == STATE_ON
    assert chill.attributes["playlist_id"] == 10
    assert chill.attributes["tags"] == {"ambiente": "chill"}
    assert chill.attributes["schedule"] == [
        {"start": "12:00", "end": "18:00", "days": [], "start_date": None, "end_date": None}
    ]
    assert hass.states.get("switch.ambientes_playlist_tarde_lounge").state == STATE_OFF
    assert hass.states.get("switch.party_time_playlist_party_mix") is None

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_lounge"}, blocking=True
    )
    mock_client.set_playlist_enabled.assert_awaited_with(1, 12, True)
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_chill"}, blocking=True
    )
    mock_client.set_playlist_enabled.assert_awaited_with(1, 10, False)


async def test_switch_error(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    mock_client.set_playlist_enabled.side_effect = AzuraCastResponseError(500, "boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.ambientes_playlist_tarde_lounge"}, blocking=True
        )


async def test_playlists_added_and_removed(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    new = Playlist.from_dict({"id": 15, "name": "manana-cafe", "description": "ha:ambiente=cafe", "is_enabled": False})
    mock_client.get_playlists.side_effect = None
    mock_client.get_playlists.return_value = [p for p in playlists() if p.id != 14] + [new]
    await init_integration.runtime_data.stations[1].manage.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("switch.ambientes_playlist_manana_cafe") is not None
    assert entity_registry.async_get("switch.ambientes_playlist_respaldo") is None


async def test_buttons(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: "button.ambientes_skip_song"}, blocking=True
    )
    mock_client.skip_song.assert_awaited_once_with(1)
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: "button.ambientes_clear_queue"}, blocking=True
    )
    mock_client.clear_queue.assert_awaited_once_with(1)
    assert hass.states.get("button.party_time_skip_song") is None
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_switch_button.py -q
```

Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'state'`.

- [ ] **Step 3: Implement `custom_components/azuracast/switch.py`**

```python
"""Playlist switches for AzuraCast stations."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.models import Playlist
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, api_errors, track_dynamic_entities
from .runtime import AzuraCastConfigEntry, StationRuntime
from .selectors import playlist_tags

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for runtime in entry.runtime_data.stations.values():
        if (coordinator := runtime.manage) is None:
            continue
        track_dynamic_entities(
            hass,
            entry,
            coordinator,
            platform=SWITCH_DOMAIN,
            keys=lambda data: {p.id for p in data.playlists},
            create=lambda pid, c=coordinator, rt=runtime: PlaylistSwitch(c, entry, rt, pid),
            add_entities=async_add_entities,
        )


class PlaylistSwitch(AzuraCastEntity[ManageCoordinator], SwitchEntity):
    """Whether a playlist is enabled."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "playlist"

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        playlist_id: int,
    ) -> None:
        super().__init__(coordinator, entry, runtime, f"playlist_{playlist_id}")
        self.dynamic_key = playlist_id
        self.playlist_id = playlist_id
        playlist = self._playlist
        self._attr_translation_placeholders = {"name": playlist.name if playlist else str(playlist_id)}

    @property
    def _playlist(self) -> Playlist | None:
        return next((p for p in self.coordinator.data.playlists if p.id == self.playlist_id), None)

    @property
    def available(self) -> bool:
        return super().available and self._playlist is not None

    @property
    def is_on(self) -> bool:
        playlist = self._playlist
        return bool(playlist and playlist.is_enabled)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        playlist = self._playlist
        if playlist is None:
            return None
        return {
            "playlist_id": playlist.id,
            "type": playlist.type,
            "order": playlist.order,
            "weight": playlist.weight,
            "schedule": [item.as_dict() for item in playlist.schedule_items],
            "tags": playlist_tags(playlist),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        async with api_errors():
            await self.client.set_playlist_enabled(self.station.id, self.playlist_id, enabled)
        await self.coordinator.async_refresh()
```

- [ ] **Step 4: Implement `custom_components/azuracast/button.py`**

```python
"""Buttons for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.client import AzuraCastClient
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class AzuraCastButtonDescription(ButtonEntityDescription):
    """Describes an AzuraCast button."""

    press_fn: Callable[[AzuraCastClient, int], Awaitable[Any]]


BUTTONS: tuple[AzuraCastButtonDescription, ...] = (
    AzuraCastButtonDescription(
        key="skip_song", translation_key="skip_song", press_fn=lambda c, sid: c.skip_song(sid)
    ),
    AzuraCastButtonDescription(
        key="clear_queue", translation_key="clear_queue", press_fn=lambda c, sid: c.clear_queue(sid)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        AzuraCastButton(rt.manage, entry, rt, description)
        for rt in entry.runtime_data.stations.values()
        if rt.manage is not None
        for description in BUTTONS
    )


class AzuraCastButton(AzuraCastEntity[ManageCoordinator], ButtonEntity):
    """A station action."""

    entity_description: AzuraCastButtonDescription

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        description: AzuraCastButtonDescription,
    ) -> None:
        super().__init__(coordinator, entry, runtime, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        async with api_errors():
            await self.entity_description.press_fn(self.client, self.station.id)
        await self.coordinator.async_refresh()
```

- [ ] **Step 5: Register the platforms**

In `custom_components/azuracast/__init__.py`:

```python
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SENSOR,
    Platform.SWITCH,
]
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_switch_button.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add custom_components/azuracast tests/test_switch_button.py
git commit -m "feat: add playlist switches and skip/clear-queue buttons"
```

---

### Task 13: Selector select entities

**Files:**
- Create: `custom_components/azuracast/select.py`
- Modify: `custom_components/azuracast/__init__.py` (`PLATFORMS`)
- Test: `tests/test_select.py`

**Interfaces:**
- Consumes: `selector_state`, `available_values`, `plan_apply` and `execute_plan` (Task 6), `station_time` (Task 5), `track_dynamic_entities` (Task 11), and `EVENT_SELECTOR_APPLIED` and `PENDING_TIMEOUT`.
- Produces: `SelectorSelect(coordinator, entry, runtime, key: str)` with `.dynamic_key = key`. Attributes: `reason`, `available_now`, `pending_value` and `playlists`. Event: `azuracast_selector_applied`.

- [ ] **Step 1: Write the failing tests `tests/test_select.py`**

```python
"""Tests for selector select entities."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, call

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.select import ATTR_OPTION, DOMAIN as SELECT_DOMAIN, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from homeassistant.util import dt as dt_util

from custom_components.azuracast.api.errors import AzuraCastResponseError
from custom_components.azuracast.const import EVENT_SELECTOR_APPLIED

from .common import make_nowplaying

ENTITY = "select.ambientes_ambiente"


@pytest.fixture(autouse=True)
def afternoon(freezer: FrozenDateTimeFactory) -> None:
    """14:00 in Costa Rica: tarde-* playlists are scheduled."""
    freezer.move_to("2026-11-20 20:00:00+00:00")


async def _select(hass: HomeAssistant, option: str) -> None:
    await hass.services.async_call(
        SELECT_DOMAIN, SERVICE_SELECT_OPTION, {ATTR_ENTITY_ID: ENTITY, ATTR_OPTION: option}, blocking=True
    )


async def test_state_and_attributes(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    state = hass.states.get(ENTITY)
    assert state.state == "chill"
    assert state.attributes["options"] == ["chill", "fiesta", "lounge"]
    assert state.attributes["available_now"] == ["chill", "lounge"]
    assert state.attributes["reason"] is None
    assert state.attributes["pending_value"] is None
    assert state.attributes["playlists"]["chill"] == ["noche-chill", "tarde-chill"]
    assert hass.states.get("select.party_time_ambiente") is None


async def test_select_applies_plan(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    events = async_capture_events(hass, EVENT_SELECTOR_APPLIED)
    await _select(hass, "lounge")
    assert mock_client.set_playlist_enabled.await_args_list == [
        call(1, 12, True),
        call(1, 10, False),
        call(1, 11, False),
    ]
    mock_client.clear_queue.assert_awaited_once_with(1)
    assert events[0].data == {
        "station_id": 1,
        "selector": "ambiente",
        "value": "lounge",
        "previous_value": "chill",
        "cleared_queue": True,
    }
    assert hass.states.get(ENTITY).attributes["pending_value"] == "lounge"


async def test_pending_clears_when_song_from_value_starts(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    await _select(hass, "lounge")
    nowplaying = init_integration.runtime_data.stations[1].nowplaying
    nowplaying.push(make_nowplaying(0, song_id="l1", playlist="tarde-lounge"))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_pending_times_out(
    hass: HomeAssistant, init_integration: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    await _select(hass, "lounge")
    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["pending_value"] is None


async def test_unavailable_value_rejected(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await _select(hass, "fiesta")
    assert info.value.translation_key == "value_not_available_now"
    mock_client.set_playlist_enabled.assert_not_called()


async def test_unavailable_value_allowed_when_configured(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    init_integration.runtime_data.stations[1].reject_unavailable_values = False
    await _select(hass, "fiesta")
    mock_client.set_playlist_enabled.assert_any_await(1, 13, True)


async def test_same_value_is_noop(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    await _select(hass, "chill")
    mock_client.set_playlist_enabled.assert_not_called()
    mock_client.clear_queue.assert_not_called()


async def test_keep_queue_option(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    init_integration.runtime_data.stations[1].clear_queue_on_change = False
    await _select(hass, "lounge")
    mock_client.clear_queue.assert_not_called()


async def test_api_failure_refreshes_and_raises(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    mock_client.set_playlist_enabled.side_effect = AzuraCastResponseError(500, "boom")
    mock_client.get_playlists.reset_mock()
    with pytest.raises(HomeAssistantError) as info:
        await _select(hass, "lounge")
    assert info.value.translation_key == "selector_apply_failed"
    assert mock_client.get_playlists.await_count >= 1
    assert hass.states.get(ENTITY).attributes["pending_value"] is None
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_select.py -q
```

Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'state'`.

- [ ] **Step 3: Implement `custom_components/azuracast/select.py`**

```python
"""Selector select entities for AzuraCast stations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.select import DOMAIN as SELECT_DOMAIN, SelectEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api.errors import AzuraCastError
from .const import DOMAIN, EVENT_SELECTOR_APPLIED, LOGGER, PENDING_TIMEOUT
from .coordinator import ManageCoordinator
from .entity import AzuraCastEntity, track_dynamic_entities
from .runtime import AzuraCastConfigEntry, StationRuntime
from .schedule import station_time
from .selectors import Selector, available_values, execute_plan, plan_apply, selector_state

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AzuraCastConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for runtime in entry.runtime_data.stations.values():
        if (coordinator := runtime.manage) is None:
            continue
        track_dynamic_entities(
            hass,
            entry,
            coordinator,
            platform=SELECT_DOMAIN,
            keys=lambda data: {key for key, selector in data.selectors.items() if selector.members},
            create=lambda key, c=coordinator, rt=runtime: SelectorSelect(c, entry, rt, key),
            add_entities=async_add_entities,
        )


class SelectorSelect(AzuraCastEntity[ManageCoordinator], SelectEntity):
    """Chooses which value of a tag selector is enabled."""

    _attr_translation_key = "selector"

    def __init__(
        self,
        coordinator: ManageCoordinator,
        entry: AzuraCastConfigEntry,
        runtime: StationRuntime,
        key: str,
    ) -> None:
        super().__init__(coordinator, entry, runtime, f"selector_{key}")
        self.dynamic_key = key
        self.selector_key = key
        self._attr_translation_placeholders = {"selector": key}
        self._pending: str | None = None
        self._cancel_pending: CALLBACK_TYPE | None = None

    @property
    def _selector(self) -> Selector | None:
        return self.coordinator.data.selectors.get(self.selector_key)

    def _now(self) -> datetime:
        return station_time(self.station.timezone, dt_util.utcnow(), dt_util.get_default_time_zone())

    @property
    def available(self) -> bool:
        selector = self._selector
        return super().available and selector is not None and bool(selector.members)

    @property
    def options(self) -> list[str]:
        selector = self._selector
        return selector.options if selector else []

    @property
    def current_option(self) -> str | None:
        selector = self._selector
        return selector_state(selector).value if selector else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        selector = self._selector
        if selector is None:
            return None
        return {
            "reason": selector_state(selector).reason,
            "available_now": available_values(selector, self._now()),
            "pending_value": self._pending,
            "playlists": {
                value: sorted(p.name for p in items) for value, items in selector.members.items()
            },
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._runtime.nowplaying.async_add_listener(self._handle_nowplaying))
        self.async_on_remove(self._clear_pending_timer)

    async def async_select_option(self, option: str) -> None:
        selector = self._selector
        if selector is None or option not in selector.members:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_option",
                translation_placeholders={"option": option, "selector": self.selector_key},
            )
        if self._runtime.reject_unavailable_values and option not in available_values(
            selector, self._now()
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="value_not_available_now",
                translation_placeholders={"value": option, "selector": self.selector_key},
            )
        previous = selector_state(selector).value
        plan = plan_apply(selector, option)
        if plan.is_empty and previous == option:
            return
        clear_queue = self._runtime.clear_queue_on_change
        try:
            await execute_plan(self.client, self.station.id, plan, clear_queue=clear_queue)
        except AzuraCastError as err:
            await self.coordinator.async_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="selector_apply_failed",
                translation_placeholders={
                    "selector": self.selector_key,
                    "value": option,
                    "error": str(err),
                },
            ) from err
        self._set_pending(option)
        self.hass.bus.async_fire(
            EVENT_SELECTOR_APPLIED,
            {
                "station_id": self.station.id,
                "selector": self.selector_key,
                "value": option,
                "previous_value": previous,
                "cleared_queue": clear_queue,
            },
        )
        await self.coordinator.async_refresh()

    @callback
    def _set_pending(self, value: str) -> None:
        self._clear_pending_timer()
        self._pending = value
        self._cancel_pending = async_call_later(self.hass, PENDING_TIMEOUT, self._pending_expired)
        self.async_write_ha_state()

    @callback
    def _clear_pending_timer(self) -> None:
        if self._cancel_pending is not None:
            self._cancel_pending()
            self._cancel_pending = None

    @callback
    def _pending_expired(self, _now: datetime) -> None:
        self._cancel_pending = None
        LOGGER.warning(
            "%s: %s=%s was applied but no song from it started within %s",
            self.station.name,
            self.selector_key,
            self._pending,
            PENDING_TIMEOUT,
        )
        self._pending = None
        self.async_write_ha_state()

    @callback
    def _handle_nowplaying(self) -> None:
        np = self._runtime.nowplaying.data
        selector = self._selector
        if self._pending is None or np is None or np.current is None or selector is None:
            return
        if np.current.playlist in selector.playlist_names(self._pending):
            self._clear_pending_timer()
            self._pending = None
            self.async_write_ha_state()
```

- [ ] **Step 4: Register the platform**

In `custom_components/azuracast/__init__.py`, add `Platform.SELECT`. Keep the list alphabetical:

```python
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_select.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast tests/test_select.py
git commit -m "feat: add tag selector select entities with pending feedback"
```

---

### Task 14: Services

**Files:**
- Replace: `custom_components/azuracast/services.py`
- Create: `custom_components/azuracast/services.yaml`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: `AzuraCastRuntime` and `StationRuntime` (Task 9), `api_errors` (Task 11) and the client methods (Task 3).
- Produces: the services `azuracast.skip_song`, `azuracast.clear_queue`, `azuracast.set_playlists_enabled` (`playlists: list[str]`, `enabled: bool`) and `azuracast.reshuffle_playlist` (`playlist: str`). Each one targets devices or entities.

- [ ] **Step 1: Write the failing tests `tests/test_services.py`**

```python
"""Tests for integration services."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.const import DOMAIN


def _device(device_registry: dr.DeviceRegistry, entry: MockConfigEntry, station_id: int) -> str:
    device = device_registry.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_{station_id}")})
    assert device is not None
    return device.id


async def test_skip_song_by_device(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, device_registry: dr.DeviceRegistry
) -> None:
    await hass.services.async_call(
        DOMAIN, "skip_song", {ATTR_DEVICE_ID: _device(device_registry, init_integration, 1)}, blocking=True
    )
    mock_client.skip_song.assert_awaited_once_with(1)


async def test_clear_queue_by_entity(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    await hass.services.async_call(
        DOMAIN, "clear_queue", {ATTR_ENTITY_ID: "sensor.ambientes_listeners"}, blocking=True
    )
    mock_client.clear_queue.assert_awaited_once_with(1)


async def test_read_only_station_rejected(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock, device_registry: dr.DeviceRegistry
) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN, "skip_song", {ATTR_DEVICE_ID: _device(device_registry, init_integration, 2)}, blocking=True
        )
    assert info.value.translation_key == "station_not_managed"
    mock_client.skip_song.assert_not_called()


async def test_set_playlists_enabled(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    await hass.services.async_call(
        DOMAIN,
        "set_playlists_enabled",
        {ATTR_ENTITY_ID: "media_player.ambientes", "playlists": ["tarde-lounge", "13"], "enabled": True},
        blocking=True,
    )
    assert [c.args for c in mock_client.set_playlist_enabled.await_args_list] == [(1, 12, True), (1, 13, True)]


async def test_unknown_playlist(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "set_playlists_enabled",
            {ATTR_ENTITY_ID: "media_player.ambientes", "playlists": ["nope"], "enabled": False},
            blocking=True,
        )
    assert info.value.translation_key == "unknown_playlist"
    mock_client.set_playlist_enabled.assert_not_called()


async def test_reshuffle(hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock) -> None:
    await hass.services.async_call(
        DOMAIN, "reshuffle_playlist", {ATTR_ENTITY_ID: "media_player.ambientes", "playlist": "10"}, blocking=True
    )
    mock_client.reshuffle_playlist.assert_awaited_once_with(1, 10)


async def test_no_target(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "skip_song", {ATTR_ENTITY_ID: "sun.sun"}, blocking=True
        )
```

- [ ] **Step 2: Run them to verify they fail**

```bash
.venv/bin/pytest tests/test_services.py -q
```

Expected: FAIL with `ServiceNotFound: Action azuracast.skip_song not found`.

- [ ] **Step 3: Implement `custom_components/azuracast/services.py`**

```python
"""Services of the AzuraCast integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
import probatio

from .api.client import AzuraCastClient
from .api.models import Playlist
from .const import DOMAIN
from .coordinator import ManageCoordinator
from .entity import api_errors
from .runtime import AzuraCastConfigEntry, StationRuntime

SERVICE_SKIP_SONG = "skip_song"
SERVICE_CLEAR_QUEUE = "clear_queue"
SERVICE_SET_PLAYLISTS_ENABLED = "set_playlists_enabled"
SERVICE_RESHUFFLE_PLAYLIST = "reshuffle_playlist"
ATTR_PLAYLISTS = "playlists"
ATTR_PLAYLIST = "playlist"
ATTR_ENABLED = "enabled"

TARGET_SCHEMA = cv.make_entity_service_schema({})
SET_PLAYLISTS_SCHEMA = cv.make_entity_service_schema(
    {
        probatio.Required(ATTR_PLAYLISTS): probatio.All(cv.ensure_list, [cv.string]),
        probatio.Required(ATTR_ENABLED): cv.boolean,
    }
)
RESHUFFLE_SCHEMA = cv.make_entity_service_schema({probatio.Required(ATTR_PLAYLIST): cv.string})

type Target = tuple[AzuraCastClient, StationRuntime, ManageCoordinator]


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""
    hass.services.async_register(DOMAIN, SERVICE_SKIP_SONG, _async_skip_song, schema=TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_QUEUE, _async_clear_queue, schema=TARGET_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SET_PLAYLISTS_ENABLED, _async_set_playlists_enabled, schema=SET_PLAYLISTS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESHUFFLE_PLAYLIST, _async_reshuffle_playlist, schema=RESHUFFLE_SCHEMA
    )


def _targets(hass: HomeAssistant, call: ServiceCall) -> list[Target]:
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device_ids = set(selected.referenced_devices)
    for entity_id in selected.referenced | selected.indirectly_referenced:
        entity = entity_registry.async_get(entity_id)
        if entity is not None and entity.platform == DOMAIN and entity.device_id:
            device_ids.add(entity.device_id)
    keys = {
        ident
        for device_id in device_ids
        if (device := device_registry.async_get(device_id)) is not None
        for domain, ident in device.identifiers
        if domain == DOMAIN
    }
    targets: list[Target] = []
    entries: list[AzuraCastConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    for entry in entries:
        for runtime in entry.runtime_data.stations.values():
            if runtime.device_key not in keys:
                continue
            if runtime.manage is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="station_not_managed",
                    translation_placeholders={"station": runtime.station.name},
                )
            targets.append((entry.runtime_data.client, runtime, runtime.manage))
    if not targets:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_station_targeted")
    return targets


def _find_playlist(runtime: StationRuntime, manage: ManageCoordinator, ref: str) -> Playlist:
    for playlist in manage.data.playlists:
        if playlist.name == ref or str(playlist.id) == ref:
            return playlist
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_playlist",
        translation_placeholders={"playlist": ref, "station": runtime.station.name},
    )


async def _async_skip_song(call: ServiceCall) -> None:
    for client, runtime, _ in _targets(call.hass, call):
        async with api_errors():
            await client.skip_song(runtime.station.id)


async def _async_clear_queue(call: ServiceCall) -> None:
    for client, runtime, manage in _targets(call.hass, call):
        async with api_errors():
            await client.clear_queue(runtime.station.id)
        await manage.async_refresh()


async def _async_set_playlists_enabled(call: ServiceCall) -> None:
    work = [
        (client, runtime, manage, [_find_playlist(runtime, manage, ref) for ref in call.data[ATTR_PLAYLISTS]])
        for client, runtime, manage in _targets(call.hass, call)
    ]
    for client, runtime, manage, playlists in work:
        async with api_errors():
            for playlist in playlists:
                await client.set_playlist_enabled(runtime.station.id, playlist.id, call.data[ATTR_ENABLED])
        await manage.async_refresh()


async def _async_reshuffle_playlist(call: ServiceCall) -> None:
    work = [
        (client, runtime, manage, _find_playlist(runtime, manage, call.data[ATTR_PLAYLIST]))
        for client, runtime, manage in _targets(call.hass, call)
    ]
    for client, runtime, manage, playlist in work:
        async with api_errors():
            await client.reshuffle_playlist(runtime.station.id, playlist.id)
        await manage.async_refresh()
```

- [ ] **Step 4: Create `custom_components/azuracast/services.yaml`**

```yaml
skip_song:
  target:
    device:
      integration: azuracast
    entity:
      integration: azuracast
clear_queue:
  target:
    device:
      integration: azuracast
    entity:
      integration: azuracast
set_playlists_enabled:
  target:
    device:
      integration: azuracast
    entity:
      integration: azuracast
  fields:
    playlists:
      required: true
      example: "tarde-chill"
      selector:
        text:
          multiple: true
    enabled:
      required: true
      default: true
      selector:
        boolean:
reshuffle_playlist:
  target:
    device:
      integration: azuracast
    entity:
      integration: azuracast
  fields:
    playlist:
      required: true
      example: "tarde-chill"
      selector:
        text:
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/pytest tests/test_services.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/azuracast/services.py custom_components/azuracast/services.yaml tests/test_services.py
git commit -m "feat: add skip, clear queue, bulk playlist and reshuffle services"
```

---

### Task 15: Diagnostics

**Files:**
- Create: `custom_components/azuracast/diagnostics.py`
- Test: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: the runtime (Task 9), `selector_state` (Task 6) and the hub flags (Task 8).
- Produces: `async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing test `tests/test_diagnostics.py`**

```python
"""Tests for diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_and_reports(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
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
```

- [ ] **Step 2: Run it to verify it fails**

```bash
.venv/bin/pytest tests/test_diagnostics.py -q
```

Expected: collection error `No module named 'custom_components.azuracast.diagnostics'`.

- [ ] **Step 3: Implement `custom_components/azuracast/diagnostics.py`**

```python
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
        "clear_queue_on_change": runtime.clear_queue_on_change,
        "reject_unavailable_values": runtime.reject_unavailable_values,
        "nowplaying_ok": runtime.nowplaying.last_update_success,
        "online": np.is_online if np else None,
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
```

- [ ] **Step 4: Run it to verify it passes**

```bash
.venv/bin/pytest tests/test_diagnostics.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add custom_components/azuracast/diagnostics.py tests/test_diagnostics.py
git commit -m "feat: add diagnostics with redacted URL and key"
```

---

### Task 16: Spanish translation, icons, docs, quality scale and CI

**Files:**
- Create: `custom_components/azuracast/translations/es.json`, `custom_components/azuracast/icons.json`, `custom_components/azuracast/quality_scale.yaml`
- Create: `README.md`, `README.es.md`, `CHANGELOG.md`, `LICENSE`
- Create: `.github/workflows/ci.yml`, `.github/workflows/validate.yml`, `.github/workflows/release.yml`

**Interfaces:**
- Consumes: the translation keys from `strings.json` (Task 9). `es.json` must have exactly the same key tree.

- [ ] **Step 1: Write the test that guards the translation key trees**

Create `tests/test_translations.py`:

```python
"""Translation files must share the same keys."""

from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).parents[1] / "custom_components" / "azuracast"


def _keys(data: object, prefix: str = "") -> set[str]:
    if not isinstance(data, dict):
        return {prefix}
    return {k for key, value in data.items() for k in _keys(value, f"{prefix}.{key}")}


def test_translation_trees_match() -> None:
    strings = json.loads((BASE / "strings.json").read_text())
    for name in ("en.json", "es.json"):
        other = json.loads((BASE / "translations" / name).read_text())
        assert _keys(other) == _keys(strings), name


def test_en_matches_strings() -> None:
    assert json.loads((BASE / "translations" / "en.json").read_text()) == json.loads(
        (BASE / "strings.json").read_text()
    )
```

Run `.venv/bin/pytest tests/test_translations.py -q`. Expected: FAIL with `FileNotFoundError` for `es.json`.

- [ ] **Step 2: Create `custom_components/azuracast/translations/es.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Conectar con AzuraCast",
        "description": "Escribe la dirección de tu instancia de AzuraCast. La API key es opcional: sin ella solo obtienes datos de lo que suena; con ella también puedes gestionar playlists, la cola y saltar canciones.",
        "data": {
          "url": "URL",
          "api_key": "API key",
          "verify_ssl": "Verificar el certificado SSL"
        },
        "data_description": {
          "url": "Dirección base, por ejemplo https://radio.example.com",
          "api_key": "Créala en AzuraCast en Mi cuenta → API Keys. Usa un usuario dedicado con permisos solo sobre las estaciones que Home Assistant debe gestionar.",
          "verify_ssl": "Desactívalo solo para instancias con certificado autofirmado."
        }
      },
      "reauth_confirm": {
        "title": "Actualizar la API key de AzuraCast",
        "description": "AzuraCast ya no acepta la API key de {name}. Escribe una nueva.",
        "data": {
          "api_key": "API key"
        }
      },
      "reconfigure": {
        "title": "Reconfigurar AzuraCast",
        "data": {
          "url": "URL",
          "api_key": "API key",
          "verify_ssl": "Verificar el certificado SSL"
        },
        "data_description": {
          "url": "Dirección base, por ejemplo https://radio.example.com",
          "api_key": "Déjala vacía para usar la instancia solo en lectura.",
          "verify_ssl": "Desactívalo solo para instancias con certificado autofirmado."
        }
      }
    },
    "error": {
      "cannot_connect": "No se pudo conectar con AzuraCast en esta dirección.",
      "invalid_auth": "AzuraCast rechazó la API key.",
      "invalid_url": "Escribe una dirección completa que empiece con http:// o https://.",
      "unknown": "Error inesperado."
    },
    "abort": {
      "already_configured": "Esta instancia de AzuraCast ya está configurada.",
      "reauth_successful": "La API key se actualizó.",
      "reconfigure_successful": "La configuración se actualizó."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Opciones de AzuraCast",
        "data": {
          "excluded_stations": "Estaciones a ignorar",
          "keep_queue_stations": "Mantener la cola al cambiar un selector",
          "allow_unavailable_stations": "Aceptar valores de selector sin nada programado ahora",
          "scan_interval": "Intervalo de actualización de playlists"
        },
        "data_description": {
          "excluded_stations": "Estas estaciones no tendrán dispositivo ni entidades.",
          "keep_queue_stations": "Por defecto, al cambiar un selector se vacía la cola para que el cambio se oiga después de la canción actual. Las estaciones de esta lista conservan su cola.",
          "allow_unavailable_stations": "Por defecto, se rechaza un valor de selector si ninguna de sus playlists puede sonar ahora. Las estaciones de esta lista lo aceptan igual.",
          "scan_interval": "Cada cuántos segundos se actualizan las playlists y la cola."
        }
      }
    },
    "abort": {
      "not_loaded": "Carga la integración antes de cambiar sus opciones.",
      "cannot_connect": "No se pudo conectar con AzuraCast para listar las estaciones."
    }
  },
  "entity": {
    "sensor": {
      "listeners": {"name": "Oyentes"},
      "current_song": {"name": "Canción actual"},
      "current_playlist": {"name": "Playlist actual"},
      "next_song": {"name": "Siguiente canción"},
      "live_streamer": {"name": "Locutor en vivo"}
    },
    "binary_sensor": {
      "online": {"name": "En línea"},
      "live": {"name": "DJ en vivo"}
    },
    "switch": {
      "playlist": {"name": "Playlist {name}"}
    },
    "button": {
      "skip_song": {"name": "Saltar canción"},
      "clear_queue": {"name": "Vaciar cola"}
    },
    "select": {
      "selector": {"name": "{selector}"}
    }
  },
  "exceptions": {
    "auth_failed": {"message": "AzuraCast rechazó la API key."},
    "cannot_connect": {"message": "No se pudo conectar con AzuraCast: {error}"},
    "update_failed": {"message": "Error al actualizar los datos de AzuraCast: {error}"},
    "missing_permissions": {"message": "La API key no tiene permisos de gestión en la estación {station}."},
    "api_error": {"message": "AzuraCast devolvió un error: {error}"},
    "unknown_option": {"message": "{option} no es un valor del selector {selector}."},
    "value_not_available_now": {"message": "Ninguna playlist con {selector}={value} puede sonar en este momento."},
    "selector_apply_failed": {"message": "No se pudo aplicar {selector}={value}: {error}"},
    "no_station_targeted": {"message": "Selecciona al menos una estación de AzuraCast."},
    "station_not_managed": {"message": "La estación {station} es de solo lectura: la API key no puede gestionarla."},
    "unknown_playlist": {"message": "La estación {station} no tiene una playlist llamada {playlist}."}
  },
  "issues": {
    "sse_unavailable": {
      "title": "Actualizaciones en tiempo real no disponibles en {instance}",
      "description": "Home Assistant no pudo abrir el stream de lo que suena en AzuraCast, así que los cambios de canción llegan hasta 30 segundos tarde. En AzuraCast, activa \"Use High-Performance Now Playing Updates\" en Administración → Configuración del sistema y recarga la integración."
    },
    "missing_permissions": {
      "title": "Sin permisos de gestión en {station}",
      "description": "La API key usada para {instance} ya no puede gestionar la estación {station}. Dale permisos sobre esa estación al usuario de AzuraCast, o exclúyela en las opciones de la integración."
    },
    "conflicting_tags": {
      "title": "Tags en conflicto en {station}",
      "description": "Estas playlists tienen dos valores para el mismo selector, así que el selector las ignora: {playlists}. Deja un solo tag ha:selector=valor por selector en la descripción de cada playlist."
    }
  },
  "services": {
    "skip_song": {
      "name": "Saltar canción",
      "description": "Salta la canción que suena en las estaciones elegidas."
    },
    "clear_queue": {
      "name": "Vaciar cola",
      "description": "Quita todas las canciones próximas de la cola del AutoDJ de las estaciones elegidas."
    },
    "set_playlists_enabled": {
      "name": "Activar o desactivar playlists",
      "description": "Activa o desactiva varias playlists de las estaciones elegidas a la vez.",
      "fields": {
        "playlists": {"name": "Playlists", "description": "Nombres o IDs numéricos de las playlists."},
        "enabled": {"name": "Activadas", "description": "Si las playlists deben quedar activadas."}
      }
    },
    "reshuffle_playlist": {
      "name": "Rebarajar playlist",
      "description": "Vuelve a mezclar el orden de las canciones de una playlist.",
      "fields": {
        "playlist": {"name": "Playlist", "description": "Nombre o ID numérico de la playlist."}
      }
    }
  }
}
```

- [ ] **Step 3: Create `custom_components/azuracast/icons.json`**

```json
{
  "entity": {
    "sensor": {
      "listeners": {"default": "mdi:account-multiple"},
      "current_song": {"default": "mdi:music-note"},
      "current_playlist": {"default": "mdi:playlist-music"},
      "next_song": {"default": "mdi:skip-next-circle-outline"},
      "live_streamer": {"default": "mdi:microphone"}
    },
    "binary_sensor": {
      "live": {"default": "mdi:microphone-off", "state": {"on": "mdi:microphone"}}
    },
    "switch": {
      "playlist": {"default": "mdi:playlist-remove", "state": {"on": "mdi:playlist-play"}}
    },
    "button": {
      "skip_song": {"default": "mdi:skip-next"},
      "clear_queue": {"default": "mdi:playlist-minus"}
    },
    "select": {
      "selector": {"default": "mdi:tune-variant"}
    }
  },
  "services": {
    "skip_song": {"service": "mdi:skip-next"},
    "clear_queue": {"service": "mdi:playlist-minus"},
    "set_playlists_enabled": {"service": "mdi:playlist-check"},
    "reshuffle_playlist": {"service": "mdi:shuffle-variant"}
  }
}
```

- [ ] **Step 4: Create `custom_components/azuracast/quality_scale.yaml`**

```yaml
rules:
  # Bronze
  action-setup: done
  appropriate-polling: done
  brands:
    status: todo
    comment: Brand images still to be designed; required before the HACS default-store submission.
  common-modules: done
  config-flow: done
  config-flow-test-coverage: done
  dependency-transparency:
    status: exempt
    comment: No third-party requirements; the client ships inside the integration.
  docs-actions: done
  docs-high-level-description: done
  docs-installation-instructions: done
  docs-removal-instructions: done
  entity-event-setup: done
  entity-unique-id: done
  has-entity-name: done
  runtime-data: done
  test-before-configure: done
  test-before-setup: done
  unique-config-entry: done
  # Silver
  action-exceptions: done
  config-entry-unloading: done
  docs-configuration-parameters: done
  docs-installation-parameters: done
  entity-unavailable: done
  integration-owner: done
  log-when-unavailable: done
  parallel-updates: done
  reauthentication-flow: done
  test-coverage: done
  # Gold
  devices: done
  diagnostics: done
  discovery:
    status: exempt
    comment: AzuraCast does not announce itself on the network.
  discovery-update-info:
    status: exempt
    comment: No discovery.
  docs-data-update: done
  docs-examples: done
  docs-known-limitations: done
  docs-supported-devices: done
  docs-supported-functions: done
  docs-troubleshooting: done
  docs-use-cases: done
  dynamic-devices:
    status: todo
    comment: New playlists and selectors appear without a reload; new stations need a reload.
  entity-category: done
  entity-device-class: done
  entity-disabled-by-default:
    status: exempt
    comment: Every entity is a primary station value.
  entity-translations: done
  exception-translations: done
  icon-translations: done
  reconfiguration-flow: done
  repair-issues: done
  stale-devices: done
  # Platinum
  async-dependency: done
  inject-websession: done
  strict-typing: todo
```

- [ ] **Step 5: Write `README.md`**

````markdown
# AzuraCast for Home Assistant

[![CI](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml/badge.svg)](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Brings every station of an [AzuraCast](https://www.azuracast.com/) instance into Home Assistant: what is playing, listeners, live DJs, playlists, the queue, skipping songs and **tag selectors** that switch groups of playlists with one action.

[Español](README.es.md)

## Features

- **One device per station** with a media player (now playing, artwork, progress, skip), sensors (listeners, current song, current playlist, next song, live streamer) and binary sensors (online, live DJ).
- **Real time.** Song changes arrive through AzuraCast's high-performance now-playing stream. If it is off, the integration polls every 30 seconds and tells you how to turn it on.
- **Playlists as switches**, with their type, weight, schedule and tags as attributes.
- **Skip song** and **clear queue** buttons, plus services to skip, clear the queue, enable or disable several playlists at once and reshuffle a playlist.
- **Tag selectors.** Write `ha:<selector>=<value>` in a playlist's description, for example `ha:ambiente=chill`. Each station gets a `select` with every value it finds. Choosing a value enables that value's playlists, disables the others of the same selector and clears the queue, so the change is heard when the current song ends. AzuraCast's own schedules keep deciding which of them plays at each hour.
- **Events** for automations: `azuracast_song_changed` and `azuracast_selector_applied`.
- Works **without an API key** (read-only public data) or with one (management). Stations the key cannot manage stay read-only.

## Requirements

- Home Assistant 2026.10 or newer.
- AzuraCast 0.23 or newer.

## Installation

### HACS

1. In HACS, open ⋮ → **Custom repositories** and add `https://github.com/NaturalDevCR/ha-azuracast` as an **Integration**.
2. Download **AzuraCast** and restart Home Assistant.

### Manual

Copy `custom_components/azuracast` into your Home Assistant `config/custom_components/` folder and restart.

## Configuration

**Settings → Devices & services → Add integration → AzuraCast**.

| Field | Description |
|---|---|
| URL | Base address of the instance, e.g. `https://radio.example.com`. |
| API key | Optional. Create it in AzuraCast under **My Account → API Keys**. |
| Verify SSL certificate | Turn off only for self-signed certificates. |

**Use a dedicated AzuraCast user for Home Assistant.** AzuraCast API keys carry every permission of the user that created them. Give that user rights only on the stations Home Assistant should manage.

### Options

- **Stations to ignore.**
- **Keep the queue when a selector changes.** By default the queue is cleared so the change happens after the current song.
- **Accept selector values with nothing scheduled now.** By default a value is rejected if none of its playlists can play at this moment, so the station never goes silent.
- **Playlist refresh interval** (30–300 s, default 60).

For better real-time behavior, turn on **Use High-Performance Now Playing Updates** in AzuraCast under **Administration → System Settings**.

## Tag selectors: example

Background music with three moods that change by time block:

| Playlist | Schedule (in AzuraCast) | Description |
|---|---|---|
| `tarde-chill` | 12:00–18:00 | `ha:ambiente=chill` |
| `tarde-lounge` | 12:00–18:00 | `ha:ambiente=lounge` |
| `noche-chill` | 18:00–02:00 | `ha:ambiente=chill` |
| `noche-fiesta` | 18:00–02:00 | `ha:ambiente=fiesta` |

The station gets `select.<station>_ambiente` with `chill`, `fiesta` and `lounge`. A wall button can run:

```yaml
action: select.select_option
target:
  entity_id: select.ambientes_ambiente
data:
  option: chill
```

At 18:00, AzuraCast moves from `tarde-chill` to `noche-chill` by itself. Choosing `fiesta` at 15:00 is rejected, because no `fiesta` playlist is scheduled then. While a change waits for the current song to end, the select's `pending_value` attribute holds the new value. Use it to blink a button LED.

## Entities

| Entity | Needs API key |
|---|---|
| `media_player.<station>` | Skip only |
| `sensor.<station>_listeners`, `_current_song`, `_current_playlist`, `_next_song`, `_live_streamer` | No |
| `binary_sensor.<station>_online`, `_live_dj` | No |
| `switch.<station>_playlist_<name>` | Yes |
| `button.<station>_skip_song`, `_clear_queue` | Yes |
| `select.<station>_<selector>` | Yes |

## Actions

| Action | Fields |
|---|---|
| `azuracast.skip_song` | target |
| `azuracast.clear_queue` | target |
| `azuracast.set_playlists_enabled` | target, `playlists` (names or IDs), `enabled` |
| `azuracast.reshuffle_playlist` | target, `playlist` |

## Events

- `azuracast_song_changed`: `station_id`, `station_shortcode`, `song`, `playlist`, `tags`.
- `azuracast_selector_applied`: `station_id`, `selector`, `value`, `previous_value`, `cleared_queue`.

## How data is updated

Now-playing data is pushed through the stream; polling every 30 s is only a fallback. Playlists and the queue are polled every 60 s (configurable) and refreshed right after every action. AzuraCast is the source of truth: changes made in its web UI show up on the next refresh.

## Known limitations

- Stations that are not public appear only if the API key can see them.
- New stations need a reload of the integration. New playlists and selectors appear on their own.
- Changes are heard at the next song boundary, not instantly.
- A playlist with two values of the same selector is ignored by that selector, and a repair issue lists it.

## Troubleshooting

- **Song changes arrive late:** check the repair issue about real-time updates and turn on high-performance now-playing updates.
- **A station has no switches or select:** the API key has no permissions on it. Check the repair issue or the diagnostics.
- **A selector value is rejected:** none of its playlists is scheduled now. Check the schedules in AzuraCast or allow it in the options.

Download diagnostics from the integration's ⋮ menu. The URL and the key are redacted.

## Removal

**Settings → Devices & services → AzuraCast → ⋮ → Delete**, then remove it from HACS if you installed it there.
````

- [ ] **Step 6: Write `README.es.md`**

This is the same structure and content as `README.md`, translated. Write the full translation, section by section, with the same tables and YAML example. Translate the headings as Funciones, Requisitos, Instalación, Configuración, Opciones, Ejemplo de selectores por tags, Entidades, Acciones, Eventos, Cómo se actualizan los datos, Limitaciones conocidas, Solución de problemas and Desinstalación. Keep entity IDs, action names, field names and code unchanged.

- [ ] **Step 7: Create `CHANGELOG.md` and `LICENSE`**

`CHANGELOG.md`:

```markdown
# Changelog

## 0.1.0

- First release: stations as devices, now-playing sensors and media player, real-time SSE with polling fallback, playlist switches, skip and clear-queue buttons, tag selectors, services, events, diagnostics and repair issues. English and Spanish.
```

`LICENSE`: the MIT license text with `Copyright (c) 2026 NaturalDevCR`.

- [ ] **Step 8: Create the workflows**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.14"
          cache: pip
          cache-dependency-path: requirements_test.txt
      - run: pip install -r requirements_test.txt
      - run: ruff check .
      - run: ruff format --check .
      - run: mypy custom_components/azuracast
      - run: pytest -q
```

`.github/workflows/validate.yml`:

```yaml
name: Validate

on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 5 * * 1"

jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master

  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with:
          category: integration
          ignore: brands
```

`.github/workflows/release.yml`:

```yaml
name: Release

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Tag matches the integration version
        run: |
          version=$(python3 -c "import json;print(json.load(open('custom_components/azuracast/manifest.json'))['version'])")
          if [ "v$version" != "$GITHUB_REF_NAME" ]; then
            echo "::error::Tag $GITHUB_REF_NAME does not match manifest version $version"
            exit 1
          fi
      - name: Release notes from the changelog
        run: |
          version="${GITHUB_REF_NAME#v}"
          awk -v v="$version" '$0 ~ "^## "v"$" {f=1; next} /^## / {f=0} f' CHANGELOG.md > notes.md
          [ -s notes.md ] || echo "See CHANGELOG.md" > notes.md
      - name: Package the integration
        run: |
          cd custom_components/azuracast
          zip -r ../../azuracast.zip . -x "__pycache__/*" "*.pyc"
      - name: Create the release
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release create "$GITHUB_REF_NAME" azuracast.zip --title "$GITHUB_REF_NAME" --notes-file notes.md
```

- [ ] **Step 9: Run every check locally**

```bash
.venv/bin/ruff check . --fix
.venv/bin/ruff format .
.venv/bin/mypy custom_components/azuracast
.venv/bin/pytest -q
```

Expected: ruff and mypy report no errors and every test passes. Fix whatever ruff or mypy reports directly in the file it names, keeping the behavior. The most likely mypy findings are `coordinator.data` access, which may be `None`, inside entities that are only created after a successful refresh. Handle those with an early `if self.coordinator.data is None: return None` rather than with `# type: ignore`.

- [ ] **Step 10: Commit**

```bash
git add .
git status --short   # confirm probe-output/ and .venv/ are not staged
git commit -m "docs: add Spanish translation, icons, README, quality scale and CI"
```

---

### Task 17: Reconcile with phase 0 and test against the real instance

**Human in the loop.** This task needs `docs/validation.md` from Task 1 and a reachable instance.

**Files:**
- Modify: whichever of the files below a phase 0 answer contradicts.
- Create: `tests/fixtures/sse_real.txt` (sanitized), if the SSE shape differs.

- [ ] **Step 1: Apply the contingencies for the answers that differ from the assumptions**

1. **Partial PUT rejected or clears other fields.** In `api/client.py`, replace `set_playlist_enabled` with a read-modify-write version, and update `test_set_playlist_enabled_sends_partial_body` to mock the `GET` and assert that the `PUT` body contains the original fields with `is_enabled` flipped:

   ```python
   async def set_playlist_enabled(self, station_id: int, playlist_id: int, enabled: bool) -> None:
       path = f"/station/{station_id}/playlist/{playlist_id}"
       current = await self._request("GET", path)
       body = {k: v for k, v in (current or {}).items() if k not in ("id", "links")}
       body["is_enabled"] = enabled
       await self._request("PUT", path, payload=body)
   ```

2. **SSE shape differs.** Copy one connect line and one pub line from `probe-output/sse_raw.txt` into `tests/fixtures/sse_real.txt`, replacing host names and listener IPs. Add a test that feeds that file to `parse_sse_message`, then adjust `parse_sse_message` until it passes.
3. **Account endpoint differs.** Change the path in `AzuraCastClient.get_account` and in `test_get_account`.
4. **`schedule_items` field names differ.** Update `ScheduleItem.from_dict` and the fixtures `playlists_1.json` to the real names.
5. **Overlapping schedules mix.** Add to the README's known limitations: "Keep time blocks of the same selector value from overlapping."
6. **The next track was already fetched.** Add to the README how many songs it takes for a change to be heard.
7. **Skip is a hard cut or uses another endpoint.** Change the path in `skip_song` or document the cut.
8. **Station admin URL differs.** Change `configuration_url` in `entity.py`.
9. **No `timezone` in stations.** Confirm that `station_time` falls back to Home Assistant's timezone (already tested) and document it.

- [ ] **Step 2: Run the suite**

```bash
.venv/bin/pytest -q
```

Expected: every test passes.

- [ ] **Step 3: Run Home Assistant against the real instance**

```bash
mkdir -p /tmp/ha-azuracast-dev/custom_components
cp -R custom_components/azuracast /tmp/ha-azuracast-dev/custom_components/
docker run --rm -d --name ha-azuracast-dev -p 8124:8123 -v /tmp/ha-azuracast-dev:/config ghcr.io/home-assistant/home-assistant:2026.10.4
```

Open `http://localhost:8124`, finish onboarding, add **AzuraCast** with the URL and the dedicated key, and check:

- Each station is a device. The media player shows the current song and its artwork changes within about 1 s of a song change (SSE).
- `switch.<station>_playlist_*` toggles match the AzuraCast UI after a refresh.
- `select.<station>_<selector>` shows the right value. Choosing another one changes the playlists in AzuraCast, and the new music is heard after the current song. `pending_value` clears when it starts.
- Choosing a value with nothing scheduled shows the error and changes nothing.
- Diagnostics download with the URL and key redacted.

Stop the container afterwards with `docker stop ha-azuracast-dev`.

- [ ] **Step 4: Commit**

```bash
git add -A
git status --short   # make sure probe-output/ is not staged
git commit -m "fix: align the client with the behavior of AzuraCast 0.23.8"
```

Skip the commit if nothing changed.
