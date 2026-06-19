"""Tests for the local web API boundary."""

from __future__ import annotations

import json
from typing import Any

from tm.web.app import _mutation_guard


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _payload(response: Any) -> dict[str, object]:
    body = response.body
    assert isinstance(body, bytes)
    return json.loads(body.decode("utf-8"))


def test_mutating_routes_require_web_token() -> None:
    blocked = _mutation_guard(_Request({}), "expected-token")

    assert blocked is not None
    assert blocked.status_code == 403
    assert _payload(blocked)["error"] == "invalid web token"


def test_mutating_routes_accept_status_token() -> None:
    assert (
        _mutation_guard(
            _Request({"x-tm-web-token": "expected-token"}), "expected-token"
        )
        is None
    )


def test_cross_site_mutation_is_rejected_even_with_token() -> None:
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
