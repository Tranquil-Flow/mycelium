# SPDX-License-Identifier: AGPL-3.0-only
"""Append-only decision generations, sealing, the public projection and the CLI.

All evidence is the labelled synthetic test fixture world; nothing here evaluates the real
repository or claims a release state.
"""

from __future__ import annotations

import copy
import getpass
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import mycelium_a15_release_decision as producer
from mycelium_a15_release_decision import (
    PUBLIC_PROTOCOL,
    SCOPE_PRODUCTION,
    ReleaseDecisionRefused,
    append_revocation,
    public_projection_bytes,
    read_ledger,
    verify_decision_bytes,
    write_exclusive,
)
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from scripts.a15_release_decision import main
from tests.a15_release_decision.synthetic_world import NOW, SyntheticWorld

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "a15_release_decision.py"
WEBKIT = "digest_class:browser#engine:webkit"


def ledger_names(world: SyntheticWorld) -> list[str]:
    return sorted(path.name for path in world.ledger.iterdir())


def seal(document: dict[str, Any]) -> bytes:
    document["body_sha256"] = sha256_bytes(canonical_json_bytes(document["body"]))
    return canonical_json_bytes(document) + b"\n"


def refusal(callable_: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(ReleaseDecisionRefused) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# -- review finding 9: sealing ----------------------------------------------------------


def test_sealed_digest_is_computed_over_the_final_ledger_bytes(world: SyntheticWorld) -> None:
    result = world.built().produce()
    entry = world.ledger / "generation-000001.json"
    content = entry.read_bytes()

    assert result.ledger_entry_path == entry
    assert content == result.sealed_bytes
    assert sha256_bytes(content) == result.decision_digest
    document = verify_decision_bytes(content)
    assert content == canonical_json_bytes(document) + b"\n"
    assert document["body_sha256"] == sha256_bytes(canonical_json_bytes(document["body"]))


def test_anything_appended_after_sealing_breaks_the_seal(world: SyntheticWorld) -> None:
    result = world.built().produce()
    document = copy.deepcopy(result.document)
    document["candidate"] = json.loads(world.candidate_path.read_bytes())
    assert refusal(verify_decision_bytes, canonical_json_bytes(document) + b"\n") == (
        "decision_shape_invalid"
    )
    document = copy.deepcopy(result.document)
    document["body"]["withheld_claims"] = ["edited"]
    assert refusal(verify_decision_bytes, canonical_json_bytes(document) + b"\n") == (
        "decision_seal_invalid"
    )
    assert refusal(verify_decision_bytes, result.sealed_bytes + b" ") == "decision_not_canonical"


def test_handwritten_ready_document_does_not_verify(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate.update(reviewer_reproduction=None)
    )
    result = world.built().produce()
    assert result.decision == "not_ready"
    forged = copy.deepcopy(result.document)
    forged["body"]["decision"] = "ready"
    assert refusal(verify_decision_bytes, seal(forged)) == "decision_structure_invalid"
    forged["body"]["findings"] = []
    assert refusal(verify_decision_bytes, seal(forged)) == "decision_structure_invalid"


# -- review finding 9: privacy-reduced projection ---------------------------------------


def private_needles(world: SyntheticWorld) -> list[str]:
    return [
        str(world.base),
        world.base.name,
        getpass.getuser(),
        "synthetic-fixture-exclusion-reviewer",
        "synthetic-fixture-verifier",
        "synthetic-fixture-owner",
        "synthetic test fixture exclusion",
        world.verifier.verification_key_digest,
        world.owner.verification_key_digest,
        "Errno",
        "No such file",
    ]


def test_public_projection_is_privacy_reduced(world: SyntheticWorld) -> None:
    world.specs["digest_class:browser"]["cases"][2]["outcome"] = "blocked"
    world.exclusions.append(world.make_exclusion([WEBKIT]))
    result = world.built().produce()
    assert result.decision == "ready", result.findings

    public = public_projection_bytes(result.sealed_bytes)
    text = public.decode("ascii")
    for needle in [*private_needles(world), "receipt.json", "gates/", "/", "\\"]:
        assert needle not in text, needle
    document = json.loads(public)
    assert document["protocol"] == PUBLIC_PROTOCOL
    assert document["body_sha256"] == sha256_bytes(canonical_json_bytes(document["body"]))
    assert public == canonical_json_bytes(document) + b"\n"
    body = document["body"]
    assert body["decision"] == "ready"
    assert body["decision_digest"] == result.decision_digest
    assert body["withheld_claims"] == [WEBKIT]
    assert sorted(body["graph"]["result_node_digests"]) == sorted(
        f"A{index}" for index in range(2, 16)
    )
    assert all("artifact_reference" not in node for node in body["graph"]["nodes"])


def test_unreadable_inputs_leak_no_path_or_error_text(
    world: SyntheticWorld, tmp_path: Path
) -> None:
    world.build()
    world.receipt_path("gate_result:A3").unlink()
    (world.receipt_directory("digest_class:audit") / "audit.json").unlink()
    result = world.produce(
        evidence_roots={**world.roots, "absent": tmp_path / "does-not-exist"},
        trust_registry_path=tmp_path / "no-such-registry.json",
    )
    assert result.decision == "not_ready"
    assert "trust_registry_missing" in result.findings
    sealed = result.sealed_bytes.decode("ascii")
    public = public_projection_bytes(result.sealed_bytes).decode("ascii")
    for needle in private_needles(world):
        assert needle not in sealed, needle
        assert needle not in public, needle
    assert "/" not in public


def test_sealed_ready_decision_holds_no_absolute_path_or_identity(world: SyntheticWorld) -> None:
    result = world.built().produce()
    sealed = result.sealed_bytes.decode("ascii")
    for needle in private_needles(world):
        assert needle not in sealed, needle
    assert "/Users/" not in sealed and "/var/" not in sealed


# -- review finding 9: exclusive output -------------------------------------------------


def test_write_exclusive_creates_once_and_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "public.json"
    write_exclusive(target, b"first\n")
    assert refusal(write_exclusive, target, b"second\n") == "output_exists"
    assert target.read_bytes() == b"first\n"


def test_write_exclusive_refuses_symlinks_inputs_and_missing_parents(tmp_path: Path) -> None:
    protected = tmp_path / "input.json"
    protected.write_bytes(b"input\n")
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "elsewhere.json")
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(tmp_path, target_is_directory=True)

    assert refusal(write_exclusive, dangling, b"x") == "output_exists"
    assert not (tmp_path / "elsewhere.json").exists()
    assert refusal(write_exclusive, tmp_path / "missing" / "out.json", b"x") == (
        "output_parent_invalid"
    )
    assert refusal(
        write_exclusive,
        alias_directory / "fresh.json",
        b"x",
        protected_paths=(tmp_path,),
    ) == "output_overlaps_input"
    assert protected.read_bytes() == b"input\n"


