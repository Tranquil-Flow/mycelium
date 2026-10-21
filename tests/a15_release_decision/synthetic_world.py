# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic A15 release world for the release-decision producer tests.

Everything built here is a labelled synthetic test fixture: a temporary Git checkout,
ephemeral process-local keys, a ``synthetic_test_fixture`` trust registry and receipts
whose evidence manifests carry the synthetic evidence class. None of it is live or
physical evidence and the producer refuses it under production trust.

Digests, signed statements, the tree-digest representation and the verifier-policy digests
are recomputed here from first principles (``hashlib`` and ``json`` plus a literal policy
table), not with the producer's helpers. If the producer changed what a digest or a
signature covers, these fixtures would stop verifying instead of following it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mycelium_a15_release_decision import (
    BROWSER_ENGINE_CASES,
    CANDIDATE_PROTOCOL,
    CLOSURE_GATES,
    CONTRACT_MANIFEST_EVIDENCE_NAME,
    CONTRACT_MANIFEST_PATH,
    DIGEST_CLASS_KINDS,
    DIGEST_CLASSES,
    DIRECT_PREREQUISITES,
    EXCLUSION_SCOPE,
    INPUT_GATES,
    NODE_PROTOCOL,
    RECEIPT_PROTOCOL,
    REVIEW_FINDINGS_CASE,
    REVIEWER_REQUIRED_STEPS,
    REVIEWER_REQUIREMENT,
    ROLE_EXCLUSION_SIGNER,
    ROLE_INDEPENDENT_REVIEWER,
    ROLE_RESULT_VERIFIER_PREFIX,
    SBOM_EVIDENCE_NAME,
    SCOPE_SYNTHETIC,
    TRUST_REGISTRY_PROTOCOL,
    append_revocation,
    produce_release_decision,
)
from mycelium_m22_sbom import build_sbom
from mycelium_qualification.evidence import (
    build_evidence_manifest,
    canonical_json_bytes,
    sha256_bytes,
)
from mycelium_qualification.signing import Ed25519EvidenceSigner, generate_ed25519_signer
from tests.release_bundle.conftest import MANIFEST_FILENAME, write_synthetic_bundle

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
EXECUTED_AT = "2026-10-11T09:00:00Z"
FRESH_UNTIL = "2026-10-12T09:00:00Z"
AUTHORITY_GENERATION = 7
SYNTHETIC_LABEL = "synthetic-test-fixture"
JUNIT_PASSING = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<testsuites><testsuite name="synthetic-test-fixture" tests="2" failures="0" '
    b'errors="0" skipped="0">'
    b'<testcase classname="synthetic.fixture" name="first"/>'
    b'<testcase classname="synthetic.fixture" name="second"/>'
    b"</testsuite></testsuites>\n"
)




def independent_canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def independent_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(independent_canonical(value)).hexdigest()


def independent_node_digest(node: dict[str, Any]) -> str:
    return independent_digest({key: item for key, item in node.items() if key != "node_digest"})


def independent_exclusion_digest(exclusion: dict[str, Any]) -> str:
    return independent_digest(
        {
            key: item
            for key, item in exclusion.items()
            if key not in {"exclusion_digest", "signature"}
        }
    )


def independent_statement(document: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in document.items() if key != "signature"}


def independent_tree_digest(object_format: str, tree_id: str) -> str:
    return "sha256:" + hashlib.sha256(f"git-tree:{object_format}:{tree_id}".encode()).hexdigest()


