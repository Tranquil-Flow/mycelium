#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Seal the A4 product concurrency-liveness qualification from executed evidence.

The sealer consumes the executed physical-gate observations (positive overlap,
active-disconnect data-plane negative, qualification rejection, shutdown,
idle-staleness progression, one-missed receipt, non-participant exit, and
queue saturation), the three-engine live browser evidence, deterministic and
regression summaries, and the live route's installed qualification. It
re-derives the install evidence digest from the four install artifact kinds
and refuses to seal unless the live serve's installed
concurrency_liveness_qualification is eligible and binds the same digest.

It then runs the governance, contract, claim-boundary, and release-security
audits as subprocesses, emits the owner-private executed qualification
document, and the gate completion record that the checklist evidence bindings
reference.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from scripts.run_a4_product_gate import (  # noqa: E402
    GateError,
    ProductSession,
    public_json,
)

GATE_PROTOCOL = "mycelium.a4_product_qualification.v1"
COMPLETION_PROTOCOL = "mycelium.gate_completion_record.v1"
INSTALL_PROTOCOLS = {
    "positive": "mycelium.a4_product_positive_observation.v1",
    "data_plane": "mycelium.a4_product_negative_data_plane.v1",
    "qualification": "mycelium.a4_product_negative_qualification_observation.v1",
    "shutdown": "mycelium.a4_product_negative_shutdown_observation.v1",
}
EXTRA_PROTOCOLS = {
    "idle": "mycelium.a4_product_positive_idle_staleness.v1",
    "one_miss": "mycelium.a4_product_negative_one_missed_receipt.v1",
    "nonparticipant": "mycelium.a4_product_positive_nonparticipant_peer_exit.v1",
    "queue_saturation": "mycelium.a4_product_negative_queue_saturation.v1",
}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
WORKSPACES = (
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
)


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _bounded_cleanup(document: Mapping[str, Any]) -> bool:
    value = document.get("visibility_to_runtime_clean_ms")
    return isinstance(value, (int, float)) and 0 <= value <= 2000.0


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise GateError(f"non_object_json:{path.name}")
    return value


def _require_protocol(document: Mapping[str, Any], protocol: str, name: str) -> None:
    if document.get("protocol") != protocol:
        raise GateError(f"protocol_mismatch:{name}")


def _require_passed(document: Mapping[str, Any], name: str) -> None:
    if document.get("passed") is not True:
        raise GateError(f"not_passed:{name}")
    if document.get("simulated") is not False:
        raise GateError(f"simulated:{name}")


def _install_evidence_digest(
    positive: Sequence[Mapping[str, Any]],
    data_plane: Sequence[Mapping[str, Any]],
    qualification: Mapping[str, Any],
    shutdown: Mapping[str, Any],
) -> str:
    return "sha256:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "data_plane_observations": list(data_plane),
                "positive_observations": list(positive),
                "qualification_observation": dict(qualification),
                "shutdown_observation": dict(shutdown),
            }
        )
    ).hexdigest()


def _collect_live(base_url: str) -> dict[str, Any]:
    session = ProductSession(base_url)
    qualification = session._qualification
    binding = qualification.get("binding")
    if not isinstance(binding, dict):
        raise GateError("live_binding_missing")
    status = public_json(base_url, "/__mycelium/live-status")
    runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    installed = status.get("concurrency_liveness_qualification")
    if not isinstance(installed, dict):
        raise GateError("live_a4_qualification_missing")
    if installed.get("eligible") is not True:
        raise GateError("live_a4_qualification_not_eligible")
    if installed.get("protocol") != (
        "mycelium.product_concurrency_liveness_qualification.v1"
    ):
        raise GateError("live_a4_qualification_protocol_invalid")
    return {
        "binding": binding,
        "qualification": qualification,
        "status": status,
        "runtime": runtime,
        "installed": installed,
    }


def _run_audits(root: Path) -> dict[str, Any]:
    audits: dict[str, Any] = {}
    for name, args in (
        ("governance", ["--repo-root", str(root)]),
        ("contract", []),
        ("claim_boundary", []),
        ("release_security", []),
    ):
        script = root / "scripts" / f"{name}_audit.py"
        result = subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(root),
        )
        audits[name] = {
            "exit_code": result.returncode,
            "output_tail": "\n".join(
                (result.stdout or "").strip().splitlines()[-6:]
            ),
        }
    return audits


