"""A15 executed release decision producer.

Derives one ``mycelium.executed_release_decision.v1`` generation from a verified,
content-addressed executed-result graph. The decision is never an input: a candidate
that carries a boolean, a readiness key or a decision is rejected, and ``ready`` is
reachable only when every requirement below verifies from artifact bytes.

Inputs and where authority comes from:

* ``repo_root`` (explicit argument, authoritative). The observed ``HEAD`` must equal the
  declared source commit, the observed tree must match the declared tree digest, and the
  tracked tree must be clean. The candidate never names a checkout.
* ``evidence_roots`` (explicit ``id -> directory`` mapping). An artifact reference is
  ``<root id>/<relative path>``; the first segment selects exactly one root, so resolution
  never depends on root order and never guesses between roots.
* trust registry (explicit path plus an out-of-band pinned SHA-256). It carries the
  verification keys, their roles, the allowlisted verifier-policy digests and the current
  authority generation. It must live outside the checkout, the evidence roots and the
  ledger, so the candidate being checked cannot supply its own trust.
* decision ledger (explicit directory). Generations are appended with exclusive create and
  chained by the digest of the previous entry's exact bytes; history is never rewritten.

Result kinds are an allowlist (``RESULT_KINDS``). Every result is a signed receipt whose
signature, bindings, evidence manifest and kind-specific artifact content are verified.
The envelope's declared outcome is only compared against what the artifact proves.

Input-node versus derived-root boundary (reconciled with the frozen synthetic validator in
``tests/a15_acceptance/validator.py``): callers supply gate-result nodes for A2..A14 only.
The A15 closure node is derived here and is refused as an input. The emitted graph then
holds A2..A15, which is exactly the frozen validator's node set, with A15 depending
directly on A3..A14.

Source identity: commits are 40-hex SHA-1 object ids as frozen by the acceptance
inventory. Tree identity is always ``source_tree_digest(object_format, tree_id)``, one
``sha256:`` representation for both Git object formats; a SHA-256 object-format checkout
is refused explicitly instead of being compared under a second representation.

Trust scope: a registry declares ``production`` or ``synthetic_test_fixture``. Synthetic
trust is admitted only on explicit request, accepts only synthetic-labelled evidence and
seals ``authority_scope = synthetic_test_fixture``; production trust never accepts
synthetic-labelled evidence. A synthetic derivation grants no release authority.
"""

from __future__ import annotations

import dataclasses
import os
import re
import secrets
import stat
import subprocess
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from mycelium_a15_run_intake import CLOSURE_GATES, REQUIRED_DIGEST_CLASSES
from mycelium_m22_sbom import validate_sbom
from mycelium_qualification.evidence import (
    EvidenceValidationError,
    canonical_json_bytes,
    canonical_json_loads,
    is_sha256_ref,
    sha256_bytes,
    sha256_document,
    validate_evidence_manifest,
)
from mycelium_qualification.signing import EvidenceSigningError, build_ed25519_verifier
from mycelium_release_bundle.verifier import MANIFEST_FILENAME as BUNDLE_MANIFEST_FILENAME
from mycelium_release_bundle.verifier import verify_bundle

DECISION_PROTOCOL = "mycelium.executed_release_decision.v1"
PUBLIC_PROTOCOL = "mycelium.executed_release_decision_public.v1"
CANDIDATE_PROTOCOL = "mycelium.a15_release_candidate.v1"
GRAPH_PROTOCOL = "mycelium.executed_result_graph.v1"
NODE_PROTOCOL = "mycelium.executed_gate_result.v1"
RECEIPT_PROTOCOL = "mycelium.a15_result_receipt.v1"
TRUST_REGISTRY_PROTOCOL = "mycelium.a15_trust_registry.v1"
VERIFIER_POLICY_PROTOCOL = "mycelium.a15_verifier_policy.v1"
CLOSURE_PROTOCOL = "mycelium.a15_release_closure_statement.v1"
PRODUCER = {"name": "mycelium_a15_release_decision", "version": 1}

SCOPE_PRODUCTION = "production"
SCOPE_SYNTHETIC = "synthetic_test_fixture"
SCOPE_UNDETERMINED = "undetermined"

CLAIM_BOUNDARY = (
    "release decision derived from a verified content-addressed executed-result graph under "
    "one pinned trust registry; cooperative private-swarm threat model only; a "
    "synthetic_test_fixture authority scope grants no release authority"
)
PUBLIC_PRIVACY = (
    "digests, gate ids, bounded finding codes and timestamps only; no artifact locations, "
    "identities, keys, exclusion text or operator state"
)

ROOT_GATE = "A15"
INPUT_GATES = ("A2", *CLOSURE_GATES)
DIRECT_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "A2": (),
    "A3": ("A2",),
    "A4": ("A3",),
    "A5": ("A4",),
    "A6": ("A4",),
    "A7": ("A6",),
    "A8": ("A3",),
    "A9": ("A8",),
    "A10": ("A4",),
    "A11": ("A10",),
    "A12": ("A4", "A9"),
    "A13": ("A12",),
    "A14": ("A8",),
    ROOT_GATE: tuple(CLOSURE_GATES),
}
DIGEST_CLASSES = tuple(sorted(REQUIRED_DIGEST_CLASSES))

REVIEWER_REQUIREMENT = "external_reviewer_reproduction"
REVIEWER_REQUIRED_STEPS = (
    "verify_source_binding",
    "verify_result_graph",
    "verify_gate_commit_graph",
    "verify_contract_manifest",
    "verify_package_digest",
    "verify_sbom_digest",
    "rerun_required_deterministic_checks",
    "reproduce_required_physical_positive",
    "reproduce_required_physical_negative",
    "record_signed_reproduction_result",
)
REVIEW_FINDINGS_CASE = "review_findings_resolved"
BROWSER_ENGINE_CASES = ("engine:chromium", "engine:firefox", "engine:webkit")
SBOM_REQUIRED_COMPONENT_KINDS = ("contract", "node-lock", "python-lock", "rust-lock")
CONTRACT_MANIFEST_PATH = "contracts/contract-manifest.v1.json"
CONTRACT_MANIFEST_EVIDENCE_NAME = "contract-manifest.v1.json"
SBOM_EVIDENCE_NAME = "sbom.json"

