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
        f"station:{np['station']['shortcode']}": {
            "publications": [{"data": {"np": np}, "offset": 1}]
        }
        for np in nps
    }
    return json.dumps({"connect": {"client": "c", "subs": subs, "ping": 25, "pong": True}})


def pub_message(np: dict) -> str:
    return json.dumps(
        {"channel": f"station:{np['station']['shortcode']}", "pub": {"data": {"np": np}}}
    )


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
    lines = _lines(
        ": comment\n", 'data: {"a":\n', "data: 1}\n", "\n", "event: x\n", "data:{}\n", "\n"
    )
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


async def test_stream_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(SSE_RE, exc=aiohttp.ClientError())
    with pytest.raises(AzuraCastConnectionError):
        async for _ in stream_nowplaying(async_get_clientsession(hass), BASE_URL, ["ambientes"]):
            pass