# -- review finding 10: append-only generations and revocation --------------------------


def test_expired_evidence_appends_a_non_ready_generation_and_keeps_history(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    first_bytes = first.ledger_entry_path.read_bytes()
    second = world.produce(now=NOW + timedelta(hours=25))

    assert first.decision == "ready" and first.decision_generation == 1
    assert second.decision == "not_ready" and second.decision_generation == 2
    assert "mandatory_evidence_expired:gate_result:A12" in second.findings
    body = second.document["body"]
    assert body["previous_decision_digest"] == first.decision_digest
    assert body["revocation"] == {
        "kind": "automatic",
        "reason": "derived_not_ready",
        "revoked_decision_digest": first.decision_digest,
        "revoked_generation": 1,
    }
    assert ledger_names(world) == ["generation-000001.json", "generation-000002.json"]
    assert first.ledger_entry_path.read_bytes() == first_bytes
    assert [entry.decision for entry in read_ledger(world.ledger)] == ["ready", "not_ready"]
    assert second.document["body"]["graph"] is None


def test_every_evaluation_appends_and_links_to_the_previous_bytes(world: SyntheticWorld) -> None:
    first = world.built().produce()
    second = world.produce(now=NOW + timedelta(minutes=1))
    assert second.decision == "ready" and second.decision_generation == 2
    assert first.document["body"]["previous_decision_digest"] is None
    assert second.document["body"]["previous_decision_digest"] == first.decision_digest
    assert second.document["body"]["revocation"] is None
    assert second.document["body"]["graph"]["root"]["decision_generation"] == 2


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        (b"{not json", "candidate_not_canonical_json"),
        (b'{"protocol": "spaced"}', "candidate_not_canonical_json"),
        (b"[]", "candidate_shape_invalid"),
        (None, "candidate_missing"),
    ],
)
def test_invalid_input_yields_a_new_non_ready_generation(
    world: SyntheticWorld, content: bytes | None, finding: str
) -> None:
    ready = world.built().produce()
    if content is None:
        world.candidate_path.unlink()
    else:
        world.candidate_path.write_bytes(content)
    result = world.produce(now=NOW + timedelta(minutes=1))
    assert result.decision == "not_ready" and result.decision_generation == 2
    assert finding in result.findings
    assert result.document["body"]["revocation"]["revoked_generation"] == 1
    assert read_ledger(world.ledger)[0].sealed_bytes == ready.sealed_bytes


