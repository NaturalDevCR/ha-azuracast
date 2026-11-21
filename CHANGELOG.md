# Changelog

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
