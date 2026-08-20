"""Deterministic tests for the serve-side A4 qualification install."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.a4_install import (  # noqa: E402
    A4EvidenceError,
    build_a4_qualification,
    load_a4_evidence_files,
)


def _positive_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": "mycelium.a4_product_positive_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "simulated": False,
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "topology_generation": 1,
        "model_id": "model-fixture",
        "resolved_commit": "commit-fixture",
        "manifest_digest": "sha256:" + "1" * 64,
        "qualification_digest": "sha256:" + "2" * 64,
        "path_manifest_digest": "sha256:" + "3" * 64,
        "graph_digest": "sha256:" + "4" * 64,
        "request_ids": ["request-a", "request-b"],
        "streams": [
            {"request_id": "request-a", "terminal": "completed"},
            {"request_id": "request-b", "terminal": "cancelled"},
        ],
        "cancellation": {"within_total_bound": True},
    }
    document.update(overrides)
    return document


def _negative_document(protocol: str, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": protocol,
        "passed": True,
        "simulated": False,
        "qualification_claim": False,
    }
    document.update(overrides)
    return document


def _evidence(
    positive: dict[str, object] | None = None,
    data_plane: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "positive_observations": (positive or _positive_document(),),
        "data_plane_observations": (
            data_plane
            or _negative_document("mycelium.a4_product_negative_data_plane.v1"),
        ),
        "qualification_observation": _negative_document(
            "mycelium.a4_product_negative_qualification_observation.v1"
        ),
        "shutdown_observation": _negative_document(
            "mycelium.a4_product_negative_shutdown_observation.v1"
        ),
        "positive_sources": (Path("positive.json"),),
        "data_plane_sources": (Path("data-plane.json"),),
        "qualification_source": Path("qualification.json"),
        "shutdown_source": Path("shutdown.json"),
    }


QUALIFICATION_DIGEST = "sha256:" + "a" * 64
GRAPH_DIGEST = "sha256:" + "4" * 64
MANIFEST_DIGEST = "sha256:" + "1" * 64


def test_build_a4_qualification_accepts_valid_evidence() -> None:
    document = build_a4_qualification(
        **_evidence(),
        qualification_digest=QUALIFICATION_DIGEST,
        graph_digest=GRAPH_DIGEST,
        manifest_digest=MANIFEST_DIGEST,
    )
    assert document["protocol"] == (
        "mycelium.product_concurrency_liveness_qualification.v1"
    )
    assert document["eligible"] is True
    assert document["qualification_digest"] == QUALIFICATION_DIGEST
    assert document["shared_process_termination_used"] is False
    assert document["evidence_digest"].startswith("sha256:")
    assert document["deployment_id"] == "deployment-fixture"


def test_build_rejects_missing_cancelled_terminal() -> None:
    evidence = _evidence(
        positive=_positive_document(
            streams={"request-a": {"terminal": "completed"}}
        )
    )
    with pytest.raises(A4EvidenceError, match="a4_positive_terminals_incomplete"):
        build_a4_qualification(
            **evidence,
            qualification_digest=QUALIFICATION_DIGEST,
            graph_digest=GRAPH_DIGEST,
            manifest_digest=MANIFEST_DIGEST,
        )


def test_build_rejects_bound_exceeded() -> None:
    evidence = _evidence(
        positive=_positive_document(cancellation={"within_total_bound": False})
    )
    with pytest.raises(A4EvidenceError, match="a4_positive_bound_exceeded"):
        build_a4_qualification(
            **evidence,
            qualification_digest=QUALIFICATION_DIGEST,
            graph_digest=GRAPH_DIGEST,
            manifest_digest=MANIFEST_DIGEST,
        )


def test_build_rejects_identity_mismatch() -> None:
    evidence = _evidence(
        positive=_positive_document(graph_digest="sha256:" + "9" * 64)
    )
    with pytest.raises(A4EvidenceError, match="a4_positive_identity_mismatch"):
        build_a4_qualification(
            **evidence,
            qualification_digest=QUALIFICATION_DIGEST,
            graph_digest=GRAPH_DIGEST,
            manifest_digest=MANIFEST_DIGEST,
        )


def test_build_rejects_unpassed_negative() -> None:
    evidence = _evidence(
        data_plane=_negative_document(
            "mycelium.a4_product_negative_data_plane.v1", passed=False
        )
    )
    with pytest.raises(A4EvidenceError, match="a4_negative_not_passed"):
        build_a4_qualification(
            **evidence,
            qualification_digest=QUALIFICATION_DIGEST,
            graph_digest=GRAPH_DIGEST,
            manifest_digest=MANIFEST_DIGEST,
        )


def test_build_rejects_simulated_positive() -> None:
    evidence = _evidence(positive=_positive_document(simulated=True))
    with pytest.raises(A4EvidenceError, match="a4_positive_simulated"):
        build_a4_qualification(
            **evidence,
            qualification_digest=QUALIFICATION_DIGEST,
            graph_digest=GRAPH_DIGEST,
            manifest_digest=MANIFEST_DIGEST,
        )


def test_load_a4_evidence_files_roundtrip(tmp_path: Path) -> None:
    positive = tmp_path / "positive.json"
    positive.write_text(json.dumps(_positive_document()), encoding="utf-8")
    data_plane = tmp_path / "data-plane.json"
    data_plane.write_text(
        json.dumps(
            _negative_document("mycelium.a4_product_negative_data_plane.v1")
        ),
        encoding="utf-8",
    )
    qualification = tmp_path / "qualification.json"
    qualification.write_text(
        json.dumps(
            _negative_document(
                "mycelium.a4_product_negative_qualification_observation.v1"
            )
        ),
        encoding="utf-8",
    )
    shutdown = tmp_path / "shutdown.json"
    shutdown.write_text(
        json.dumps(
            _negative_document(
                "mycelium.a4_product_negative_shutdown_observation.v1"
            )
        ),
        encoding="utf-8",
    )
    evidence = load_a4_evidence_files(
        positive=(positive,),
        data_plane=(data_plane,),
        qualification=qualification,
        shutdown=shutdown,
    )
    document = build_a4_qualification(
        **evidence,
        qualification_digest=QUALIFICATION_DIGEST,
        graph_digest=GRAPH_DIGEST,
        manifest_digest=MANIFEST_DIGEST,
    )
    assert document["eligible"] is True


def test_load_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_positive_document()), encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(A4EvidenceError, match="a4_artifact_unsafe"):
        load_a4_evidence_files(
            positive=(link,),
            data_plane=(real,),
            qualification=real,
            shutdown=real,
        )


def test_load_rejects_incomplete_set() -> None:
    with pytest.raises(A4EvidenceError, match="a4_evidence_incomplete"):
        load_a4_evidence_files(
            positive=(),
            data_plane=(),
            qualification=None,
            shutdown=None,
        )
