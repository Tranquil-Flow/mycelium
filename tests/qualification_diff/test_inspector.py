from __future__ import annotations

import json

from mycelium_qualification_diff import inspect_evidence_diff

from .conftest import canonical, make_bundle


def _category_documents(suffix: str) -> dict[str, object]:
    number = 1 if suffix == "a" else 2
    return {
        "run/qualification.json": {
            "deployment_epoch": number,
            "endpoints": [{"endpoint_id": f"private-endpoint-{suffix}"}],
            "graph": {"execution_graph_digest": "sha256:" + suffix * 64},
            "identity": {"deployment_id": f"deployment-{suffix}"},
            "kv_ownership": [{"peak_kv_bytes": number * 1024}],
            "load_proofs": [{"load_proof_digest": "sha256:" + suffix * 64}],
            "model": {
                "manifest_digest": "sha256:" + suffix * 64,
                "model_id": f"org/model-{suffix}",
                "resolved_commit": suffix * 40,
            },
            "negative_runs": [{"reason_code": f"negative-{suffix}"}],
            "parity": {"passed": suffix == "b"},
            "processes": [{"process_id": number}],
            "signatures": [{"signature": f"PRIVATE-SIGNATURE-CANARY-{suffix.upper()}"}],
            "tensors": {"assigned_tensor_keys": [f"layer.{number}"]},
            "timing": {"receiver_elapsed_ns": number * 10},
            "topology_version": number,
            "transport": {"adapter": f"transport-{suffix}"},
        },
        "qualification/source-provenance.json": {
            "source_commit": suffix * 40,
        },
    }


def test_report_is_canonical_deterministic_redacted_and_covers_every_required_category() -> None:
    baseline_manifest, baseline_files = make_bundle(_category_documents("a"))
    candidate_manifest, candidate_files = make_bundle(_category_documents("b"))

    first = inspect_evidence_diff(
        baseline_manifest,
        baseline_files,
        candidate_manifest,
        candidate_files,
    )
    second = inspect_evidence_diff(
        baseline_manifest,
        baseline_files,
        candidate_manifest,
        candidate_files,
    )
    report = json.loads(first)

    expected_categories = {
        "deployment_epoch",
        "endpoints",
        "graph",
        "identity",
        "kv_ownership",
        "load_proofs",
        "model_commit_manifest",
        "negative_runs",
        "parity",
        "processes",
        "signatures",
        "source_provenance",
        "tensors",
        "timing",
        "topology_version",
        "transport",
    }
    expected_counts = {category: 1 for category in expected_categories}
    expected_counts["model_commit_manifest"] = 3
    assert first == second == canonical(report)
    assert report["protocol"] == "mycelium.qualification_evidence_diff.v1"
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["qualification_evaluated"] is False
    assert report["inspection_only"] is True
    assert report["identical"] is False
    assert report["summary"]["total_changes"] == sum(expected_counts.values())
    assert expected_categories <= set(report["summary"]["by_category"])
    assert all(
        report["summary"]["by_category"][category] == count
        for category, count in expected_counts.items()
    )
    assert {change["category"] for change in report["changes"]} == expected_categories
    assert all(change["change"] == "changed" for change in report["changes"])
    assert all(
        change["code"] == f"{change['category'].upper()}_CHANGED"
        for change in report["changes"]
    )
    assert all(
        set(change)
        == {
            "after_digest",
            "before_digest",
            "category",
            "change",
            "code",
            "document_path_digest",
            "location_digest",
        }
        for change in report["changes"]
    )
    assert b"PRIVATE-SIGNATURE-CANARY" not in first
    assert b"private-endpoint" not in first
    assert b"deployment-a" not in first
    assert not first.endswith(b"\n")


def test_additions_removals_and_binary_changes_have_stable_codes() -> None:
    baseline_manifest, baseline_files = make_bundle(
        {"runtime/processes.json": {"processes": [{"process_id": 7}]}},
        raw_files={"provenance/dependencies.lock": b"BASELINE-SECRET-LOCK"},
    )
    candidate_manifest, candidate_files = make_bundle(
        {"runtime/tensors.json": {"tensor_keys": ["layer.7.weight"]}},
        raw_files={"provenance/dependencies.lock": b"CANDIDATE-SECRET-LOCK"},
    )

    output = inspect_evidence_diff(
        baseline_manifest,
        baseline_files,
        candidate_manifest,
        candidate_files,
    )
    report = json.loads(output)

    assert {change["code"] for change in report["changes"]} == {
        "PROCESSES_REMOVED",
        "SOURCE_PROVENANCE_CHANGED",
        "TENSORS_ADDED",
    }
    assert report["summary"]["by_change"] == {
        "added": 1,
        "changed": 1,
        "removed": 1,
    }
    assert b"SECRET-LOCK" not in output


def test_internal_manifest_location_cannot_collide_with_valid_evidence_path() -> None:
    baseline_manifest, baseline_files = make_bundle(
        raw_files={"@manifest": b"a"},
        run_id="baseline-run",
    )
    candidate_manifest, candidate_files = make_bundle(
        raw_files={"@manifest": b"b"},
        run_id="candidate-run",
    )

    report = json.loads(
        inspect_evidence_diff(
            baseline_manifest,
            baseline_files,
            candidate_manifest,
            candidate_files,
        )
    )

    assert {change["category"] for change in report["changes"]} == {
        "identity",
        "other",
    }
    assert len(
        {change["document_path_digest"] for change in report["changes"]}
    ) == 2


def test_identical_bundles_produce_empty_non_authoritative_report(small_bundle) -> None:
    manifest, files = small_bundle

    output = inspect_evidence_diff(manifest, files, manifest, files)
    report = json.loads(output)

    assert output == canonical(report)
    assert report["identical"] is True
    assert report["changes"] == []
    assert report["summary"]["total_changes"] == 0
    assert report["baseline"]["manifest_digest"] == report["candidate"]["manifest_digest"]
    assert report["route_ready"] is False
    assert report["release_ready"] is False
    assert report["qualification_evaluated"] is False
