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