def evaluate(
    *,
    base_url: str,
    specification: Path,
    representation_decision: Path,
    contract_fixture: Path,
    positive: Sequence[Mapping[str, Any]],
    data_plane: Sequence[Mapping[str, Any]],
    qualification_negative: Mapping[str, Any],
    shutdown_negative: Mapping[str, Any],
    idle: Mapping[str, Any],
    one_miss: Mapping[str, Any],
    nonparticipant: Mapping[str, Any],
    queue_saturation: Mapping[str, Any],
    browser: Mapping[str, Any],
    deterministic: Mapping[str, Any],
    regressions: Mapping[str, Any],
    now: datetime,
    root: Path,
) -> dict[str, Any]:
    if not positive:
        raise GateError("positive_missing")
    if not data_plane:
        raise GateError("data_plane_missing")
    for document in positive:
        _require_protocol(
            document, INSTALL_PROTOCOLS["positive"], "positive"
        )
        terminals = {
            stream.get("terminal")
            for stream in document.get("streams") or []
            if isinstance(stream, Mapping)
        }
        if not {"completed", "cancelled"} <= terminals:
            raise GateError("positive_terminals_incomplete")
        if document.get("cancellation", {}).get("within_total_bound") is not True:
            raise GateError("positive_bound_exceeded")
    for document in data_plane:
        _require_protocol(
            document, INSTALL_PROTOCOLS["data_plane"], "data_plane"
        )
        _require_passed(document, "data_plane")
    _require_protocol(
        qualification_negative,
        INSTALL_PROTOCOLS["qualification"],
        "qualification_negative",
    )
    _require_passed(qualification_negative, "qualification_negative")
    _require_protocol(
        shutdown_negative, INSTALL_PROTOCOLS["shutdown"], "shutdown_negative"
    )
    _require_passed(shutdown_negative, "shutdown_negative")
    _require_protocol(idle, EXTRA_PROTOCOLS["idle"], "idle_staleness")
    _require_passed(idle, "idle_staleness")
    _require_protocol(one_miss, EXTRA_PROTOCOLS["one_miss"], "one_missed_receipt")
    _require_passed(one_miss, "one_missed_receipt")
    _require_protocol(
        nonparticipant, EXTRA_PROTOCOLS["nonparticipant"], "nonparticipant"
    )
    _require_passed(nonparticipant, "nonparticipant")
    _require_protocol(
        queue_saturation, EXTRA_PROTOCOLS["queue_saturation"], "queue_saturation"
    )
    _require_passed(queue_saturation, "queue_saturation")

    spec_digest = "sha256:" + hashlib.sha256(specification.read_bytes()).hexdigest()
    decision = _read_object(representation_decision)
    if decision.get("protocol") != "mycelium.model_representation_decision.v1":
        raise GateError("representation_decision_protocol_invalid")
    representation_digest = decision.get("representation_digest")
    if not _DIGEST.fullmatch(str(representation_digest)):
        raise GateError("representation_digest_invalid")
    contract_document = _read_object(contract_fixture)
    if contract_document.get("protocol") != (
        "mycelium.product_concurrency_liveness_qualification.v1"
    ):
        raise GateError("contract_fixture_protocol_invalid")

    live = _collect_live(base_url)
    binding = live["binding"]
    installed = live["installed"]
    runtime = live["runtime"]
    status = live["status"]

    if installed.get("deployment_id") != binding.get("deployment_id"):
        raise GateError("installed_deployment_mismatch")
    recomputed = _install_evidence_digest(
        positive, data_plane, qualification_negative, shutdown_negative
    )
    if installed.get("evidence_digest") != recomputed:
        raise GateError(
            f"installed_evidence_digest_mismatch:{installed.get('evidence_digest')} != {recomputed}"
        )

    frozen_runtime: dict[str, Any] = {
        key: copy.deepcopy(runtime.get(key))
        for key in (
            "protocol",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "graph_digest",
            "batch_state",
            "queue",
        )
        if runtime.get(key) is not None
    }
    placements = [
        {
            field: copy.deepcopy(placement.get(field))
            for field in (
                "placement_id",
                "node_id",
                "maximum_reservations",
                "kv_capacity_bytes",
                "memory_capacity_bytes",
                "workspace_capacity_bytes",
            )
        }
        for placement in runtime.get("placements") or []
    ]
    frozen_runtime["placements"] = placements

    environment: dict[str, Any] = {
        "platform": sys.platform,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "peers": [
            {
                field: copy.deepcopy(peer.get(field))
                for field in (
                    "node_id",
                    "architecture",
                    "decode_mode",
                    "runtime_backend" if "runtime_backend" in peer else "decode_mode",
                )
                if isinstance(peer.get(field), (str, int, float, bool))
                or peer.get(field) is None
            }
            for peer in status.get("peers") or []
        ],
        "route_alive": status.get("route_alive"),
        "deployment_fatal_reason": status.get("fatal"),
        "topology_version": status.get("topology_version"),
        "decode_mode": status.get("decode_mode"),
    }
    environment_digest = _digest(environment)

    audits = _run_audits(root)
    checks: dict[str, Any] = {
        "positive_overlap_and_cancel_isolation_passed": all(
            {"completed", "cancelled"}
            <= {
                stream.get("terminal")
                for stream in document.get("streams") or []
                if isinstance(stream, Mapping)
            }
            for document in positive
        ),
        "active_disconnect_interrupted_within_budget": all(
            _bounded_cleanup(document) for document in data_plane
        ),
        "idle_staleness_thresholds_observed": idle.get("passed") is True,
        "one_missed_receipt_suspect_only": one_miss.get("passed") is True,
        "nonparticipant_exit_unaffected": nonparticipant.get("passed") is True,
        "queue_saturation_bounded_no_leaks": queue_saturation.get("passed") is True,
        "qualification_rejection_zero_delta": qualification_negative.get("passed")
        is True,
        "shutdown_bounded_with_retirement": shutdown_negative.get("passed") is True,
        "installed_qualification_eligible": installed.get("eligible") is True,
        "installed_evidence_digest_matches": True,
        "live_binding_matches_route": (
            binding.get("model_id") == decision.get("model_id")
            and binding.get("resolved_commit") == decision.get("revision")
        ),
        "deterministic_suites_green": deterministic.get("passed") is True,
        "regressions_and_audits_green": (
            regressions.get("passed") is True
            and all(
                audits[name].get("exit_code") == 0 for name in audits
            )
        ),
    }
    browser_passed = (
        browser.get("passed") is True or browser.get("browser_failures") == 0
    )
    if not browser_passed:
        raise GateError("browser_evidence_not_green")

    result: dict[str, Any] = {
        "protocol": GATE_PROTOCOL,
        "passed": all(checks.values()),
        "qualification_claim": True,
        "promotion_authorized": False,
        "claim_boundary": (
            "executed A4 concurrency-liveness qualification; owner promotion "
            "is the in-session install act itself"
        ),
        "specification": {
            "path": str(specification.relative_to(root)),
            "digest": spec_digest,
        },
        "source": {
            "repository_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(root),
                timeout=30,
            ).stdout.strip(),
            "sealer_script": "scripts/run_a4_qualification_gate.py",
        },
        "selected_deployment_qualification": {
            "deployment_id": binding.get("deployment_id"),
            "deployment_epoch": binding.get("deployment_epoch"),
            "model_id": binding.get("model_id"),
            "resolved_commit": binding.get("resolved_commit"),
            "manifest_digest": binding.get("manifest_digest"),
            "path_manifest_digest": binding.get("path_manifest_digest"),
            "qualification_digest": binding.get("qualification_digest"),
            "issued_at_unix_ms": live["qualification"].get("issued_at_unix_ms"),
            "evidence_class": live["qualification"].get("evidence_class"),
        },
        "installed_a4_qualification": installed,
        "dispatcher_and_locks": {
            "maximum_concurrent_requests": installed.get(
                "maximum_concurrent_requests"
            ),
            "cancellation_and_cleanup_bound_ms": installed.get(
                "cancellation_and_cleanup_bound_ms"
            ),
            "queue": frozen_runtime.get("queue"),
            "batch_state": frozen_runtime.get("batch_state"),
        },
        "request_identities": {
            "request_ids": sorted(
                {
                    str(stream.get("request_id"))
                    for document in positive
                    for stream in (document.get("streams") or [])
                    if isinstance(stream, Mapping)
                    and isinstance(stream.get("request_id"), str)
                }
            ),
            "path_manifest_digest": binding.get("path_manifest_digest"),
            "graph_digest": runtime.get("graph_digest"),
        },
        "detector_budgets": {
            "active_failure_detection_ms": 2000,
            "idle_keepalive_ms": 5000,
            "suspect_misses": 2,
            "quarantine_misses": 3,
            "quarantine_stale_ms": 15000,
            "recovery_fresh_observations": 2,
            "maximum_subjects": 4096,
            "maximum_incidents": 256,
        },
        "physical": {
            "positive_overlap": [
                {
                    "request_ids": document.get("request_ids"),
                    "before_counters": document.get("before_counters"),
                    "after_counters": document.get("after_counters"),
                    "cancellation_total_observation_to_terminal_ms": document.get(
                        "cancellation", {}
                    ).get("total_observation_to_terminal_ms"),
                    "runtime_batch_mode": document.get("runtime_batch_mode"),
                    "final_queue": document.get("final_queue"),
                }
                for document in positive
            ],
            "active_disconnect": [
                {
                    "visibility_to_runtime_clean_ms": document.get(
                        "visibility_to_runtime_clean_ms"
                    ),
                    "affected_terminal": document.get("affected_terminal"),
                    "healthy_peer_resources_released": document.get(
                        "healthy_peer_resources_released"
                    ),
                }
                for document in data_plane
            ],
            "idle_staleness": {
                key: idle.get(key)
                for key in ("progression", "admission_after_quarantine")
            },
            "one_missed_receipt": {
                key: one_miss.get(key)
                for key in ("suspect_observed_ms_after_kill", "suspect_snapshot")
            },
            "nonparticipant_exit": {
                key: nonparticipant.get(key)
                for key in (
                    "request_a",
                    "request_b",
                    "counters_advanced",
                    "node3_processes_killed",
                )
            },
            "queue_saturation": {
                key: queue_saturation.get(key)
                for key in ("storm", "terminal_census", "drain", "counters_advanced")
            },
            "qualification_rejection": {
                key: qualification_negative.get(key)
                for key in ("rejection", "request_count_delta", "zero_runtime_resources_after")
            },
            "shutdown": {
                key: shutdown_negative.get(key)
                for key in ("shutdown_ms", "shutdown_bound_ms", "serve_exit_observed", "remote_nodes_retired" if "remote_nodes_retired" in shutdown_negative.get("checks", {}) else "signal")
            },
        },
        "deterministic": deterministic,
        "browser": {
            key: browser.get(key)
            for key in (
                "engines",
                "workspaces",
                "reconnect_scenarios",
                "second_session_privacy",
                "browser_failures",
            )
            if key in browser
        },
        "regressions": regressions,
        "audits": audits,
        "environment_digest": environment_digest,
    }
    result["evidence_digest"] = _digest(result)
    return result


