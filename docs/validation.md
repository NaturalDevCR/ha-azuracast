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
