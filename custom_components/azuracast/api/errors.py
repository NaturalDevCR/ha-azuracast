"""Errors raised by the AzuraCast client."""

from __future__ import annotations

from typing import TypeIs


class AzuraCastError(Exception):
    """Base class for AzuraCast errors."""


class AzuraCastConnectionError(AzuraCastError):
    """The instance could not be reached."""


class AzuraCastResponseError(AzuraCastError):
    """The instance answered with an error or an unusable body."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")
        self.status = status
        self.message = message


class AzuraCastAuthError(AzuraCastResponseError):
    """The API key is missing or invalid (401)."""


class AzuraCastPermissionError(AzuraCastResponseError):
    """The API key lacks permission for this resource (403)."""


class AzuraCastNotFoundError(AzuraCastResponseError):
    """The resource does not exist (404)."""


_BY_STATUS: dict[int, type[AzuraCastResponseError]] = {
    401: AzuraCastAuthError,
    403: AzuraCastPermissionError,
    404: AzuraCastNotFoundError,
}


def error_for_status(status: int, message: str) -> AzuraCastResponseError:
    """Return the error class instance that matches an HTTP status."""
    return _BY_STATUS.get(status, AzuraCastResponseError)(status, message)


def is_client_error(err: AzuraCastError) -> TypeIs[AzuraCastResponseError]:
    """Whether the instance refused the request with a 4xx other than 401.

    AzuraCast answers 403 for a missing permission, but a station without the
    feature a route needs (e.g. a remote relay or AutoDJ off) may answer another
    4xx; neither will change by retrying.
    """
    return isinstance(err, AzuraCastResponseError) and 400 <= err.status < 500 and err.status != 401