def test_edited_history_is_refused_and_nothing_is_appended(world: SyntheticWorld) -> None:
    world.candidate_mutators.append(
        lambda candidate: candidate.update(reviewer_reproduction=None)
    )
    first = world.built().produce()
    assert first.decision == "not_ready"
    forged = copy.deepcopy(first.document)
    forged["body"]["decision"] = "ready"
    first.ledger_entry_path.chmod(0o644)
    first.ledger_entry_path.write_bytes(canonical_json_bytes(forged) + b"\n")

    assert refusal(world.produce, now=NOW + timedelta(minutes=1)) == "decision_history_tampered"
    assert ledger_names(world) == ["generation-000001.json"]


def test_resealed_history_edit_breaks_the_chain(world: SyntheticWorld) -> None:
    first = world.built().produce()
    world.produce(now=NOW + timedelta(minutes=1))
    edited = copy.deepcopy(first.document)
    edited["body"]["decided_at"] = "2026-10-11T11:00:00Z"
    edited["body"]["graph"]["nodes"][-1]["executed_at"] = "2026-10-11T11:00:00Z"
    first.ledger_entry_path.chmod(0o644)
    first.ledger_entry_path.write_bytes(seal(edited))

    assert refusal(world.produce, now=NOW + timedelta(minutes=2)) == "decision_history_tampered"
    assert refusal(read_ledger, world.ledger) == "decision_history_tampered"
    assert len(ledger_names(world)) == 2


def test_generation_gap_and_foreign_files_are_refused(world: SyntheticWorld) -> None:
    first = world.built().produce()
    (world.ledger / "notes.txt").write_bytes(b"synthetic-test-fixture\n")
    assert refusal(world.produce, now=NOW + timedelta(minutes=1)) == "decision_history_tampered"
    (world.ledger / "notes.txt").unlink()
    first.ledger_entry_path.rename(world.ledger / "generation-000002.json")
    assert refusal(world.produce, now=NOW + timedelta(minutes=1)) == "decision_history_tampered"


def test_ledger_must_be_an_existing_real_directory(world: SyntheticWorld, tmp_path: Path) -> None:
    world.build()
    alias = tmp_path / "ledger-alias"
    alias.symlink_to(world.ledger, target_is_directory=True)
    assert refusal(world.produce, ledger_dir=alias) == "ledger_dir_invalid"
    assert refusal(world.produce, ledger_dir=tmp_path / "no-ledger") == "ledger_dir_invalid"
    assert ledger_names(world) == []


def test_clock_regression_is_refused(world: SyntheticWorld) -> None:
    world.built().produce()
    assert refusal(world.produce, now=NOW - timedelta(hours=1)) == "clock_regression"
    assert ledger_names(world) == ["generation-000001.json"]


