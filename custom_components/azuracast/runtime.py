"""Runtime data stored on the config entry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .api.client import AzuraCastClient
from .api.models import Station
from .const import ChangeMode
from .coordinator import ManageCoordinator, NowPlayingCoordinator
from .hub import NowPlayingHub


def station_device_key(entry_id: str, station_id: int) -> str:
    """Return the device identifier value of a station."""
    return f"{entry_id}_{station_id}"


@dataclass(slots=True)
class StationRuntime:
    """Everything the platforms need about one station."""

    station: Station
    device_key: str
    nowplaying: NowPlayingCoordinator
    manage: ManageCoordinator | None
    clear_queue_on_change: bool
    reject_unavailable_values: bool
    change_mode: ChangeMode = ChangeMode.WAIT

    @property
    def can_broadcast(self) -> bool:
        """Whether the key may use the queue and skip songs on this station."""
        return (
            self.manage is not None
            and self.manage.data is not None
            and self.manage.data.can_broadcast
        )


@dataclass(slots=True)
class AzuraCastRuntime:
    """Runtime data of one AzuraCast instance."""

    client: AzuraCastClient
    hub: NowPlayingHub
    stations: dict[int, StationRuntime]


type AzuraCastConfigEntry = ConfigEntry[AzuraCastRuntime]
