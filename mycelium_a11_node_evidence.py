# SPDX-License-Identifier: AGPL-3.0-only
"""Offline verification of the A11 node evidence for prereg R9 §6 bars 5 and 6.

Trust chain (nothing is taken from the evidence bundle on trust):

1. **Plan.** The bound operator plan's raw bytes must hash to the plan digest
   the run-day record pinned (``expected_plan_digest``), and every assignment
   offer must verify (``verify_membership_message``) under the seed key whose
   digest is pinned outside the bundle (``seed_key_digest``, derived from the
   owner's seed state).  The node endpoint ids come only from those verified
   offers.
2. **Node envelopes.** Each node's observation must be the closed
   ``mycelium.physical_node_observation.v1`` statement of event
   ``a11_snapshot`` for the plan's run and deployment, from that node's
   endpoint; its verification key must *be* that endpoint's Ed25519 key, and
   the signature must verify over the canonical statement bytes.  Only the
   signed ``details`` are read, and they must match the closed
   ``mycelium.a11_node_evidence.v1`` schema exactly.
3. **Binding.** The signed details carry the collector's binding: the window
   id the gate run created, the phase (``before``/``after``), the plan digest,
   the frozen prereg digest and, after the benchmark, the digest of the exact
   benchmark document.  Evidence from another window, run, plan, prereg or
   benchmark is refused.  ``before`` and ``after`` must come from the same node
   processes, in that order.

Then, over the benchmark's own requests (the entry history records after the
signed ``before`` watermark that state a preference, in completion order: two
warmups, then the six windows):

* **bar 5** -- every benchmark request was released with ``normal_completion``
  on every route node, and after the last request of each window every node
  held zero KV states (and still holds none at collection);
* **bar 6** -- across the speculative-requested requests the run observed a
  proposal, an acceptance, a rejection or correction, and a fallback.

Any defect raises :class:`A11EvidenceError`; there is no partial or skipped
verdict.  Claim boundary: this checks evidence; it decides nothing about
benefit.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

PROTOCOL = "mycelium.a11_node_evidence_verdict.v1"
OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
DETAILS_PROTOCOL = "mycelium.a11_node_evidence.v1"
EVENT = "a11_snapshot"
WINDOW_MODES = ("target_only", "speculative", "speculative", "target_only", "target_only", "speculative")
_PREFERENCE = {"target_only": "target_only", "speculative": "speculative_requested"}
_OBSERVATION_FIELDS = frozenset(
    {"protocol", "event", "monotonic_ns", "run_id", "deployment_id", "node_id", "host_id", "process_id",
     "endpoint_id", "peer_generation", "state", "route_ready", "details"}
)
_DETAILS_FIELDS = frozenset({"protocol", "binding", "kv", "speculation"})
_BINDING_FIELDS = frozenset({"window_id", "phase", "plan_digest", "prereg_digest", "benchmark_digest"})
_KV_FIELDS = frozenset({"release_sequence", "active_state_count", "releases"})
_RELEASE_FIELDS = frozenset(
    {"sequence", "request_id", "path_id", "placement_id", "reason", "active_state_count_after"}
)
_SPECULATION_FIELDS = frozenset(
    {"draft_identity_digest", "width", "baseline_samples", "baseline_measured", "protocol",
     "history_sequence", "requests"}
)
_COUNTERS = (
    "rounds", "decode_rounds", "deadline_fallbacks", "proposed_count", "verified_count", "accepted_count",
    "rejected_count", "target_corrected_count", "target_bonus_count", "committed_count", "rollback_count",
)
_HISTORY_FIELDS = frozenset(
    {"sequence", "request_id", "preference", "mode", "fallback_reason", "stop_reason", *_COUNTERS}
)


class A11EvidenceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _fail(code: str, detail: str = "") -> None:
    raise A11EvidenceError(code, detail)


def _int(value: Any, code: str, detail: str = "") -> int:
    if type(value) is not int or value < 0:
        _fail(code, detail)
    return value


def sha256_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def strict_json(raw: bytes) -> Any:
    """Parse JSON refusing duplicate keys (a second value can never shadow the first)."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail("a11_evidence_duplicate_key", key)
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _c: _fail("a11_evidence_json_invalid"))
    except A11EvidenceError:
        raise
    except (ValueError, UnicodeDecodeError):
        _fail("a11_evidence_json_invalid")


