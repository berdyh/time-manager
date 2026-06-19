"""Tests for the local web API boundary."""

from __future__ import annotations

import json
from typing import Any

from tm.web.app import _mutation_guard, _web_request_guard


class _Client:
    def __init__(self, host: str) -> None:
        self.host = host


class _Request:
    def __init__(self, headers: dict[str, str], *, host: str | None = None) -> None:
        self.headers = headers
        self.client = _Client(host) if host is not None else None


def _payload(response: Any) -> dict[str, object]:
    body = response.body
    assert isinstance(body, bytes)
    return json.loads(body.decode("utf-8"))


def test_private_api_routes_require_web_token() -> None:
    blocked = _mutation_guard(_Request({}), "expected-token")

    assert blocked is not None
    assert blocked.status_code == 403
    assert _payload(blocked)["error"] == "invalid web token"


def test_private_api_routes_accept_status_token() -> None:
    assert (
        _mutation_guard(
            _Request({"x-tm-web-token": "expected-token"}), "expected-token"
        )
        is None
    )


def test_status_bootstrap_allows_local_requests_without_token() -> None:
    assert (
        _web_request_guard(
            _Request({}, host="127.0.0.1"), "expected-token", require_token=False
        )
        is None
    )


def test_cross_site_requests_are_rejected_even_with_token() -> None:
    blocked = _mutation_guard(
        _Request(
            {
                "x-tm-web-token": "expected-token",
                "sec-fetch-site": "cross-site",
            }
        ),
        "expected-token",
    )

    assert blocked is not None
    assert blocked.status_code == 403
    assert _payload(blocked)["error"] == "cross-site request blocked"


def test_cross_site_status_bootstrap_is_rejected_without_token() -> None:
    blocked = _web_request_guard(
        _Request({"sec-fetch-site": "cross-site"}, host="127.0.0.1"),
        "expected-token",
        require_token=False,
    )

    assert blocked is not None
    assert blocked.status_code == 403
    assert _payload(blocked)["error"] == "cross-site request blocked"


def test_remote_clients_are_rejected_even_with_token() -> None:
    blocked = _mutation_guard(
        _Request({"x-tm-web-token": "expected-token"}, host="192.0.2.10"),
        "expected-token",
    )

    assert blocked is not None
    assert blocked.status_code == 403
    assert _payload(blocked)["error"] == "remote web access blocked"