ATTESTATION = "signer_attestation_with_hashed_evidence"
# Literal copy of the verifier policy each kind must have. Deliberately not derived from
# the producer's RESULT_KINDS: a silent policy change must break these fixtures.
EXPECTED_POLICIES: dict[str, tuple[str, list[str], list[str], str]] = {
    "physical_gate": ("gate_result", ["clean_tree", "physical"], [], ATTESTATION),
    "junit_suite": ("digest_class:test", ["clean_tree"], [], "junit_xml_reports"),
    "audit_report": ("digest_class:audit", ["clean_tree"], [], ATTESTATION),
    "physical_evidence_bundle": (
        "digest_class:physical",
        ["physical"],
        [],
        "mycelium_release_bundle.verifier.verify_bundle",
    ),
    "browser_engine_matrix": (
        "digest_class:browser",
        ["browser_engine", "clean_tree"],
        ["engine:chromium", "engine:firefox", "engine:webkit"],
        ATTESTATION,
    ),
    "model_qualification": ("digest_class:model", ["physical"], [], ATTESTATION),
    "contract_manifest": (
        "digest_class:contract",
        ["clean_tree"],
        [],
        "source_contract_manifest_bytes",
    ),
    "package_manifest": (
        "digest_class:package",
        ["clean_tree", "platform_specific"],
        [],
        ATTESTATION,
    ),
    "sbom_document": ("digest_class:sbom", ["clean_tree"], [], "mycelium_m22_sbom.validate_sbom"),
    "external_reviewer_reproduction": (
        "external_reviewer_reproduction",
        ["clean_tree", "independently_reviewed", "physical"],
        sorted(
            [
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
                "review_findings_resolved",
            ]
        ),
        ATTESTATION,
    ),
    "release_closure": ("derived_root", ["clean_tree"], [], "derived_by_producer"),
}


def independent_policy_digest(result_kind: str) -> str:
    requirement, properties, cases, verifier = EXPECTED_POLICIES[result_kind]
    return independent_digest(
        {
            "protocol": "mycelium.a15_verifier_policy.v1",
            "policy_version": 1,
            "result_kind": result_kind,
            "requirement": requirement,
            "required_provenance": "live",
            "required_properties": properties,
            "required_cases": cases,
            "artifact_verifier": verifier,
        }
    )


def git(repo: Path, *args: str) -> str:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Synthetic Fixture",
        "GIT_AUTHOR_EMAIL": "synthetic@fixture.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Fixture",
        "GIT_COMMITTER_EMAIL": "synthetic@fixture.invalid",
        "GIT_AUTHOR_DATE": "2026-10-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-01T00:00:00+00:00",
    }
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return completed.stdout.strip()