# ---------------------------------------------------------------------------
# Plan trust anchor
# ---------------------------------------------------------------------------


def verified_plan_endpoints(plan: Mapping[str, Any], *, seed_key_digest: str) -> dict[str, str]:
    """``node_id -> endpoint_id`` from offers that verify under the pinned seed key."""

    from mycelium_membership.contracts import (
        ASSIGNMENT_OFFER_PROTOCOL,
        MembershipContractError,
        verify_membership_message,
    )

    try:
        run_plan = plan["controller"]["run_plan"]
        offers = plan["controller"]["membership_snapshot"]["assignment_offers"]
        deployment_id = run_plan["deployment_id"]
    except (KeyError, TypeError):
        _fail("a11_evidence_plan_invalid")
    if not isinstance(offers, list) or not offers:
        _fail("a11_evidence_plan_invalid")
    endpoints: dict[str, str] = {}
    recipients: set[str] = set()
    for envelope in offers:
        try:
            message = envelope["message"]
            # Authenticity, not freshness: verify at the offer's own issue time.
            verified = verify_membership_message(
                envelope,
                now=float(message["issued_at"]),
                expected_key_digest=seed_key_digest,
                expected_protocol=ASSIGNMENT_OFFER_PROTOCOL,
            )
        except (MembershipContractError, KeyError, TypeError, ValueError):
            _fail("a11_evidence_plan_offer_unverified")
        if verified["deployment_id"] != deployment_id or verified["recipient_node_id"] in recipients:
            _fail("a11_evidence_plan_offer_unverified")
        recipients.add(verified["recipient_node_id"])
        for record in verified["peer_endpoint_records"]:
            node_id, endpoint_id = record["node_id"], record["endpoint_id"]
            if endpoints.setdefault(node_id, endpoint_id) != endpoint_id:
                _fail("a11_evidence_plan_endpoint_conflict", node_id)
    if not endpoints or set(endpoints) != recipients:
        _fail("a11_evidence_plan_endpoints_incomplete")
    return endpoints


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def _verify_envelope(envelope: Any, *, node_id: str, endpoint_id: str, run_id: str, deployment_id: str) -> dict[str, Any]:
    from mycelium_qualification.evidence import canonical_json_bytes
    from mycelium_qualification.signing import build_ed25519_verifier

    if not isinstance(envelope, Mapping) or set(envelope) != {"observation", "signature", "verification_key"}:
        _fail("a11_evidence_envelope_invalid", node_id)
    observation, signature, key = envelope["observation"], envelope["signature"], envelope["verification_key"]
    if (
        not isinstance(observation, Mapping)
        or set(observation) != _OBSERVATION_FIELDS
        or observation["protocol"] != OBSERVATION_PROTOCOL
        or observation["event"] != EVENT
        or observation["node_id"] != node_id
        or observation["run_id"] != run_id
        or observation["deployment_id"] != deployment_id
        or observation["endpoint_id"] != endpoint_id
        or type(observation["process_id"]) is not int
        or type(observation["monotonic_ns"]) is not int
        or not isinstance(observation["details"], Mapping)
        or not isinstance(signature, Mapping)
        or signature.get("signer_endpoint_id") != endpoint_id
        or not isinstance(key, Mapping)
    ):
        _fail("a11_evidence_observation_invalid", node_id)
    try:
        key_bytes = base64.b64decode(key["verification_key"], validate=True)
    except (KeyError, TypeError, ValueError):
        _fail("a11_evidence_key_invalid", node_id)
    if key_bytes.hex() != endpoint_id.lower():
        _fail("a11_evidence_key_not_bound_to_endpoint", node_id)
    try:
        verifier = build_ed25519_verifier([dict(key)])
        valid = verifier(canonical_json_bytes(dict(observation)), dict(signature))
    except Exception:  # noqa: BLE001 - a refused key or statement is an invalid signature
        valid = False
    if valid is not True:
        _fail("a11_evidence_signature_invalid", node_id)
    return dict(observation)


