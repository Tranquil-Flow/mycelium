# SPDX-License-Identifier: AGPL-3.0-only
"""The direct/relay ledger survives a planned serve restart, fail-closed."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from mycelium_product_spine.projector import _unknown_internet_native
from mycelium_product_spine.state import (
    TRANSPORT_HISTORY_FILE,
    TRANSPORT_HISTORY_PROTOCOL,
    ProductEvidenceStateError,
    TransportHistoryStateStore,
)
from mycelium_product_spine.transport_history import MAX_RETAINED, TransportTransitionHistory

NOW = 1_000_000
ENDPOINT = "sha256:" + "a" * 64
DEPLOYMENT = "12345678-1234-5678-9234-abcdefabcdef"


def _observation(generation: int, transport: str, *, observed_at: int | None = None,
                 observation_id: str | None = None):
    base = copy.deepcopy(_unknown_internet_native(NOW)["activation_observation"])
    base.update(observation_id=observation_id or f"transport-{generation}-{transport}",
                connection_generation=generation, path_class=transport,
                path_source="bound_live_connection", endpoint_pseudonym=ENDPOINT,
                observed_at_unix_ms=observed_at if observed_at is not None else NOW - 5000 + generation,
                evidence_lifetime_until_unix_ms=NOW + 600_000, freshness="current",
                metrics=dict(rtt_ms=4, warm_rtt_ms=2, jitter_ms=1, goodput_bytes_per_second=1000,
                             loss_ratio=0, sample_count=2, measured_zero=True))
    return base


def _document(*history):
    document = _unknown_internet_native(NOW)
    history = list(history) or [document["activation_observation"]]
    document.update(activation_history=history, activation_observation=history[-1])
    return document


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _ledger(root: Path, binding: str = DEPLOYMENT) -> TransportTransitionHistory:
    return TransportTransitionHistory(store=TransportHistoryStateStore(root, binding=binding))


def test_a_planned_restart_keeps_the_direct_segment_and_observes_the_relay_transition(root):
    first = _ledger(root)
    first.observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    persisted = root / TRANSPORT_HISTORY_FILE
    assert persisted.stat().st_mode & 0o777 == 0o600
    document = json.loads(persisted.read_text())
    assert document["protocol"] == TRANSPORT_HISTORY_PROTOCOL
    assert document["binding"] == DEPLOYMENT

    # Serve 2: a fresh bind advanced the membership generation; force-relay.
    second = _ledger(root)
    published = second.observe(_document(_observation(9, "relay")), now_unix_ms=NOW)
    assert [(item["path_class"], item["connection_generation"])
            for item in published["activation_history"]] == [("direct", 6), ("relay", 9)]
    assert second.transitions() == [{
        "endpoint_pseudonym": ENDPOINT, "from_path_class": "direct", "to_path_class": "relay",
        "from_connection_generation": 6, "to_connection_generation": 9,
        "from_observation_id": "transport-6-direct", "to_observation_id": "transport-9-relay"}]


def test_a_regression_across_the_restart_is_refused(root):
    _ledger(root).observe(_document(_observation(9, "direct")), now_unix_ms=NOW)
    second = _ledger(root)
    published = second.observe(_document(_observation(6, "relay")), now_unix_ms=NOW)
    assert second.rejections["regression"] == 1
    assert published["activation_observation"]["path_class"] == "unknown"
    assert second.transitions() == []


def test_a_foreign_deployment_ledger_is_refused_not_merged(root):
    _ledger(root).observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    with pytest.raises(ProductEvidenceStateError) as raised:
        _ledger(root, binding="87654321-4321-8765-4321-fedcbafedcba")
    assert raised.value.code == "product_transport_history_foreign_deployment"


@pytest.mark.parametrize("tamper", [
    # A contradiction (same generation, other path class) injected into the file.
    lambda items: items + [_observation(6, "relay", observation_id="transport-6-forged",
                                        observed_at=NOW)],
    # A regression injected after the retained observation.
    lambda items: items + [_observation(3, "relay", observation_id="transport-3-forged")],
    # A duplicate observation id.
    lambda items: items + [copy.deepcopy(items[0])],
    # A malformed observation.
    lambda items: [{**items[0], "path_class": "teleport"}],
    # Over the bound.
    lambda items: [_observation(g, "direct" if g % 2 else "relay")
                   for g in range(1, MAX_RETAINED + 2)],
])
def test_a_tampered_ledger_fails_closed(root, tamper):
    _ledger(root).observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    path = root / TRANSPORT_HISTORY_FILE
    document = json.loads(path.read_text())
    document["observations"] = tamper(document["observations"])
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    with pytest.raises(ProductEvidenceStateError) as raised:
        _ledger(root)
    assert raised.value.code == "product_transport_history_corrupt"


def test_unparseable_or_wrong_protocol_ledger_fails_closed(root):
    path = root / TRANSPORT_HISTORY_FILE
    for payload in (b"{not json", json.dumps({"protocol": "other", "binding": DEPLOYMENT,
                                                "observations": []}).encode()):
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        with pytest.raises(ProductEvidenceStateError) as raised:
            _ledger(root)
        assert raised.value.code == "product_transport_history_corrupt"


def test_a_ledger_file_that_is_not_owner_private_is_refused(root):
    _ledger(root).observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    os.chmod(root / TRANSPORT_HISTORY_FILE, 0o644)
    with pytest.raises(ProductEvidenceStateError) as raised:
        _ledger(root)
    assert raised.value.code == "product_transport_history_unavailable"


def test_a_state_root_that_is_not_owner_private_is_refused(root):
    os.chmod(root, 0o755)
    with pytest.raises(ProductEvidenceStateError) as raised:
        _ledger(root)
    assert raised.value.code == "product_transport_history_root_invalid"


@pytest.mark.parametrize("binding", ["", "x" * 129, "has space", None, 7])
def test_the_binding_must_be_a_bounded_identifier(root, binding):
    with pytest.raises(ValueError, match="product_transport_history_binding_invalid"):
        TransportHistoryStateStore(root, binding=binding)


def test_without_a_store_the_ledger_stays_in_memory_only(tmp_path):
    ledger = TransportTransitionHistory()
    ledger.observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    assert list(tmp_path.iterdir()) == []


def test_unchanged_observations_do_not_rewrite_the_file(root):
    ledger = _ledger(root)
    ledger.observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    path = root / TRANSPORT_HISTORY_FILE
    before = path.stat().st_ino, path.read_bytes()
    ledger.observe(_document(_observation(6, "direct")), now_unix_ms=NOW)
    assert (path.stat().st_ino, path.read_bytes()) == before


def _application(root: Path, internet, *, binding: str | None = DEPLOYMENT):
    from tests.product_spine.test_application import _sources

    from mycelium_product_spine import ProductEvidenceApplication, ProductProjector

    members, route, qualification = _sources()
    return ProductEvidenceApplication(
        projector=ProductProjector(pseudonym_salt=b"s" * 32),
        membership_source=lambda: members,
        route_source=lambda: route,
        qualification_source=lambda: qualification,
        internet_native_source=lambda: internet,
        clock_unix_ms=lambda: NOW,
        state_root=str(root),
        transport_binding=binding,
    )


def test_the_product_application_serves_the_transition_across_a_planned_restart(root):
    _application(root, _document(_observation(6, "direct")))._publish()
    snapshot = _application(root, _document(_observation(9, "relay")))._publish()
    segments = [(s["transport"], s["connection_generation"], s["history"])
                for s in snapshot["route_explorer"]["segments"]
                if s["edge_kind"] == "transport_observation"]
    assert segments == [("direct", 6, True), ("relay", 9, False)]


def test_the_product_application_without_a_binding_keeps_the_ledger_in_memory(root):
    _application(root, _document(_observation(6, "direct")), binding=None)._publish()
    assert not (root / TRANSPORT_HISTORY_FILE).exists()


def test_a_binding_without_a_state_root_is_refused():
    from mycelium_product_spine import ProductEvidenceApplication, ProductProjector

    with pytest.raises(ValueError, match="product_transport_binding_requires_state_root"):
        ProductEvidenceApplication(
            projector=ProductProjector(pseudonym_salt=b"s" * 32),
            membership_source=list, route_source=dict, qualification_source=dict,
            transport_binding=DEPLOYMENT,
        )
