"""Phase 0 probe: record how a real AzuraCast instance behaves.

Environment:
  AZURACAST_URL                base URL, e.g. https://radio.example.com
  AZURACAST_API_KEY            API key of a user with rights on the test station
  AZURACAST_STATION_ID         numeric id of a TEST station
  AZURACAST_PROBE_PLAYLIST_ID  optional: playlist on that station to flip and restore
  AZURACAST_VERIFY_SSL         optional, "0" to skip certificate checks
  AZURACAST_PROBE_CONFIRM      optional, "yes" to skip the interactive confirmation
                               before the playlist flip (for non-interactive runs)

Writes raw responses to ./probe-output/ (gitignored) and prints a summary.

With AZURACAST_PROBE_PLAYLIST_ID set, the probe asks you to type "yes", sends
PUT {"is_enabled": x} to flip that playlist, then always sends
PUT {"is_enabled": original} to restore it, even on errors or Ctrl-C. Use a TEST
playlist: only `is_enabled` is restored. The playlist as it was before the flip
is saved in probe-output/partial_put.json ("before"). If the restore fails or
the partial PUT changed other fields, the probe prints a WARNING and exits
non-zero; repair the playlist by hand from that snapshot.
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


def is_playlist(status: Any, body: Any) -> bool:
    return (
        isinstance(status, int)
        and 200 <= status < 300
        and isinstance(body, dict)
        and "is_enabled" in body
    )


def confirmed(prompt: str) -> bool:
    if os.environ.get("AZURACAST_PROBE_CONFIRM") == "yes":
        return True
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


async def restore(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str], original: bool
) -> dict[str, Any]:
    """Send the restore PUT and re-read the playlist; never raises an Exception."""
    result: dict[str, Any] = {"status": None, "body": None, "restored": None, "error": None}
    try:
        result["status"], result["body"] = await call(
            session, "PUT", url, headers, {"is_enabled": original}
        )
        _, result["restored"] = await call(session, "GET", url, headers)
    except Exception as err:
        result["error"] = repr(err)
    return result


def check_flip(record: dict[str, Any], original: bool, changed: set[str]) -> tuple[bool, list[str]]:
    """Return (restored_ok, problems) for a flip-and-restore record."""
    rest = record.get("restore") or {}
    restored = rest.get("restored")
    status = rest.get("status")
    restored_ok = (
        rest.get("error") is None
        and isinstance(status, int)
        and 200 <= status < 300
        and isinstance(restored, dict)
        and restored.get("is_enabled") == original
    )
    problems: list[str] = []
    if not restored_ok:
        now = restored.get("is_enabled") if isinstance(restored, dict) else "unknown"
        problems.append(
            f"restore not confirmed (PUT HTTP {status}, error {rest.get('error')}, "
            f"is_enabled now {now}, expected {original})"
        )
    if changed:
        problems.append(f"partial PUT changed other fields: {sorted(changed)}")
    if "put_status" in record and not is_playlist(record.get("after_status"), record.get("after")):
        problems.append(
            f"could not re-read the playlist after the flip (HTTP {record.get('after_status')}); "
            "other fields not verified"
        )
    return restored_ok, problems


async def main() -> int:
    base = str(env("AZURACAST_URL")).rstrip("/")
    key = str(env("AZURACAST_API_KEY"))
    station_id = int(str(env("AZURACAST_STATION_ID")))
    probe_playlist = env("AZURACAST_PROBE_PLAYLIST_ID", required=False)
    verify = os.environ.get("AZURACAST_VERIFY_SSL", "1") != "0"
    headers = {"Accept": "application/json", "X-API-Key": key}
    api = f"{base}/api"
    summary: list[str] = []
    exit_code = 0

    try:
        connector = aiohttp.TCPConnector(ssl=None if verify else False)
        async with aiohttp.ClientSession(connector=connector) as session:
            results: dict[str, tuple[int, Any]] = {}
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
                results[name] = (status, data)

            stations_status, stations = results["stations"]
            if not isinstance(stations, list):
                sys.exit(
                    f"GET /stations did not return a list (HTTP {stations_status}); "
                    f"see {OUT / 'stations.json'}"
                )
            station = next(
                (s for s in stations if isinstance(s, dict) and s.get("id") == station_id), None
            )
            if station is None:
                ids = [s.get("id") for s in stations if isinstance(s, dict)]
                sys.exit(f"Station id {station_id} not found in /stations (ids: {ids})")
            summary.append(
                f"- station has 'timezone': {'timezone' in station} ({station.get('timezone')})"
            )
            _, playlists = results["playlists"]
            if isinstance(playlists, list) and playlists:
                summary.append(f"- playlist keys: {sorted(playlists[0])}")
                scheduled = [p for p in playlists if p.get("schedule_items")]
                if scheduled:
                    summary.append(f"- schedule_items sample: {scheduled[0]['schedule_items'][0]}")

            if probe_playlist:
                url = f"{api}/station/{station_id}/playlist/{probe_playlist}"
                before_status, before = await call(session, "GET", url, headers)
                if not is_playlist(before_status, before):
                    sys.exit(
                        f"GET /station/{station_id}/playlist/{probe_playlist} "
                        f"did not return a playlist (HTTP {before_status}); nothing was changed"
                    )
                original = bool(before["is_enabled"])
                station_label = (
                    f"station {station.get('name')!r} ({station.get('shortcode')}, id {station_id})"
                )
                playlist_label = f"playlist {before.get('name')!r} (id {probe_playlist})"
                print(
                    f"About to flip is_enabled {original} -> {not original} on {playlist_label} "
                    f"of {station_label}, then restore it."
                )
                if not confirmed("Type 'yes' to continue: "):
                    summary.append("- partial PUT: skipped (not confirmed)")
                else:
                    record: dict[str, Any] = {
                        "station": station_label,
                        "playlist": playlist_label,
                        "before": before,
                    }
                    save("partial_put.json", record)
                    changed: set[str] = set()
                    try:
                        put_status, put_body = await call(
                            session, "PUT", url, headers, {"is_enabled": not original}
                        )
                        record.update(put_status=put_status, put_body=put_body)
                        after_status, after = await call(session, "GET", url, headers)
                        record.update(after_status=after_status, after=after)
                        if is_playlist(after_status, after):
                            changed = {
                                k
                                for k in before
                                if k not in ("is_enabled", "links") and before[k] != after.get(k)
                            }
                    finally:
                        try:
                            record["restore"] = await restore(session, url, headers, original)
                        finally:
                            save("partial_put.json", record)
                            restored_ok, problems = check_flip(record, original, changed)
                            after = record.get("after")
                            flipped = (
                                after["is_enabled"] != original
                                if is_playlist(record.get("after_status"), after)
                                else "unknown"
                            )
                            summary.append(
                                f"- partial PUT: HTTP {record.get('put_status')}, "
                                f"flipped={flipped}, "
                                f"other fields changed={sorted(changed) or 'none'}, "
                                f"restored={restored_ok}"
                            )
                            if problems:
                                exit_code = 1
                                warning = (
                                    f"WARNING: {playlist_label} of {station_label} "
                                    f"may need manual repair: "
                                    f"{'; '.join(problems)}. "
                                    f"The playlist as it was before the flip is "
                                    f"saved in {OUT / 'partial_put.json'} under 'before'."
                                )
                                print(warning, file=sys.stderr)
                                summary.append(f"- {warning}")

            connect = json.dumps(
                {"subs": {f"station:{station.get('shortcode')}": {"recover": True}}},
                separators=(",", ":"),
            )
            sse_url = f"{api}/live/nowplaying/sse?cf_connect={quote(connect, safe='')}"
            lines: list[str] = []
            try:
                timeout = aiohttp.ClientTimeout(total=25, sock_read=25)
                async with session.get(
                    sse_url, headers={"Accept": "text/event-stream"}, timeout=timeout
                ) as resp:
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
                try:
                    first = json.loads(data_lines[0][5:].strip())
                except ValueError:
                    summary.append(f"- SSE first message is not JSON: {data_lines[0][:200]!r}")
                else:
                    keys = sorted(first) if isinstance(first, dict) else type(first).__name__
                    summary.append(f"- SSE first message keys: {keys}")
    finally:
        print("\n".join(summary))
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
