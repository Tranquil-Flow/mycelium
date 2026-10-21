# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio

import pytest

from mycelium_internet.contracts import compatibility_fixtures as internet_fixtures
from mycelium_live.supervisor import ColdRouteRestartError, _product_application
from tests.live.test_cold_route_restart import _owner_for, _Route, _Sink
from tests.live.test_product_application_internet import _Qualification, _snapshot


def _internet_native() -> dict:
    fixtures = internet_fixtures()
    activation = fixtures["internet-activation-observation-v1.json"]
    return {
        "bootstrap_status": fixtures["internet-bootstrap-status-v1.json"],
        "activation_observation": activation,
        "activation_history": [activation],
        "relay_projection": fixtures["relay-projection-v1.json"],
        "qualification": fixtures["internet-native-qualification-v1.json"],
    }


class _ProductRoute(_Route):
    """A route whose product readers fail once it is closed, like ``LiveRoute``."""

    def _require_open(self) -> None:
        if self.close_count:
            raise RuntimeError("live_seed_authority_unavailable")

    def product_membership_records(self) -> tuple[dict, ...]:
        self._require_open()
        return ()

    def product_assignment_records(self) -> tuple[dict, ...]:
        self._require_open()
        return ()

    def product_pseudonym_salt(self) -> bytes:
        self._require_open()
        return b"p" * 32

    def product_internet_native_snapshot(self) -> dict:
        self._require_open()
        return _internet_native()


def test_product_snapshot_follows_the_route_after_a_cold_restart() -> None:
    first = _ProductRoute("first", fail_on_infer=True)
    second = _ProductRoute("second")
    owner = _owner_for([first, second])
    owner.open()

    app = _product_application(owner, _Qualification())
    status, snapshot = asyncio.run(_snapshot(app))
    assert status == 200
    assert snapshot["internet_native"] == _internet_native()

    # The owner swaps in the second route and closes the first one.
    with pytest.raises(ColdRouteRestartError):
        owner.infer((1, 2), max_new_tokens=2, request_id="request-1", sink=_Sink())
    owner.release_request("request-1")
    assert first.close_count == 1
    assert owner.public_status()["route_name"] == "second"

    status, snapshot = asyncio.run(_snapshot(app))
    assert status == 200
    assert snapshot["internet_native"] == _internet_native()
