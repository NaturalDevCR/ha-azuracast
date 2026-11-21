# Changelog

## 0.4.0

- New option **Change selectors by reloading the station after the song** (per station): like the reload, but the current song plays out first. The station soft-reloads two seconds before the song ends, so the new value starts with the next song instead of after the songs AzuraCast had already loaded. A song from the previous value starting early (for example, after a skip) triggers the reload at once; a song from the new value starting first cancels it. Stations that cannot soft-reload wait instead.
- `azuracast_selector_applied` events can carry `change_mode: reload_after_song`.

## 0.3.0

- New option **Change selectors by reloading the station** (per station): when a selector changes, soft-reload the station once AzuraCast has the next song ready. It drops the songs AzuraCast had already loaded, so the new value plays at once after one short cut; listeners stay connected. Only for Icecast and RSAS stations (others skip songs instead) and never while a DJ is live.
- **Change selectors immediately** is now **Change selectors by skipping songs**. Both immediate modes leave a station alone while it is offline or a DJ is live.
- `azuracast_selector_applied` events carry `change_mode` (`wait`, `skip` or `reload`) instead of `skipped_current`.
- Diagnostics show each station's change mode and frontend.

## 0.2.0

- New option **Change selectors immediately** (per station): when a selector changes, skip the current song and then each song from the previous value that AzuraCast had already handed to its player, as it starts (at most three). A few seconds of each skipped song are heard during the fade.
- By default nothing is skipped: AzuraCast keeps one or two songs loaded that clearing the queue cannot take back, so a change is heard after those. The docs now say so.
- `azuracast_selector_applied` events carry `skipped_current`.
- A pending selector value now waits 30 minutes (was 15) before it is dropped with a warning.

## 0.1.3

- A stopped station (missing from AzuraCast's now-playing list) now shows its Online sensor and media player as off instead of unavailable.

## 0.1.2

- Fix setup failing with `'id'` against a real AzuraCast: the queue list has no row id. Clearing the queue now uses AzuraCast's own clear endpoint, which removes only songs not yet sent to the AutoDJ and returns them to their playlists' shuffle order.

## 0.1.1

- Fix the actions' target definition so Home Assistant's validation accepts it.

## 0.1.0

- First release: stations as devices, now-playing sensors and media player, real-time SSE with polling fallback, playlist switches, skip and clear-queue buttons, tag selectors, actions, events, diagnostics and repair issues. English and Spanish.