def _check_details(details: Mapping[str, Any], *, node_id: str, entry: bool) -> None:
    if set(details) != _DETAILS_FIELDS or details["protocol"] != DETAILS_PROTOCOL:
        _fail("a11_evidence_details_invalid", node_id)
    if not isinstance(details["binding"], Mapping) or set(details["binding"]) != _BINDING_FIELDS:
        _fail("a11_evidence_binding_invalid", node_id)
    kv = details["kv"]
    if not isinstance(kv, Mapping) or set(kv) != _KV_FIELDS or not isinstance(kv["releases"], list):
        _fail("a11_evidence_kv_log_invalid", node_id)
    _int(kv["release_sequence"], "a11_evidence_kv_log_invalid", node_id)
    _int(kv["active_state_count"], "a11_evidence_kv_log_invalid", node_id)
    previous = 0
    for item in kv["releases"]:
        if not isinstance(item, Mapping) or set(item) != _RELEASE_FIELDS or not isinstance(item["request_id"], str):
            _fail("a11_evidence_kv_log_invalid", node_id)
        sequence = _int(item["sequence"], "a11_evidence_kv_log_invalid", node_id)
        _int(item["active_state_count_after"], "a11_evidence_kv_log_invalid", node_id)
        if sequence <= previous:
            _fail("a11_evidence_kv_log_invalid", node_id)
        previous = sequence
    if kv["releases"] and previous != kv["release_sequence"]:
        _fail("a11_evidence_kv_log_invalid", node_id)
    speculation = details["speculation"]
    if not entry:
        if speculation is not None:
            _fail("a11_evidence_details_invalid", node_id)
        return
    if (
        not isinstance(speculation, Mapping)
        or set(speculation) != _SPECULATION_FIELDS
        or not isinstance(speculation["requests"], list)
    ):
        _fail("a11_evidence_entry_history_invalid", node_id)
    _int(speculation["history_sequence"], "a11_evidence_entry_history_invalid", node_id)
    previous = 0
    for record in speculation["requests"]:
        if not isinstance(record, Mapping) or set(record) != _HISTORY_FIELDS or not isinstance(record["request_id"], str):
            _fail("a11_evidence_entry_history_invalid", node_id)
        for key in ("sequence", *_COUNTERS):
            _int(record[key], "a11_evidence_entry_history_invalid", node_id)
        if record["sequence"] <= previous:
            _fail("a11_evidence_entry_history_invalid", node_id)
        previous = record["sequence"]
    if speculation["requests"] and previous != speculation["history_sequence"]:
        _fail("a11_evidence_entry_history_invalid", node_id)


