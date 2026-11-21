"""Tests for the client error helpers."""

from __future__ import annotations

import pytest

from custom_components.azuracast.api.errors import (
    AzuraCastConnectionError,
    AzuraCastError,
    error_for_status,
    is_client_error,
)


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (error_for_status(400, ""), True),
        (error_for_status(403, ""), True),
        (error_for_status(404, ""), True),
        (error_for_status(401, ""), False),
        (error_for_status(408, ""), False),
        (error_for_status(429, ""), False),
        (error_for_status(500, ""), False),
        (error_for_status(503, ""), False),
        (AzuraCastConnectionError("down"), False),
    ],
)
def test_is_client_error(err: AzuraCastError, expected: bool) -> None:
    assert is_client_error(err) is expected