def test_explicit_revocation_appends_and_blocks_the_revoked_candidate(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    first_bytes = first.ledger_entry_path.read_bytes()
    revoked = world.revoke("critical_defect", first, now=NOW + timedelta(minutes=1))
    again = world.produce(now=NOW + timedelta(minutes=2))

    assert revoked.decision == "not_ready" and revoked.decision_generation == 2
    assert revoked.findings == ("release_revoked:critical_defect",)
    graph = first.document["body"]["graph"]
    assert revoked.document["body"]["revocation"] == {
        "kind": "explicit",
        "reason": "critical_defect",
        "revoked_candidate_digest": first.document["body"]["candidate_digest"],
        "revoked_decision_digest": first.decision_digest,
        "revoked_generation": 1,
        "revoked_key_digests": [],
        "revoked_result_digests": sorted(
            [node["artifact_digest"] for node in graph["nodes"] if node["gate_id"] != "A15"]
            + list(graph["root"]["digest_class_roots"].values())
            + [graph["root"]["reviewer_reproduction_digest"]]
        ),
        "revoked_source_commit": world.commit,
    }
    assert revoked.document["body"]["candidate_digest"] == first.document["body"]["candidate_digest"]
    assert again.decision == "not_ready" and again.decision_generation == 3
    assert "candidate_revoked" in again.findings
    assert first.ledger_entry_path.read_bytes() == first_bytes


def test_compromised_key_revocation_blocks_the_key_and_the_revoked_decision(
    world: SyntheticWorld,
) -> None:
    first = world.built().produce()
    revoked = world.revoke(
        "compromised_key",
        first,
        now=NOW + timedelta(minutes=1),
        compromised_key_digests=[world.verifier.verification_key_digest],
        trust_registry_path=world.registry_path,
    )
    recorded = revoked.document["body"]["revocation"]
    assert recorded["revoked_key_digests"] == [world.verifier.verification_key_digest]
    assert recorded["revoked_source_commit"] == world.commit
    assert len(recorded["revoked_result_digests"]) == 13 + 8 + 1
    world.specs["gate_result:A14"]["fields"]["runtime_digest"] = sha256_bytes(b"synthetic-new")
    result = world.built().produce(now=NOW + timedelta(minutes=2))
    assert result.decision == "not_ready"
    assert "trust_registry_key_revoked" in result.findings
    assert "source_revoked" in result.findings
    assert "candidate_revoked" not in result.findings


def test_revocation_can_name_an_older_ready_generation(world: SyntheticWorld) -> None:
    first = world.built().produce()
    expired = world.produce(now=NOW + timedelta(hours=25))
    assert expired.decision == "not_ready"
    revoked = world.revoke("invalid_evidence", first, now=NOW + timedelta(hours=26))
    recorded = revoked.document["body"]["revocation"]
    assert recorded["revoked_generation"] == 1
    assert recorded["revoked_decision_digest"] == first.decision_digest
    assert recorded["revoked_source_commit"] == world.commit
    assert revoked.document["body"]["previous_decision_digest"] == expired.decision_digest


def test_key_digests_are_only_accepted_for_a_compromised_key(world: SyntheticWorld) -> None:
    first = world.built().produce()
    for arguments, code in [
        ({"reason": "critical_defect", "compromised_key_digests": ["sha256:" + "1" * 64]},
         "revocation_key_digests_unexpected"),
        ({"reason": "compromised_key", "compromised_key_digests": ["not-a-digest"]},
         "revocation_key_digests_invalid"),
    ]:
        assert refusal(
            append_revocation,
            ledger_dir=world.ledger,
            now=NOW + timedelta(minutes=1),
            expected_ledger_head=first.decision_digest,
            revoked_decision_digest=first.decision_digest,
            **arguments,
        ) == code
    assert ledger_names(world) == ["generation-000001.json"]


def test_revocation_reason_is_an_allowlist_and_needs_a_generation(world: SyntheticWorld) -> None:
    assert refusal(
        append_revocation,
        ledger_dir=world.ledger,
        reason="critical_defect",
        now=NOW,
        expected_ledger_head=None,
        revoked_decision_digest="sha256:" + "0" * 64,
    ) == "nothing_to_revoke"
    first = world.built().produce()
    assert refusal(
        append_revocation,
        ledger_dir=world.ledger,
        reason="operator says so",
        now=NOW,
        expected_ledger_head=first.decision_digest,
        revoked_decision_digest=first.decision_digest,
    ) == "revocation_reason_invalid"
    assert ledger_names(world) == ["generation-000001.json"]


def test_synthetic_and_production_generations_never_share_a_ledger(
    world: SyntheticWorld,
    tmp_path_factory: pytest.TempPathFactory,
    template: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = world.built().produce()
    production = SyntheticWorld(
        tmp_path_factory.mktemp("production-class"), template, registry_class=SCOPE_PRODUCTION
    )
    production.build()
    # Test-only: recognise this throwaway production-labelled registry so the production
    # branch can be exercised. The source allowlist itself stays empty.
    monkeypatch.setattr(
        producer, "PRODUCTION_REGISTRY_PINS", frozenset({production.registry_digest})
    )
    assert refusal(
        production.produce,
        ledger_dir=world.ledger,
        now=NOW + timedelta(minutes=1),
        expected_ledger_head=first.decision_digest,
    ) == "ledger_scope_mismatch"
    assert ledger_names(world) == ["generation-000001.json"]


# -- CLI --------------------------------------------------------------------------------


def cli_arguments(world: SyntheticWorld, public_out: Path, *extra: str) -> list[str]:
    return [
        "decide",
        "--candidate",
        str(world.candidate_path),
        "--repo-root",
        str(world.repo),
        "--evidence-root",
        f"gates={world.roots['gates']}",
        "--evidence-root",
        f"matrices={world.roots['matrices']}",
        "--trust-registry",
        str(world.registry_path),
        "--trust-registry-sha256",
        world.registry_digest,
        "--ledger-dir",
        str(world.ledger),
        "--expect-head",
        world.head or "none",
        "--public-out",
        str(public_out),
        *extra,
    ]


def clock(now: datetime = NOW) -> Any:
    return lambda: now


def test_cli_writes_sealed_outputs_and_reports_their_digests(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    public_out = tmp_path / "public.json"
    code = main(cli_arguments(world, public_out, "--synthetic-self-test"), clock=clock())
    captured = capsys.readouterr()

    # Exit 4, not 0: a synthetic self-test ready is never production readiness.
    assert code == 4, captured.err
    report = json.loads(captured.out)
    entry = world.ledger / "generation-000001.json"
    assert report == {
        "authority_scope": "synthetic_test_fixture",
        "decision": "ready",
        "decision_digest": sha256_bytes(entry.read_bytes()),
        "decision_generation": 1,
        "public_digest": sha256_bytes(public_out.read_bytes()),
    }
    assert public_out.read_bytes() == public_projection_bytes(entry.read_bytes())
    verify_decision_bytes(entry.read_bytes())
    assert str(tmp_path) not in captured.out + captured.err


def test_cli_exits_non_zero_for_a_derived_not_ready_generation(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    public_out = tmp_path / "public.json"
    code = main(cli_arguments(world, public_out), clock=clock())
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["decision"] == "not_ready" and report["decision_generation"] == 1
    public = json.loads(public_out.read_bytes())
    assert "synthetic_trust_registry_not_admitted" in public["body"]["findings"]


@pytest.mark.parametrize("target", ["existing", "candidate", "registry", "dangling", "ledger"])
def test_cli_refuses_to_overwrite_inputs_or_existing_outputs(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    world.build()
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"synthetic-test-fixture: keep\n")
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "elsewhere.json")
    public_out = {
        "existing": existing,
        "candidate": world.candidate_path,
        "registry": world.registry_path,
        "dangling": dangling,
        "ledger": world.ledger / "generation-000001.json",
    }[target]
    before = {
        path: path.read_bytes() for path in (existing, world.candidate_path, world.registry_path)
    }

    code = main(cli_arguments(world, public_out, "--synthetic-self-test"), clock=clock())
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["refused"] in {"output_exists", "output_overlaps_input"}
    assert str(tmp_path) not in captured.err
    assert ledger_names(world) == []
    assert not (tmp_path / "elsewhere.json").exists()
    assert {path: path.read_bytes() for path in before} == before


def test_cli_refusal_carries_only_a_bounded_code(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    arguments = cli_arguments(world, tmp_path / "public.json", "--synthetic-self-test")
    arguments[arguments.index("--ledger-dir") + 1] = str(tmp_path / "no-ledger")
    code = main(arguments, clock=clock())
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.err) == {"refused": "ledger_dir_invalid"}
    assert not (tmp_path / "public.json").exists()


def test_cli_rejects_a_malformed_evidence_root_argument(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.build()
    arguments = cli_arguments(world, tmp_path / "public.json", "--synthetic-self-test")
    arguments[arguments.index("--evidence-root") + 1] = str(world.roots["gates"])
    code = main(arguments, clock=clock())
    assert code == 2
    assert json.loads(capsys.readouterr().err) == {"refused": "evidence_root_argument_invalid"}
    assert ledger_names(world) == []


def test_cli_revoke_appends_an_explicit_revocation(
    world: SyntheticWorld, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.built().produce()
    public_out = tmp_path / "revocation-public.json"
    code = main(
        [
            "revoke",
            "--ledger-dir",
            str(world.ledger),
            "--expect-head",
            world.head,
            "--revoke-decision-digest",
            world.head,
            "--reason",
            "compromised_package",
            "--public-out",
            str(public_out),
        ],
        clock=clock(NOW + timedelta(minutes=1)),
    )
    report = json.loads(capsys.readouterr().out)

    assert code == 1
    assert report["decision"] == "not_ready" and report["decision_generation"] == 2
    public = json.loads(public_out.read_bytes())
    assert public["body"]["findings"] == ["release_revoked:compromised_package"]
    assert public["body"]["revocation"]["kind"] == "explicit"


def test_cli_process_refuses_without_a_traceback(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(SCRIPT),
            "revoke",
            "--ledger-dir",
            str(tmp_path / "no-ledger"),
            "--expect-head",
            "none",
            "--revoke-decision-digest",
            "sha256:" + "0" * 64,
            "--reason",
            "critical_defect",
            "--public-out",
            str(tmp_path / "public.json"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert json.loads(completed.stderr) == {"refused": "ledger_dir_invalid"}
    assert not (tmp_path / "public.json").exists()
