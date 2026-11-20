# AzuraCast for Home Assistant

[![CI](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml/badge.svg)](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Brings every station of an [AzuraCast](https://www.azuracast.com/) instance into Home Assistant: what is playing, listeners, live DJs, playlists, the queue, skipping songs and **tag selectors** that switch groups of playlists with one action.

[Español](README.es.md)

## Features

- **One device per station** with a media player (now playing, artwork, progress, skip), sensors (listeners, current song, current playlist, next song, live streamer) and binary sensors (online, live DJ).
- **Real time.** Song changes arrive through AzuraCast's high-performance now-playing stream. If it is off, the integration polls every 30 seconds and tells you how to turn it on.
- **Artwork through Home Assistant.** Album art is fetched by Home Assistant and served to your browser through its media proxy, so it works for LAN-only instances and self-signed certificates, and it respects the **Verify SSL certificate** setting.
- **Playlists as switches**, with their ID, type, order, weight, schedule and tags as attributes.
- **Skip song** and **clear queue** buttons, plus actions to skip, clear the queue, enable or disable several playlists at once and reshuffle a playlist.
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
| Verify SSL certificate | Turn off only for self-signed certificates. It also applies to artwork downloads. |

**Use a dedicated AzuraCast user for Home Assistant.** AzuraCast API keys carry every permission of the user that created them. Give that user rights only on the stations Home Assistant should manage.

The URL, the API key and the SSL setting can be changed later with **Reconfigure**. If AzuraCast stops accepting the key, Home Assistant asks for a new one.

### Options

- **Stations to ignore.** They get no device or entities.
- **Keep the queue when a selector changes.** By default the queue is cleared so the change happens after the current song.
- **Accept selector values with nothing scheduled now.** By default a value is rejected if none of its playlists can play at this moment, so the station never goes silent.
- **Playlist refresh interval** (30–300 s, default 60).

The first three options are lists of stations: stations that are not listed, including stations added later, get the default behavior.

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

### How selectors behave

- Tags are written in the playlist description as whitespace-separated words. Selector names use `a-z`, `0-9` and `_`; values also allow `-`. Matching ignores case.
- Choosing a value enables every playlist of that value and disables the playlists of the other values of the same selector. Playlists without a tag of that selector are never touched.
- **Re-selecting the current value never clears the queue and is never rejected.** It only re-enables any playlist of that value that was disabled (for example by hand), so the mood carries into the next time block.
- A value with nothing playable right now is rejected, unless the station is listed in **Accept selector values with nothing scheduled now**.
- The select shows a value only when exactly one value has enabled playlists. Otherwise it shows no value, and the `reason` attribute says why: `none_enabled` or `mixed`.
- Attributes: `available_now` (values that can play right now), `pending_value`, `playlists` (playlists of each value) and `reason`. `pending_value` clears when a song from the new value starts, or after 15 minutes.
- A playlist with two values of the same selector is ignored by that selector, and a repair issue lists it.

### Schedules

Whether a playlist "can play now" follows AzuraCast's own schedule semantics, in the station's time zone:

- A playlist without schedules can always play.
- A window that crosses midnight (for example 18:00–02:00) belongs to the day it starts, for day-of-week and start/end date checks.
- A schedule whose start and end time are equal is AzuraCast's "play once" window: it counts as active for 15 minutes from its start time.

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
| `azuracast.reshuffle_playlist` | target, `playlist` (name or ID) |

```yaml
action: azuracast.set_playlists_enabled
target:
  device_id: <station device>
data:
  playlists:
    - tarde-chill
    - "42"
  enabled: false
```

- **Targets.** Pick station devices, AzuraCast entities of a station, or areas and labels that contain them. Only station devices are acted on: the AzuraCast instance device is ignored, and a call that targets no station is rejected.
- **Read-only stations.** If a target includes a station the API key cannot manage, the whole call is rejected and nothing is changed.
- **Playlist references.** An exact (case-sensitive) playlist name takes precedence over a numeric ID: a playlist literally named `42` wins over the playlist with ID 42. When several playlists share a name, the call is rejected; reference them by ID. Every reference is checked on every targeted station before anything is changed.

## Events

- `azuracast_song_changed`: `station_id`, `station_shortcode`, `song`, `playlist`, `tags`.
- `azuracast_selector_applied`: `station_id`, `selector`, `value`, `previous_value`, `cleared_queue`. It is not fired when the current value is re-selected.

## How data is updated

Now-playing data is pushed through the stream; polling every 30 s is only a fallback. Playlists and the queue are polled every 60 s (configurable) and refreshed right after every action. AzuraCast is the source of truth: changes made in its web UI show up on the next refresh.

## Known limitations

- Stations that are not public appear only if the API key can see them.
- New stations need a reload of the integration. New playlists and selectors appear on their own.
- Changes are heard at the next song boundary, not instantly.
- Time blocks of the same selector value should not overlap. How AzuraCast picks between overlapping scheduled playlists has not been verified yet.
- A playlist with two values of the same selector is ignored by that selector, and a repair issue lists it.

## Troubleshooting

- **Song changes arrive late:** check the repair issue about real-time updates and turn on high-performance now-playing updates.
- **A station has no switches or select:** the API key has no permissions on it. Check the repair issue or the diagnostics.
- **A selector value is rejected:** none of its playlists is scheduled now. Check the schedules in AzuraCast or allow it in the options.
- **An action says a playlist name is ambiguous:** several playlists share that name. Use the playlist's numeric ID.

Download diagnostics from the integration's ⋮ menu. The URL and the key are redacted.

## Removal

**Settings → Devices & services → AzuraCast → ⋮ → Delete**, then remove it from HACS if you installed it there.
