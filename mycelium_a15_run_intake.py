# SPDX-License-Identifier: AGPL-3.0-only
"""A15 executed-artifact intake: bounded, fail-closed intake over REAL retained artifacts.

**This is not the A15 production verifier and not a release decision.** It reads the
actual bytes of retained pilot artifacts, checks the claims those artifacts make about
themselves (declared digest, declared protocol, declared source commit, provenance class,
evidence expiry), and then assembles an explicitly **not-ready** report naming the
mandatory evidence that is still missing and the verifier contracts that do not exist
yet. It never produces, and structurally cannot produce, a readiness/`complete` claim.

Design constraints that are load-bearing:

* **Fail closed.** A missing, unreadable, undeclared-digest, mismatched-digest,
  non-allowlisted-protocol, escaping-path, wrong-provenance or expired input is a
  finding. Nothing unsettled is ever treated as a pass, and `intake_ok` is not a
  readiness signal.
* **Real bytes only.** Digests are computed by streaming the files the declaration
  names. No fixture body is substituted for a live artifact, and a declaration whose
  provenance class is `fixture`/`replay` may not stand in for a mandatory live artifact.
* **Retained flags are not authority.** A retained `verdict: GO` or `state: complete`
  is recorded as an *observation* and never consulted to derive readiness.
* **Not the synthetic validator.** `tests/a15_acceptance/validator.py` is a design-only
  validator of a canonical *synthetic* candidate graph with `fixture-signature:`
  exclusions. That module is not imported here and its green result is never live
  evidence. This module is intentionally a different, narrower thing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from mycelium_replica_contracts import (
    BENCHMARK_ACCEPTANCE_V2_PROTOCOL,
    REPLICA_BENCHMARK_V2_PROTOCOL,
    validate_replica_benchmark_v2,
)

PROTOCOL = "mycelium.a15_run_intake.v1"
REPORT_PROTOCOL = "mycelium.a15_run_intake_report.v1"

CLAIM_BOUNDARY = (
    "non-authoritative executed-artifact intake over retained artifacts: reads real bytes and "
    "reports observed digests, protocol/source/provenance/expiry findings, and missing mandatory "
    "evidence. It is not the A15 production verifier, not an executed-result graph, not a release "
    "manifest, not a signature, not a governance decision, and never a readiness or completion "
    "claim. The A15 gate state remains design_only."
)

_ERR = "a15_run_intake_declaration_invalid"
_DIGEST = "sha256:"
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")

# Protocols actually observed in the retained artifacts this slice reads, plus the
# A15 target protocols the spec names. Anything else fails closed.
ALLOWED_ARTIFACT_PROTOCOLS = frozenset(
    {
        # retained pilot producers
        "mycelium.pilot_readiness_gate.v1",
        "mycelium.pilot_readiness_gate.package.v1",
        "mycelium.pilot_readiness_gate.package",
        "mycelium.service_package.v1",
        "mycelium.completion_checklist.v1",
        "mycelium.gate_completion_record.v1",
        "mycelium.executed_requirement_evidence.v1",
        "mycelium.candidate_source_manifest.v1",
        "mycelium.a3_useful_model_qualification.v1",
        "mycelium.a4_product_qualification.v1",
        "mycelium.a5_product_positive_observation.v1",
        "mycelium.a5_product_browser_observation.v1",
        "mycelium.a5_benchmark_evidence.v1",
        "mycelium.a5_benchmark_evidence.v2",
        "mycelium.a5_concurrent_cancellation_repeat.v1",
        "mycelium.a5_negative_illegal_observation.v1",
        "mycelium.a5_replica_loss_negative_observation.v1",
        # A15 target languages from the spec (producers may not exist yet)
        "mycelium.executed_gate_result.v1",
        "mycelium.executed_result_graph.v1",
        "mycelium.executed_release_decision.v1",
    }
)

# The A5 benchmark evidence envelope v2 is admitted only as the envelope of the
# pre-registered calibrated-load protocol v2 (owner rulings of 2026-09-27): its
# bytes must bind exactly this protocol digest at every level and carry a valid
# mycelium.replica_benchmark.v2 record. v1 stays admitted for historical r33
# evidence, unchanged. A tests/ pin keeps this digest equal to the registered
# tests/a5_acceptance/benchmark_protocol.v2.json.
A5_BENCHMARK_EVIDENCE_V2_PROTOCOL = "mycelium.a5_benchmark_evidence.v2"
A5_BENCHMARK_PROTOCOL_V2_DIGEST = (
    "sha256:d7863cdf7f7072cf6b7e8fc243fbe775b8a90b64fbf9a31b49650f93818740e8"
)

ALLOWED_PROVENANCE = frozenset({"source", "live", "replay", "fixture", "historical"})

# Provenance classes that can never stand in for a mandatory executed artifact.
NON_SUBSTITUTABLE_PROVENANCE = frozenset({"fixture", "replay"})

CLOSURE_GATES = tuple(f"A{index}" for index in range(3, 15))

REQUIRED_DIGEST_CLASSES = (
    "test",
    "audit",
    "physical",
    "browser",
    "model",
    "contract",
    "package",
    "sbom",
)

# Spec section 4 matrices, as bounded slugs. None of these has a retained current result.
REQUIRED_MATRIX_EVIDENCE = (
    "clean_python_and_rust_suites",
    "contract_compatibility_and_canonicalization",
    "governance_and_dependency_sbom",
    "frontend_engine_matrix_and_accessibility",
    "restart_reconnect_and_checkpoint_resume",
    "model_discovery_representation_and_physical_qualification",
    "concurrency_replication_recovery_batching_and_speculation",
    "internet_native_membership_and_route_explorer",
)

# What stands between the retained artifacts this slice reads and a current executed gate
# result. A release-decision producer now exists in ``mycelium_a15_release_decision``; it
# verifies a candidate graph of signed receipts under a pinned trust registry. None of the
# retained artifacts is in that form, this slice never calls the producer, and the producer
# has no production trust registry pinned. The entries name those remaining gaps and must
# not be read as "no verifier exists".
MISSING_VERIFIER_CONTRACTS = (
    {
        "contract": "mycelium.executed_gate_result.v1",
        "reason": (
            "retained artifacts are not executed-result envelopes backed by a signed result "
            "receipt; this slice does not convert them and must not, and the design-only "
            "synthetic validator in tests/a15_acceptance/validator.py is never called on "
            "retained artifacts or reported as live evidence"
        ),
    },
    {
        "contract": "mycelium.executed_result_graph.v1",
        "reason": (
            "no content-addressed A2 to A14 result graph has been assembled from current "
            "executions; the producer verifies such a graph but this slice does not build one"
        ),
    },
    {
        "contract": "mycelium.executed_release_decision.v1",
        "reason": (
            "this slice emits no decision generation and none may be inferred from retained "
            "records; the producer's decision is unsigned and no production decision exists"
        ),
    },
    {
        "contract": "mycelium.exclusion_signature_verifier.v1",
        "reason": (
            "no owner exclusion-signer key is registered in any pinned production trust "
            "registry, so no owner exclusion can be verified for a release; the synthetic "
            "'fixture-signature:' form is never a signature"
        ),
    },
    {
        "contract": "mycelium.verifier_policy_registry.v1",
        "reason": (
            "the producer's production trust-registry allowlist is empty: no owner-reviewed "
            "registry of verifier keys and verifier-policy digests has been pinned"
        ),
    },
    {
        "contract": "mycelium.artifact_reference_root_policy.v1",
        "reason": (
            "retained artifact references are ambiguous about their evidence root: A3 names a "
            "repo-relative-looking path that only resolves under the off-repo physical evidence root, "
            "and A5 names absolute owner-private paths; they do not use the producer's explicit "
            "root-id references"
        ),
    },
    {
        "contract": "mycelium.authority_generation_currentness.v1",
        "reason": (
            "the current authority generation is declared only by a pinned trust registry and "
            "none is pinned for production, so a retained record cannot be promoted to a "
            "current gate result"
        ),
    },
)

NON_AUTHORITATIVE_INPUTS = {
    "tests/a15_acceptance/validator.py": (
        "design-only synthetic graph validator; accepts a canonical synthetic candidate with "
        "fixture-signature exclusions; its green result is not live evidence"
    ),
    "tests/a15_acceptance/inventory.v1.json": (
        "frozen acceptance inputs and rejection behaviour, not an executed-result graph and not a producer"
    ),
    "docs/handover/mycelium-completion-checklist.v1.json": (
        "navigation and declared bindings, not evidence"
    ),
    "docs/handover/a*-completion-record.v1.json": (
        "sealed historical proof of an executed milestone; not current authority, not freshness, "
        "not release readiness"
    ),
}

SCOPE_LIMITS = (
    "not_a_release_decision",
    "not_the_a15_production_verifier",
    "not_a_signature_or_governance_decision",
    "retained_flags_are_not_authority",
    "gate_state_design_only",
    "no_public_security_claim",
    "no_byzantine_or_anonymous_compute_claim",
    "no_multi_tenant_claim",
    "report_is_time_invariant_by_design",
)

# Finding codes, so callers can assert on stable strings.
F_MISSING = "artifact_missing"
F_UNREADABLE = "artifact_unreadable"
F_NOT_REGULAR = "artifact_not_regular_file"
F_ESCAPE = "artifact_path_escapes_root"
F_DIGEST_MISMATCH = "artifact_digest_mismatch"
F_DIGEST_UNDECLARED = "artifact_digest_undeclared"
F_PROTO_ABSENT = "artifact_protocol_undeclared"
F_PROTO_UNKNOWN = "artifact_protocol_not_allowlisted"
F_PROTO_MISMATCH = "artifact_protocol_mismatch"
F_A5_V2_UNBOUND = "a5_benchmark_evidence_v2_not_bound_to_registered_protocol"
F_PROVENANCE_INVALID = "provenance_class_invalid"
F_PROVENANCE_SUBSTITUTION = "provenance_substitution"
F_COMMIT_INVALID = "source_commit_invalid"
F_COMMIT_ABSENT = "source_commit_absent"
F_SOURCE_MISMATCH = "source_binding_mismatch"
F_EXPIRED = "mandatory_evidence_expired"
F_FRESHNESS = "freshness_binding_invalid"
F_SEALED_MISSING = "sealed_evidence_missing"
F_SEALED_MISMATCH = "sealed_evidence_digest_mismatch"
F_REF_ESCAPE = "artifact_reference_escapes_evidence_roots"
F_CHECKLIST_MISSING = "checklist_binding_missing"
F_CHECKLIST_MISMATCH = "checklist_binding_digest_mismatch"
F_CHECKLIST_PROVENANCE = "checklist_binding_provenance_invalid"
F_CHECKLIST_DIGEST = "checklist_digest_mismatch"
F_GATE_STATE = "gate_state_not_design_only"
F_ARCHIVE_ABSENT = "source_archive_absent"
F_RECORD_ABSENT = "completion_record_missing"
F_TREE = "source_tree_not_clean"

_DECLARATION_FIELDS = {
    "protocol",
    "claim_boundary",
    "run_dir",
    "evidence_roots",
    "repo_root",
    "source",
    "artifacts",
    "completion_records",
    "checklist",
}
_SOURCE_FIELDS = {"commit", "branch"}
_ARTIFACT_FIELDS = {
    "artifact_id",
    "path",
    "declared_digest",
    "protocol",
    "provenance",
    "required",
    "fresh_until",
}
_ARTIFACT_OPTIONAL_FIELDS = {"observed_at"}
_COMPLETION_FIELDS = {"gate_id", "path"}
_CHECKLIST_FIELDS = {"path", "declared_digest"}


def sha256_file(path: os.PathLike[str] | str) -> str:
    """Stream a file and return its ``sha256:<hex>`` digest."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return _DIGEST + digest.hexdigest()


def canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return _DIGEST + hashlib.sha256(payload).hexdigest()


def _text(value: Any, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(_ERR)
    return value


def _closed(value: Any, fields: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(_ERR)
    if not set(value) <= fields | set(optional) or not fields <= set(value):
        raise ValueError(_ERR)
    return copy.deepcopy(dict(value))


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _has_top_level_claim(value: Any) -> bool:
    """True when a mapping carries a bare boolean, i.e. a handwritten flag."""
    if isinstance(value, Mapping):
        return any(type(item) is bool for item in value.values())
    return False


def validate_declaration(declaration: Any) -> dict[str, Any]:
    """Validate a closed-shape intake declaration; refuse anything else."""
    try:
        document = _closed(declaration, _DECLARATION_FIELDS)
        if document["protocol"] != PROTOCOL:
            raise ValueError(_ERR)
        _text(document["claim_boundary"])
        _text(document["run_dir"])
        _text(document["repo_root"])
        if _has_top_level_claim(document) or _has_top_level_claim(document["source"]):
            raise ValueError(_ERR)
        if any(key in document for key in ("complete", "release_ready", "ready")):
            raise ValueError(_ERR)
        source = _closed(document["source"], _SOURCE_FIELDS)
        _text(source["commit"])
        _text(source["branch"])
        if not isinstance(document["evidence_roots"], list):
            raise ValueError(_ERR)
        for root in document["evidence_roots"]:
            _text(root)
        if not isinstance(document["artifacts"], list) or not document["artifacts"]:
            raise ValueError(_ERR)
        seen: set[str] = set()
        for artifact in document["artifacts"]:
            item = _closed(artifact, _ARTIFACT_FIELDS, _ARTIFACT_OPTIONAL_FIELDS)
            artifact_id = _text(item["artifact_id"])
            if artifact_id in seen:
                raise ValueError(_ERR)
            seen.add(artifact_id)
            _text(item["path"])
            if item["declared_digest"] is not None:
                digest = _text(item["declared_digest"])
                if _SHA256.fullmatch(digest) is None:
                    raise ValueError(_ERR)
            _text(item["protocol"], allow_none=True)
            if item["provenance"] not in ALLOWED_PROVENANCE:
                raise ValueError(_ERR)
            if type(item["required"]) is not bool:
                raise ValueError(_ERR)
            for key in ("fresh_until", "observed_at"):
                if key in item and item[key] is not None:
                    if _timestamp(item[key]) is None:
                        raise ValueError(_ERR)
        if not isinstance(document["completion_records"], list):
            raise ValueError(_ERR)
        for record in document["completion_records"]:
            item = _closed(record, _COMPLETION_FIELDS)
            _text(item["gate_id"])
            _text(item["path"])
        checklist = _closed(document["checklist"], _CHECKLIST_FIELDS)
        _text(checklist["path"])
        if checklist["declared_digest"] is not None:
            if _SHA256.fullmatch(_text(checklist["declared_digest"])) is None:
                raise ValueError(_ERR)
        return document
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == _ERR:
            raise
        raise ValueError(_ERR) from exc


def load_declaration(path: os.PathLike[str] | str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        return validate_declaration(json.loads(handle.read().decode("utf-8")))


def missing_verifier_contracts() -> list[dict[str, str]]:
    return [copy.deepcopy(item) for item in MISSING_VERIFIER_CONTRACTS]


def _real_path(path: Path) -> Path:
    """Fully resolve a path (every symlink component), without requiring existence."""
    try:
        return Path(path).resolve(strict=False)
    except OSError:  # pragma: no cover - pathological filesystems only
        return Path(path)


def _is_contained(real: Path, real_root: Path) -> bool:
    return real == real_root or real_root in real.parents


def _relocate_sealed_reference(
    reference: Path, evidence_roots: list[Path]
) -> tuple[Path | None, str | None]:
    """Re-root an absolute sealed-evidence reference onto a declared evidence root.

    A retained corpus is routinely relocated out of the owner-private absolute prefix the
    sealed record was written against, so the literal path can be absent while the exact
    bytes remain available under a declared evidence root (the staged corpus records the
    move as ``evidence_root_origin``). The declared roots are authoritative relocation
    targets: the reference is walked from its longest suffix towards its shortest, and
    the first suffix naming a regular file that is contained -- on the resolved real
    path -- in a declared root is the resolution.

    The walk is bounded by the reference's own component count and the declared-root
    count. It can never name a file outside a declared root, and the caller still
    requires the observed bytes to equal the declared digest, so an incorrect relocation
    surfaces as a digest finding rather than as a pass.
    """
    if not evidence_roots:
        return None, None
    parts = reference.parts
    for root in evidence_roots:
        real_root = _real_path(root)
        for start in range(1, len(parts)):
            candidate = Path(*parts[start:])
            if not candidate.parts:
                continue
            resolved = root / candidate
            if not resolved.is_file():
                continue
            if not _is_contained(_real_path(resolved), real_root):
                continue
            return resolved, str(root)
    return None, None


def _resolve_relative(path_text: str, root: Path) -> Path | None:
    """Resolve a declared relative path under ``root``, refusing anything that leaves it.

    A lexical refusal (absolute path, ``..`` component) is not sufficient: a symlinked
    *intermediate* directory lets a lexically relative path resolve outside the declared
    root. Containment is therefore decided on the fully resolved real path of both the
    candidate and the root, so both intermediate-directory and final-component symlinks
    that leave the root are refused. Refused paths are the caller's escape finding.
    """
    candidate = Path(path_text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    resolved = root / candidate
    if not _is_contained(_real_path(resolved), _real_path(root)):
        return None
    return resolved


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return completed.returncode, completed.stdout.strip()


def _load_json(path: Path) -> Any:
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _intake_artifact(
    artifact: Mapping[str, Any],
    run_dir: Path,
    findings: set[str],
    now: datetime,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "artifact_id": artifact["artifact_id"],
        "path": artifact["path"],
        "declared_digest": artifact["declared_digest"],
        "declared_protocol": artifact["protocol"],
        "observed_digest": None,
        "observed_protocol": None,
        "byte_length": 0,
        "read_bytes": 0,
        "verified": False,
        "required": artifact["required"],
        "provenance": artifact["provenance"],
    }
    if not artifact["required"]:
        entry["note"] = "optional input"

    resolved = _resolve_relative(artifact["path"], run_dir)
    if resolved is None:
        findings.add(F_ESCAPE)
        entry["finding"] = F_ESCAPE
        return entry

    if not resolved.exists():
        findings.add(F_MISSING)
        entry["finding"] = F_MISSING
        return entry
    if not resolved.is_file() or resolved.is_symlink():
        findings.add(F_NOT_REGULAR)
        entry["finding"] = F_NOT_REGULAR
        return entry

    try:
        entry["observed_digest"] = sha256_file(resolved)
        entry["byte_length"] = resolved.stat().st_size
        entry["read_bytes"] = entry["byte_length"]
    except OSError:
        findings.add(F_UNREADABLE)
        entry["finding"] = F_UNREADABLE
        return entry

    if artifact["declared_digest"] is None:
        findings.add(F_DIGEST_UNDECLARED)
    elif entry["observed_digest"] != artifact["declared_digest"]:
        findings.add(F_DIGEST_MISMATCH)
        entry["finding"] = F_DIGEST_MISMATCH

    payload: Any = None
    if resolved.suffix == ".json":
        try:
            payload = _load_json(resolved)
            if isinstance(payload, Mapping):
                observed = payload.get("protocol")
                entry["observed_protocol"] = observed if isinstance(observed, str) else None
        except (OSError, ValueError):
            payload = None
            entry["observed_protocol"] = None

    declared_protocol = artifact["protocol"]
    if declared_protocol is None:
        findings.add(F_PROTO_ABSENT)
    elif declared_protocol not in ALLOWED_ARTIFACT_PROTOCOLS:
        findings.add(F_PROTO_UNKNOWN)
    elif entry["observed_protocol"] is not None and entry["observed_protocol"] != declared_protocol:
        findings.add(F_PROTO_MISMATCH)
    if (
        declared_protocol == A5_BENCHMARK_EVIDENCE_V2_PROTOCOL
        or entry["observed_protocol"] == A5_BENCHMARK_EVIDENCE_V2_PROTOCOL
    ) and not _a5_benchmark_evidence_v2_bound(payload):
        findings.add(F_A5_V2_UNBOUND)
        entry["finding"] = F_A5_V2_UNBOUND

    if artifact["provenance"] not in ALLOWED_PROVENANCE:
        findings.add(F_PROVENANCE_INVALID)
    elif artifact["required"] and artifact["provenance"] in NON_SUBSTITUTABLE_PROVENANCE:
        findings.add(F_PROVENANCE_SUBSTITUTION)

    observed_at = _timestamp(artifact.get("observed_at"))
    fresh_until = _timestamp(artifact.get("fresh_until"))
    if observed_at is not None and fresh_until is not None and fresh_until <= observed_at:
        findings.add(F_FRESHNESS)
        entry["freshness"] = "inverted"
    elif fresh_until is not None:
        entry["fresh_until"] = artifact["fresh_until"]
        # An artifact-level window is evaluated against the caller's instant exactly like
        # a sealed-evidence window: a closed window is a finding, never ignored.
        if _apply_expiry(fresh_until, now, findings):
            entry["expired"] = True

    entry["verified"] = (
        entry["observed_digest"] == artifact["declared_digest"]
        and entry["observed_digest"] is not None
    )
    return entry


def _a5_benchmark_evidence_v2_bound(payload: Any) -> bool:
    """The v2 envelope binds the registered v2 protocol at every level.

    The envelope, its run fixture and its replica_benchmark.v2 record must each
    name the pre-registered protocol digest, and the record must pass the v2
    contract validator. Anything else (unreadable bytes, another digest, a
    missing or invalid record) is not admitted as v2 benchmark evidence.
    """

    if not isinstance(payload, Mapping):
        return False
    run_fixture = payload.get("run_fixture")
    record = payload.get("replica_benchmark")
    if (
        payload.get("protocol") != A5_BENCHMARK_EVIDENCE_V2_PROTOCOL
        or payload.get("benchmark_protocol") != BENCHMARK_ACCEPTANCE_V2_PROTOCOL
        or payload.get("benchmark_protocol_digest") != A5_BENCHMARK_PROTOCOL_V2_DIGEST
        or payload.get("qualification_claim") is not False
        or payload.get("promotion_authorized") is not False
        or not isinstance(run_fixture, Mapping)
        or run_fixture.get("benchmark_protocol_digest") != A5_BENCHMARK_PROTOCOL_V2_DIGEST
        or not isinstance(record, Mapping)
        or record.get("protocol") != REPLICA_BENCHMARK_V2_PROTOCOL
        or record.get("benchmark_protocol_digest") != A5_BENCHMARK_PROTOCOL_V2_DIGEST
    ):
        return False
    try:
        validate_replica_benchmark_v2(record)
    except (ValueError, TypeError):
        return False
    return True


def _apply_expiry(
    fresh_until: datetime | None, now: datetime, findings: set[str]
) -> bool:
    if fresh_until is None:
        return False
    if fresh_until <= now:
        findings.add(F_EXPIRED)
        return True
    return False


def _intake_completion_record(
    record: Mapping[str, Any],
    repo_root: Path,
    evidence_roots: list[Path],
    now: datetime,
    findings: set[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "gate_id": record["gate_id"],
        "path": record["path"],
        "record_digest": None,
        "sealed_evidence_count": 0,
        "sealed_digest_matched": 0,
        "sealed_digest_mismatched": 0,
        "expired": False,
        "findings": [],
        "resolved": [],
    }
    local: set[str] = set()

    path = _resolve_relative(record["path"], repo_root)
    if path is None or not path.is_file():
        local.add(F_RECORD_ABSENT)
        result["findings"] = sorted(local)
        findings.update(local)
        return result

    result["record_digest"] = sha256_file(path)
    document = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(document, Mapping):
        local.add(F_RECORD_ABSENT)
        result["findings"] = sorted(local)
        findings.update(local)
        return result
    result["declared_protocol"] = document.get("protocol")
    if document.get("protocol") != "mycelium.gate_completion_record.v1":
        local.add(F_PROTO_MISMATCH)

    sealed = document.get("sealed_evidence")
    if isinstance(sealed, Mapping):
        sealed_items = [sealed]
    elif isinstance(sealed, list):
        sealed_items = sealed
    else:
        sealed_items = []
    result["sealed_evidence_count"] = len(sealed_items)

    expired_any = False
    for item in sealed_items:
        if not isinstance(item, Mapping):
            local.add(F_SEALED_MISSING)
            continue
        reference = item.get("artifact")
        declared = item.get("digest") or item.get("artifact_digest")
        if not isinstance(reference, str) or not reference:
            local.add(F_SEALED_MISSING)
            continue
        protocol = item.get("protocol")
        if protocol is not None and protocol not in ALLOWED_ARTIFACT_PROTOCOLS:
            local.add(F_PROTO_UNKNOWN)

        candidate = Path(reference)
        resolved: Path | None = None
        resolved_via: str | None = None
        if candidate.is_absolute():
            # Containment is decided on the resolved real path: a symlinked component
            # must not smuggle an absolute reference back outside a declared root.
            real_candidate = _real_path(candidate)
            contained = any(
                _is_contained(real_candidate, _real_path(root)) for root in evidence_roots
            )
            if contained and candidate.is_file():
                resolved = candidate
                resolved_via = "literal"
            else:
                # The literal owner-private path is either absent or outside every
                # declared root -- and bytes outside the declared roots are never read.
                # A declared evidence root is an authoritative relocation target for the
                # retained corpus, so re-root the reference by its longest contained
                # suffix. The bytes must still match the declared digest below.
                resolved, relocated_root = _relocate_sealed_reference(candidate, evidence_roots)
                if resolved is not None:
                    resolved_via = f"relocated:{relocated_root}"
                elif not contained:
                    local.add(F_REF_ESCAPE)
                    continue
        else:
            repo_candidate = repo_root / candidate
            if repo_candidate.is_file() and _is_contained(
                _real_path(repo_candidate), _real_path(repo_root)
            ):
                resolved = repo_candidate
            else:
                for root in evidence_roots:
                    root_candidate = root / candidate
                    if root_candidate.is_file() and _is_contained(
                        _real_path(root_candidate), _real_path(root)
                    ):
                        resolved = root_candidate
                        break

        if resolved is None:
            local.add(F_SEALED_MISSING)
            result["sealed_digest_mismatched"] += 1
            continue

        observed = sha256_file(resolved)
        result["resolved"].append(
            {
                "artifact": reference,
                "declared_digest": declared,
                "observed_digest": observed,
                "resolved_via": resolved_via or "evidence_root",
            }
        )
        if declared is not None and observed == declared:
            result["sealed_digest_matched"] += 1
        else:
            result["sealed_digest_mismatched"] += 1
            local.add(F_SEALED_MISMATCH)

        if _apply_expiry(_timestamp(item.get("fresh_until")), now, local):
            expired_any = True

    result["expired"] = expired_any
    result["findings"] = sorted(local)
    findings.update(local)
    return result


def _intake_checklist(
    checklist: Mapping[str, Any],
    repo_root: Path,
    findings: set[str],
) -> dict[str, Any]:
    observations: dict[str, Any] = {
        "path": checklist["path"],
        "declared_digest": checklist["declared_digest"],
        "verified_digest": False,
        "protocol": None,
        "binding_count": 0,
        "bindings_verified": 0,
        "bindings_missing": 0,
        "bindings_mismatched": 0,
        "a15_state": None,
        "pending_requirement_count": 0,
        "pending_requirements": [],
    }
    path = _resolve_relative(checklist["path"], repo_root)
    if path is None or not path.is_file():
        findings.add(F_CHECKLIST_MISSING)
        return observations

    observed = sha256_file(path)
    observations["verified_digest"] = observed == checklist["declared_digest"]
    if not observations["verified_digest"]:
        findings.add(F_CHECKLIST_DIGEST)

    document = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(document, Mapping):
        findings.add(F_CHECKLIST_MISSING)
        return observations
    observations["protocol"] = document.get("protocol")
    if document.get("protocol") != "mycelium.completion_checklist.v1":
        findings.add(F_PROTO_MISMATCH)

    gates = document.get("gates")
    if not isinstance(gates, list):
        findings.add(F_CHECKLIST_MISSING)
        return observations

    pending: list[str] = []
    for gate in gates:
        if not isinstance(gate, Mapping):
            findings.add(F_CHECKLIST_MISSING)
            continue
        gate_id = gate.get("gate_id")
        if gate_id == "A15":
            observations["a15_state"] = gate.get("state")
            if gate.get("state") != "design_only":
                findings.add(F_GATE_STATE)
            for requirement in gate.get("pending_requirements") or []:
                if isinstance(requirement, str):
                    pending.append(f"{gate_id}:{requirement}")
        bindings = gate.get("evidence_bindings")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, Mapping):
                findings.add(F_CHECKLIST_MISSING)
                continue
            observations["binding_count"] += 1
            reference = binding.get("artifact_reference")
            declared = binding.get("artifact_digest")
            provenance = binding.get("provenance")
            candidate = (
                _resolve_relative(reference, repo_root)
                if isinstance(reference, str) and reference
                else None
            )
            if candidate is None or not candidate.is_file():
                observations["bindings_missing"] += 1
                findings.add(F_CHECKLIST_MISSING)
                continue
            if declared is None or sha256_file(candidate) != declared:
                observations["bindings_mismatched"] += 1
                findings.add(F_CHECKLIST_MISMATCH)
                continue
            if provenance not in ALLOWED_PROVENANCE:
                findings.add(F_CHECKLIST_PROVENANCE)
                continue
            observations["bindings_verified"] += 1

    observations["pending_requirement_count"] = len(pending)
    observations["pending_requirements"] = sorted(pending)
    return observations


def _package_observation(run_dir: Path, findings: set[str]) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "declared_package_digest": None,
        "file_digest_count": 0,
        "file_digests_mismatched": 0,
        "source_archive_present": False,
        "observed_source_head": None,
        "observed_git_tree": None,
        "working_tree_dirty": None,
    }
    path = run_dir / "PACKAGE.json"
    if not path.is_file():
        return observation
    document = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(document, Mapping):
        return observation
    observation["declared_package_digest"] = document.get("package_digest")
    source = document.get("source")
    if isinstance(source, Mapping):
        observation["observed_source_head"] = source.get("git_head")
        observation["observed_git_tree"] = source.get("git_tree")
        archive = source.get("archive_path")
        observation["source_archive_present"] = bool(
            isinstance(archive, str) and os.path.isfile(archive)
        )
        if not observation["source_archive_present"]:
            findings.add(F_ARCHIVE_ABSENT)
    working = document.get("working_tree")
    if isinstance(working, Mapping):
        observation["working_tree_dirty"] = working.get("dirty")
    for artifact in document.get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        role = artifact.get("role")
        for item in artifact.get("files") or []:
            if not isinstance(item, Mapping):
                continue
            observation["file_digest_count"] += 1
            candidate = run_dir / "package" / str(role) / str(item.get("path"))
            if not candidate.is_file() or sha256_file(candidate) != item.get("sha256"):
                observation["file_digests_mismatched"] += 1
    return observation


def _readiness_observation(run_dir: Path) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "protocol": None,
        "pilot_readiness_verdict": None,
        "check_count": 0,
        "failed_checks": [],
        "observed_head": None,
    }
    path = run_dir / "readiness-gate.json"
    if not path.is_file():
        return observation
    document = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(document, Mapping):
        return observation
    observation["protocol"] = document.get("protocol")
    observation["pilot_readiness_verdict"] = document.get("verdict")
    checks = document.get("checks")
    if isinstance(checks, list):
        observation["check_count"] = len(checks)
    failed = document.get("failed_checks")
    if isinstance(failed, list):
        observation["failed_checks"] = [str(item) for item in failed]
    execution = run_dir / "gate-execution.json"
    if execution.is_file():
        payload = json.loads(execution.read_bytes().decode("utf-8"))
        if isinstance(payload, Mapping):
            observation["observed_head"] = payload.get("head")
    return observation


def _missing_evidence(
    checklist: Mapping[str, Any], completion: list[dict[str, Any]]
) -> list[str]:
    missing: list[str] = []
    for gate in CLOSURE_GATES:
        missing.append(f"executed_gate_result:{gate}")
    for digest_class in REQUIRED_DIGEST_CLASSES:
        missing.append(f"digest_class:{digest_class}")
    for slug in REQUIRED_MATRIX_EVIDENCE:
        missing.append(f"matrix:{slug}")
    missing.append("reviewer_reproduction")
    missing.append("signed_exclusions")
    missing.append("release_decision_producer")
    for item in completion:
        if item["sealed_digest_mismatched"]:
            missing.append(f"verified_sealed_evidence:{item['gate_id']}")
    for requirement in checklist.get("pending_requirements") or []:
        missing.append(f"requirement:{requirement}")
    return sorted(set(missing))


def intake_run(
    declaration: Mapping[str, Any],
    *,
    now: datetime,
    repo_root: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Intake a declared artifact set and assemble a non-authoritative, not-ready report."""
    if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
        raise ValueError("a15_run_intake_now_must_be_utc")
    document = validate_declaration(declaration)
    root = Path(repo_root)
    run_dir = Path(document["run_dir"])
    evidence_roots = [Path(item) for item in document["evidence_roots"]]

    findings: set[str] = set()

    source = document["source"]
    commit = source["commit"]
    if _COMMIT.fullmatch(commit) is None:
        findings.add(F_COMMIT_INVALID)
        commit_present = False
    else:
        code, _ = _git(root, "cat-file", "-e", f"{commit}^{{commit}}")
        commit_present = code == 0
        if not commit_present:
            findings.add(F_COMMIT_ABSENT)
    status_code, status_out = _git(root, "status", "--porcelain")
    tree_state_observed = (
        "unknown" if status_code != 0 else ("clean" if not status_out else "dirty")
    )
    if tree_state_observed != "clean":
        findings.add(F_TREE)

    artifact_results = [
        _intake_artifact(artifact, run_dir, findings, now)
        for artifact in document["artifacts"]
    ]

    readiness_observation = _readiness_observation(run_dir)
    measured_heads = (
        {str(readiness_observation["observed_head"])}
        if readiness_observation["observed_head"]
        else set()
    )
    package_observation = _package_observation(run_dir, findings)
    if package_observation["observed_source_head"]:
        measured_heads.add(str(package_observation["observed_source_head"]))
    if _COMMIT.fullmatch(commit) is not None and measured_heads and measured_heads != {commit}:
        findings.add(F_SOURCE_MISMATCH)

    sealed_evidence_results = [
        _intake_completion_record(record, root, evidence_roots, now, findings)
        for record in document["completion_records"]
    ]

    checklist = _intake_checklist(document["checklist"], root, findings)

    observed_protocol = readiness_observation
    retained_observations = {
        "package": package_observation,
        "checklist": checklist,
        "pilot_readiness_protocol": observed_protocol["protocol"],
        "pilot_readiness_verdict": observed_protocol["pilot_readiness_verdict"],
        "pilot_readiness_check_count": observed_protocol["check_count"],
        "pilot_readiness_failed_checks": observed_protocol["failed_checks"],
    }

    missing_evidence = _missing_evidence(checklist, sealed_evidence_results)

    body: dict[str, Any] = {
        "protocol": REPORT_PROTOCOL,
        "intake_protocol": PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "authoritative": False,
        "production_verifier": False,
        "release_ready": False,
        "gate_state": "design_only",
        "scope_limits": list(SCOPE_LIMITS),
        "non_authoritative_inputs": copy.deepcopy(NON_AUTHORITATIVE_INPUTS),
        "run_dir": str(run_dir),
        "source": {
            "commit": commit,
            "branch": source["branch"],
            "commit_present": commit_present,
            "tree_state_observed": tree_state_observed,
            "measured_heads": sorted(measured_heads),
        },
        "artifact_results": artifact_results,
        "sealed_evidence_results": sealed_evidence_results,
        "retained_observations": retained_observations,
        "findings": sorted(findings),
        "intake_ok": not findings,
        "missing_evidence": missing_evidence,
        "missing_evidence_count": len(missing_evidence),
        "missing_verifier_contracts": missing_verifier_contracts(),
    }
    body["report_digest"] = canonical_digest(body)

    # Structural guarantee: no code path in this module can emit a readiness claim.
    if body["release_ready"] is not False or body["authoritative"] is not False:
        raise ValueError("a15_run_intake_readiness_claim_forbidden")
    if body["gate_state"] != "design_only":
        raise ValueError("a15_run_intake_gate_state_mutation_forbidden")
    return body


def findings_of(report: Mapping[str, Any]) -> list[str]:
    return list(report.get("findings") or [])


__all__ = [
    "A5_BENCHMARK_EVIDENCE_V2_PROTOCOL",
    "A5_BENCHMARK_PROTOCOL_V2_DIGEST",
    "ALLOWED_ARTIFACT_PROTOCOLS",
    "ALLOWED_PROVENANCE",
    "CLAIM_BOUNDARY",
    "CLOSURE_GATES",
    "MISSING_VERIFIER_CONTRACTS",
    "PROTOCOL",
    "REPORT_PROTOCOL",
    "REQUIRED_DIGEST_CLASSES",
    "REQUIRED_MATRIX_EVIDENCE",
    "SCOPE_LIMITS",
    "canonical_digest",
    "findings_of",
    "intake_run",
    "load_declaration",
    "missing_verifier_contracts",
    "sha256_file",
    "validate_declaration",
]
