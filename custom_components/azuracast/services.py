"""Services of the AzuraCast integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration services."""
