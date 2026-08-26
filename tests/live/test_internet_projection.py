from __future__ import annotations

import json
from pathlib import Path

from mycelium_internet.activation import RelayProjector
from mycelium_live.route import (
    _merge_product_activation_history,
    _product_internet_native_projection,
)


ROOT = Path(__file__).resolve().parents[2]


def _path() -> dict:
    return json.loads(
        (ROOT / "contracts/compatibility-fixtures/transport-path-observation-v1.json").read_text()
    )


def _snapshot(paths: list[dict]) -> dict:
    return {
        "node-a": {
            "node_id": "node-a",
            "details": {"transport": {"transport_path_observations": paths}},
        }
    }


def test_product_projection_preserves_signed_path_transition_and_redacts_relay() -> None:
    direct = _path()
    relay = {
        **_path(),
        "connection_generation": 2,
        "path_class": "relay",
        "relay_identity": "https://relay.secret.example:443",
        "relay_region": "unknown",
        "measured_at_unix_ms": 2_000,
        "fresh_until_unix_ms": 7_202_000,
        "reconnect_count": 1,
        "selected_path_changes": 1,
    }
    key = b"r" * 32

    projection = _product_internet_native_projection(
        snapshots=_snapshot([direct, relay]),
        projection_key=key,
        now_unix_ms=2_000,
        route_available=True,
    )

    assert projection["activation_observation"]["path_class"] == "relay"
    assert projection["activation_observation"]["connection_generation"] == 2
    assert projection["activation_observation"]["connection_reuse"] == 7
    assert [item["path_class"] for item in projection["activation_history"]] == [
        "direct",
        "relay",
    ]
    assert projection["relay_projection"] == {
        "protocol": "mycelium.relay_projection.v1",
        "relay_reference": RelayProjector(projection_key=key).reference(
            "https://relay.secret.example:443"
        ),
        "region": "unknown",
        "projection_generation": 2,
        "stable": True,
        "observed_at_unix_ms": 2_000,
    }
    assert "relay.secret.example" not in json.dumps(projection)


def test_product_projection_degrades_stale_or_missing_path_to_unknown_not_zero() -> None:
    projection = _product_internet_native_projection(
        snapshots=_snapshot([_path()]),
        projection_key=b"r" * 32,
        now_unix_ms=8_000_000,
        route_available=True,
    )

    assert projection["activation_observation"]["path_class"] == "unknown"
    assert projection["activation_observation"]["metrics"] == {
        "rtt_ms": None,
        "warm_rtt_ms": None,
        "jitter_ms": None,
        "goodput_bytes_per_second": None,
        "loss_ratio": None,
        "sample_count": None,
        "measured_zero": False,
    }
    assert projection["activation_history"] == [projection["activation_observation"]]
    assert projection["relay_projection"] is None


def test_product_projection_retains_transition_across_successive_route_snapshots() -> None:
    direct = _path()
    relay = {
        **_path(),
        "connection_generation": 2,
        "path_class": "relay",
        "relay_identity": "https://relay.secret.example:443",
        "relay_region": "unknown",
        "measured_at_unix_ms": 2_000,
        "fresh_until_unix_ms": 7_202_000,
    }
    first = _product_internet_native_projection(
        snapshots=_snapshot([direct]),
        projection_key=b"r" * 32,
        now_unix_ms=1_000,
        route_available=True,
    )
    second = _product_internet_native_projection(
        snapshots=_snapshot([relay]),
        projection_key=b"r" * 32,
        now_unix_ms=2_000,
        route_available=True,
    )

    merged = _merge_product_activation_history(
        first["activation_history"], second
    )

    assert [item["path_class"] for item in merged["activation_history"]] == [
        "direct",
        "relay",
    ]
    assert merged["activation_observation"] == merged["activation_history"][-1]
