#!/usr/bin/env python3
"""A9 physical gate driver: v1 -> v2 membership migration and Windows int8 provider.

Subcommands (each writes canonical JSON receipts into a 0700 gate directory):

Seed host (macOS, the seed's own machine):
  seed-export     Copy the live seed database with the SQLite backup API, refuse
                  it unless it is bound to the seed key and swarm, initialise the
                  private migration store, and write one seed-signed profile
                  request per member.
  migrate         Execute the migration over the members' signed v2 profiles,
                  record the cutover (migration record) and remove the
                  migration-only reader.  ``--stop-after`` interrupts at a
                  checkpoint for the restart receipt.
  negatives       Deterministic negative battery on private copies of the gate
                  state (stale v1 after cutover, changed key, revoked member,
                  foreign request, forged request, substituted seed copy).
  summary         Collect every receipt into one A9 gate summary.

Member hosts (macOS laptop, Linux SB2, Windows):
  member-sign     Verify the seed-signed request against the member's own durable
                  membership state and sign the v2 profile with the existing
                  durable node key.  Platform and architecture are observed on
                  this host.

Windows host:
  windows-probe      Observe Windows x86_64 dispatch, bind the digest-pinned kernel
                     DLL and check its numerics against the NumPy reference.
  windows-benchmark  Frozen paired reference/candidate benchmark of the exact
                     Qwen2.5-0.5B int8 model; authors the
                     mycelium.runtime_efficiency_qualification.v1 decision.

Nothing here edits the live seed, restarts a node, or grants a role.  Every
receipt states its claim boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL = "mycelium.a9_gate_receipt.v1"
SPEC = ROOT / "docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md"
CONTRACT_MANIFEST = ROOT / "contracts/contract-manifest.v1.json"
ACCEPTANCE_INVENTORY = ROOT / "tests/a9_acceptance/inventory.v1.json"
WINDOWS_BACKEND_ID = "windows_native_provider"


class GateError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise GateError(f"refusing symlinked gate directory {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _write(path: Path, document: Any) -> None:
    _private_dir(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _receipt(kind: str, claim_boundary: str, **fields: Any) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "kind": kind,
        "recorded_at": time.time(),
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "claim_boundary": claim_boundary,
        **fields,
    }


def _source_identity() -> dict[str, Any]:
    head = None
    try:
        import subprocess

        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - an archive checkout has no git
        candidate = ROOT / "CANDIDATE_SHA.txt"
        head = candidate.read_text().strip() if candidate.exists() else None
    return {"source_commit": head}


# -- seed host ------------------------------------------------------------------


def _backup_sqlite(live: Path, copy: Path) -> None:
    if copy.exists():
        raise GateError(f"seed copy already exists: {copy}")
    source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    if os.name != "nt":
        copy.chmod(0o600)


def cmd_seed_export(args: argparse.Namespace) -> int:
    from mycelium_node.identity import load_node_signer
    from mycelium_platform_neutral.migration_executor import FileMembershipState
    from mycelium_platform_neutral.seed_migration import export_seed_baseline, profile_requests
    from mycelium_seed.state import SqliteSeedState

    gate = _private_dir(Path(args.gate_dir).resolve())
    seed_dir = _private_dir(gate / "seed-copy")
    copy = seed_dir / "state.sqlite3"
    _backup_sqlite(Path(args.seed_db).resolve(), copy)
    signer = load_node_signer(Path(args.seed_key).resolve())
    state = SqliteSeedState(copy)
    baseline = export_seed_baseline(
        state,
        software_build_digest=args.build_digest,
        swarm_id=args.swarm_id,
        seed_key_digest=signer.verification_key_digest,
    )
    store = FileMembershipState(gate / "private")
    store.initialize(baseline)
    requests = profile_requests(
        state,
        signer=signer,
        swarm_id=args.swarm_id,
        software_build_digest=args.build_digest,
        software_version=args.software_version,
    )
    request_dir = _private_dir(gate / "requests")
    for envelope in requests:
        _write(request_dir / f"{envelope['request']['node_id']}.json", envelope)
    earliest_lease = min(record["lease_expires_at"] for record in baseline.values())
    _write(
        gate / "01-seed-export.json",
        _receipt(
            "seed_export",
            "a private copy of the seed's durable v1 members; the live seed is not written",
            source=_source_identity(),
            seed_key_digest=signer.verification_key_digest,
            seed_copy_digest=_sha256_file(copy),
            member_count=len(baseline),
            members={
                node: {
                    "generation": record["generation"],
                    "revoked": record["revoked"],
                    "lease_expires_at": record["lease_expires_at"],
                    "peer_class": next(e["request"]["legacy_peer_class"] for e in requests if e["request"]["node_id"] == node),
                }
                for node, record in baseline.items()
            },
            sign_and_migrate_before=earliest_lease,
            seconds_remaining=earliest_lease - time.time(),
        ),
    )
    print(f"exported {len(baseline)} members; sign and migrate within {earliest_lease - time.time():.0f} s")
    return 0


def cmd_member_sign(args: argparse.Namespace) -> int:
    from mycelium_node.durable_membership import load_membership_state
    from mycelium_node.identity import load_node_signer
    from mycelium_platform_neutral.primitives import record_digest
    from mycelium_platform_neutral.seed_migration import observe_host_platform, sign_migration_profile

    data_dir = Path(args.data_dir).resolve()
    member_state = load_membership_state(data_dir)
    if member_state is None:
        raise GateError("this host has no durable membership state; it must re-enroll through an invite")
    key_file = Path(args.key_file).resolve() if args.key_file else data_dir / "identity" / "node.key"
    signer = load_node_signer(key_file)
    envelope = _read(Path(args.request))
    observed = observe_host_platform()
    signed = sign_migration_profile(
        envelope,
        signer=signer,
        member_state=member_state,
        new_incarnation=args.new_incarnation,
        issued_at=time.time(),
        observed_platform=observed,
    )
    output = Path(args.out)
    _write(output, signed)
    profile_digest = record_digest(signed["profile"], "migration_profile_noncanonical")
    print(json.dumps({"node_id": signed["profile"]["node_id"], "platform": observed, "profile_digest": profile_digest}))
    return 0


def _contract_digests() -> list[str]:
    return [_sha256_file(CONTRACT_MANIFEST), _sha256_file(SPEC)]


def cmd_migrate(args: argparse.Namespace) -> int:
    from mycelium_platform_neutral import migration
    from mycelium_platform_neutral.migration_executor import FileMembershipState, MigrationRequest

    gate = Path(args.gate_dir).resolve()
    store = FileMembershipState(gate / "private")
    profiles = tuple(_read(path) for path in sorted(Path(args.profiles).glob("*.json")))
    request = MigrationRequest(migration_id=args.migration_id, profiles=profiles)
    now = time.time()
    journal = gate / "private" / "journal.json"
    if args.stop_after:
        try:
            migration.execute_identity_preserving_migration(
                storage=store, request=request, now=now, stop_after=args.stop_after
            )
        except migration.MigrationUnavailable as stopped:
            _write(gate / f"02-interrupted-{args.stop_after}.json", _receipt(
                "migration_interrupted", "deliberate interruption for the restart receipt",
                checkpoint=args.stop_after, code=stopped.code))
            print(f"interrupted at {args.stop_after}")
            return 0
    if journal.exists():
        state = migration.restart_from_checkpoint(storage=store, request=request, now=now)
    else:
        state = {"checkpoint_before": None, "checkpoint": migration.execute_identity_preserving_migration(
            storage=store, request=request, now=now), "resumed": False}
    record = migration.record_cutover(
        storage=store,
        contract_digests=_contract_digests(),
        inventory_digest=_sha256_file(ACCEPTANCE_INVENTORY),
        qualification_digests=(),
        now=time.time(),
    )
    for operation in ("lease_renewal", "join_request"):
        try:
            migration.gate_v1_write(storage=store, operation=operation)
            raise GateError(f"v1 {operation} admitted after cutover")
        except migration.MembershipContractUpgradeRequired:
            pass
    removal = migration.remove_migration_only_reader(storage=store, now=time.time())
    roles = {
        node: migration.re_evaluate_roles(
            node_id=node, legacy_activation_eligible=True,
            roles=("probe_contributor", "artifact_source", "qualified_model_stage", "performance_ready_model_stage"),
        ).to_mapping()["roles"]
        for node in record["post_generations"]
    }
    parsed = migration.ContractMigrationRecord.from_mapping(removal["record"])
    _write(gate / "03-migration-record.private.json", removal["record"])
    _write(gate / "03-migration.json", _receipt(
        "migration_complete",
        "local executed migration over a seed copy: identity continuity, n+1 generations, recorded cutover and reader removal; no role is granted and the live seed is not cut over",
        source=_source_identity(),
        restart=state,
        status_projection=parsed.status_projection(),
        record_digest=parsed.digest,
        post_cutover_v1_write="membership_contract_upgrade_required",
        roles_after_migration=roles,
    ))
    print(json.dumps(parsed.status_projection()))
    return 0


def cmd_negatives(args: argparse.Namespace) -> int:
    from mycelium_platform_neutral import migration
    from mycelium_platform_neutral.migration_executor import FileMembershipState, MigrationRequest
    from mycelium_platform_neutral.seed_migration import export_seed_baseline, verify_profile_request
    from mycelium_qualification.signing import generate_ed25519_signer
    from mycelium_seed.state import SqliteSeedState

    gate = Path(args.gate_dir).resolve()
    work = _private_dir(gate / "negatives")
    results: dict[str, Any] = {}

    def expect(name: str, fn, *codes: str) -> None:
        try:
            fn()
        except Exception as error:  # noqa: BLE001 - recorded, then judged
            code = getattr(error, "code", str(error))
            results[name] = {"refused": True, "code": code, "pass": not codes or code in codes}
            return
        results[name] = {"refused": False, "pass": False}

    store = FileMembershipState(gate / "private")
    for operation in ("lease_renewal", "capability_change", "assignment_request", "readiness_input", "resume_request"):
        expect(f"stale_v1_{operation}_after_cutover",
               lambda op=operation: migration.gate_v1_write(storage=store, operation=op),
               "membership_contract_upgrade_required")
    expect("migration_reader_after_removal", lambda: migration.read_v1_transition_input(storage=store),
           "membership_contract_upgrade_required")
    expect("rollback_after_commit", lambda: migration.rollback_before_commit(storage=store, now=time.time()),
           "rollback_not_available")

    profiles = tuple(_read(path) for path in sorted(Path(args.profiles).glob("*.json")))
    seed_copy = SqliteSeedState(gate / "seed-copy" / "state.sqlite3")
    binding = seed_copy.identity_binding()
    baseline = export_seed_baseline(seed_copy, software_build_digest=args.build_digest,
                                    swarm_id=binding["swarm_id"], seed_key_digest=binding["seed_key_digest"])

    def fresh(name: str, mutate=None) -> FileMembershipState:
        root = work / name
        if root.exists():
            shutil.rmtree(root)
        members = json.loads(json.dumps(baseline))
        if mutate:
            mutate(members)
        fresh_store = FileMembershipState(root)
        fresh_store.initialize(members)
        return fresh_store

    def changed_key(members):
        for record in members.values():
            record["verification_key_digest"] = generate_ed25519_signer(endpoint_id="changed").verification_key_digest
            break

    def revoked(members):
        next(iter(members.values()))["revoked"] = True

    for name, mutate in (("changed_key_migration", changed_key), ("revoked_member_migration", revoked)):
        target = fresh(name, mutate)
        before = (target.root / "members.json").read_bytes()
        expect(name, lambda t=target: migration.execute_identity_preserving_migration(
            storage=t, request=MigrationRequest("negative", profiles), now=time.time()))
        results[name]["prior_database_preserved"] = (target.root / "members.json").read_bytes() == before
        results[name]["pass"] = results[name]["pass"] and results[name]["prior_database_preserved"]

    request = next(iter(sorted((gate / "requests").glob("*.json"))))
    envelope = _read(request)
    forger = generate_ed25519_signer(endpoint_id="forger")
    forged = {**envelope, "signature": forger.sign(envelope["request"]), "verification_key": forger.public_key_record()}
    member = {
        "node_id": envelope["request"]["node_id"], "swarm_id": envelope["request"]["swarm_id"],
        "incarnation": envelope["request"]["current_incarnation"],
        "membership_generation": envelope["request"]["membership_generation"] - 1,
        "seed_key_digest": binding["seed_key_digest"],
    }
    expect("forged_seed_request", lambda: verify_profile_request(forged, member_state=member),
           "migration_profile_request_not_seed_signed")
    expect("foreign_member_request", lambda: verify_profile_request(
        envelope, member_state={**member, "node_id": "someone-else"}),
        "migration_profile_request_not_for_this_member")
    expect("substituted_seed_copy", lambda: export_seed_baseline(
        seed_copy, software_build_digest=args.build_digest, swarm_id=binding["swarm_id"],
        seed_key_digest=forger.verification_key_digest), "seed_identity_binding_mismatch")
    passed = all(item["pass"] for item in results.values())
    _write(gate / "04-negatives.json", _receipt(
        "deterministic_negatives",
        "deterministic refusals on private copies of the executed gate state; the physical negatives on the live product path are separate",
        results=results, all_passed=passed))
    print(json.dumps({"all_passed": passed, "cases": len(results)}))
    return 0 if passed else 1


# -- windows host ------------------------------------------------------------------


def cmd_windows_probe(args: argparse.Namespace) -> int:
    import numpy as np

    from mycelium_platform_neutral.native_int8 import NativeInt8Library
    from mycelium_platform_neutral.windows_provider import WindowsProviderUnavailable, detect_windows_dispatch

    try:
        dispatch = detect_windows_dispatch()
    except WindowsProviderUnavailable as refused:
        if not args.conformance_host:
            print(f"refused: {refused.code} (not a Windows x86_64 host)")
            return 2
        dispatch = {"refused": refused.code}
    library = NativeInt8Library(Path(args.kernel).resolve(), expected_digest=args.kernel_digest)
    rng = np.random.default_rng(20261007)
    worst = 0.0
    for rows, out_features, in_features in ((1, 896, 896), (1, 4864, 896), (1, 896, 4864), (8, 151936, 896)):
        values = rng.integers(-127, 128, size=(out_features, in_features), dtype=np.int8)
        scales = (rng.random(out_features, dtype=np.float32) * 0.01 + 1e-4).astype(np.float32)
        hidden = rng.normal(0, 1, size=(1, rows, in_features)).astype(np.float32)
        expected = np.matmul(hidden, values.astype(np.float32).T) * scales
        actual = library.linear(hidden, values, scales, threads=args.threads)
        scale = float(np.max(np.abs(expected))) or 1.0
        worst = max(worst, float(np.max(np.abs(actual - expected))) / scale)
    ok = worst < 1e-4 and (args.conformance_host or library.dispatch == "avx2_fma")
    _write(Path(args.gate_dir).resolve() / "10-windows-probe.json", _receipt(
        "windows_provider_probe",
        "dispatch observation and kernel numerics only; not acceleration or qualification evidence",
        dispatch=dispatch, kernel_digest=library.build_digest, kernel_dispatch=library.dispatch,
        kernel_threading=library.threading, threads=args.threads, cpu_count=os.cpu_count(),
        processor=platform.processor(), worst_relative_error=worst, passed=ok,
        conformance_host=bool(args.conformance_host)))
    print(json.dumps({"dispatch": library.dispatch, "threading": library.threading, "worst_relative_error": worst, "passed": ok}))
    return 0 if ok else 1


def cmd_windows_benchmark(args: argparse.Namespace) -> int:
    import numpy as np

    from mycelium_platform_neutral import int8_stage_benchmark as bench
    from mycelium_platform_neutral.native_int8 import NativeInt8Library, NativeInt8LinearProvider
    from mycelium_platform_neutral.runtime_efficiency import THRESHOLDS, decide_efficiency
    from mycelium_platform_neutral.windows_provider import WindowsProviderUnavailable, detect_windows_dispatch

    gate = Path(args.gate_dir).resolve()
    try:
        detect_windows_dispatch()
        windows = True
    except WindowsProviderUnavailable as refused:
        if not args.conformance_host:
            print(f"refused: {refused.code} (not a Windows x86_64 host)")
            return 2
        windows = False
    library = NativeInt8Library(Path(args.kernel).resolve(), expected_digest=args.kernel_digest)
    provider = NativeInt8LinearProvider(library, threads=args.threads)
    loaded = time.perf_counter()
    tensors, config, files = bench.load_qwen2_int8_model(Path(args.snapshot).resolve())
    load_ms = (time.perf_counter() - loaded) * 1000.0
    manifest = bench.frozen_manifest(
        vocab_size=int(config["vocab_size"]),
        paired_windows=args.paired_windows,
        output_lengths=tuple(args.output_lengths),
        prompt_lengths=tuple(args.prompt_lengths),
        repeats_per_cell=args.repeats_per_cell,
    )
    _write(gate / "11-benchmark-manifest.json", {**manifest, "model_files": files, "kernel_digest": library.build_digest,
                                                 "threads": args.threads, "frozen_at": time.time()})
    log_path = gate / "11-benchmark.log"
    with log_path.open("a", encoding="utf-8") as log:
        result = bench.run_paired_benchmark(
            tensors, config, manifest, candidate_provider=provider,
            log=lambda line: (log.write(line + "\n"), log.flush()),
        )
    drift = result["candidate_rate_drift"]
    thermal = args.thermal_state if drift >= 0.85 else "serious"
    driver_digest = "sha256:" + hashlib.sha256(json.dumps({
        "python": platform.python_version(), "numpy": np.__version__, "system": platform.platform(),
        "kernel_threading": library.threading,
    }, sort_keys=True).encode()).hexdigest()
    now = time.time()
    requests = min(result["requests_per_mode"].values())
    tokens = min(result["generated_tokens_per_mode"].values())
    failures = dict(result["reliability"])
    if result["token_parity_mismatches"]:
        failures["failures"] += len(result["token_parity_mismatches"])
    record = decide_efficiency({
        "protocol": "mycelium.runtime_efficiency_qualification.v1",
        "qualification_id": f"a9-windows-stage-{int(now)}",
        "provider_binding": {
            "backend_id": WINDOWS_BACKEND_ID if windows else "numpy",
            "build_digest": library.build_digest,
            "device_class": "windows_x86_64" if windows else "conformance_host",
            "driver_runtime_digest": driver_digest,
        },
        "model_binding": {
            "model_id": bench.MODEL_ID, "revision": bench.MODEL_REVISION,
            "representation": bench.REPRESENTATION, "profile_digest": args.profile_digest,
        },
        "benchmark": {
            "manifest_digest": manifest["manifest_digest"],
            "paired_windows": result["paired_windows"],
            "requests_per_mode": requests,
            "prompt_buckets": manifest["prompt_buckets"],
            "output_buckets": manifest["output_buckets"],
            "generated_tokens_per_mode": tokens,
            "thresholds": dict(THRESHOLDS),
            "thermal_state": thermal,
            "measured_at": now,
        },
        "results": {key: float(result[key]) for key in (
            "median_decode_ratio", "paired_confidence_lower_bound",
            "median_decode_tokens_per_second", "p95_ttft_ms")},
        "reliability": failures,
        "cleanup": result["cleanup"],
        "materialization_proof": {
            "warm_decode_full_weight_materializations": provider.full_weight_materializations
            + (0 if result["packed_state_unchanged"] else 1),
            "warm_decode_windows": result["paired_windows"],
        },
        "planner_result": {
            "vocabulary_head_cost_explicit": True,
            "unknown_component_costs": [],
            "prediction_error_ms": float(result["component_reconciliation_error_ms"]),
        },
        "evidence_generation": 1,
        "issued_at": now,
        "expires_at": now + 7 * 24 * 3600,
    })
    decision = record.to_mapping() if hasattr(record, "to_mapping") else record
    _write(gate / "12-windows-benchmark.json", _receipt(
        "windows_stage_benchmark",
        "single-host Qwen2.5-0.5B int8 stage pre-qualification on the exact provider build; the product-route two-host gate through the browser is still required for A9",
        windows_host=windows, load_ms=load_ms, thermal_state_declared=args.thermal_state,
        thermal_source="operator_declared", candidate_rate_drift=drift, result=result,
        provider=provider.report(), efficiency_qualification=decision))
    state = decision["decision"]["state"] if isinstance(decision, dict) else None
    print(json.dumps({"decision": state, "blockers": decision["decision"]["blockers"],
                      "median_tok_s": result["median_decode_tokens_per_second"],
                      "ratio": result["median_decode_ratio"], "p95_ttft_ms": result["p95_ttft_ms"],
                      "parity_mismatches": len(result["token_parity_mismatches"])}))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    gate = Path(args.gate_dir).resolve()
    receipts = {path.name: _read(path) for path in sorted(gate.glob("[0-9][0-9]-*.json")) if "private" not in path.name}
    required = {
        "01-seed-export.json": "seed export",
        "03-migration.json": "migration + cutover + reader removal",
        "04-negatives.json": "deterministic negatives",
        "10-windows-probe.json": "windows provider probe",
        "12-windows-benchmark.json": "windows stage benchmark",
    }
    status = {label: (name in receipts) for name, label in required.items()}
    remaining = [label for label, done in status.items() if not done] + [
        "product-route two-host M4 Pro -> Windows browser run (runs/v2/runday/a9/a9_runday.sh windows-route)",
        "physical negatives on the live product path",
        "all eight UI workspaces live verification",
        "one atomic A9 feature commit",
    ]
    _write(gate / "99-summary.json", _receipt(
        "a9_gate_summary", "an index of receipts; A9 is closed only by the spec section 11 conditions",
        receipts=sorted(receipts), status=status, remaining=remaining))
    print(json.dumps({"status": status, "remaining": remaining}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed-export")
    p.add_argument("--gate-dir", required=True)
    p.add_argument("--seed-db", required=True, help="live seed state.sqlite3 (read-only backup)")
    p.add_argument("--seed-key", required=True, help="the seed's identity/seed.key")
    p.add_argument("--swarm-id", required=True)
    p.add_argument("--build-digest", required=True, help="sha256: digest of the candidate every member runs")
    p.add_argument("--software-version", required=True)
    p.set_defaults(func=cmd_seed_export)

    p = sub.add_parser("member-sign")
    p.add_argument("--request", required=True)
    p.add_argument("--data-dir", required=True, help="the node's --data-dir (membership.state.json)")
    p.add_argument("--key-file", help="default: <data-dir>/identity/node.key")
    p.add_argument("--new-incarnation", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_member_sign)

    p = sub.add_parser("migrate")
    p.add_argument("--gate-dir", required=True)
    p.add_argument("--profiles", required=True, help="directory of signed member profiles")
    p.add_argument("--migration-id", default="a9-v2-migration")
    p.add_argument("--stop-after", choices=("profiles_signed", "profiles_verified", "commit_pending", "committed"))
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("negatives")
    p.add_argument("--gate-dir", required=True)
    p.add_argument("--profiles", required=True)
    p.add_argument("--build-digest", required=True)
    p.set_defaults(func=cmd_negatives)

    for name, func in (("windows-probe", cmd_windows_probe), ("windows-benchmark", cmd_windows_benchmark)):
        p = sub.add_parser(name)
        p.add_argument("--gate-dir", required=True)
        p.add_argument("--kernel", required=True, help="absolute path of mycelium_int8.dll")
        p.add_argument("--kernel-digest", required=True)
        p.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 2) // 2))
        p.add_argument("--conformance-host", action="store_true", help="allow a non-Windows smoke run (never evidence)")
        p.set_defaults(func=func)
        if name == "windows-benchmark":
            p.add_argument("--snapshot", required=True, help=".../snapshots/7ae557604adf67be50417f59c2c2f167def9a775")
            p.add_argument("--profile-digest", required=True, help="the Windows member's v2 profile digest (member-sign)")
            p.add_argument("--thermal-state", required=True, choices=("nominal", "fair", "serious", "critical", "unknown"))
            p.add_argument("--paired-windows", type=int, default=3)
            p.add_argument("--prompt-lengths", type=int, nargs=2, default=[16, 128])
            p.add_argument("--output-lengths", type=int, nargs=2, default=[16, 48])
            p.add_argument("--repeats-per-cell", type=int, default=2)

    p = sub.add_parser("summary")
    p.add_argument("--gate-dir", required=True)
    p.set_defaults(func=cmd_summary)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except GateError as error:
        print(f"a9 gate refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
