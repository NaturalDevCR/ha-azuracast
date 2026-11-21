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


async def test_get_stations_sends_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/stations", json=load_fixture("stations.json"))
    stations = await _client(hass).get_stations()
    assert [s.shortcode for s in stations] == ["ambientes", "party_time"]
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["X-API-Key"] == "secret"


async def test_no_key_sends_no_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
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


async def test_playlists_and_queue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{API}/station/1/playlists", json=load_fixture("playlists_1.json"))
    aioclient_mock.get(f"{API}/station/1/queue", json=load_fixture("queue_1.json"))
    client = _client(hass)
    assert [p.id for p in await client.get_playlists(1)] == [10, 11, 12, 13, 14]
    assert [q.sent_to_autodj for q in await client.get_queue(1)] == [True, False]


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


async def test_clear_queue_uses_clear_endpoint(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API}/station/1/queue/clear", json={"success": True})
    await _client(hass).clear_queue(1)
    assert [(c[0], str(c[1])) for c in aioclient_mock.mock_calls] == [
        ("POST", f"{API}/station/1/queue/clear"),
    ]


async def test_clear_queue_errors_propagate(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API}/station/1/queue/clear", status=500, json={"message": "boom"})
    with pytest.raises(AzuraCastResponseError):
        await _client(hass).clear_queue(1)


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


async def test_reload_station(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{API}/station/1/reload", json={"success": True})
    await _client(hass).reload_station(1)
    assert [(c[0], str(c[1])) for c in aioclient_mock.mock_calls] == [
        ("POST", f"{API}/station/1/reload"),
    ]
