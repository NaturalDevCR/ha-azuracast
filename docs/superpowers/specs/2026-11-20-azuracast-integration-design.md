# AzuraCast integration for Home Assistant: design

Date: 2026-11-20
Status: approved design, pending spec review

## 1. Goal

A general-purpose Home Assistant custom integration for [AzuraCast](https://www.azuracast.com/). It turns each station of an AzuraCast instance into a Home Assistant device with sensors, a media player, controls and services, and adds a generic **tag-based playlist selector** primitive.

The integration must stay agnostic: nothing in it knows about hotels, moods or buttons. The first real use case, which is switching background-music "moods" from physical buttons, must work well using only the generic pieces (see section 12).

Non-goals:

- Replacing AzuraCast's own UI for uploading media, editing playlists or editing schedules.
- Instant mid-song transitions. Changes take effect at the next track boundary using AzuraCast's normal crossfade.

## 2. Distribution

- Integration domain: `azuracast`.
- Repository: standalone public repository `ha-azuracast` under the `NaturalDevCR` GitHub account. It lives in this folder as its own git repository.
- Installation: HACS custom repository at first. Submit to the HACS default store once v1 is stable. That submission needs releases, brand assets and a passing `hacs/action` and `hassfest`.
- Minimum Home Assistant version: 2026.10.
- The HTTP client lives in `custom_components/azuracast/api/` and does not import Home Assistant. That keeps a path open to publish it on PyPI as `aioazuracast` if the integration ever targets HA core. No PyPI package is published for v1.

## 3. Configuration

### 3.1 Config flow (one entry per AzuraCast instance)

Fields:

| Field | Required | Notes |
|---|---|---|
| `url` | yes | Base URL, e.g. `https://cast.example.com`. Normalized: no trailing slash, no `/api`. |
| `api_key` | no | Enables management features. Stored in the config entry. |
| `verify_ssl` | yes, default `true` | |

Validation:

1. `GET /api/status` must answer. Otherwise the error is `cannot_connect`.
2. If a key is given, an authenticated request (the current-account endpoint) must return 200. Otherwise the error is `invalid_auth`.

The entry's `unique_id` is the normalized URL. The flow aborts with `already_configured` on a duplicate.

The integration also provides:

- A reauth flow that starts on any 401 from an authenticated call.
- A reconfigure flow to change the URL, key or SSL setting.

### 3.2 Station discovery and capabilities

Stations come from `GET /api/stations`, which lists public stations. A station that is not public is only visible if the API key can see it through station-scoped endpoints. This limitation is documented. Stations added in AzuraCast later are picked up on the next reload.

Each station gets a capability level:

- `read`: no key, or the key gets 403 on the station's playlist endpoint. Only public now-playing data is available.
- `manage`: `GET /api/station/{id}/playlists` returns 200.

Entities and services that need `manage` are never created for `read` stations.

### 3.3 Options flow

- **Stations**: multi-select to exclude stations. All are included by default.
- **Per-station selector behavior**, applied to every selector on that station:
  - `clear_queue_on_change` (default `true`).
  - `reject_unavailable_values` (default `true`, see 6.4).
- **Polling interval** for the management data: 30 to 300 s, default 60.

## 4. Data sources

### 4.1 Now playing hub (one per instance)

Primary source: AzuraCast's Centrifugo SSE endpoint (`/api/live/nowplaying/sse`). One connection subscribes to `station:<shortcode>` for every included station. On connect, AzuraCast sends the current state, so no separate initial call is needed.

Reconnects use exponential backoff from 1 s to 60 s, with jitter.

Fallback: while the SSE stream is down, or if the instance does not offer it, the hub polls `GET /api/nowplaying` every 30 s. That one call returns all stations. Polling stops as soon as SSE is healthy again.

If SSE has never connected after setup (404 or connection refused), the integration raises a Repairs issue, `sse_unavailable`. It explains how to enable AzuraCast's high-performance now-playing updates and says that updates are delayed meanwhile.

The hub feeds one `DataUpdateCoordinator` per station. These coordinators have no polling interval of their own (`update_interval=None`) and are updated through `async_set_updated_data`.

### 4.2 Management coordinator (one per `manage` station)

Fetches:

- `GET /api/station/{id}/playlists`: playlists including `is_enabled`, `description`, `type`, `weight`, `order` and `schedule_items`.
- `GET /api/station/{id}/queue`: the upcoming AutoDJ queue.

It runs on the configured interval and refreshes right after every action the integration performs.

### 4.3 Source of truth

AzuraCast is the only source of truth. Home Assistant persists no state of its own about playlists or selectors. Everything shown is derived from the latest data. This means:

- A restart mid-change cannot leave Home Assistant out of sync.
- Manual edits in AzuraCast's UI show up on the next refresh.

## 5. Entities

One device per station, named after the station, with manufacturer `AzuraCast` and `configuration_url` pointing to the station's admin page. All entities use `has_entity_name` and translation keys (`en` and `es`). Unique IDs are built from the config entry ID, the station ID and a key.

| Platform | Entity | Requires | Notes |
|---|---|---|---|
| `media_player` | Station | read | State `playing` when online with a current song, `idle` when online without one, `off` when offline. Title, artist, album, artwork URL, duration, position and position-updated-at. Supports `next_track` (skip) only on `manage` stations. No stop, pause or volume. |
| `sensor` | Listeners | read | Current listeners. Attributes: unique and total. |
| `sensor` | Current song | read | `artist - title`. Attributes: artist, title, album, art, playlist, duration. |
| `sensor` | Current playlist | read | Playlist name from now playing. Attribute: the playlist's tags (when `manage`). |
| `sensor` | Next song | read | From `playing_next`. |
| `sensor` | Live streamer | read | Streamer name, or empty. |
| `binary_sensor` | Online | read | Device class `connectivity`. |
| `binary_sensor` | Live DJ | read | On while a streamer is broadcasting. |
| `switch` | Playlist `<name>` | manage | One per playlist. On means `is_enabled`. Attributes: id, type, order, weight, schedule (normalized), tags. Entity category `config`. |
| `button` | Skip song | manage | |
| `button` | Clear queue | manage | |
| `select` | `<selector>` | manage | One per selector found in tags (section 6). |

Entity lifecycle: when a playlist or selector disappears from AzuraCast, its entity is removed from the registry on the next refresh. New ones are added dynamically without a reload.

## 6. Tag-based selectors

### 6.1 Tag syntax

Tags are written in the playlist's `description` field. AzuraCast has no native tags for playlists: the only free-text field is `description`, and custom fields exist only for media.

A tag is a whitespace-delimited token:

```
ha:<selector>=<value>
```

- `<selector>` matches `[a-z0-9_]+`.
- `<value>` matches `[a-z0-9_-]+`.
- Matching is case-insensitive. Both parts are lowercased.
- Other text in the description is ignored.
- A playlist can carry several tags for different selectors, e.g. `Evening set ha:ambiente=chill ha:zona=lobby`.

### 6.2 Selector entities

For each station, every distinct `<selector>` seen in tags becomes one `select` entity. Its options are the sorted distinct values. The entity's name is the selector key, e.g. `select.<station>_ambiente`.

### 6.3 State

Let `P(v)` be the set of playlists tagged with value `v` for this selector.

- The state is `v` when at least one playlist in `P(v)` is enabled and no playlist tagged with any other value of this selector is enabled.
- Otherwise the state is `unknown`, and the attribute `reason` is `none_enabled` or `mixed`.

Attributes:

- `available_now`: the values that can play right now (6.4).
- `pending_value`: set while a change is waiting to be heard (6.5).
- `playlists`: a map from value to playlist names.

### 6.4 Availability now

A playlist is *playable now* when it has no `schedule_items`, which means general rotation, or when at least one schedule item covers the current time.

Schedule items are evaluated in the station's timezone and check:

- `start_time` and `end_time` as HHMM integers. When `end_time` is less than `start_time`, the window wraps past midnight.
- `days`, as ISO weekdays. An empty list means every day.
- `start_date` and `end_date`, when present.

A value is available now when any playlist in `P(v)` is playable now. The value's own playlists do not need to be enabled for this check.

If `reject_unavailable_values` is on and the requested value is not available now, `select_option` raises `ServiceValidationError` (translation key `value_not_available_now`). Nothing changes in AzuraCast and the entity keeps its state.

### 6.5 Applying a value

`select.select_option(v)` does the following:

1. Validates availability (6.4).
2. Enables every playlist in `P(v)` that is disabled.
3. Disables every enabled playlist tagged with another value of the same selector. Doing step 2 before step 3 means the station never has a moment with neither set enabled.
4. If `clear_queue_on_change` is on, deletes every item in the station queue.
5. Sets `pending_value = v` and fires `azuracast_selector_applied`.
6. Refreshes the management coordinator.

Untagged playlists are never touched.

Every write uses an explicit `is_enabled` value, never AzuraCast's toggle endpoint. That makes repeated calls idempotent.

`pending_value` clears when either:

- a song change reports a current playlist in `P(v)`, or
- 15 minutes pass. In that case a warning is logged.

If any step fails, the integration:

- stops,
- refreshes,
- raises `HomeAssistantError` with the AzuraCast error.

The displayed state then reflects what AzuraCast actually has.

### 6.6 Conflicts

A playlist carrying two different values for the same selector is ignored for that selector. The integration raises a Repairs issue, `conflicting_tags`, that names the playlist. The issue clears once the conflict is fixed.

## 7. Services

Every service targets a station device or entity of that station. The ones that need management fail clearly on `read` stations.

| Service | Fields | Effect |
|---|---|---|
| `azuracast.skip_song` | target | Skips the current song. |
| `azuracast.clear_queue` | target | Deletes all upcoming queue items. |
| `azuracast.set_playlists_enabled` | target, `playlists` (names or IDs), `enabled` | Bulk enable or disable. Explicit values, idempotent. |
| `azuracast.reshuffle_playlist` | target, `playlist` | Reshuffles one playlist. |

Selector changes use the standard `select.select_option`. No custom service is added for them.

## 8. Events

| Event | Data |
|---|---|
| `azuracast_song_changed` | `station_id`, `station_shortcode`, `song` (artist, title, album, art), `playlist`, `tags` (map of the playlist's selector values). |
| `azuracast_selector_applied` | `station_id`, `selector`, `value`, `previous_value`, `cleared_queue`. |

`azuracast_song_changed` fires only when the song ID changes. It never fires on the initial load or on reconnects.

## 9. Error handling and resilience

- **API unreachable**: the station's entities become unavailable. They come back automatically.
- **401 on an authenticated call**: starts reauth. Management entities become unavailable.
- **403 on a management call after setup**: the integration raises a Repairs issue, `missing_permissions`, for that station and logs the endpoint.
- **SSE drops**: the hub switches to polling without making any entity unavailable.
- **Timeouts**: 10 s per request. SSE has a read timeout of 90 s, and Centrifugo's pings keep the connection alive.
- **Diagnostics**: the integration provides diagnostics. The API key and URL are redacted. Diagnostics include the station capability levels, the SSE status, the parsed selectors and any tag conflicts.

## 10. Security

- The README recommends a dedicated AzuraCast user for Home Assistant, with permissions only on the stations it should manage. AzuraCast API keys inherit every permission of their user.
- The README recommends HTTPS and warns when `verify_ssl` is off.
- The key is never logged and is redacted in diagnostics.

## 11. Phase 0: validation against a real instance

These checks run before the code that depends on them is written, against a test station on the user's instance. The results are recorded in `docs/validation.md`.

1. Does `PUT /api/station/{id}/playlist/{pid}` accept a partial body with only `is_enabled`, or does it need the full object? If it needs the full object, the client does read-modify-write.
2. The exact SSE URL, the `cf_connect` subscription format and the payload shape on version 0.23.8. Also: is the high-performance now-playing setting needed?
3. The current-account endpoint used to validate the key.
4. The exact shape of `schedule_items` in the playlist response: field names, date fields and timezone handling.
5. Two scheduled playlists with overlapping times, both enabled: do they mix or does one win?
6. After clearing the queue, does the next track come from the newly enabled set? How late before the end of a track does Liquidsoap fetch the next one?
7. The skip mechanism. Is it a `backend/skip` action or something else, and does it respect crossfade?

## 12. Reference use case: hotel background-music moods

This is a configuration recipe, with no use-case code in the integration.

- In the station `ambientes`, each time block has one playlist per mood, scheduled in AzuraCast. Examples: `tarde-chill` (12:00–18:00, tagged `ha:ambiente=chill`) and `noche-chill` (18:00–02:00, tagged `ha:ambiente=chill`). A block can offer only some of the moods.
- Exactly one mood value is enabled at a time. AzuraCast's scheduler picks the block. When the time block changes, the chosen mood carries over automatically at the next track boundary.
- Three Shelly buttons call `select.select_option` on `select.ambientes_ambiente` with `chill`, `lounge` or `fiesta`. If the current block has no playlist for that mood, the change is rejected (6.4) and the current mood keeps playing.
- To give staff instant feedback, an automation blinks the button LED while `pending_value` is set.
- A nightly automation resets the mood to the default.
- The Party Button and Event Mode keep acting on Snapcast and are independent of this integration.

## 13. Testing

- `pytest-homeassistant-custom-component`.
- API fixtures recorded from the real instance, with private data removed.
- A fake SSE server that streams recorded payloads.
- Unit tests, with no HA, for the tag parser, the selector state rules, the schedule "playable now" evaluation (wrap past midnight, days, dates, timezone) and the apply ordering.
- Integration tests for the config, options, reauth and reconfigure flows, entity creation per capability, dynamic add and remove of playlists and selectors, the SSE-to-polling fallback, Repairs issues and diagnostics redaction.
- CI: `ruff`, `mypy`, `pytest`, `hassfest`, `hacs/action`.
- Target Integration Quality Scale: Silver at first release, then Gold.

## 14. Layout

```
custom_components/azuracast/
  __init__.py          setup and unload, platform forwarding, runtime_data
  manifest.json
  config_flow.py       user, reauth, reconfigure, options
  const.py
  api/                 HTTP client with no HA imports
    __init__.py
    client.py          REST calls and errors
    sse.py             Centrifugo SSE stream
    models.py          dataclasses: Station, NowPlaying, Playlist, ScheduleItem, QueueItem
  hub.py               now-playing hub (SSE plus polling fallback)
  coordinator.py       per-station now-playing and management coordinators
  selectors.py         tag parsing, selector state, availability, apply
  entity.py            base entity and device info
  media_player.py sensor.py binary_sensor.py switch.py button.py select.py
  services.py services.yaml
  diagnostics.py repairs.py
  strings.json translations/en.json translations/es.json
  quality_scale.yaml
tests/
docs/
hacs.json README.md README.es.md LICENSE
```

## 15. Out of scope for v1

- Song requests.
- Podcasts.
- Streamer/DJ management.
- Media file management.
- Mount and remote relay entities.
- Starting, stopping or restarting a station.
- Per-listener data.
- A custom Lovelace card.
