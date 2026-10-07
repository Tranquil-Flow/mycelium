# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline verification of the A11 node evidence for prereg R9 §6 bars 5 and 6.

Input: two documents from the A11 evidence door (``before`` the benchmark and
``after`` it), the bound operator plan the serve ran, and the product benchmark
document.  Every node envelope is checked before any number is read:

* the observation is a ``mycelium.physical_node_observation`` of event
  ``a11_snapshot`` for the plan's run and deployment, from a node of the plan;
* its endpoint id and the signature's signer endpoint id equal that node's
  endpoint id in the plan's **signed** assignment offers;
* the verification key *is* that endpoint's Ed25519 key (the node signs with
  its endpoint secret), and the Ed25519 signature verifies over the canonical
  observation.

So a record cannot be invented or edited by the serve, the evidence door or the
run-day collector.  Then, over the benchmark's own requests (the history
records after the ``before`` watermark that state a preference, in completion
order: the two warmups, then the six windows):

* **bar 5** -- every benchmark request was released with ``normal_completion``
  on every route node, no request's KV expired, and after the last request of
  each window every node held zero KV states;
* **bar 6** -- across the speculative-requested requests the run observed a
  proposal, an acceptance, a rejection or correction, and a fallback (a deadline
  fallback, a stopped speculation or a target-only admission).

Claim boundary: this checks evidence; it decides nothing about benefit.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Any

PROTOCOL = "mycelium.a11_node_evidence_verdict.v1"
OBSERVATION_PROTOCOL = "mycelium.physical_node_observation.v1"
EVENT = "a11_snapshot"
WINDOW_MODES = ("target_only", "speculative", "speculative", "target_only", "target_only", "speculative")
_PREFERENCE = {"target_only": "target_only", "speculative": "speculative_requested"}


class A11EvidenceError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _fail(code: str, detail: str = "") -> None:
    raise A11EvidenceError(code, detail)


def plan_endpoints(plan: Mapping[str, Any]) -> dict[str, str]:
    """``node_id -> endpoint_id`` from the bound plan's signed assignment offers."""

    endpoints: dict[str, str] = {}
    try:
        offers = plan["controller"]["membership_snapshot"]["assignment_offers"]
        for envelope in offers:
            for record in envelope["message"]["peer_endpoint_records"]:
                node_id, endpoint_id = record["node_id"], record["endpoint_id"]
                if endpoints.setdefault(node_id, endpoint_id) != endpoint_id:
                    _fail("a11_evidence_plan_endpoint_conflict", node_id)
    except (KeyError, TypeError):
        _fail("a11_evidence_plan_invalid")
    if not endpoints:
        _fail("a11_evidence_plan_invalid")
    return endpoints


def _verify_envelope(envelope: Any, *, node_id: str, endpoint_id: str, run_id: str, deployment_id: str) -> dict[str, Any]:
    from mycelium_qualification.signing import build_ed25519_verifier
    from mycelium_qualification.evidence import canonical_json_bytes

    if not isinstance(envelope, Mapping) or set(envelope) != {"observation", "signature", "verification_key"}:
        _fail("a11_evidence_envelope_invalid", node_id)
    observation, signature, key = envelope["observation"], envelope["signature"], envelope["verification_key"]
    if (
        not isinstance(observation, Mapping)
        or observation.get("protocol") != OBSERVATION_PROTOCOL
        or observation.get("event") != EVENT
        or observation.get("node_id") != node_id
        or observation.get("run_id") != run_id
        or observation.get("deployment_id") != deployment_id
        or observation.get("endpoint_id") != endpoint_id
        or not isinstance(observation.get("details"), Mapping)
        or not isinstance(signature, Mapping)
        or signature.get("signer_endpoint_id") != endpoint_id
    ):
        _fail("a11_evidence_observation_invalid", node_id)
    try:
        key_bytes = base64.b64decode(key["verification_key"], validate=True)
    except (KeyError, TypeError, ValueError):
        _fail("a11_evidence_key_invalid", node_id)
    if key_bytes.hex() != endpoint_id.lower():
        _fail("a11_evidence_key_not_bound_to_endpoint", node_id)
    try:
        valid = build_ed25519_verifier([dict(key)])(canonical_json_bytes(dict(observation)), dict(signature))
    except Exception:  # noqa: BLE001 - any verifier refusal is an invalid signature
        valid = False
    if valid is not True:
        _fail("a11_evidence_signature_invalid", node_id)
    return dict(observation["details"])


