"""Data coordinators for AzuraCast stations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import AzuraCastClient
from .api.errors import (
    AzuraCastAuthError,
    AzuraCastError,
    AzuraCastPermissionError,
    is_client_error,
)
from .api.models import NowPlaying, Playlist, QueueItem, Station
from .const import DOMAIN, EVENT_SONG_CHANGED, LOGGER
from .issues import set_conflicting_tags, set_missing_permissions
from .selectors import Selector, build_selectors, playlist_tags


@dataclass(frozen=True, slots=True)
class ManageData:
    """Management data of one station."""

    playlists: tuple[Playlist, ...]
    queue: tuple[QueueItem, ...]
    selectors: dict[str, Selector]
    # False when the key may manage media but not broadcasting (the queue is 403).
    can_broadcast: bool = True

    def playlist_by_name(self, name: str | None) -> Playlist | None:
        if not name:
            return None
        return next((p for p in self.playlists if p.name == name), None)


class ManageCoordinator(DataUpdateCoordinator[ManageData]):
    """Polls playlists and queue of a station the API key can manage."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AzuraCastClient,
        station: Station,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {station.shortcode} management",
            update_interval=interval,
        )
        self.client = client
        self.station = station

    async def _async_update_data(self) -> ManageData:
        try:
            playlists = await self.client.get_playlists(self.station.id)
        except AzuraCastAuthError as err:
            raise _auth_failed() from err
        except AzuraCastPermissionError as err:
            await self._async_check_api_key()
            set_missing_permissions(self.hass, self.config_entry, self.station, True)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="missing_permissions",
                translation_placeholders={"station": self.station.name},
            ) from err
        except AzuraCastError as err:
            raise _update_failed(err) from err
        # The queue needs the Broadcasting permission (and a station that has a
        # queue), playlists only Media: a 403 or another 4xx here alone leaves
        # playlists and selectors manageable.
        can_broadcast = True
        try:
            queue = await self.client.get_queue(self.station.id)
        except AzuraCastAuthError as err:
            raise _auth_failed() from err
        except AzuraCastError as err:
            if not is_client_error(err):
                raise _update_failed(err) from err
            LOGGER.debug(
                "Queue of station %s unavailable (%s); no broadcasting", self.station.name, err
            )
            queue, can_broadcast = [], False
        set_missing_permissions(self.hass, self.config_entry, self.station, False)
        selectors = build_selectors(playlists)
        set_conflicting_tags(
            self.hass,
            self.config_entry,
            self.station,
            [playlist for selector in selectors.values() for playlist in selector.conflicts],
        )
        return ManageData(
            playlists=tuple(playlists),
            queue=tuple(queue),
            selectors=selectors,
            can_broadcast=can_broadcast,
        )

    async def _async_check_api_key(self) -> None:
        """Tell a rejected key (reauth) from a key that lacks a station permission.

        AzuraCast answers 403 both when the key lacks a permission and when it
        no longer accepts the key (the request then runs as anonymous).
        """
        try:
            await self.client.get_account()
        except (AzuraCastAuthError, AzuraCastPermissionError) as err:
            raise _auth_failed() from err
        except AzuraCastError as err:
            raise _update_failed(err) from err


def _auth_failed() -> ConfigEntryAuthFailed:
    return ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed")


def _update_failed(err: AzuraCastError) -> UpdateFailed:
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key="update_failed",
        translation_placeholders={"error": str(err)},
    )


def tags_for_playlist(manage: ManageCoordinator | None, name: str | None) -> dict[str, str]:
    """Return the selector tags of a playlist, looked up by name."""
    if manage is None or manage.data is None:
        return {}
    playlist = manage.data.playlist_by_name(name)
    return playlist_tags(playlist) if playlist else {}


class NowPlayingCoordinator(DataUpdateCoordinator[NowPlaying | None]):
    """Holds now-playing data pushed by the hub; it never polls by itself."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        station: Station,
        tags_for: Callable[[str | None], dict[str, str]],
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {station.shortcode} now playing",
            update_interval=None,
        )
        self.station = station
        self._tags_for = tags_for
        self._last_song_id: str | None = None
        self.off_air = False

    async def _async_update_data(self) -> NowPlaying | None:
        return self.data

    @callback
    def mark_off_air(self) -> None:
        """The station is missing from a full now-playing list: it is stopped.

        AzuraCast leaves a station it is not broadcasting out of /nowplaying, so
        no data will come; entities that can tell "off" from "unknown" use this.
        """
        self.off_air = True
        self.async_set_updated_data(None)

    @callback
    def push(self, nowplaying: NowPlaying) -> None:
        self.off_air = False
        self.async_set_updated_data(nowplaying)
        current = nowplaying.current
        if current is None:
            return
        last_song_id = self._last_song_id
        self._last_song_id = current.song.id
        if last_song_id is None or last_song_id == current.song.id:
            return
        self.hass.bus.async_fire(
            EVENT_SONG_CHANGED,
            {
                "station_id": self.station.id,
                "station_shortcode": self.station.shortcode,
                "song": {
                    "id": current.song.id,
                    "artist": current.song.artist,
                    "title": current.song.title,
                    "album": current.song.album,
                    "art": current.song.art,
                },
                "playlist": current.playlist,
                "tags": self._tags_for(current.playlist),
            },
        )