def verified_snapshot(
    document: Any,
    *,
    plan: Mapping[str, Any],
    endpoints: Mapping[str, str],
    binding: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Every plan node's verified observation from one door document, bound as expected."""

    run_plan = plan["controller"]["run_plan"]
    entry = run_plan.get("entry_node_id")
    nodes = document.get("nodes") if isinstance(document, Mapping) else None
    if not isinstance(nodes, Mapping) or set(nodes) != set(endpoints):
        _fail("a11_evidence_node_set_mismatch")
    if entry not in endpoints:
        _fail("a11_evidence_entry_missing")
    result: dict[str, dict[str, Any]] = {}
    for node_id in sorted(endpoints):
        observation = _verify_envelope(
            nodes[node_id],
            node_id=node_id,
            endpoint_id=endpoints[node_id],
            run_id=plan.get("run_id"),
            deployment_id=run_plan.get("deployment_id"),
        )
        details = observation["details"]
        _check_details(details, node_id=node_id, entry=node_id == entry)
        if dict(details["binding"]) != dict(binding):
            _fail("a11_evidence_binding_mismatch", node_id)
        result[node_id] = observation
    return result


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def verify_node_evidence(
    *,
    before: Any,
    after: Any,
    plan_raw: bytes,
    benchmark_raw: bytes,
    expected_plan_digest: str,
    prereg_digest: str,
    seed_key_digest: str,
    window_id: str,
) -> dict[str, Any]:
    plan_digest = sha256_digest(plan_raw)
    if plan_digest != expected_plan_digest:
        _fail("a11_evidence_plan_digest_mismatch")
    plan = strict_json(plan_raw)
    benchmark = strict_json(benchmark_raw)
    endpoints = verified_plan_endpoints(plan, seed_key_digest=seed_key_digest)
    entry = plan["controller"]["run_plan"].get("entry_node_id")
    sealed = benchmark.get("benchmark") if isinstance(benchmark, Mapping) else None
    if not isinstance(sealed, Mapping) or sealed.get("plan_digest") != prereg_digest:
        _fail("a11_evidence_benchmark_not_bound_to_prereg")
    common = {"window_id": window_id, "plan_digest": plan_digest, "prereg_digest": prereg_digest}
    before_nodes = verified_snapshot(
        before, plan=plan, endpoints=endpoints, binding={**common, "phase": "before", "benchmark_digest": None}
    )
    after_nodes = verified_snapshot(
        after, plan=plan, endpoints=endpoints,
        binding={**common, "phase": "after", "benchmark_digest": sha256_digest(benchmark_raw)},
    )
    for node_id in endpoints:
        first, last = before_nodes[node_id], after_nodes[node_id]
        if (
            (first["host_id"], first["process_id"]) != (last["host_id"], last["process_id"])
            or last["monotonic_ns"] <= first["monotonic_ns"]
        ):
            _fail("a11_evidence_process_changed", node_id)
    before_details = {node_id: item["details"] for node_id, item in before_nodes.items()}
    after_details = {node_id: item["details"] for node_id, item in after_nodes.items()}

    watermark = before_details[entry]["speculation"]["history_sequence"]
    history = after_details[entry]["speculation"]
    if history["history_sequence"] < watermark:
        _fail("a11_evidence_history_regressed")
    if history["requests"] and history["requests"][0]["sequence"] > watermark + 1:
        _fail("a11_evidence_history_truncated")
    records = [
        record for record in history["requests"]
        if record["sequence"] > watermark and record["preference"] in {"target_only", "speculative_requested"}
    ]
    requests = benchmark.get("requests")
    if not isinstance(requests, list) or not requests or len(requests) % len(WINDOW_MODES):
        _fail("a11_evidence_benchmark_invalid")
    size = len(requests) // len(WINDOW_MODES)
    if len(records) != size * (2 + len(WINDOW_MODES)):
        _fail("a11_evidence_request_count_mismatch", f"{len(records)} != {size * (2 + len(WINDOW_MODES))}")
    phases = [("warmup_target_only", "target_only"), ("warmup_speculative", "speculative")] + [
        (f"window_{index}", mode) for index, mode in enumerate(WINDOW_MODES)
    ]
    for index, item in enumerate(requests):
        if not isinstance(item, Mapping) or item.get("mode") != WINDOW_MODES[index // size]:
            _fail("a11_evidence_benchmark_invalid")
    windows = []
    for index, (label, mode) in enumerate(phases):
        chunk = records[index * size : (index + 1) * size]
        if any(record["preference"] != _PREFERENCE[mode] for record in chunk):
            _fail("a11_evidence_window_mode_mismatch", label)
        windows.append((label, chunk))
    bench_ids = [record["request_id"] for record in records]
    if len(set(bench_ids)) != len(bench_ids):
        _fail("a11_evidence_request_ids_not_unique")

    # Bar 5 -- every route node must have released every benchmark request.
    residue: dict[str, Any] = {}
    bar5 = True
    for node_id, details in after_details.items():
        kv = details["kv"]
        before_sequence = before_details[node_id]["kv"]["release_sequence"]
        if kv["release_sequence"] < before_sequence:
            _fail("a11_evidence_kv_log_regressed", node_id)
        if kv["releases"] and kv["releases"][0]["sequence"] > before_sequence + 1:
            _fail("a11_evidence_kv_log_truncated", node_id)
        by_request: dict[str, list[Mapping[str, Any]]] = {}
        for item in kv["releases"]:
            if item["sequence"] > before_sequence:
                by_request.setdefault(item["request_id"], []).append(item)
        missing = [rid for rid in bench_ids if rid not in by_request]
        abnormal = [
            rid for rid in bench_ids
            if any(item["reason"] != "normal_completion" for item in by_request.get(rid, ()))
        ]
        window_end = {
            label: (by_request[chunk[-1]["request_id"]][-1]["active_state_count_after"]
                    if chunk[-1]["request_id"] in by_request else None)
            for label, chunk in windows
        }
        node_ok = (
            not missing
            and not abnormal
            and all(value == 0 for value in window_end.values())
            and kv["active_state_count"] == 0
        )
        bar5 = bar5 and node_ok
        residue[node_id] = {
            "benchmark_requests_released": len(bench_ids) - len(missing),
            "missing_release_requests": len(missing),
            "abnormal_release_requests": len(abnormal),
            "active_states_after_window": window_end,
            "active_state_count_at_collection": kv["active_state_count"],
            "ok": node_ok,
        }

    # Bar 6 -- coverage across the speculative-requested requests.
    speculative = [record for record in records if record["preference"] == "speculative_requested"]
    totals = {key: sum(record[key] for record in speculative) for key in _COUNTERS}
    stopped = sum(1 for record in speculative if record["stop_reason"])
    refused = sum(1 for record in speculative if record["mode"] == "target_only")
    fallback = totals["deadline_fallbacks"] + stopped + refused
    coverage = {
        "proposal": totals["proposed_count"] > 0,
        "acceptance": totals["accepted_count"] > 0,
        "rejection_or_correction": totals["rejected_count"] + totals["target_corrected_count"] > 0,
        "fallback": fallback > 0,
    }
    return {
        "protocol": PROTOCOL,
        "window_id": window_id,
        "plan_digest": plan_digest,
        "prereg_digest": prereg_digest,
        "benchmark_digest": sha256_digest(benchmark_raw),
        "entry_node_id": entry,
        "requests_per_window": size,
        "bar5_zero_kv_residue": bar5,
        "bar6_coverage": all(coverage.values()),
        "kv_residue": residue,
        "coverage": coverage,
        "speculative_totals": {**totals, "stopped_requests": stopped, "target_only_admissions": refused,
                               "fallback_events": fallback},
        "claim_boundary": "evidence check for prereg R9 bars 5 and 6 only; no benefit claim",
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 only when both bars are met; 1 when evidence verifies but a bar fails; 2 otherwise."""

    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--before", "--after", "--plan", "--benchmark"):
        parser.add_argument(name, type=Path, required=True)
    for name in ("--plan-digest", "--prereg-digest", "--seed-key-digest", "--window-id"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)

    def door(path: Path) -> Any:
        document = strict_json(path.read_bytes())
        if not isinstance(document, Mapping) or set(document) != {"evidence"}:
            _fail("a11_evidence_door_document_invalid")
        return document["evidence"]

    try:
        verdict = verify_node_evidence(
            before=door(args.before),
            after=door(args.after),
            plan_raw=args.plan.read_bytes(),
            benchmark_raw=args.benchmark.read_bytes(),
            expected_plan_digest=args.plan_digest,
            prereg_digest=args.prereg_digest,
            seed_key_digest=args.seed_key_digest,
            window_id=args.window_id,
        )
    except A11EvidenceError as error:
        print(json.dumps({"protocol": PROTOCOL, "verified": False, "error": error.code,
                          "bar5_zero_kv_residue": False, "bar6_coverage": False}))
        return 2
    except Exception:  # noqa: BLE001 - any other failure is unverified evidence, never a pass
        print(json.dumps({"protocol": PROTOCOL, "verified": False, "error": "a11_evidence_verification_failed",
                          "bar5_zero_kv_residue": False, "bar6_coverage": False}))
        return 2
    print(json.dumps({**verdict, "verified": True}, sort_keys=True))
    return 0 if verdict["bar5_zero_kv_residue"] and verdict["bar6_coverage"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
