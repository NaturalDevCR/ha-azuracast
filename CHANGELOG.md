# Changelog

## 0.1.3

- A stopped station (missing from AzuraCast's now-playing list) now shows its Online sensor and media player as off instead of unavailable.

## 0.1.2

- Fix setup failing with `'id'` against a real AzuraCast: the queue list has no row id. Clearing the queue now uses AzuraCast's own clear endpoint, which removes only songs not yet sent to the AutoDJ and returns them to their playlists' shuffle order.

## 0.1.1

- Fix the actions' target definition so Home Assistant's validation accepts it.

## 0.1.0

- First release: stations as devices, now-playing sensors and media player, real-time SSE with polling fallback, playlist switches, skip and clear-queue buttons, tag selectors, actions, events, diagnostics and repair issues. English and Spanish.