def commit_file(repo: Path, relative: str, content: bytes, message: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(repo, "add", "--", relative)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def build_template_repo(repo: Path, *, object_format: str = "sha1") -> dict[str, str]:
    """A real Git history with one distinct commit per closure gate, in prerequisite order."""
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main", f"--object-format={object_format}")
    commit_file(
        repo,
        CONTRACT_MANIFEST_PATH,
        canonical_json_bytes({"fixture": SYNTHETIC_LABEL, "subject": "contract manifest"}),
        "synthetic fixture: A2 external prerequisite",
    )
    gate_commits = {
        gate_id: commit_file(
            repo,
            f"gates/{gate_id}.txt",
            f"{SYNTHETIC_LABEL}: {gate_id}\n".encode("ascii"),
            f"synthetic fixture: {gate_id}",
        )
        for gate_id in CLOSURE_GATES
    }
    commit_file(
        repo, "candidate.txt", f"{SYNTHETIC_LABEL}: candidate\n".encode("ascii"), "candidate"
    )
    return gate_commits


def label_digest(label: str) -> str:
    return sha256_bytes(f"{SYNTHETIC_LABEL}:{label}".encode("ascii"))


def fixture_file(subject: str) -> bytes:
    return canonical_json_bytes(
        {"evidence_class": "synthetic_test_fixture", "subject": f"{SYNTHETIC_LABEL}:{subject}"}
    )


def passing(case_ids: tuple[str, ...] | list[str]) -> list[dict[str, str]]:
    return [{"case_id": case_id, "outcome": "pass"} for case_id in sorted(case_ids)]


class SyntheticWorld:
    def __init__(
        self,
        base: Path,
        template: tuple[Path, dict[str, str]],
        *,
        registry_class: str = SCOPE_SYNTHETIC,
    ) -> None:
        self.base = base
        self.repo = base / "checkout"
        shutil.copytree(template[0], self.repo, symlinks=True)
        self.gate_commits = dict(template[1])
        self.roots: dict[str, Path] = {
            "gates": base / "evidence-gates",
            "matrices": base / "evidence-matrices",
        }
        for root in self.roots.values():
            root.mkdir()
        self.ledger = base / "ledger"
        self.ledger.mkdir()
        self.trust_dir = base / "trust"
        self.trust_dir.mkdir()
        self.registry_path = self.trust_dir / "trust-registry.json"
        self.candidate_path = base / "candidate.json"
        self.verifier = generate_ed25519_signer(endpoint_id="synthetic-fixture-verifier")
        self.reviewer = generate_ed25519_signer(endpoint_id="synthetic-fixture-reviewer")
        self.owner = generate_ed25519_signer(endpoint_id="synthetic-fixture-owner")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.tree_digest = independent_tree_digest(
            "sha1", git(self.repo, "rev-parse", "HEAD^{tree}")
        )
        self.contract_bytes = (self.repo / CONTRACT_MANIFEST_PATH).read_bytes()
        self.contract_digest = sha256_bytes(self.contract_bytes)
        self.registry = self.default_registry(registry_class)
        self.registry_digest = ""
        self.specs: dict[str, dict[str, Any]] = {}
        self.node_mutators: list[Callable[[dict[str, dict[str, Any]]], None]] = []
        self.candidate_mutators: list[Callable[[dict[str, Any]], None]] = []
        self.exclusions: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.references: dict[str, tuple[str, str]] = {}
        self.candidate: dict[str, Any] = {}
        self.exclusion_bindings: dict[str, Any] = {}
        # The ledger head this caller last saw; pinned on every produce call.
        self.head: str | None = None
        self.reviewer_binds_nodes = True
        self._default_specs()

    # -- trust -------------------------------------------------------------------------

    def default_registry(self, registry_class: str) -> dict[str, Any]:
        operator_kinds = sorted(
            kind
            for kind in EXPECTED_POLICIES
            if kind not in {"external_reviewer_reproduction", "release_closure"}
        )
        return {
            "protocol": TRUST_REGISTRY_PROTOCOL,
            "registry_class": registry_class,
            "registry_generation": 1,
            "issued_at": "2026-10-10T00:00:00Z",
            "expires_at": "2026-11-10T00:00:00Z",
            "current_authority_generation": AUTHORITY_GENERATION,
            "keys": [
                {
                    "key": self.verifier.public_key_record(),
                    "roles": [ROLE_RESULT_VERIFIER_PREFIX + kind for kind in operator_kinds],
                },
                {"key": self.reviewer.public_key_record(), "roles": [ROLE_INDEPENDENT_REVIEWER]},
                {"key": self.owner.public_key_record(), "roles": [ROLE_EXCLUSION_SIGNER]},
            ],
            "verifier_policy_digests": {
                kind: independent_policy_digest(kind) for kind in sorted(EXPECTED_POLICIES)
            },
        }

    def write_registry(self) -> None:
        content = canonical_json_bytes(self.registry)
        self.registry_path.write_bytes(content)
        self.registry_digest = sha256_bytes(content)

    # -- receipts ----------------------------------------------------------------------

    def _spec(
        self,
        requirement: str,
        *,
        root: str,
        directory: str,
        kind: str,
        subject: str,
        files: dict[str, bytes] | Callable[[Path], dict[str, bytes]],
        cases: list[dict[str, str]],
        signer: Ed25519EvidenceSigner,
    ) -> None:
        self.specs[requirement] = {
            "root": root,
            "directory": directory,
            "kind": kind,
            "subject": subject,
            "files": files,
            "cases": cases,
            "signer": signer,
            "evidence_class": "synthetic_test_fixture",
            "fields": {},
        }

    def _bundle_files(self, directory: Path) -> dict[str, bytes]:
        bundle = directory / "bundle"
        if bundle.exists():
            shutil.rmtree(bundle)
        write_synthetic_bundle(bundle)
        return {f"bundle/{MANIFEST_FILENAME}": (bundle / MANIFEST_FILENAME).read_bytes()}

    def sbom_bytes(self, *, revision: str | None = None, omit: str | None = None) -> bytes:
        components = [
            {
                "kind": kind,
                "name": f"{SYNTHETIC_LABEL}-{kind}",
                "version": None,
                "size_bytes": len(self.contract_bytes),
                "sha256": self.contract_digest if kind == "contract" else label_digest(kind),
            }
            for kind in ("contract", "node-lock", "python-lock", "rust-lock")
            if kind != omit
        ]
        return canonical_json_bytes(
            build_sbom(revision=revision or self.commit, components=components)
        )

    def _default_specs(self) -> None:
        for gate_id in INPUT_GATES:
            self._spec(
                f"gate_result:{gate_id}",
                root="gates",
                directory=gate_id,
                kind="physical_gate",
                subject=f"{gate_id}:executed_gate_result",
                files={"result.json": fixture_file(gate_id)},
                cases=passing(["synthetic_fixture_case"]),
                signer=self.verifier,
            )
        class_files: dict[str, Any] = {
            "test": {"junit.xml": JUNIT_PASSING},
            "audit": {"audit.json": fixture_file("audit")},
            "physical": self._bundle_files,
            "browser": {"browser.json": fixture_file("browser")},
            "model": {"model.json": fixture_file("model")},
            "contract": {CONTRACT_MANIFEST_EVIDENCE_NAME: self.contract_bytes},
            "package": {"package.json": fixture_file("package")},
            "sbom": {SBOM_EVIDENCE_NAME: self.sbom_bytes()},
        }
        class_cases = {
            "browser": passing(BROWSER_ENGINE_CASES),
            "package": passing(["platform:synthetic_fixture"]),
        }
        for digest_class in DIGEST_CLASSES:
            self._spec(
                f"digest_class:{digest_class}",
                root="matrices",
                directory=digest_class,
                kind=DIGEST_CLASS_KINDS[digest_class],
                subject=f"digest_class:{digest_class}",
                files=class_files[digest_class],
                cases=class_cases.get(digest_class, passing(["synthetic_fixture_case"])),
                signer=self.verifier,
            )
        self._spec(
            REVIEWER_REQUIREMENT,
            root="matrices",
            directory="reviewer",
            kind="external_reviewer_reproduction",
            subject=REVIEWER_REQUIREMENT,
            files={"reproduction.json": fixture_file("reviewer")},
            cases=passing([*REVIEWER_REQUIRED_STEPS, REVIEW_FINDINGS_CASE]),
            signer=self.reviewer,
        )

    def receipt_directory(self, requirement: str) -> Path:
        spec = self.specs[requirement]
        return self.roots[spec["root"]] / spec["directory"]

    def receipt_path(self, requirement: str) -> Path:
        return self.receipt_directory(requirement) / "receipt.json"

    def _write_receipt(self, requirement: str) -> None:
        spec = self.specs[requirement]
        directory = self.receipt_directory(requirement)
        directory.mkdir(parents=True, exist_ok=True)
        files = spec["files"](directory) if callable(spec["files"]) else dict(spec["files"])
        for relative, content in files.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        kind = spec["kind"]
        gate_id = requirement.split(":", 1)[1] if requirement.startswith("gate_result:") else None
        receipt: dict[str, Any] = {
            "protocol": RECEIPT_PROTOCOL,
            "result_kind": kind,
            "requirement_id": requirement,
            "subject": spec["subject"],
            "provenance": "live",
            "properties": EXPECTED_POLICIES[kind][1] if kind in EXPECTED_POLICIES else [],
            "outcome": "pass",
            "source_commit": self.commit,
            "source_tree_digest": self.tree_digest,
            "tree_state": "clean",
            "contract_digest": self.contract_digest,
            "model_digest": label_digest("model-revision"),
            "representation_digest": label_digest("serving-representation"),
            "runtime_digest": label_digest("runtime-build"),
            "environment_digest": label_digest("execution-environment"),
            "assignment_digest": label_digest("assignment"),
            "route_digest": label_digest("route"),
            "qualification_digest": label_digest("qualification"),
            "gate_commit": self.gate_commits.get(gate_id) if gate_id else None,
            "authority_generation": AUTHORITY_GENERATION,
            "verifier_policy_digest": (
                independent_policy_digest(kind)
                if kind in EXPECTED_POLICIES
                else label_digest("policy")
            ),
            "executed_at": EXECUTED_AT,
            "fresh_until": FRESH_UNTIL,
            "cases": spec["cases"],
            "bound_result_digests": [],
            "evidence_manifest": build_evidence_manifest(
                run_id=f"{SYNTHETIC_LABEL}:{requirement}",
                evidence_class=spec["evidence_class"],
                files=files,
            ),
        }
        receipt.update(spec["fields"])
        receipt["signature"] = (
            None if spec["signer"] is None else spec["signer"].sign(independent_statement(receipt))
        )
        content = canonical_json_bytes(receipt)
        self.receipt_path(requirement).write_bytes(content)
        self.receipts[requirement] = receipt
        self.references[requirement] = (
            f"{spec['root']}/{spec['directory']}/receipt.json",
            sha256_bytes(content),
        )

    # -- graph -------------------------------------------------------------------------

    def _nodes(self) -> list[dict[str, Any]]:
        by_gate: dict[str, dict[str, Any]] = {}
        for gate_id in INPUT_GATES:
            requirement = f"gate_result:{gate_id}"
            receipt = self.receipts[requirement]
            reference, digest = self.references[requirement]
            by_gate[gate_id] = {
                "protocol": NODE_PROTOCOL,
                "node_digest": "",
                "gate_id": gate_id,
                "result_kind": receipt["result_kind"],
                "outcome": "verified",
                "source_commit": receipt["source_commit"],
                "source_tree_digest": receipt["source_tree_digest"],
                "tree_state": receipt["tree_state"],
                "provenance": receipt["provenance"],
                "subject": receipt["subject"],
                "artifact_reference": reference,
                "artifact_digest": digest,
                "dependency_gate_ids": sorted(DIRECT_PREREQUISITES[gate_id]),
                "dependency_digests": [],
                "verifier_policy_digest": receipt["verifier_policy_digest"],
                "executed_at": receipt["executed_at"],
                "fresh_until": receipt["fresh_until"],
                "contract_digest": receipt["contract_digest"],
                "model_digest": receipt["model_digest"],
                "representation_digest": receipt["representation_digest"],
                "runtime_digest": receipt["runtime_digest"],
                "environment_digest": receipt["environment_digest"],
                "authority_generation": receipt["authority_generation"],
            }
        for mutate in self.node_mutators:
            mutate(by_gate)
        seal_nodes(by_gate)
        return list(by_gate.values())

    def make_exclusion(
        self,
        requirement_ids: list[str],
        *,
        signer: Ed25519EvidenceSigner | None = None,
        sign: bool = True,
        **fields: Any,
    ) -> dict[str, Any]:
        signer = signer or self.owner
        exclusion: dict[str, Any] = {
            "exclusion_digest": "",
            "requirement_ids": list(requirement_ids),
            "scope": EXCLUSION_SCOPE,
            "reason": "synthetic test fixture exclusion",
            "risk": "synthetic fixture risk",
            "compensating_behavior": "affected claim withheld",
            "reviewer_identity": "synthetic-fixture-exclusion-reviewer",
            "signer_key_id": signer.verification_key_digest,
            "issued_at": "2026-10-11T08:00:00Z",
            "expires_at": "2026-10-18T08:00:00Z",
            "signature": None,
            # Bound to this source, authority generation and the affected browser result.
            "source_commit": self.commit,
            "authority_generation": AUTHORITY_GENERATION,
            "bound_result_digest": self.browser_result_digest(),
        }
        exclusion.update(self.exclusion_bindings)
        exclusion.update(fields)
        exclusion["exclusion_digest"] = independent_exclusion_digest(exclusion)
        if sign:
            exclusion["signature"] = signer.sign(independent_statement(exclusion))
        return exclusion

    def browser_result_digest(self) -> str:
        """Digest of the browser receipt as currently specified (signing is deterministic)."""
        self._write_receipt("digest_class:browser")
        return self.references["digest_class:browser"][1]

    def build(self) -> None:
        self.write_registry()
        for requirement in self.specs:
            if requirement != REVIEWER_REQUIREMENT:
                self._write_receipt(requirement)
        nodes = self._nodes()
        if self.reviewer_binds_nodes:
            self.specs[REVIEWER_REQUIREMENT]["fields"]["bound_result_digests"] = sorted(
                [node["node_digest"] for node in nodes]
                + [
                    self.references[f"digest_class:{digest_class}"][1]
                    for digest_class in DIGEST_CLASSES
                ]
            )
        self._write_receipt(REVIEWER_REQUIREMENT)
        reviewer_reference, reviewer_digest = self.references[REVIEWER_REQUIREMENT]
        self.candidate = {
            "protocol": CANDIDATE_PROTOCOL,
            "source": {"commit": self.commit, "tree_digest": self.tree_digest},
            "gate_commits": dict(self.gate_commits),
            "nodes": nodes,
            "class_results": {
                digest_class: {
                    "result_kind": self.specs[f"digest_class:{digest_class}"]["kind"],
                    "artifact_reference": self.references[f"digest_class:{digest_class}"][0],
                    "artifact_digest": self.references[f"digest_class:{digest_class}"][1],
                }
                for digest_class in DIGEST_CLASSES
            },
            "reviewer_reproduction": {
                "artifact_reference": reviewer_reference,
                "artifact_digest": reviewer_digest,
            },
            "exclusions": list(self.exclusions),
        }
        for mutate in self.candidate_mutators:
            mutate(self.candidate)
        self.candidate_path.write_bytes(canonical_json_bytes(self.candidate))

    def produce(self, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "candidate_path": self.candidate_path,
            "repo_root": self.repo,
            "evidence_roots": self.roots,
            "trust_registry_path": self.registry_path,
            "trust_registry_sha256": self.registry_digest,
            "ledger_dir": self.ledger,
            "expected_ledger_head": self.head,
            "now": NOW,
            "allow_synthetic_trust": True,
        }
        arguments.update(overrides)
        result = produce_release_decision(**arguments)
        if os.path.realpath(arguments["ledger_dir"]) == os.path.realpath(self.ledger):
            self.head = result.decision_digest
        return result

    def revoke(self, reason: str, target: Any, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "ledger_dir": self.ledger,
            "reason": reason,
            "now": NOW,
            "expected_ledger_head": self.head,
            "revoked_decision_digest": target.decision_digest,
        }
        arguments.update(overrides)
        result = append_revocation(**arguments)
        self.head = result.decision_digest
        return result

    def built(self) -> "SyntheticWorld":
        self.build()
        return self


def seal_nodes(by_gate: dict[str, dict[str, Any]]) -> None:
    for gate_id in sorted(by_gate, key=lambda item: int(item[1:])):
        node = by_gate[gate_id]
        node["dependency_digests"] = [
            by_gate[dependency]["node_digest"]
            for dependency in node["dependency_gate_ids"]
            if dependency in by_gate
        ]
        node["node_digest"] = independent_node_digest(node)