def verified_nodes(document: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every node's verified details from one door document."""

    endpoints = plan_endpoints(plan)
    run_id, deployment_id = plan.get("run_id"), plan.get("controller", {}).get("run_plan", {}).get("deployment_id")
    nodes = document.get("nodes") if isinstance(document, Mapping) else None
    if not isinstance(nodes, Mapping) or set(nodes) != set(endpoints):
        _fail("a11_evidence_node_set_mismatch")
    return {
        node_id: _verify_envelope(
            nodes[node_id], node_id=node_id, endpoint_id=endpoints[node_id], run_id=run_id, deployment_id=deployment_id
        )
        for node_id in sorted(nodes)
    }


def _entry_history(details: Mapping[str, dict[str, Any]], entry: str) -> dict[str, Any]:
    speculation = details[entry].get("speculation")
    if not isinstance(speculation, Mapping) or not isinstance(speculation.get("requests"), list):
        _fail("a11_evidence_entry_history_missing")
    return speculation


def verify_node_evidence(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    plan: Mapping[str, Any],
    benchmark: Mapping[str, Any],
) -> dict[str, Any]:
    entry = plan.get("controller", {}).get("run_plan", {}).get("entry_node_id")
    before_nodes = verified_nodes(before, plan)
    after_nodes = verified_nodes(after, plan)
    if entry not in after_nodes:
        _fail("a11_evidence_entry_missing")
    watermark = int(_entry_history(before_nodes, entry)["history_sequence"])
    history = _entry_history(after_nodes, entry)
    records = [
        record
        for record in history["requests"]
        if record["sequence"] > watermark and record.get("preference") in {"target_only", "speculative_requested"}
    ]
    # The history ring must still hold every request since the watermark.
    if history["requests"] and history["requests"][0]["sequence"] > watermark + 1:
        _fail("a11_evidence_history_truncated")
    requests = benchmark.get("requests")
    if not isinstance(requests, list) or not requests or len(requests) % len(WINDOW_MODES):
        _fail("a11_evidence_benchmark_invalid")
    size = len(requests) // len(WINDOW_MODES)
    if len(records) != size * (2 + len(WINDOW_MODES)):
        _fail("a11_evidence_request_count_mismatch", f"{len(records)} != {size * (2 + len(WINDOW_MODES))}")
    phases = [("warmup_target_only", "target_only"), ("warmup_speculative", "speculative")] + [
        (f"window_{index}", mode) for index, mode in enumerate(WINDOW_MODES)
    ]
    windows = []
    for index, (label, mode) in enumerate(phases):
        chunk = records[index * size : (index + 1) * size]
        if any(record["preference"] != _PREFERENCE[mode] for record in chunk):
            _fail("a11_evidence_window_mode_mismatch", label)
        windows.append((label, mode, chunk))

    # Bar 5: KV residue, per node, from the signed release logs.
    bench_ids = {record["request_id"] for record in records}
    residue: dict[str, Any] = {}
    bar5 = True
    for node_id, details in after_nodes.items():
        kv = details.get("kv")
        if not isinstance(kv, Mapping) or not isinstance(kv.get("releases"), list):
            _fail("a11_evidence_kv_log_missing", node_id)
        before_sequence = int(before_nodes[node_id]["kv"]["release_sequence"])
        releases = [item for item in kv["releases"] if item["sequence"] > before_sequence]
        if kv["releases"] and kv["releases"][0]["sequence"] > before_sequence + 1:
            _fail("a11_evidence_kv_log_truncated", node_id)
        by_request: dict[str, list[dict[str, Any]]] = {}
        for item in releases:
            by_request.setdefault(item["request_id"], []).append(item)
        released = {rid for rid in bench_ids if rid in by_request}
        abnormal = sorted(
            rid for rid in released if any(item["reason"] != "normal_completion" for item in by_request[rid])
        )
        window_end = {}
        for label, _mode, chunk in windows:
            last = chunk[-1]["request_id"]
            final = by_request.get(last, [])
            window_end[label] = None if not final else final[-1]["active_state_count_after"]
        participates = bool(released)
        node_ok = (
            (not participates or released == bench_ids)
            and not abnormal
            and (not participates or all(value == 0 for value in window_end.values()))
            and kv.get("active_state_count") == 0
        )
        bar5 = bar5 and node_ok
        residue[node_id] = {
            "benchmark_requests_released": len(released),
            "abnormal_release_requests": len(abnormal),
            "active_states_after_window": window_end,
            "active_state_count_at_collection": kv.get("active_state_count"),
            "ok": node_ok,
        }
    if not any(item["benchmark_requests_released"] for item in residue.values()):
        bar5 = False

    # Bar 6: coverage across the speculative-requested requests of the run.
    speculative = [record for record in records if record["preference"] == "speculative_requested"]
    totals = {
        key: sum(int(record[key]) for record in speculative)
        for key in ("proposed_count", "accepted_count", "rejected_count", "target_corrected_count",
                    "target_bonus_count", "deadline_fallbacks", "rounds", "decode_rounds", "committed_count")
    }
    stopped = sum(1 for record in speculative if record.get("stop_reason"))
    refused = sum(1 for record in speculative if record.get("mode") == "target_only")
    fallback = totals["deadline_fallbacks"] + stopped + refused
    coverage = {
        "proposal": totals["proposed_count"] > 0,
        "acceptance": totals["accepted_count"] > 0,
        "rejection_or_correction": totals["rejected_count"] + totals["target_corrected_count"] > 0,
        "fallback": fallback > 0,
    }
    return {
        "protocol": PROTOCOL,
        "entry_node_id": entry,
        "requests_per_window": size,
        "bar5_zero_kv_residue": bar5,
        "bar6_coverage": all(coverage.values()),
        "kv_residue": residue,
        "coverage": coverage,
        "speculative_totals": {
            **totals,
            "stopped_requests": stopped,
            "target_only_admissions": refused,
            "fallback_events": fallback,
        },
        "claim_boundary": "evidence check for prereg R9 bars 5 and 6 only; no benefit claim",
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    args = parser.parse_args(argv)

    def load(path: Path) -> Any:
        document = json.loads(path.read_text(encoding="utf-8"))
        return document.get("evidence", document) if isinstance(document, Mapping) else document

    try:
        verdict = verify_node_evidence(
            before=load(args.before), after=load(args.after), plan=load(args.plan), benchmark=load(args.benchmark)
        )
    except A11EvidenceError as error:
        print(json.dumps({"protocol": PROTOCOL, "error": error.code}))
        return 2
    print(json.dumps(verdict, sort_keys=True))
    return 0 if verdict["bar5_zero_kv_residue"] and verdict["bar6_coverage"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
