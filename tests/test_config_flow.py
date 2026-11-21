"""Tests for the config and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.azuracast.api.errors import (
    AzuraCastAuthError,
    AzuraCastConnectionError,
    AzuraCastPermissionError,
)
from custom_components.azuracast.const import (
    CONF_ALLOW_UNAVAILABLE_STATIONS,
    CONF_EXCLUDED_STATIONS,
    CONF_IMMEDIATE_CHANGE_STATIONS,
    CONF_KEEP_QUEUE_STATIONS,
    CONF_RELOAD_CHANGE_STATIONS,
    DOMAIN,
    ChangeMode,
)

from .common import BASE_URL


@pytest.fixture
def no_setup() -> Generator[None]:
    with patch("custom_components.azuracast.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_with_key(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://radio.example.com/api/", CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "radio.example.com"
    assert result["data"] == {CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True}
    assert result["result"].unique_id == BASE_URL
    mock_client.get_account.assert_awaited_once()


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_without_key(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] is None
    mock_client.get_account.assert_not_called()


@pytest.mark.usefixtures("no_setup")
@pytest.mark.parametrize(
    ("method", "exc", "error"),
    [
        ("get_status", AzuraCastConnectionError("down"), "cannot_connect"),
        ("get_account", AzuraCastAuthError(401, ""), "invalid_auth"),
        ("get_status", RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recover(
    hass: HomeAssistant, mock_client: MagicMock, method: str, exc: Exception, error: str
) -> None:
    getattr(mock_client, method).side_effect = exc
    result = await _start(hass)
    user_input = {CONF_URL: BASE_URL, CONF_API_KEY: "secret", CONF_VERIFY_SSL: True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    getattr(mock_client, method).side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_invalid_url(hass: HomeAssistant, mock_client: MagicMock) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "radio", CONF_VERIFY_SSL: True}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}


@pytest.mark.usefixtures("no_setup")
async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{BASE_URL}/", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("no_setup")
async def test_reauth(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    mock_client.get_account.side_effect = AzuraCastAuthError(401, "")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    mock_client.get_account.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_KEY] == "new"


@pytest.mark.usefixtures("no_setup")
async def test_reconfigure(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://cast.example.org", CONF_API_KEY: "secret", CONF_VERIFY_SSL: False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.unique_id == "https://cast.example.org"
    assert mock_config_entry.title == "cast.example.org"
    assert mock_config_entry.data[CONF_API_KEY] == "secret"
    assert mock_config_entry.data[CONF_VERIFY_SSL] is False


@pytest.mark.usefixtures("no_setup")
async def test_reauth_permission_error(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    mock_client.get_account.side_effect = AzuraCastPermissionError(403, "")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "limited"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_API_KEY] == "secret"


@pytest.mark.usefixtures("no_setup")
async def test_reconfigure_already_configured(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title="cast.example.org",
        unique_id="https://cast.example.org",
        data={CONF_URL: "https://cast.example.org", CONF_API_KEY: None, CONF_VERIFY_SSL: True},
    )
    other.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://cast.example.org/", CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.unique_id == BASE_URL
    assert mock_config_entry.data[CONF_URL] == BASE_URL


@pytest.mark.usefixtures("no_setup")
async def test_reconfigure_invalid_url(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "radio", CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}
    assert mock_config_entry.unique_id == BASE_URL


@pytest.mark.usefixtures("no_setup")
async def test_reconfigure_cannot_connect(
    hass: HomeAssistant, mock_client: MagicMock, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    mock_client.get_status.side_effect = AzuraCastConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "https://cast.example.org", CONF_API_KEY: "secret", CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert mock_config_entry.unique_id == BASE_URL


async def test_options_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_client: MagicMock
) -> None:
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_EXCLUDED_STATIONS: ["2"],
            CONF_KEEP_QUEUE_STATIONS: [],
            CONF_IMMEDIATE_CHANGE_STATIONS: ["1"],
            CONF_RELOAD_CHANGE_STATIONS: [],
            CONF_ALLOW_UNAVAILABLE_STATIONS: [],
            CONF_SCAN_INTERVAL: 120,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert init_integration.options[CONF_EXCLUDED_STATIONS] == ["2"]
    assert init_integration.runtime_data.stations[1].change_mode is ChangeMode.SKIP
    assert init_integration.options[CONF_SCAN_INTERVAL] == 120
    assert set(init_integration.runtime_data.stations) == {1}


async def test_options_flow_drops_missing_stations(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        options={
            CONF_EXCLUDED_STATIONS: ["99"],
            CONF_KEEP_QUEUE_STATIONS: ["1", "99"],
            CONF_IMMEDIATE_CHANGE_STATIONS: ["97"],
            CONF_RELOAD_CHANGE_STATIONS: ["96", "1"],
            CONF_ALLOW_UNAVAILABLE_STATIONS: ["98"],
            CONF_SCAN_INTERVAL: 60,
        },
    )
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert mock_config_entry.options == {
        CONF_EXCLUDED_STATIONS: [],
        CONF_KEEP_QUEUE_STATIONS: ["1"],
        CONF_IMMEDIATE_CHANGE_STATIONS: [],
        CONF_RELOAD_CHANGE_STATIONS: ["1"],
        CONF_ALLOW_UNAVAILABLE_STATIONS: [],
        CONF_SCAN_INTERVAL: 60,
    }
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_options_flow_not_loaded(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_loaded"


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (AzuraCastConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_options_flow_station_errors(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_client: MagicMock,
    exc: Exception,
    reason: str,
) -> None:
    mock_client.get_stations.side_effect = exc
    result = await hass.config_entries.options.async_init(init_integration.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