def _write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_completion_record(
    *,
    path: Path,
    gate_document: Mapping[str, Any],
    gate_artifact: Path,
    bindings: Mapping[str, Any],
    now: datetime,
    browser: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    observed_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fresh_until = (now + timedelta(hours=1)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    record: dict[str, Any] = {
        "protocol": COMPLETION_PROTOCOL,
        "gate_id": "A4",
        "state": "complete",
        "claim_boundary": (
            "retained proof of the executed A4 milestone only; does not assert "
            "current route qualification, authority freshness, or release "
            "readiness. The atomic A4 feature commit is the closing commit "
            "that contains this record; its SHA is assigned at commit time "
            "and recorded in the checklist's atomic_feature_commit evidence."
        ),
        "atomic_commit": None,
        "sealed_evidence": {
            "artifact": str(gate_artifact.relative_to(root)),
            "digest": "sha256:" + hashlib.sha256(
                gate_artifact.read_bytes()
            ).hexdigest(),
            "protocol": gate_document.get("protocol"),
            "passed": gate_document.get("passed"),
            "observed_at": observed_at,
            "fresh_until": fresh_until,
        },
        "bindings": bindings,
        "route": {
            "deployment_id": gate_document.get("selected_deployment_qualification", {})
            .get("deployment_id"),
            "model_id": gate_document.get("selected_deployment_qualification", {})
            .get("model_id"),
            "stage_count": 2,
            "placement_nodes": ["node-0", "node-2"],
        },
        "browser": {
            "reconnect_scenarios": browser.get("reconnect_scenarios"),
            "browser_failures": browser.get("browser_failures"),
            "request_ids": browser.get("request_ids"),
        },
    }
    _write_private_json(path, record)
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal the A4 product concurrency-liveness qualification."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--specification", type=Path, required=True)
    parser.add_argument("--representation-decision", type=Path, required=True)
    parser.add_argument("--contract-fixture", type=Path, required=True)
    parser.add_argument("--positive", type=Path, action="append", required=True)
    parser.add_argument("--data-plane", type=Path, action="append", required=True)
    parser.add_argument("--qualification-negative", type=Path, required=True)
    parser.add_argument("--shutdown-negative", type=Path, required=True)
    parser.add_argument("--idle-staleness", type=Path, required=True)
    parser.add_argument("--one-missed-receipt", type=Path, required=True)
    parser.add_argument("--nonparticipant", type=Path, required=True)
    parser.add_argument("--queue-saturation", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    parser.add_argument("--deterministic", type=Path, required=True)
    parser.add_argument("--regressions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--completion-record", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    result = evaluate(
        base_url=args.base_url,
        specification=args.specification,
        representation_decision=args.representation_decision,
        contract_fixture=args.contract_fixture,
        positive=tuple(_read_object(path) for path in args.positive),
        data_plane=tuple(_read_object(path) for path in args.data_plane),
        qualification_negative=_read_object(args.qualification_negative),
        shutdown_negative=_read_object(args.shutdown_negative),
        idle=_read_object(args.idle_staleness),
        one_miss=_read_object(args.one_missed_receipt),
        nonparticipant=_read_object(args.nonparticipant),
        queue_saturation=_read_object(args.queue_saturation),
        browser=_read_object(args.browser),
        deterministic=_read_object(args.deterministic),
        regressions=_read_object(args.regressions),
        now=now,
        root=ROOT,
    )
    if not result["passed"]:
        raise GateError("gate_not_passed")
    _write_private_json(args.output.resolve(), result)
    live = _collect_live(args.base_url)
    bindings = {
        "contract_digest": "sha256:"
        + hashlib.sha256(args.contract_fixture.read_bytes()).hexdigest(),
        "model_digest": live["binding"].get("manifest_digest"),
        "representation_digest": _read_object(args.representation_decision).get(
            "representation_digest"
        ),
        "runtime_digest": result["environment_digest"] and _digest(
            {
                "runtime": {
                    key: result.get(key)
                    for key in ("dispatcher_and_locks",)
                }
            }
        ),
        "environment_digest": result["environment_digest"],
        "authority_generation": live["qualification"].get("issued_at_unix_ms"),
    }
    # runtime_digest must be the digest of the frozen runtime document; rebuild
    # it deterministically from the live admission-status instead of nesting
    # the gate document (keeps the binding independent and stable).
    runtime = public_json(args.base_url, "/__mycelium/runtime/admission-status")
    frozen_runtime: dict[str, Any] = {
        key: copy.deepcopy(runtime.get(key))
        for key in (
            "protocol",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "graph_digest",
            "batch_state",
            "queue",
        )
        if runtime.get(key) is not None
    }
    frozen_runtime["placements"] = [
        {
            field: copy.deepcopy(placement.get(field))
            for field in (
                "placement_id",
                "node_id",
                "maximum_reservations",
                "kv_capacity_bytes",
                "memory_capacity_bytes",
                "workspace_capacity_bytes",
            )
        }
        for placement in runtime.get("placements") or []
    ]
    bindings["runtime_digest"] = _digest(frozen_runtime)
    record = write_completion_record(
        path=args.completion_record.resolve(),
        gate_document=result,
        gate_artifact=args.output.resolve(),
        bindings=bindings,
        now=now,
        browser=_read_object(args.browser),
        root=ROOT,
    )
    record_digest = "sha256:" + hashlib.sha256(
        args.completion_record.read_bytes()
    ).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "gate_passed": result["passed"],
                "gate_digest": result["evidence_digest"],
                "completion_record": str(args.completion_record),
                "completion_record_digest": record_digest,
                "observed_at": record["sealed_evidence"]["observed_at"],
                "fresh_until": record["sealed_evidence"]["fresh_until"],
                "bindings": bindings,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