# Allowlisted result kinds. ``requirement`` names where a kind may be used; ``properties``
# are the properties the kind must hold; ``artifact_verifier`` names the content check.
RESULT_KINDS: dict[str, dict[str, Any]] = {
    "physical_gate": {
        "requirement": "gate_result",
        "properties": ("clean_tree", "physical"),
        "required_cases": (),
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "junit_suite": {
        "requirement": "digest_class:test",
        "properties": ("clean_tree",),
        "required_cases": (),
        "artifact_verifier": "junit_xml_reports",
    },
    "audit_report": {
        "requirement": "digest_class:audit",
        "properties": ("clean_tree",),
        "required_cases": (),
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "physical_evidence_bundle": {
        "requirement": "digest_class:physical",
        "properties": ("physical",),
        "required_cases": (),
        "artifact_verifier": "mycelium_release_bundle.verifier.verify_bundle",
    },
    "browser_engine_matrix": {
        "requirement": "digest_class:browser",
        "properties": ("browser_engine", "clean_tree"),
        "required_cases": BROWSER_ENGINE_CASES,
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "model_qualification": {
        "requirement": "digest_class:model",
        "properties": ("physical",),
        "required_cases": (),
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "contract_manifest": {
        "requirement": "digest_class:contract",
        "properties": ("clean_tree",),
        "required_cases": (),
        "artifact_verifier": "source_contract_manifest_bytes",
    },
    "package_manifest": {
        "requirement": "digest_class:package",
        "properties": ("clean_tree", "platform_specific"),
        "required_cases": (),
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "sbom_document": {
        "requirement": "digest_class:sbom",
        "properties": ("clean_tree",),
        "required_cases": (),
        "artifact_verifier": "mycelium_m22_sbom.validate_sbom",
    },
    "external_reviewer_reproduction": {
        "requirement": REVIEWER_REQUIREMENT,
        "properties": ("clean_tree", "independently_reviewed", "physical"),
        "required_cases": (*REVIEWER_REQUIRED_STEPS, REVIEW_FINDINGS_CASE),
        "artifact_verifier": "signed_receipt_and_evidence_manifest",
    },
    "release_closure": {
        "requirement": "derived_root",
        "properties": ("clean_tree",),
        "required_cases": (),
        "artifact_verifier": "derived_by_producer",
    },
}
REQUIRED_PROVENANCE = "live"
PROVENANCE_KINDS = ("fixture", "historical", "live", "replay")
RECEIPT_PROPERTIES = (
    "browser_engine",
    "clean_tree",
    "destructive_negative",
    "independently_reviewed",
    "physical",
    "platform_specific",
    "same_run",
)
DIGEST_CLASS_KINDS = {
    kind_policy["requirement"].split(":", 1)[1]: kind
    for kind, kind_policy in RESULT_KINDS.items()
    if kind_policy["requirement"].startswith("digest_class:")
}

ROLE_EXCLUSION_SIGNER = "exclusion_signer"
ROLE_INDEPENDENT_REVIEWER = "independent_reviewer"
ROLE_RESULT_VERIFIER_PREFIX = "result_verifier:"

MAX_EXCLUSIONS = 16
MAX_REQUIREMENTS_PER_EXCLUSION = 8
MAX_EXCLUSION_REASON_BYTES = 1024
MAX_EXCLUSION_LIFETIME = timedelta(hours=720)
EXCLUSION_SCOPE = "exact_requirement_ids"
# Narrow by design: one browser engine case may be owner-excluded. Everything else the
# producer evaluates is mandatory integrity, provenance, source or qualification evidence.
EXCLUDABLE_REQUIREMENTS = frozenset(
    f"digest_class:browser#{case_id}" for case_id in BROWSER_ENGINE_CASES
)
NON_EXCLUDABLE_REQUIREMENTS = frozenset(
    {
        "source_integrity",
        "clean_tree",
        "content_digest_integrity",
        "owner_authority",
        "privacy_and_credential_safety",
        "qualified_only_selection",
        "fail_closed_execution",
        "provenance_truth",
        REVIEWER_REQUIREMENT,
        "trust_registry",
        "gate_graph",
        *(f"gate_result:{gate_id}" for gate_id in INPUT_GATES),
        *(f"digest_class:{digest_class}" for digest_class in DIGEST_CLASSES),
    }
)
REVOCATION_REASONS = (
    "compromised_key",
    "compromised_package",
    "critical_defect",
    "expired_mandatory_result",
    "invalid_evidence",
)

MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_DECISION_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_FILES = 256

NODE_FIELDS = frozenset(
    {
        "protocol",
        "node_digest",
        "gate_id",
        "result_kind",
        "outcome",
        "source_commit",
        "source_tree_digest",
        "tree_state",
        "provenance",
        "subject",
        "artifact_reference",
        "artifact_digest",
        "dependency_gate_ids",
        "dependency_digests",
        "verifier_policy_digest",
        "executed_at",
        "fresh_until",
        "contract_digest",
        "model_digest",
        "representation_digest",
        "runtime_digest",
        "environment_digest",
        "authority_generation",
    }
)
ROOT_FIELDS = frozenset(
    {
        "root_digest",
        "source_commit",
        "source_tree_digest",
        "tree_state",
        "gate_commit_digests",
        "result_node_digests",
        "digest_class_roots",
        "exclusion_digests",
        "reviewer_reproduction_digest",
        "decision_generation",
    }
)
EXCLUSION_FIELDS = frozenset(
    {
        "exclusion_digest",
        "requirement_ids",
        "scope",
        "reason",
        "risk",
        "compensating_behavior",
        "reviewer_identity",
        "signer_key_id",
        "issued_at",
        "expires_at",
        "signature",
    }
)
CANDIDATE_FIELDS = frozenset(
    {
        "protocol",
        "source",
        "gate_commits",
        "nodes",
        "class_results",
        "reviewer_reproduction",
        "exclusions",
    }
)
RECEIPT_FIELDS = frozenset(
    {
        "protocol",
        "result_kind",
        "requirement_id",
        "subject",
        "provenance",
        "properties",
        "outcome",
        "source_commit",
        "source_tree_digest",
        "tree_state",
        "contract_digest",
        "model_digest",
        "representation_digest",
        "runtime_digest",
        "environment_digest",
        "authority_generation",
        "verifier_policy_digest",
        "executed_at",
        "fresh_until",
        "cases",
        "bound_result_digests",
        "evidence_manifest",
        "signature",
    }
)
TRUST_REGISTRY_FIELDS = frozenset(
    {
        "protocol",
        "registry_class",
        "registry_generation",
        "issued_at",
        "expires_at",
        "current_authority_generation",
        "keys",
        "verifier_policy_digests",
    }
)
DECISION_BODY_FIELDS = frozenset(
    {
        "authority_scope",
        "candidate_digest",
        "claim_boundary",
        "closure_statement",
        "decided_at",
        "decision",
        "decision_generation",
        "exclusions",
        "findings",
        "graph",
        "previous_decision_digest",
        "producer",
        "requirements",
        "revocation",
        "source",
        "trust_registry_digest",
        "valid_until",
        "withheld_claims",
    }
)

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TREE_IDS = {"sha1": re.compile(r"[0-9a-f]{40}\Z"), "sha256": re.compile(r"[0-9a-f]{64}\Z")}
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ROOT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,95}\Z")
_SUBJECT = re.compile(r"[A-Za-z0-9_.:#-]{1,160}\Z")
_PUBLIC_TEXT = re.compile(r"[A-Za-z0-9 _.,:;#()-]*\Z")
_LEDGER_NAME = re.compile(r"generation-(\d{6})\.json\Z")
_PRIVATE_BYTES = re.compile(
    rb"(?:/Users/|/home/|/private/var/|/var/folders/|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_CLAIM_KEYS = frozenset(
    {"complete", "completed", "decision", "readiness", "ready", "release_ready"}
)


class ReleaseDecisionRefused(Exception):
    """The producer could not safely append a generation; ``code`` is a bounded slug."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True, slots=True)
class DecisionResult:
    decision: str
    decision_generation: int
    decision_digest: str
    sealed_bytes: bytes
    ledger_entry_path: Path
    findings: tuple[str, ...]
    document: dict[str, Any]


def source_tree_digest(object_format: str, tree_id: str) -> str:
    """The single tree representation used by sources, nodes, receipts and roots."""
    pattern = _TREE_IDS.get(object_format)
    if pattern is None or not isinstance(tree_id, str) or pattern.fullmatch(tree_id) is None:
        raise ValueError("a15_source_tree_id_invalid")
    return sha256_bytes(f"git-tree:{object_format}:{tree_id}".encode("ascii"))


def verifier_policy(result_kind: str) -> dict[str, Any]:
    kind_policy = RESULT_KINDS[result_kind]
    return {
        "protocol": VERIFIER_POLICY_PROTOCOL,
        "policy_version": 1,
        "result_kind": result_kind,
        "requirement": kind_policy["requirement"],
        "required_provenance": REQUIRED_PROVENANCE,
        "required_properties": sorted(kind_policy["properties"]),
        "required_cases": sorted(kind_policy["required_cases"]),
        "artifact_verifier": kind_policy["artifact_verifier"],
    }


def verifier_policy_digest(result_kind: str) -> str:
    return sha256_document(verifier_policy(result_kind))


def node_digest(node: Mapping[str, Any]) -> str:
    return sha256_document({key: value for key, value in node.items() if key != "node_digest"})


def exclusion_digest(exclusion: Mapping[str, Any]) -> str:
    return sha256_document(
        {
            key: value
            for key, value in exclusion.items()
            if key not in {"exclusion_digest", "signature"}
        }
    )


def signed_statement(document: Mapping[str, Any]) -> dict[str, Any]:
    """The statement a receipt or exclusion signature covers: everything but the signature."""
    return {key: value for key, value in document.items() if key != "signature"}


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- small helpers ---------------------------------------------------------------------


def _refuse(code: str) -> ReleaseDecisionRefused:
    return ReleaseDecisionRefused(code)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _require_utc(now: Any) -> datetime:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() != timedelta(0)
    ):
        raise _refuse("now_must_be_utc")
    return now.replace(microsecond=0)


def _is_generation(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _has_claim(value: Any) -> bool:
    """A boolean anywhere, or a readiness/completion key, is a handwritten claim."""
    if type(value) is bool:
        return True
    if isinstance(value, dict):
        return any(key in _CLAIM_KEYS or _has_claim(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_claim(item) for item in value)
    return False


def _is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def _load_file(path: Path, maximum: int) -> tuple[bytes | None, str | None]:
    """Read one regular file without following a final symlink. Status is a bounded slug."""
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if stat.S_ISLNK(metadata.st_mode):
        return None, "symlink"
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        return None, "unreadable"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None, "unreadable"
    try:
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if len(content) > maximum:
        return None, "unreadable"
    return content, None


class _Findings:
    """Bounded finding codes grouped by the requirement they invalidate."""

    def __init__(self) -> None:
        self.by_requirement: dict[str, list[str]] = {}

    def add(self, requirement: str, code: str, subject: str | None = None) -> None:
        text = code if subject is None else f"{code}:{subject}"
        if _SUBJECT.fullmatch(text) is None:
            text = f"{code}:unprintable_subject"
        bucket = self.by_requirement.setdefault(requirement, [])
        if text not in bucket:
            bucket.append(text)

    def count(self, requirement: str) -> int:
        return len(self.by_requirement.get(requirement, ()))

    def all(self) -> list[str]:
        return sorted({text for bucket in self.by_requirement.values() for text in bucket})


@dataclasses.dataclass(slots=True)
class _Trust:
    digest: str
    scope: str
    current_generation: int
    expires_at: datetime
    roles: dict[str, frozenset[str]]
    verify: Any
    policy_digests: dict[str, str]


@dataclasses.dataclass(slots=True)
class _Source:
    commit: str
    tree_digest: str
    object_format: str
    tree_state: str
    parents: dict[str, tuple[str, ...]]
    contract_bytes: bytes | None


@dataclasses.dataclass(slots=True)
class _Context:
    now: datetime
    roots: dict[str, Path]
    trust: _Trust
    source: _Source | None
    declared_commit: Any
    declared_tree: Any
    findings: _Findings
    excluded_cases: set[str] = dataclasses.field(default_factory=set)
    validity: list[datetime] = dataclasses.field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    signers: dict[str, str] = dataclasses.field(default_factory=dict)


# -- trust registry --------------------------------------------------------------------


def _parse_trust_registry(document: Any, digest: str) -> _Trust | None:
    if not isinstance(document, dict) or set(document) != TRUST_REGISTRY_FIELDS:
        return None
    issued_at = _parse_timestamp(document["issued_at"])
    expires_at = _parse_timestamp(document["expires_at"])
    keys = document["keys"]
    policies = document["verifier_policy_digests"]
    if (
        document["protocol"] != TRUST_REGISTRY_PROTOCOL
        or document["registry_class"] not in {SCOPE_PRODUCTION, SCOPE_SYNTHETIC}
        or not _is_generation(document["registry_generation"])
        or not _is_generation(document["current_authority_generation"])
        or issued_at is None
        or expires_at is None
        or expires_at <= issued_at
        or not isinstance(keys, list)
        or not keys
        or not isinstance(policies, dict)
        or any(kind not in RESULT_KINDS or not is_sha256_ref(value) for kind, value in policies.items())
    ):
        return None
    operator_roles = {
        ROLE_RESULT_VERIFIER_PREFIX + kind
        for kind in RESULT_KINDS
        if kind not in {"external_reviewer_reproduction", "release_closure"}
    } | {ROLE_EXCLUSION_SIGNER}
    roles: dict[str, frozenset[str]] = {}
    records = []
    for entry in keys:
        if not isinstance(entry, dict) or set(entry) != {"key", "roles"}:
            return None
        entry_roles = entry["roles"]
        record = entry["key"]
        if (
            not isinstance(entry_roles, list)
            or not entry_roles
            or any(not isinstance(role, str) for role in entry_roles)
            or len(set(entry_roles)) != len(entry_roles)
            or not isinstance(record, dict)
            or not isinstance(record.get("verification_key_digest"), str)
        ):
            return None
        role_set = frozenset(entry_roles)
        # An independent reviewer key holds no operator authority at all.
        if role_set != {ROLE_INDEPENDENT_REVIEWER} and not role_set <= operator_roles:
            return None
        roles[record["verification_key_digest"]] = role_set
        records.append(record)
    try:
        verify = build_ed25519_verifier(records)
    except (EvidenceSigningError, EvidenceValidationError, KeyError, TypeError):
        return None
    if len(roles) != len(records):
        return None
    return _Trust(
        digest=digest,
        scope=document["registry_class"],
        current_generation=document["current_authority_generation"],
        expires_at=expires_at,
        roles=roles,
        verify=verify,
        policy_digests=dict(policies),
    )


def _load_trust(
    path: os.PathLike[str] | str,
    pinned: Any,
    forbidden_directories: list[str],
    now: datetime,
    allow_synthetic: bool,
    findings: _Findings,
) -> tuple[_Trust | None, str | None, str]:
    """Return (usable trust, pinned-and-observed digest, authority scope)."""
    requirement = "trust_registry"
    if not is_sha256_ref(pinned):
        findings.add(requirement, "trust_registry_pin_invalid")
        return None, None, SCOPE_UNDETERMINED
    try:
        absolute = os.path.abspath(os.fspath(path))
        real = os.path.realpath(absolute)
    except (OSError, TypeError, ValueError):
        findings.add(requirement, "trust_registry_missing")
        return None, None, SCOPE_UNDETERMINED
    content, status = _load_file(Path(absolute), MAX_REGISTRY_BYTES)
    if status == "missing":
        findings.add(requirement, "trust_registry_missing")
        return None, None, SCOPE_UNDETERMINED
    if status == "symlink" or any(_is_within(real, item) for item in forbidden_directories):
        findings.add(requirement, "trust_registry_origin_invalid")
        return None, None, SCOPE_UNDETERMINED
    if content is None:
        findings.add(requirement, "trust_registry_unreadable")
        return None, None, SCOPE_UNDETERMINED
    digest = sha256_bytes(content)
    if digest != pinned:
        findings.add(requirement, "trust_registry_digest_mismatch")
        return None, None, SCOPE_UNDETERMINED
    try:
        trust = _parse_trust_registry(canonical_json_loads(content, path="trust-registry"), digest)
    except EvidenceValidationError:
        trust = None
    if trust is None:
        findings.add(requirement, "trust_registry_invalid")
        return None, digest, SCOPE_UNDETERMINED
    document = canonical_json_loads(content, path="trust-registry")
    issued_at = _parse_timestamp(document["issued_at"])
    usable = True
    if issued_at is None or issued_at > now:
        findings.add(requirement, "trust_registry_not_yet_valid")
        usable = False
    if trust.expires_at <= now:
        findings.add(requirement, "trust_registry_expired")
        usable = False
    if trust.scope == SCOPE_SYNTHETIC and not allow_synthetic:
        findings.add(requirement, "synthetic_trust_registry_not_admitted")
        return None, digest, SCOPE_UNDETERMINED
    return (trust if usable else None), digest, trust.scope


def _check_signature(trust: _Trust, document: Mapping[str, Any], role: str) -> str | None:
    signature = document.get("signature")
    if signature is None:
        return "signature_missing"
    if not isinstance(signature, dict):
        return "signature_invalid"
    key_digest = signature.get("verification_key_digest")
    if not isinstance(key_digest, str) or key_digest not in trust.roles:
        return "signature_untrusted"
    try:
        verified = trust.verify(canonical_json_bytes(signed_statement(document)), signature)
    except EvidenceValidationError:
        verified = False
    if not verified:
        return "signature_invalid"
    if role not in trust.roles[key_digest]:
        return "signer_not_authorized"
    return None


# -- source binding --------------------------------------------------------------------


def _git(repo: str, *arguments: str) -> tuple[int, bytes]:
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, b""
    return completed.returncode, completed.stdout


def _observe_source(repo_root: os.PathLike[str] | str, findings: _Findings) -> _Source | None:
    """Observe the explicitly supplied checkout. Nothing in the candidate selects it."""
    requirement = "source_integrity"
    try:
        real = os.path.realpath(os.fspath(repo_root))
    except (OSError, TypeError, ValueError):
        real = ""
    if not real or not os.path.isdir(real):
        findings.add(requirement, "source_checkout_invalid")
        return None
    code, output = _git(
        real, "rev-parse", "--show-toplevel", "--show-object-format", "HEAD", "HEAD^{tree}"
    )
    lines = output.decode("utf-8", "replace").splitlines()
    if code != 0 or len(lines) != 4 or os.path.realpath(lines[0]) != real:
        findings.add(requirement, "source_checkout_invalid")
        return None
    object_format, head, tree_id = lines[1], lines[2], lines[3]
    if object_format != "sha1" or _COMMIT.fullmatch(head) is None:
        # The frozen inventory fixes commits to 40-hex SHA-1 object ids.
        findings.add(requirement, "source_object_format_unsupported")
        return None
    try:
        tree_digest = source_tree_digest(object_format, tree_id)
    except ValueError:
        findings.add(requirement, "source_checkout_invalid")
        return None
    code, output = _git(real, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [entry for entry in output.split(b"\0") if entry]
    if code != 0:
        tree_state = "unknown"
    elif any(not entry.startswith(b"?? ") for entry in entries):
        tree_state = "dirty"
    else:
        tree_state = "clean"
    if tree_state != "clean":
        findings.add(requirement, "clean_tree_required")
    if code == 0 and any(entry.startswith(b"?? ") for entry in entries):
        findings.add(requirement, "untracked_source_input_present")
    code, output = _git(real, "rev-list", "--parents", "HEAD")
    parents: dict[str, tuple[str, ...]] = {}
    if code == 0:
        for line in output.decode("ascii", "replace").splitlines():
            fields = line.split()
            if fields:
                parents[fields[0]] = tuple(fields[1:])
    if head not in parents:
        findings.add(requirement, "source_history_unreadable")
    code, output = _git(real, "cat-file", "blob", f"HEAD:{CONTRACT_MANIFEST_PATH}")
    contract_bytes = output if code == 0 else None
    if contract_bytes is None:
        findings.add(requirement, "contract_manifest_absent_from_source")
    return _Source(
        commit=head,
        tree_digest=tree_digest,
        object_format=object_format,
        tree_state=tree_state,
        parents=parents,
        contract_bytes=contract_bytes,
    )


def _strict_ancestors(parents: Mapping[str, tuple[str, ...]], commit: str) -> set[str]:
    seen: set[str] = set()
    stack = list(parents.get(commit, ()))
    while stack:
        current = stack.pop()
        if current not in seen:
            seen.add(current)
            stack.extend(parents.get(current, ()))
    return seen


def _check_gate_commits(
    gate_commits: Any, source: _Source | None, findings: _Findings
) -> dict[str, str] | None:
    requirement = "gate_graph"
    if not isinstance(gate_commits, dict) or set(gate_commits) != set(CLOSURE_GATES):
        findings.add(requirement, "gate_commit_set_mismatch")
        return None
    values = list(gate_commits.values())
    if any(
        not isinstance(value, str) or _COMMIT.fullmatch(value) is None for value in values
    ) or len(set(values)) != len(values):
        findings.add(requirement, "gate_commit_binding_invalid")
        return None
    if source is None:
        findings.add(requirement, "gate_commit_graph_unverifiable")
        return None
    reachable = set(_strict_ancestors(source.parents, source.commit)) | {source.commit}
    for gate_id in CLOSURE_GATES:
        commit = gate_commits[gate_id]
        if commit not in reachable:
            findings.add(requirement, "gate_commit_not_ancestor", gate_id)
            continue
        ancestors = _strict_ancestors(source.parents, commit)
        for prerequisite in DIRECT_PREREQUISITES[gate_id]:
            if prerequisite in gate_commits and gate_commits[prerequisite] not in ancestors:
                findings.add(requirement, "gate_commit_prerequisite_not_ancestor", gate_id)
    return {gate_id: gate_commits[gate_id] for gate_id in CLOSURE_GATES}


# -- evidence roots and artifacts ------------------------------------------------------


def _prepare_roots(evidence_roots: Any, findings: _Findings) -> dict[str, Path]:
    requirement = "evidence_roots"
    if not isinstance(evidence_roots, Mapping):
        findings.add(requirement, "evidence_root_invalid")
        return {}
    resolved: dict[str, str] = {}
    for root_id, raw in evidence_roots.items():
        if not isinstance(root_id, str) or _ROOT_ID.fullmatch(root_id) is None:
            findings.add(requirement, "evidence_root_invalid")
            continue
        try:
            real = os.path.realpath(os.fspath(raw))
        except (OSError, TypeError, ValueError):
            real = ""
        if not real or not os.path.isdir(real):
            findings.add(requirement, "evidence_root_missing", root_id)
            continue
        resolved[root_id] = real
    items = sorted(resolved.items())
    for index, (_, first) in enumerate(items):
        for _, second in items[index + 1 :]:
            if _is_within(first, second) or _is_within(second, first):
                # One directory reachable under two identities: refuse to choose.
                findings.add(requirement, "evidence_root_ambiguous")
                return {}
    return {root_id: Path(real) for root_id, real in resolved.items()}


def _resolve_reference(reference: Any, roots: Mapping[str, Path]) -> tuple[Path | None, str | None]:
    if not isinstance(reference, str) or len(reference) > 512:
        return None, "artifact_reference_invalid"
    parts = reference.split("/")
    if len(parts) < 2 or any(_SEGMENT.fullmatch(part) is None for part in parts):
        return None, "artifact_reference_invalid"
    root = roots.get(parts[0])
    if root is None:
        return None, "artifact_root_unknown"
    path = root.joinpath(*parts[1:])
    if os.path.realpath(path) != str(path):
        return None, "artifact_reference_invalid"
    return path, None


_FILE_STATUS_FINDINGS = {
    "missing": "artifact_missing",
    "symlink": "artifact_reference_invalid",
    "unreadable": "artifact_unreadable",
}


def _load_evidence_files(directory: Path, manifest: Any) -> tuple[dict[str, bytes] | None, str | None]:
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries or len(entries) > MAX_EVIDENCE_FILES:
        return None, "evidence_manifest_invalid"
    files: dict[str, bytes] = {}
    for entry in entries:
        relative = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(relative, str) or len(relative) > 512:
            return None, "evidence_manifest_invalid"
        parts = relative.split("/")
        if any(_SEGMENT.fullmatch(part) is None for part in parts) or relative in files:
            return None, "evidence_manifest_invalid"
        path = directory.joinpath(*parts)
        if os.path.realpath(path) != str(path):
            return None, "artifact_reference_invalid"
        content, status = _load_file(path, MAX_ARTIFACT_BYTES)
        if content is None:
            return None, _FILE_STATUS_FINDINGS[status or "unreadable"]
        files[relative] = content
    return files, None


def _receipt_shape_valid(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        return False
    properties = receipt["properties"]
    cases = receipt["cases"]
    bound = receipt["bound_result_digests"]
    if (
        receipt["protocol"] != RECEIPT_PROTOCOL
        or any(
            not isinstance(receipt[field], str) or _SUBJECT.fullmatch(receipt[field]) is None
            for field in ("result_kind", "requirement_id", "subject", "source_commit")
        )
        or receipt["provenance"] not in PROVENANCE_KINDS
        or receipt["outcome"] not in {"pass", "fail", "blocked"}
        or receipt["tree_state"] not in {"clean", "dirty"}
        or not isinstance(properties, list)
        or any(item not in RECEIPT_PROPERTIES for item in properties)
        or properties != sorted(set(properties))
        or any(
            not is_sha256_ref(receipt[field])
            for field in (
                "source_tree_digest",
                "contract_digest",
                "model_digest",
                "representation_digest",
                "runtime_digest",
                "environment_digest",
                "verifier_policy_digest",
            )
        )
        or not _is_generation(receipt["authority_generation"])
        or not isinstance(receipt["executed_at"], str)
        or not isinstance(receipt["fresh_until"], str)
        or not isinstance(cases, list)
        or len(cases) > 512
        or not isinstance(bound, list)
        or any(not is_sha256_ref(item) for item in bound)
        or bound != sorted(set(bound))
        or not isinstance(receipt["evidence_manifest"], dict)
        or not (receipt["signature"] is None or isinstance(receipt["signature"], dict))
    ):
        return False
    case_ids: list[str] = []
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"case_id", "outcome"}
            or not isinstance(case["case_id"], str)
            or _SLUG.fullmatch(case["case_id"]) is None
            or case["outcome"] not in {"pass", "fail", "blocked"}
        ):
            return False
        case_ids.append(case["case_id"])
    return case_ids == sorted(set(case_ids))


# -- result-kind artifact verifiers ----------------------------------------------------


def _verify_junit(context: _Context, files: dict[str, bytes], directory: Path, add: Any) -> None:
    reports = [content for name, content in sorted(files.items()) if name.endswith(".xml")]
    if not reports:
        add("junit_report_missing")
        return
    tests = failures = skips = 0
    for content in reports:
        if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
            add("junit_report_invalid")
            return
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            add("junit_report_invalid")
            return
        for case in root.iter("testcase"):
            tests += 1
            children = {child.tag for child in case}
            failures += bool(children & {"failure", "error"})
            skips += "skipped" in children
        for suite in root.iter("testsuite"):
            failures += suite.get("failures", "0") != "0" or suite.get("errors", "0") != "0"
            skips += suite.get("skipped", "0") != "0"
    if tests == 0:
        add("junit_no_executed_tests")
    if failures:
        add("junit_failures_present")
    if skips:
        add("junit_skips_present")


def _verify_physical_bundle(
    context: _Context, files: dict[str, bytes], directory: Path, add: Any
) -> None:
    manifests = [
        name
        for name in files
        if "/" in name and name.rsplit("/", 1)[1] == BUNDLE_MANIFEST_FILENAME
    ]
    if len(manifests) != 1:
        add("physical_bundle_missing")
        return
    bundle_root = directory.joinpath(*manifests[0].split("/")[:-1])
    result = verify_bundle(
        bundle_root, expected_manifest_sha256=sha256_bytes(files[manifests[0]])
    )
    if result.get("ok") is not True:
        add("physical_bundle_invalid")
    elif context.trust.scope == SCOPE_SYNTHETIC:
        if result.get("synthetic_fixture") is not True:
            add("evidence_class_scope_mismatch")
    elif (
        result.get("synthetic_fixture") is not False
        or result.get("evidence_class") != "physical_qualification"
    ):
        add("synthetic_evidence_not_admissible")
    elif result.get("physical_input_inventory_complete") is not True:
        add("physical_bundle_incomplete")


def _verify_contract_manifest(
    context: _Context, files: dict[str, bytes], directory: Path, add: Any
) -> None:
    content = files.get(CONTRACT_MANIFEST_EVIDENCE_NAME)
    source = context.source
    if content is None or source is None or content != source.contract_bytes:
        add("contract_manifest_mismatch")


def _verify_sbom(context: _Context, files: dict[str, bytes], directory: Path, add: Any) -> None:
    content = files.get(SBOM_EVIDENCE_NAME)
    if content is None:
        add("sbom_missing")
        return
    try:
        document = validate_sbom(canonical_json_loads(content, path="sbom"))
    except (EvidenceValidationError, ValueError, TypeError, KeyError, AttributeError):
        add("sbom_invalid")
        return
    if document["revision"] != context.declared_commit:
        add("sbom_source_mismatch")
    components = [item for item in document["components"] if isinstance(item, dict)]
    if not set(SBOM_REQUIRED_COMPONENT_KINDS) <= {item.get("kind") for item in components}:
        add("sbom_incomplete")
    source = context.source
    contract_digest = (
        None if source is None or source.contract_bytes is None else sha256_bytes(source.contract_bytes)
    )
    if any(
        item.get("kind") == "contract" and item.get("sha256") != contract_digest
        for item in components
    ):
        add("sbom_contract_mismatch")


_ARTIFACT_VERIFIERS = {
    "junit_suite": _verify_junit,
    "physical_evidence_bundle": _verify_physical_bundle,
    "contract_manifest": _verify_contract_manifest,
    "sbom_document": _verify_sbom,
}


def _verify_result(
    context: _Context,
    requirement: str,
    result_kind: Any,
    reference: Any,
    declared_digest: Any,
) -> dict[str, Any] | None:
    """Verify one referenced result from its bytes. Returns the receipt when it parses."""

    def add(code: str, detail: str | None = None) -> None:
        subject = requirement if detail is None else f"{requirement}#{detail}"
        context.findings.add(requirement, code, subject)

    kind_policy = RESULT_KINDS.get(result_kind) if isinstance(result_kind, str) else None
    if kind_policy is None or result_kind == "release_closure":
        add("result_kind_not_allowlisted")
        return None
    slot = "gate_result" if requirement.startswith("gate_result:") else requirement
    if kind_policy["requirement"] != slot:
        add("result_kind_wrong_for_requirement")
        return None
    trust = context.trust
    if trust.policy_digests.get(result_kind) != verifier_policy_digest(result_kind):
        add("verifier_policy_not_allowlisted")
    if not is_sha256_ref(declared_digest):
        add("artifact_digest_invalid")
        return None
    path, code = _resolve_reference(reference, context.roots)
    if path is None:
        add(code or "artifact_reference_invalid")
        return None
    content, status = _load_file(path, MAX_ARTIFACT_BYTES)
    if content is None:
        add(_FILE_STATUS_FINDINGS[status or "unreadable"])
        return None
    if sha256_bytes(content) != declared_digest:
        add("artifact_digest_inconsistent")
        return None
    if _PRIVATE_BYTES.search(content):
        add("privacy_leak_detected")
    try:
        receipt = canonical_json_loads(content, path="receipt")
    except EvidenceValidationError:
        receipt = None
    if not _receipt_shape_valid(receipt):
        add("result_receipt_invalid")
        return None

    role = (
        ROLE_INDEPENDENT_REVIEWER
        if result_kind == "external_reviewer_reproduction"
        else ROLE_RESULT_VERIFIER_PREFIX + result_kind
    )
    signature_problem = _check_signature(trust, receipt, role)
    if signature_problem is not None:
        add("result_" + signature_problem)
    else:
        context.signers[requirement] = receipt["signature"]["verification_key_digest"]
    expected_subject = (
        f"{requirement.split(':', 1)[1]}:executed_gate_result"
        if slot == "gate_result"
        else requirement
    )
    if (
        receipt["result_kind"] != result_kind
        or receipt["requirement_id"] != requirement
        or receipt["subject"] != expected_subject
        or (result_kind != "external_reviewer_reproduction" and receipt["bound_result_digests"])
    ):
        add("result_binding_mismatch")
    if receipt["provenance"] != REQUIRED_PROVENANCE:
        add("provenance_requirement_unsatisfied")
    for required_property in sorted(kind_policy["properties"]):
        if required_property not in receipt["properties"]:
            add("required_property_missing", required_property)
    if (
        receipt["source_commit"] != context.declared_commit
        or receipt["source_tree_digest"] != context.declared_tree
    ):
        add("source_binding_inconsistent")
    if "clean_tree" in kind_policy["properties"] and receipt["tree_state"] != "clean":
        add("clean_tree_required")
    if receipt["authority_generation"] != trust.current_generation:
        add("authority_generation_not_current")
    if receipt["verifier_policy_digest"] != verifier_policy_digest(result_kind):
        add("verifier_policy_mismatch")
    executed_at = _parse_timestamp(receipt["executed_at"])
    fresh_until = _parse_timestamp(receipt["fresh_until"])
    if executed_at is None or fresh_until is None or fresh_until <= executed_at:
        add("freshness_binding_invalid")
    else:
        if executed_at > context.now:
            add("evidence_executed_in_future")
        if fresh_until <= context.now:
            add("mandatory_evidence_expired")
        context.validity.append(fresh_until)

    manifest = receipt["evidence_manifest"]
    files, problem = _load_evidence_files(path.parent, manifest)
    if files is None:
        add(problem or "evidence_manifest_invalid")
    else:
        try:
            validate_evidence_manifest(manifest, files)
        except EvidenceValidationError as error:
            add(
                "artifact_digest_inconsistent"
                if error.code in {"evidence_file_digest_mismatch", "evidence_file_size_mismatch"}
                else "evidence_manifest_invalid"
            )
            files = None
    if files is not None:
        evidence_class = manifest.get("evidence_class")
        if trust.scope == SCOPE_PRODUCTION and evidence_class != "physical_qualification":
            add("synthetic_evidence_not_admissible")
        if trust.scope == SCOPE_SYNTHETIC and evidence_class != "synthetic_test_fixture":
            add("evidence_class_scope_mismatch")
        if any(_PRIVATE_BYTES.search(item) for item in files.values()):
            add("privacy_leak_detected")
        artifact_verifier = _ARTIFACT_VERIFIERS.get(result_kind)
        if artifact_verifier is not None:
            artifact_verifier(context, files, path.parent, add)

    outcomes = {case["case_id"]: case["outcome"] for case in receipt["cases"]}
    if not outcomes:
        add("result_cases_missing")
    required_cases = kind_policy["required_cases"]
    for case_id in required_cases:
        outcome = outcomes.get(case_id)
        if outcome == "pass":
            continue
        excluded = f"{requirement}#{case_id}" in context.excluded_cases
        if excluded and outcome in {None, "blocked"}:
            continue
        add("required_case_missing" if outcome is None else "required_case_not_passed", case_id)
    if any(
        outcome != "pass" for case_id, outcome in outcomes.items() if case_id not in required_cases
    ):
        add("case_not_passed")
    if result_kind == "package_manifest" and not all(
        case_id.startswith("platform:") for case_id in outcomes
    ):
        add("platform_case_missing")
    if receipt["outcome"] != "pass":
        add("result_outcome_not_pass")
    context.receipts[requirement] = receipt
    return receipt


# -- exclusions (spec section 3) -------------------------------------------------------


def _evaluate_exclusions(context: _Context, exclusions: Any) -> list[dict[str, Any]]:
    requirement = "exclusions"
    findings = context.findings
    if not isinstance(exclusions, list):
        findings.add(requirement, "exclusion_set_invalid")
        return []
    if len(exclusions) > MAX_EXCLUSIONS:
        findings.add(requirement, "exclusion_count_exceeded")
    summaries: list[dict[str, Any]] = []
    covered: dict[str, datetime] = {}
    for exclusion in exclusions:
        if not isinstance(exclusion, dict):
            findings.add(requirement, "exclusion_shape_invalid")
            continue
        if set(exclusion) != EXCLUSION_FIELDS:
            only_signature_missing = EXCLUSION_FIELDS - set(exclusion) == {"signature"} and not (
                set(exclusion) - EXCLUSION_FIELDS
            )
            findings.add(
                requirement,
                "exclusion_signature_missing" if only_signature_missing else "exclusion_shape_invalid",
            )
            continue
        requirement_ids = exclusion["requirement_ids"]
        if (
            any(
                not isinstance(exclusion[field], str) or not exclusion[field].strip()
                for field in (
                    "exclusion_digest",
                    "scope",
                    "reason",
                    "risk",
                    "compensating_behavior",
                    "reviewer_identity",
                    "signer_key_id",
                )
            )
            or not isinstance(requirement_ids, list)
            or not requirement_ids
            or any(not isinstance(item, str) for item in requirement_ids)
        ):
            findings.add(requirement, "exclusion_shape_invalid")
            continue
        before = findings.count(requirement)
        digest = exclusion_digest(exclusion)
        if exclusion["exclusion_digest"] != digest:
            findings.add(requirement, "exclusion_digest_mismatch")
        if len(requirement_ids) > MAX_REQUIREMENTS_PER_EXCLUSION:
            findings.add(requirement, "exclusion_requirement_count_exceeded")
        for requirement_id in sorted(set(requirement_ids)):
            if requirement_id in EXCLUDABLE_REQUIREMENTS:
                continue
            findings.add(
                requirement,
                "mandatory_requirement_excluded"
                if requirement_id in NON_EXCLUDABLE_REQUIREMENTS
                else "exclusion_requirement_unknown",
            )
        if exclusion["scope"] != EXCLUSION_SCOPE:
            findings.add(requirement, "exclusion_scope_unbounded")
        if len(exclusion["reason"].encode("utf-8")) > MAX_EXCLUSION_REASON_BYTES:
            findings.add(requirement, "exclusion_reason_unbounded")
        issued_at = _parse_timestamp(exclusion["issued_at"])
        expires_at = _parse_timestamp(exclusion["expires_at"])
        if issued_at is None or expires_at is None or expires_at <= issued_at:
            findings.add(requirement, "exclusion_expiry_invalid")
        else:
            if expires_at - issued_at > MAX_EXCLUSION_LIFETIME:
                findings.add(requirement, "exclusion_lifetime_unbounded")
            if issued_at > context.now:
                findings.add(requirement, "exclusion_issued_in_future")
            if expires_at <= context.now:
                findings.add(requirement, "exclusion_expired")
        signature_problem = _check_signature(context.trust, exclusion, ROLE_EXCLUSION_SIGNER)
        if signature_problem is not None:
            findings.add(requirement, "exclusion_" + signature_problem)
        elif exclusion["signer_key_id"] != exclusion["signature"]["verification_key_digest"]:
            findings.add(requirement, "exclusion_signer_key_mismatch")
        approved = findings.count(requirement) == before and expires_at is not None
        if approved:
            for requirement_id in sorted(set(requirement_ids)):
                if requirement_id in covered:
                    findings.add(requirement, "exclusion_duplicate_requirement")
                covered[requirement_id] = expires_at
        summaries.append(
            {
                "exclusion_digest": digest,
                "expires_at": exclusion["expires_at"] if approved else None,
                "requirement_ids": sorted(set(requirement_ids)) if approved else [],
                "state": "approved" if approved else "invalid",
            }
        )
    if EXCLUDABLE_REQUIREMENTS <= set(covered):
        # Excluding every engine would remove the browser matrix, not narrow it.
        findings.add(requirement, "exclusion_scope_unbounded")
    if findings.count(requirement) == 0:
        context.excluded_cases = set(covered)
        context.validity.extend(covered.values())
    return summaries


# -- graph nodes -----------------------------------------------------------------------

_MIRRORED_NODE_FIELDS = (
    "result_kind",
    "provenance",
    "subject",
    "source_commit",
    "source_tree_digest",
    "tree_state",
    "verifier_policy_digest",
    "executed_at",
    "fresh_until",
    "contract_digest",
    "model_digest",
    "representation_digest",
    "runtime_digest",
    "environment_digest",
    "authority_generation",
)


def _has_cycle(by_gate: Mapping[str, Mapping[str, Any]]) -> bool:
    state: dict[str, int] = {}

    def visit(gate_id: str) -> bool:
        if state.get(gate_id) == 1:
            return True
        if state.get(gate_id) == 2:
            return False
        state[gate_id] = 1
        dependencies = by_gate[gate_id].get("dependency_gate_ids")
        for dependency in dependencies if isinstance(dependencies, list) else []:
            if isinstance(dependency, str) and dependency in by_gate and visit(dependency):
                return True
        state[gate_id] = 2
        return False

    return any(visit(gate_id) for gate_id in by_gate)


def _evaluate_nodes(context: _Context, nodes: Any) -> dict[str, dict[str, Any]]:
    findings = context.findings
    by_gate: dict[str, dict[str, Any]] = {}
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or set(node) != NODE_FIELDS:
            findings.add("gate_graph", "node_shape_invalid")
            continue
        gate_id = node["gate_id"]
        if gate_id == ROOT_GATE:
            # The closure root is derived here; a caller cannot declare it.
            findings.add("gate_graph", "derived_root_node_supplied")
            continue
        if not isinstance(gate_id, str) or gate_id not in INPUT_GATES:
            findings.add("gate_graph", "node_set_mismatch")
            continue
        requirement = f"gate_result:{gate_id}"
        if gate_id in by_gate:
            findings.add(
                requirement,
                "duplicate_logical_result" if node == by_gate[gate_id] else "conflicting_duplicate_node",
                requirement,
            )
            continue
        by_gate[gate_id] = node
    if not isinstance(nodes, list):
        findings.add("gate_graph", "node_set_mismatch")
    for gate_id in INPUT_GATES:
        if gate_id not in by_gate:
            findings.add("gate_graph", "node_set_mismatch")
            findings.add(f"gate_result:{gate_id}", "result_missing", f"gate_result:{gate_id}")
    if _has_cycle(by_gate):
        findings.add("gate_graph", "dependency_cycle_detected")

    for gate_id in INPUT_GATES:
        node = by_gate.get(gate_id)
        if node is None:
            continue
        requirement = f"gate_result:{gate_id}"
        if node["protocol"] != NODE_PROTOCOL:
            findings.add(requirement, "node_protocol_invalid", requirement)
        if node["node_digest"] != node_digest(node):
            findings.add(requirement, "node_digest_mismatch", requirement)
        expected = DIRECT_PREREQUISITES[gate_id]
        dependencies = node["dependency_gate_ids"]
        edges_exact = (
            isinstance(dependencies, list)
            and all(isinstance(item, str) for item in dependencies)
            and len(dependencies) == len(expected)
            and set(dependencies) == set(expected)
        )
        if not edges_exact or node["dependency_digests"] != [
            by_gate[item]["node_digest"] for item in dependencies if item in by_gate
        ] or len(node["dependency_digests"]) != len(expected):
            findings.add(requirement, "dependency_digests_not_exact", requirement)
        if (
            node["source_commit"] != context.declared_commit
            or node["source_tree_digest"] != context.declared_tree
        ):
            findings.add(requirement, "source_binding_inconsistent", requirement)
        if node["tree_state"] != "clean":
            findings.add(requirement, "clean_tree_required", requirement)
        if node["subject"] != f"{gate_id}:executed_gate_result":
            findings.add(requirement, "subject_binding_invalid", requirement)

        before = findings.count(requirement)
        receipt = _verify_result(
            context, requirement, node["result_kind"], node["artifact_reference"], node["artifact_digest"]
        )
        if receipt is not None and any(
            node[field] != receipt[field] for field in _MIRRORED_NODE_FIELDS
        ):
            findings.add(requirement, "envelope_contradicts_artifact", requirement)
        artifact_verified = receipt is not None and findings.count(requirement) == before
        if node["outcome"] != "verified":
            findings.add(requirement, "verified_outcome_required", requirement)
        elif not artifact_verified:
            # The envelope claims a pass that the referenced artifact does not prove.
            findings.add(requirement, "outcome_contradicts_artifact", requirement)
    return by_gate


def _evaluate_bindings(context: _Context) -> tuple[str | None, str | None, str | None]:
    """One contract, model and representation across every verified result."""
    source = context.source
    contract_digest = (
        None if source is None or source.contract_bytes is None else sha256_bytes(source.contract_bytes)
    )
    model_receipt = context.receipts.get("digest_class:model")
    model_digest = None if model_receipt is None else model_receipt["model_digest"]
    representation_digest = (
        None if model_receipt is None else model_receipt["representation_digest"]
    )
    for requirement, receipt in sorted(context.receipts.items()):
        if receipt["contract_digest"] != contract_digest:
            context.findings.add(requirement, "contract_binding_inconsistent", requirement)
        if model_receipt is None:
            continue
        if receipt["model_digest"] != model_digest:
            context.findings.add(requirement, "model_binding_inconsistent", requirement)
        if receipt["representation_digest"] != representation_digest:
            context.findings.add(requirement, "representation_binding_inconsistent", requirement)
    return contract_digest, model_digest, representation_digest


# -- evaluation ------------------------------------------------------------------------

_REQUIREMENT_ORDER = (
    "candidate",
    "evidence_roots",
    "trust_registry",
    "source_integrity",
    "gate_graph",
    *(f"gate_result:{gate_id}" for gate_id in INPUT_GATES),
    *(f"digest_class:{digest_class}" for digest_class in DIGEST_CLASSES),
    REVIEWER_REQUIREMENT,
    "exclusions",
    "decision_history",
)
_TRUST_DEPENDENT = _REQUIREMENT_ORDER[4:-1]


@dataclasses.dataclass(slots=True)
class _Evaluation:
    findings: _Findings
    not_evaluated: set[str]
    authority_scope: str
    candidate_digest: str | None
    trust_registry_digest: str | None
    source: _Source | None
    exclusions: list[dict[str, Any]]
    withheld_claims: list[str]
    valid_until: datetime | None
    graph_inputs: dict[str, Any] | None


def _evaluate(
    *,
    candidate_path: os.PathLike[str] | str,
    repo_root: os.PathLike[str] | str,
    evidence_roots: Any,
    trust_registry_path: os.PathLike[str] | str,
    trust_registry_sha256: Any,
    forbidden_trust_directories: list[str],
    now: datetime,
    allow_synthetic_trust: bool,
    revoked_candidates: set[str],
    revoked_registries: set[str],
) -> _Evaluation:
    findings = _Findings()
    roots = _prepare_roots(evidence_roots, findings)
    source = _observe_source(repo_root, findings)
    trust, registry_digest, scope = _load_trust(
        trust_registry_path,
        trust_registry_sha256,
        forbidden_trust_directories,
        now,
        allow_synthetic_trust is True,
        findings,
    )
    if registry_digest is not None and registry_digest in revoked_registries:
        findings.add("trust_registry", "trust_registry_revoked")
        trust = None
    evaluation = _Evaluation(
        findings=findings,
        not_evaluated=set(),
        authority_scope=scope,
        candidate_digest=None,
        trust_registry_digest=registry_digest,
        source=source,
        exclusions=[],
        withheld_claims=[],
        valid_until=None,
        graph_inputs=None,
    )

    try:
        content, status = _load_file(Path(os.path.abspath(os.fspath(candidate_path))), MAX_CANDIDATE_BYTES)
    except (TypeError, ValueError):
        content, status = None, "missing"
    candidate: Any = None
    if content is None:
        findings.add("candidate", "candidate_missing" if status == "missing" else "candidate_unreadable")
    else:
        evaluation.candidate_digest = sha256_bytes(content)
        if evaluation.candidate_digest in revoked_candidates:
            findings.add("candidate", "candidate_revoked")
        try:
            candidate = canonical_json_loads(content, path="candidate")
        except EvidenceValidationError:
            findings.add("candidate", "candidate_not_canonical_json")
            content = None
    if content is not None:
        if _has_claim(candidate):
            findings.add("candidate", "unsupported_completion_claim")
        if (
            not isinstance(candidate, dict)
            or set(candidate) != CANDIDATE_FIELDS
            or candidate["protocol"] != CANDIDATE_PROTOCOL
            or not isinstance(candidate["source"], dict)
            or set(candidate["source"]) != {"commit", "tree_digest"}
        ):
            findings.add("candidate", "candidate_shape_invalid")
            candidate = None
    if candidate is None:
        evaluation.not_evaluated = set(_TRUST_DEPENDENT)
        return evaluation

    declared_commit = candidate["source"]["commit"]
    declared_tree = candidate["source"]["tree_digest"]
    if not isinstance(declared_commit, str) or _COMMIT.fullmatch(declared_commit) is None:
        findings.add("source_integrity", "exact_source_commit_required")
    if not is_sha256_ref(declared_tree):
        findings.add("source_integrity", "source_tree_digest_invalid")
    if source is not None:
        if source.commit != declared_commit:
            findings.add("source_integrity", "source_head_mismatch")
        if source.tree_digest != declared_tree:
            findings.add("source_integrity", "source_tree_mismatch")
    gate_commits = _check_gate_commits(candidate["gate_commits"], source, findings)

    if trust is None:
        # Without a usable pinned registry no signature or policy can be judged.
        evaluation.not_evaluated = set(_TRUST_DEPENDENT) - {"gate_graph"}
        return evaluation

    context = _Context(
        now=now,
        roots=roots,
        trust=trust,
        source=source,
        declared_commit=declared_commit,
        declared_tree=declared_tree,
        findings=findings,
    )
    context.validity.append(trust.expires_at)
    evaluation.exclusions = _evaluate_exclusions(context, candidate["exclusions"])
    evaluation.withheld_claims = sorted(context.excluded_cases)
    by_gate = _evaluate_nodes(context, candidate["nodes"])

    class_results = candidate["class_results"]
    class_digests: dict[str, str] = {}
    if not isinstance(class_results, dict) or set(class_results) != set(DIGEST_CLASSES):
        findings.add("candidate", "digest_class_set_mismatch")
    for digest_class in DIGEST_CLASSES:
        requirement = f"digest_class:{digest_class}"
        entry = class_results.get(digest_class) if isinstance(class_results, dict) else None
        if entry is None:
            findings.add(requirement, "result_missing", requirement)
            continue
        if not isinstance(entry, dict) or set(entry) != {
            "result_kind",
            "artifact_reference",
            "artifact_digest",
        }:
            findings.add(requirement, "class_result_shape_invalid", requirement)
            continue
        _verify_result(
            context, requirement, entry["result_kind"], entry["artifact_reference"], entry["artifact_digest"]
        )
        class_digests[digest_class] = entry["artifact_digest"]

    reviewer = candidate["reviewer_reproduction"]
    reviewer_digest: str | None = None
    if reviewer is None:
        findings.add(REVIEWER_REQUIREMENT, "reviewer_reproduction_missing")
    elif not isinstance(reviewer, dict) or set(reviewer) != {"artifact_reference", "artifact_digest"}:
        findings.add(REVIEWER_REQUIREMENT, "reviewer_reproduction_shape_invalid")
    else:
        receipt = _verify_result(
            context,
            REVIEWER_REQUIREMENT,
            "external_reviewer_reproduction",
            reviewer["artifact_reference"],
            reviewer["artifact_digest"],
        )
        reviewer_digest = reviewer["artifact_digest"]
        if receipt is not None:
            node_digests = sorted(
                node["node_digest"] for node in by_gate.values() if isinstance(node["node_digest"], str)
            )
            if receipt["bound_result_digests"] != node_digests or len(node_digests) != len(INPUT_GATES):
                findings.add(REVIEWER_REQUIREMENT, "reviewer_result_binding_mismatch")
            reviewer_key = context.signers.get(REVIEWER_REQUIREMENT)
            if reviewer_key is not None and any(
                key == reviewer_key
                for requirement, key in context.signers.items()
                if requirement != REVIEWER_REQUIREMENT
            ):
                findings.add(REVIEWER_REQUIREMENT, "reviewer_not_independent")

    contract_digest, model_digest, representation_digest = _evaluate_bindings(context)
    if findings.all():
        return evaluation
    evaluation.valid_until = min(context.validity)
    evaluation.graph_inputs = {
        "by_gate": by_gate,
        "gate_commits": gate_commits,
        "class_digests": class_digests,
        "reviewer_digest": reviewer_digest,
        "exclusion_digests": [item["exclusion_digest"] for item in evaluation.exclusions],
        "contract_digest": contract_digest,
        "model_digest": model_digest,
        "representation_digest": representation_digest,
        "authority_generation": trust.current_generation,
        "declared_commit": declared_commit,
        "declared_tree": declared_tree,
    }
    return evaluation


def _derive_graph(
    evaluation: _Evaluation, generation: int, now: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive the A15 closure node and the root from fully verified inputs."""
    inputs = evaluation.graph_inputs
    assert inputs is not None and evaluation.valid_until is not None
    by_gate = inputs["by_gate"]
    closure = {
        "protocol": CLOSURE_PROTOCOL,
        "authority_scope": evaluation.authority_scope,
        "candidate_digest": evaluation.candidate_digest,
        "trust_registry_digest": evaluation.trust_registry_digest,
        "source_commit": inputs["declared_commit"],
        "source_tree_digest": inputs["declared_tree"],
        "gate_commit_digests": inputs["gate_commits"],
        "dependency_node_digests": {
            gate_id: by_gate[gate_id]["node_digest"] for gate_id in CLOSURE_GATES
        },
        "digest_class_roots": inputs["class_digests"],
        "exclusion_digests": inputs["exclusion_digests"],
        "reviewer_reproduction_digest": inputs["reviewer_digest"],
        "withheld_claims": evaluation.withheld_claims,
    }
    dependencies = sorted(DIRECT_PREREQUISITES[ROOT_GATE])
    closure_node: dict[str, Any] = {
        "protocol": NODE_PROTOCOL,
        "node_digest": "",
        "gate_id": ROOT_GATE,
        "result_kind": "release_closure",
        "outcome": "verified",
        "source_commit": inputs["declared_commit"],
        "source_tree_digest": inputs["declared_tree"],
        "tree_state": "clean",
        "provenance": REQUIRED_PROVENANCE,
        "subject": f"{ROOT_GATE}:executed_gate_result",
        "artifact_reference": "derived/A15-release-closure.json",
        "artifact_digest": sha256_document(closure),
        "dependency_gate_ids": dependencies,
        "dependency_digests": [by_gate[gate_id]["node_digest"] for gate_id in dependencies],
        "verifier_policy_digest": verifier_policy_digest("release_closure"),
        "executed_at": format_timestamp(now),
        "fresh_until": format_timestamp(evaluation.valid_until),
        "contract_digest": inputs["contract_digest"],
        "model_digest": inputs["model_digest"],
        "representation_digest": inputs["representation_digest"],
        "runtime_digest": sha256_document({"producer": PRODUCER}),
        "environment_digest": sha256_document(
            {
                "authority_scope": evaluation.authority_scope,
                "trust_registry_digest": evaluation.trust_registry_digest,
            }
        ),
        "authority_generation": inputs["authority_generation"],
    }
    closure_node["node_digest"] = node_digest(closure_node)
    nodes = [by_gate[gate_id] for gate_id in INPUT_GATES] + [closure_node]
    root: dict[str, Any] = {
        "root_digest": "",
        "source_commit": inputs["declared_commit"],
        "source_tree_digest": inputs["declared_tree"],
        "tree_state": "clean",
        "gate_commit_digests": inputs["gate_commits"],
        "result_node_digests": {node["gate_id"]: node["node_digest"] for node in nodes},
        "digest_class_roots": inputs["class_digests"],
        "exclusion_digests": inputs["exclusion_digests"],
        "reviewer_reproduction_digest": inputs["reviewer_digest"],
        "decision_generation": generation,
    }
    root["root_digest"] = _root_digest(root)
    return {"protocol": GRAPH_PROTOCOL, "root": root, "nodes": nodes}, closure


def _root_digest(root: Mapping[str, Any]) -> str:
    return sha256_document({key: value for key, value in root.items() if key != "root_digest"})


def _requirements(evaluation: _Evaluation) -> list[dict[str, Any]]:
    excluded = {claim.split("#", 1)[0] for claim in evaluation.withheld_claims}
    # Propagate an invalid gate result to every gate that depends on it, transitively.
    invalidated: set[str] = set()
    for gate_id in INPUT_GATES:
        if any(
            evaluation.findings.count(f"gate_result:{prerequisite}") or prerequisite in invalidated
            for prerequisite in DIRECT_PREREQUISITES[gate_id]
        ):
            invalidated.add(gate_id)
    rows = []
    for requirement in _REQUIREMENT_ORDER:
        requirement_findings = sorted(evaluation.findings.by_requirement.get(requirement, ()))
        if requirement_findings:
            state = "not_verified"
        elif requirement in evaluation.not_evaluated:
            state = "not_evaluated"
        elif requirement.split(":", 1)[-1] in invalidated:
            state = "invalidated_by_dependency"
        elif requirement in excluded:
            state = "excluded_approved"
        else:
            state = "verified"
        rows.append(
            {"requirement_id": requirement, "state": state, "findings": requirement_findings}
        )
    return rows


# -- sealed decision documents ---------------------------------------------------------


def _structure_valid(body: Mapping[str, Any]) -> bool:
    """A ``ready`` body must carry a self-consistent graph; nothing else may claim ready."""
    findings = body["findings"]
    requirements = body["requirements"]
    if (
        body["decision"] not in {"ready", "not_ready"}
        or not _is_generation(body["decision_generation"])
        or _parse_timestamp(body["decided_at"]) is None
        or not isinstance(findings, list)
        or any(not isinstance(item, str) for item in findings)
        or not isinstance(requirements, list)
        or any(not isinstance(item, dict) for item in requirements)
        or not (
            body["previous_decision_digest"] is None
            or is_sha256_ref(body["previous_decision_digest"])
        )
        or (body["previous_decision_digest"] is None) != (body["decision_generation"] == 1)
        or body["authority_scope"] not in {SCOPE_PRODUCTION, SCOPE_SYNTHETIC, SCOPE_UNDETERMINED}
    ):
        return False
    graph = body["graph"]
    closure = body["closure_statement"]
    if body["decision"] == "not_ready":
        return bool(findings) and graph is None and closure is None and body["valid_until"] is None
    if (
        findings
        or body["revocation"] is not None
        or body["authority_scope"] == SCOPE_UNDETERMINED
        or _parse_timestamp(body["valid_until"]) is None
        or any(item.get("state") not in {"verified", "excluded_approved"} for item in requirements)
        or not isinstance(closure, dict)
        or not isinstance(graph, dict)
        or set(graph) != {"protocol", "root", "nodes"}
        or graph["protocol"] != GRAPH_PROTOCOL
        or not isinstance(graph["root"], dict)
        or set(graph["root"]) != ROOT_FIELDS
        or not isinstance(graph["nodes"], list)
    ):
        return False
    root = graph["root"]
    nodes = graph["nodes"]
    if any(not isinstance(node, dict) or set(node) != NODE_FIELDS for node in nodes):
        return False
    by_gate = {node["gate_id"]: node for node in nodes}
    if [node["gate_id"] for node in nodes] != [*INPUT_GATES, ROOT_GATE]:
        return False
    for gate_id, node in by_gate.items():
        dependencies = node["dependency_gate_ids"]
        if (
            node["node_digest"] != node_digest(node)
            or node["outcome"] != "verified"
            or node["provenance"] != REQUIRED_PROVENANCE
            or set(dependencies) != set(DIRECT_PREREQUISITES[gate_id])
            or node["dependency_digests"] != [by_gate[item]["node_digest"] for item in dependencies]
        ):
            return False
    return (
        root["root_digest"] == _root_digest(root)
        and root["decision_generation"] == body["decision_generation"]
        and root["result_node_digests"] == {gate: node["node_digest"] for gate, node in by_gate.items()}
        and set(root["digest_class_roots"]) == set(DIGEST_CLASSES)
        and set(root["gate_commit_digests"]) == set(CLOSURE_GATES)
        and by_gate[ROOT_GATE]["artifact_digest"] == sha256_document(closure)
        and by_gate[ROOT_GATE]["fresh_until"] == body["valid_until"]
    )


def verify_decision_bytes(content: bytes) -> dict[str, Any]:
    """Check the seal and structure of one decision generation's exact bytes."""
    if (
        not isinstance(content, bytes)
        or len(content) > MAX_DECISION_BYTES
        or not content.endswith(b"\n")
    ):
        raise _refuse("decision_not_canonical")
    try:
        document = canonical_json_loads(content[:-1], path="decision")
    except EvidenceValidationError:
        raise _refuse("decision_not_canonical") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"body", "body_sha256", "protocol"}
        or document["protocol"] != DECISION_PROTOCOL
        or not isinstance(document["body"], dict)
    ):
        raise _refuse("decision_shape_invalid")
    body = document["body"]
    if document["body_sha256"] != sha256_document(body):
        raise _refuse("decision_seal_invalid")
    if set(body) != DECISION_BODY_FIELDS:
        raise _refuse("decision_shape_invalid")
    try:
        valid = _structure_valid(body)
    except (KeyError, TypeError, AttributeError, EvidenceValidationError):
        valid = False
    if not valid:
        raise _refuse("decision_structure_invalid")
    return document


def _result_from_bytes(content: bytes, path: Path) -> DecisionResult:
    document = verify_decision_bytes(content)
    body = document["body"]
    return DecisionResult(
        decision=body["decision"],
        decision_generation=body["decision_generation"],
        decision_digest=sha256_bytes(content),
        sealed_bytes=content,
        ledger_entry_path=path,
        findings=tuple(body["findings"]),
        document=document,
    )


# -- append-only ledger ----------------------------------------------------------------


def _ledger_directory(ledger_dir: os.PathLike[str] | str) -> Path:
    try:
        absolute = os.path.abspath(os.fspath(ledger_dir))
        metadata = os.lstat(absolute)
    except (OSError, TypeError, ValueError):
        raise _refuse("ledger_dir_invalid") from None
    if not stat.S_ISDIR(metadata.st_mode) or os.path.realpath(absolute) != os.path.join(
        os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute)
    ):
        raise _refuse("ledger_dir_invalid")
    return Path(absolute)


def read_ledger(ledger_dir: os.PathLike[str] | str) -> list[DecisionResult]:
    """Read and verify the whole generation chain. Any inconsistency refuses."""
    directory = _ledger_directory(ledger_dir)
    try:
        names = sorted(name for name in os.listdir(directory) if not name.startswith("."))
    except OSError:
        raise _refuse("ledger_dir_invalid") from None
    entries: list[DecisionResult] = []
    scopes: set[str] = set()
    for index, name in enumerate(names, start=1):
        match = _LEDGER_NAME.fullmatch(name)
        if match is None or int(match.group(1)) != index:
            raise _refuse("decision_history_tampered")
        content, _status = _load_file(directory / name, MAX_DECISION_BYTES)
        if content is None:
            raise _refuse("decision_history_tampered")
        try:
            entry = _result_from_bytes(content, directory / name)
        except ReleaseDecisionRefused:
            raise _refuse("decision_history_tampered") from None
        body = entry.document["body"]
        previous = entries[-1] if entries else None
        if (
            entry.decision_generation != index
            or body["previous_decision_digest"] != (None if previous is None else previous.decision_digest)
            or (
                previous is not None
                and body["decided_at"] < previous.document["body"]["decided_at"]
            )
        ):
            raise _refuse("decision_history_tampered")
        if body["authority_scope"] != SCOPE_UNDETERMINED:
            scopes.add(body["authority_scope"])
        entries.append(entry)
    if len(scopes) > 1:
        raise _refuse("decision_history_tampered")
    return entries


def _append(directory: Path, entries: list[DecisionResult], body: dict[str, Any]) -> DecisionResult:
    """Seal the body, then create the next generation exclusively. Nothing is rewritten."""
    document = {"body": body, "body_sha256": sha256_document(body), "protocol": DECISION_PROTOCOL}
    sealed = canonical_json_bytes(document) + b"\n"
    target = directory / f"generation-{body['decision_generation']:06d}.json"
    try:
        sealed.decode("ascii")
        result = _result_from_bytes(sealed, target)
    except (ReleaseDecisionRefused, UnicodeDecodeError):
        raise _refuse("decision_self_check_failed") from None
    temporary = directory / f".tmp-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except OSError:
        raise _refuse("ledger_write_failed") from None
    try:
        try:
            view = memoryview(sealed)
            while view:
                view = view[os.write(descriptor, view) :]
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # A hard link fails if the generation exists: complete bytes or nothing.
        os.link(temporary, target)
    except FileExistsError:
        raise _refuse("decision_generation_conflict") from None
    except OSError:
        raise _refuse("ledger_write_failed") from None
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return result


def _history_state(
    directory: Path, now: datetime
) -> tuple[list[DecisionResult], set[str], set[str], set[str]]:
    entries = read_ledger(directory)
    if entries and format_timestamp(now) < entries[-1].document["body"]["decided_at"]:
        raise _refuse("clock_regression")
    revoked_candidates: set[str] = set()
    revoked_registries: set[str] = set()
    scopes: set[str] = set()
    for entry in entries:
        body = entry.document["body"]
        if body["authority_scope"] != SCOPE_UNDETERMINED:
            scopes.add(body["authority_scope"])
        revocation = body["revocation"]
        if isinstance(revocation, dict) and revocation.get("kind") == "explicit":
            if body["candidate_digest"] is not None:
                revoked_candidates.add(body["candidate_digest"])
            if revocation.get("reason") == "compromised_key" and body["trust_registry_digest"]:
                revoked_registries.add(body["trust_registry_digest"])
    return entries, revoked_candidates, revoked_registries, scopes


def produce_release_decision(
    *,
    candidate_path: os.PathLike[str] | str,
    repo_root: os.PathLike[str] | str,
    evidence_roots: Mapping[str, os.PathLike[str] | str],
    trust_registry_path: os.PathLike[str] | str,
    trust_registry_sha256: str,
    ledger_dir: os.PathLike[str] | str,
    now: datetime,
    allow_synthetic_trust: bool = False,
) -> DecisionResult:
    """Derive the decision and append it as the next generation.

    Invalid, missing, stale or untrusted input is never a refusal: it derives a new
    ``not_ready`` generation naming the finding. A refusal means the ledger itself cannot
    be safely extended (unsafe directory, tampered history, clock regression).
    """
    now = _require_utc(now)
    directory = _ledger_directory(ledger_dir)
    entries, revoked_candidates, revoked_registries, scopes = _history_state(directory, now)

    private_paths: list[str] = [str(directory), os.path.realpath(directory)]
    forbidden: list[str] = [os.path.realpath(directory)]
    for raw in (repo_root, *(evidence_roots.values() if isinstance(evidence_roots, Mapping) else ())):
        try:
            real = os.path.realpath(os.fspath(raw))
        except (OSError, TypeError, ValueError):
            continue
        forbidden.append(real)
        private_paths.extend((real, os.path.abspath(os.fspath(raw))))
    for raw in (candidate_path, trust_registry_path):
        try:
            private_paths.append(os.path.dirname(os.path.abspath(os.fspath(raw))))
        except (TypeError, ValueError):
            continue

    evaluation = _evaluate(
        candidate_path=candidate_path,
        repo_root=repo_root,
        evidence_roots=evidence_roots,
        trust_registry_path=trust_registry_path,
        trust_registry_sha256=trust_registry_sha256,
        forbidden_trust_directories=forbidden,
        now=now,
        allow_synthetic_trust=allow_synthetic_trust,
        revoked_candidates=revoked_candidates,
        revoked_registries=revoked_registries,
    )
    if evaluation.authority_scope != SCOPE_UNDETERMINED and scopes - {evaluation.authority_scope}:
        # A synthetic derivation must never extend a production decision history.
        raise _refuse("ledger_scope_mismatch")

    generation = len(entries) + 1
    previous = entries[-1] if entries else None
    findings = evaluation.findings.all()
    ready = not findings and evaluation.graph_inputs is not None
    graph: dict[str, Any] | None = None
    closure: dict[str, Any] | None = None
    if ready:
        graph, closure = _derive_graph(evaluation, generation, now)
    elif not findings:
        evaluation.findings.add("decision_history", "decision_not_derivable")
        findings = evaluation.findings.all()
    revocation = None
    if not ready and previous is not None and previous.decision == "ready":
        revocation = {
            "kind": "automatic",
            "reason": "derived_not_ready",
            "revoked_decision_digest": previous.decision_digest,
            "revoked_generation": previous.decision_generation,
        }
    source = evaluation.source
    body = {
        "authority_scope": evaluation.authority_scope,
        "candidate_digest": evaluation.candidate_digest,
        "claim_boundary": CLAIM_BOUNDARY,
        "closure_statement": closure,
        "decided_at": format_timestamp(now),
        "decision": "ready" if ready else "not_ready",
        "decision_generation": generation,
        "exclusions": evaluation.exclusions,
        "findings": findings,
        "graph": graph,
        "previous_decision_digest": None if previous is None else previous.decision_digest,
        "producer": dict(PRODUCER),
        "requirements": _requirements(evaluation),
        "revocation": revocation,
        "source": {
            "commit": None if source is None else source.commit,
            "object_format": None if source is None else source.object_format,
            "tree_digest": None if source is None else source.tree_digest,
            "tree_state": None if source is None else source.tree_state,
        },
        "trust_registry_digest": evaluation.trust_registry_digest,
        "valid_until": format_timestamp(evaluation.valid_until) if ready and evaluation.valid_until else None,
        "withheld_claims": evaluation.withheld_claims if ready else [],
    }
    sealed_preview = canonical_json_bytes(body)
    if any(len(item) > 1 and item.encode("utf-8") in sealed_preview for item in private_paths):
        raise _refuse("privacy_guard_tripped")
    return _append(directory, entries, body)


def append_revocation(
    *, ledger_dir: os.PathLike[str] | str, reason: str, now: datetime
) -> DecisionResult:
    """Append an explicit non-ready generation revoking the latest one (spec section 8)."""
    now = _require_utc(now)
    directory = _ledger_directory(ledger_dir)
    entries, _candidates, _registries, _scopes = _history_state(directory, now)
    if not entries:
        raise _refuse("nothing_to_revoke")
    if reason not in REVOCATION_REASONS:
        raise _refuse("revocation_reason_invalid")
    previous = entries[-1]
    revoked = previous.document["body"]
    finding = f"release_revoked:{reason}"
    body = {
        "authority_scope": revoked["authority_scope"],
        "candidate_digest": revoked["candidate_digest"],
        "claim_boundary": CLAIM_BOUNDARY,
        "closure_statement": None,
        "decided_at": format_timestamp(now),
        "decision": "not_ready",
        "decision_generation": previous.decision_generation + 1,
        "exclusions": [],
        "findings": [finding],
        "graph": None,
        "previous_decision_digest": previous.decision_digest,
        "producer": dict(PRODUCER),
        "requirements": [
            {"requirement_id": "decision_history", "state": "not_verified", "findings": [finding]}
        ],
        "revocation": {
            "kind": "explicit",
            "reason": reason,
            "revoked_decision_digest": previous.decision_digest,
            "revoked_generation": previous.decision_generation,
        },
        "source": dict(revoked["source"]),
        "trust_registry_digest": revoked["trust_registry_digest"],
        "valid_until": None,
        "withheld_claims": [],
    }
    return _append(directory, entries, body)


# -- public projection and exclusive output --------------------------------------------

_PUBLIC_NODE_FIELDS = (
    "gate_id",
    "result_kind",
    "provenance",
    "outcome",
    "node_digest",
    "artifact_digest",
    "executed_at",
    "fresh_until",
    "authority_generation",
)


def _public_text_only(value: Any) -> bool:
    if isinstance(value, str):
        return _PUBLIC_TEXT.fullmatch(value) is not None
    if isinstance(value, dict):
        return all(_public_text_only(key) and _public_text_only(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_public_text_only(item) for item in value)
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def public_projection_bytes(sealed_bytes: bytes) -> bytes:
    """Privacy-reduced projection, sealed over its own final bytes.

    It keeps digests, gate ids, bounded finding codes and timestamps. Artifact locations,
    exclusion text, reviewer and signer identities and keys are not carried over.
    """
    body = verify_decision_bytes(sealed_bytes)["body"]
    graph = body["graph"]
    public_graph = None
    if graph is not None:
        root = graph["root"]
        public_graph = {
            "root_digest": root["root_digest"],
            "result_node_digests": root["result_node_digests"],
            "digest_class_roots": root["digest_class_roots"],
            "gate_commit_digests": root["gate_commit_digests"],
            "exclusion_digests": root["exclusion_digests"],
            "reviewer_reproduction_digest": root["reviewer_reproduction_digest"],
            "nodes": [{field: node[field] for field in _PUBLIC_NODE_FIELDS} for node in graph["nodes"]],
        }
    public_body = {
        "authority_scope": body["authority_scope"],
        "claim_boundary": body["claim_boundary"],
        "decided_at": body["decided_at"],
        "decision": body["decision"],
        "decision_digest": sha256_bytes(sealed_bytes),
        "decision_generation": body["decision_generation"],
        "exclusions": body["exclusions"],
        "findings": body["findings"],
        "graph": public_graph,
        "previous_decision_digest": body["previous_decision_digest"],
        "privacy": PUBLIC_PRIVACY,
        "producer": body["producer"],
        "requirements": body["requirements"],
        "revocation": body["revocation"],
        "source": {
            "commit": body["source"]["commit"],
            "tree_digest": body["source"]["tree_digest"],
            "tree_state": body["source"]["tree_state"],
        },
        "valid_until": body["valid_until"],
        "withheld_claims": body["withheld_claims"],
    }
    if not _public_text_only(public_body):
        raise _refuse("privacy_guard_tripped")
    document = {
        "body": public_body,
        "body_sha256": sha256_document(public_body),
        "protocol": PUBLIC_PROTOCOL,
    }
    return canonical_json_bytes(document) + b"\n"


def check_exclusive_target(
    path: os.PathLike[str] | str,
    *,
    protected_paths: tuple[os.PathLike[str] | str, ...] = (),
) -> str:
    """Resolve an output path that does not exist and overlaps no protected input."""
    try:
        absolute = os.path.abspath(os.fspath(path))
        parent = os.path.realpath(os.path.dirname(absolute))
    except (OSError, TypeError, ValueError):
        raise _refuse("output_parent_invalid") from None
    if os.path.lexists(absolute):
        raise _refuse("output_exists")
    if not os.path.isdir(parent):
        raise _refuse("output_parent_invalid")
    target = os.path.join(parent, os.path.basename(absolute))
    if os.path.lexists(target):
        raise _refuse("output_exists")
    for protected in protected_paths:
        try:
            real = os.path.realpath(os.fspath(protected))
        except (OSError, TypeError, ValueError):
            continue
        if _is_within(target, real):
            raise _refuse("output_overlaps_input")
    return target


def write_exclusive(
    path: os.PathLike[str] | str,
    content: bytes,
    *,
    protected_paths: tuple[os.PathLike[str] | str, ...] = (),
) -> None:
    """Create a new file with exactly ``content``; never overwrite or follow a symlink."""
    target = check_exclusive_target(path, protected_paths=protected_paths)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(target, flags, 0o644)
    except FileExistsError:
        raise _refuse("output_exists") from None
    except OSError:
        raise _refuse("output_write_failed") from None
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except OSError:
        raise _refuse("output_write_failed") from None
    finally:
        os.close(descriptor)


__all__ = [
    "CANDIDATE_PROTOCOL",
    "DECISION_PROTOCOL",
    "DIRECT_PREREQUISITES",
    "DecisionResult",
    "PUBLIC_PROTOCOL",
    "RESULT_KINDS",
    "ReleaseDecisionRefused",
    "TRUST_REGISTRY_PROTOCOL",
    "append_revocation",
    "check_exclusive_target",
    "exclusion_digest",
    "node_digest",
    "produce_release_decision",
    "public_projection_bytes",
    "read_ledger",
    "signed_statement",
    "source_tree_digest",
    "verifier_policy",
    "verifier_policy_digest",
    "verify_decision_bytes",
    "write_exclusive",
]
