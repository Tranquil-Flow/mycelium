#!/usr/bin/env python3
"""Run the A5 positive gate through the ordinary product HTTP API.

Proves (spec §1, §10) that two concurrent requests submitted through the
ordinary browser gateway use distinct complete legal tracks — the incumbent
A4 path and one qualified replica track — with per-placement work,
Router-frame movement, stage-local KV, and zero cleanup delta.

The output is a bounded privacy-reduced observation (protocol
``mycelium.a5_product_positive_observation.v1``). It contains no prompt,
decoded text, token IDs, cookies, CSRF values, hostnames, addresses, paths,
command lines, or exception strings.

Requirements at run time: the serve is up with at least one installed
``replica_qualification.v1`` (``--replica-qualification``), live-status shows
``route_alive=true`` and an empty replica-loss set, and the admission-status
endpoint reports zero live resources before the gate starts.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_replica_contracts import validate_replica_qualification  # noqa: E402


class GateError(RuntimeError):
    """Stable gate failure without response bodies or private material."""


class ProductSession:
    """Minimal ordinary-browser-gateway client (bootstrap + inference)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self._bootstrap = self.json("GET", "/api/v1/bootstrap")
        self._qualification = self.json("GET", "/api/v1/qualification/current")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
        timeout: float = 180.0,
    ):
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if mutate:
            session = self._bootstrap["session"]
            headers[session["csrf_header"]] = session["csrf_token"]
            headers["Origin"] = self.base_url
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise GateError(f"http_{method.lower()}_{error.code}") from error
        except (OSError, TimeoutError) as error:
            raise GateError(f"http_{method.lower()}_unavailable") from error

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.request(method, path, **kwargs) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise GateError("http_response_not_object")
        return document

    def submit(self, *, label: str, maximum_new_tokens: int) -> dict[str, Any]:
        return self.json(
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": f"A5 physical positive gate request {label}",
                "max_new_tokens": maximum_new_tokens,
                "qualification": self._qualification["binding"],
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            mutate=True,
        )

    def stream_summary(self, accepted: dict[str, Any]) -> dict[str, Any]:
        event_counts: dict[str, int] = {}
        event_ids: list[tuple[int, int]] = []
        terminal = None
        terminal_at = None
        current_id: tuple[int, int] | None = None
        with self.request("GET", accepted["event_path"]) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "strict").rstrip("\r\n")
                if line.startswith("id: "):
                    parts = line[4:].split(":", 1)
                    if len(parts) != 2:
                        raise GateError("event_id_invalid")
                    current_id = (int(parts[0]), int(parts[1]))
                    continue
                if not line.startswith("event: "):
                    continue
                kind = line[7:]
                if current_id is None:
                    raise GateError("event_identity_missing")
                if event_ids and current_id <= event_ids[-1]:
                    raise GateError("event_identity_not_monotonic")
                event_ids.append(current_id)
                event_counts[kind] = event_counts.get(kind, 0) + 1
                if kind in {"completed", "cancelled", "failed"}:
                    terminal = kind
                    terminal_at = time.monotonic()
        if not event_ids:
            raise GateError("event_stream_empty")
        generations = sorted({item[0] for item in event_ids})
        if len(generations) != 1 or generations[0] < 1:
            raise GateError("publisher_generation_invalid")
        return {
            "request_id": accepted["request_id"],
            "event_counts": dict(sorted(event_counts.items())),
            "publisher_generation": generations[0],
            "first_sequence": event_ids[0][1],
            "last_sequence": event_ids[-1][1],
            "terminal": terminal,
            "terminal_at_monotonic_s": terminal_at,
        }


def public_json(base_url: str, path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10.0) as response:
            document = json.load(response)
    except (OSError, urllib.error.HTTPError, TimeoutError) as error:
        raise GateError("public_status_unavailable") from error
    if not isinstance(document, dict):
        raise GateError("public_status_not_object")
    return document


def wait_for(predicate, *, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise GateError("gate_state_timeout")


def _zero_live_resources(status: dict[str, Any]) -> bool:
    queue = status["queue"]
    return (
        queue["depth"] == 0
        and queue["active_request_ids"] == []
        and all(item["active_reservations"] == 0 for item in status["placements"])
    )


def _request_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["request_id"]: item for item in status["requests"]}


def _peer_snapshot(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["node_id"]: {
            "frames_sent": item.get("frames_sent", 0),
            "frames_received": item.get("frames_received", 0),
            "applied_operation_count": item.get("applied_operation_count", 0),
            "active_kv_state_count": item.get("active_kv_state_count", 0),
        }
        for item in status["peers"]
    }


def _inflight_work_observed(
    before: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    expected_node_ids: set[str],
) -> bool:
    if not expected_node_ids or not expected_node_ids <= current.keys():
        return False
    return all(
        current[node_id][counter] > before.get(node_id, {}).get(counter, 0)
        for node_id in expected_node_ids
        for counter in (
            "frames_sent",
            "frames_received",
            "applied_operation_count",
        )
    )


def run_gate(base_url: str, *, maximum_new_tokens: int) -> dict[str, Any]:
    sessions = (ProductSession(base_url), ProductSession(base_url))
    bindings = tuple(session._qualification.get("binding") for session in sessions)
    if (
        not all(isinstance(binding, dict) for binding in bindings)
        or bindings[0] != bindings[1]
    ):
        raise GateError("qualification_binding_not_shared")
    binding = bindings[0]
    assert isinstance(binding, dict)

    before_status = public_json(base_url, "/__mycelium/live-status")
    if before_status.get("route_alive") is not True:
        raise GateError("route_not_alive")
    replica_qualifications = before_status.get("replica_track_qualification") or []
    if not replica_qualifications:
        raise GateError("replica_track_not_qualified")
    try:
        replica_qualifications = [
            validate_replica_qualification(document)
            for document in replica_qualifications
        ]
    except ValueError as exc:
        raise GateError("replica_track_qualification_invalid") from exc
    if any(
        document["route_ready"] is not True
        or document["parity_verified"] is not True
        or document["startup_challenge_passed"] is not True
        or document["memory_within_bounds"] is not True
        or document["cleanup_within_bounds"] is not True
        or document["directed_link_qualified"] is not True
        or document["deployment_id"] != before_status.get("deployment_id")
        or document["deployment_epoch"] != before_status.get("topology_version")
        for document in replica_qualifications
    ):
        raise GateError("replica_track_evidence_not_current")
    if sum(document["traffic_fraction"] for document in replica_qualifications) > 1.0 + 1e-9:
        raise GateError("replica_track_traffic_fraction_invalid")
    if before_status.get("replica_loss_placement_ids"):
        raise GateError("replica_loss_present_before_gate")
    replica_placement_ids = sorted(
        {
            document.get("placement_id")
            for document in replica_qualifications
            if isinstance(document.get("placement_id"), str)
        }
    )
    if not replica_placement_ids:
        raise GateError("replica_placement_identity_missing")

    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    if not _zero_live_resources(before_runtime):
        raise GateError("route_not_clean_and_alive")
    before_peers = _peer_snapshot(before_status)

    accepted: list[dict[str, Any]] = [None, None]  # type: ignore[list-item]
    submit_errors: list[str] = []

    def submit(index: int) -> None:
        try:
            accepted[index] = sessions[index].submit(
                label=("one" if index == 0 else "two"),
                maximum_new_tokens=maximum_new_tokens,
            )
        except GateError as error:
            submit_errors.append(str(error))

    submit_threads = tuple(
        threading.Thread(target=submit, args=(index,)) for index in range(2)
    )
    for thread in submit_threads:
        thread.start()
    for thread in submit_threads:
        thread.join(timeout=30.0)
        if thread.is_alive():
            raise GateError("submit_worker_join_timeout")
    if submit_errors:
        raise GateError("submit_worker_failed")
    if accepted[0]["request_id"] == accepted[1]["request_id"]:
        raise GateError("duplicate_request_identity")

    summaries: dict[int, dict[str, Any]] = {}
    stream_errors: list[str] = []

    def stream(index: int) -> None:
        try:
            summaries[index] = sessions[index].stream_summary(accepted[index])
        except GateError as error:
            stream_errors.append(str(error))
        except BaseException:
            stream_errors.append("stream_worker_failed")

    threads = tuple(
        threading.Thread(target=stream, args=(index,), daemon=True)
        for index in range(2)
    )
    for thread in threads:
        thread.start()

    request_ids = {item["request_id"] for item in accepted}

    def observed_overlap() -> dict[str, Any] | None:
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
        if not request_ids <= set(runtime["queue"]["active_request_ids"]):
            return None
        requests = _request_map(runtime)
        if not request_ids <= requests.keys():
            return None
        placement_nodes = {
            item["placement_id"]: item["node_id"] for item in runtime["placements"]
        }
        expected_node_ids = {
            placement_nodes[placement_id]
            for request_id in request_ids
            for placement_id in requests[request_id]["placement_ids"]
            if placement_id in placement_nodes
        }
        live = public_json(base_url, "/__mycelium/live-status")
        if not _inflight_work_observed(
            before_peers,
            _peer_snapshot(live),
            expected_node_ids,
        ):
            return None
        return {"runtime": runtime, "live": live}

    overlap_bundle = wait_for(observed_overlap, timeout=60.0)
    overlap = overlap_bundle["runtime"]
    overlap_peers = _peer_snapshot(overlap_bundle["live"])
    requests = _request_map(overlap)
    track_placements = [
        tuple(requests[item["request_id"]]["placement_ids"]) for item in accepted
    ]
    if track_placements[0] == track_placements[1]:
        raise GateError("tracks_not_distinct")
    if not (
        bool(set(track_placements[0]) & set(replica_placement_ids))
        ^ bool(set(track_placements[1]) & set(replica_placement_ids))
    ):
        raise GateError("replica_usage_ambiguous")
    before_reservations = {
        item["placement_id"]: item["active_reservations"]
        for item in before_runtime["placements"]
    }
    overlap_reservation_deltas = {
        item["placement_id"]: (
            item["active_reservations"]
            - before_reservations.get(item["placement_id"], 0)
        )
        for item in overlap["placements"]
    }
    overlap_placements = {
        placement_id
        for track in track_placements
        for placement_id in track
    }
    if any(
        overlap_reservation_deltas.get(placement_id, 0) <= 0
        for placement_id in overlap_placements
    ):
        raise GateError("overlap_placement_reservation_unproven")

    for thread in threads:
        thread.join(timeout=240.0)
        if thread.is_alive():
            raise GateError("stream_worker_join_timeout")
    if stream_errors:
        raise GateError("stream_worker_failed")
    for summary in summaries.values():
        if summary["terminal"] != "completed":
            raise GateError("positive_terminal_invalid")

    after_runtime = wait_for(
        lambda: (
            status
            if _zero_live_resources(
                status := public_json(
                    base_url,
                    "/__mycelium/runtime/admission-status",
                )
            )
            else None
        ),
        timeout=10.0,
    )
    after_status = public_json(base_url, "/__mycelium/live-status")
    after_peers = _peer_snapshot(after_status)

    worked_placements = sorted(
        {
            placement_id
            for track in track_placements
            for placement_id in track
        }
    )
    working_nodes = sorted(
        {
            node_id
            for node_id, overlap_peer in overlap_peers.items()
            if overlap_peer["applied_operation_count"]
            > before_peers.get(node_id, {}).get("applied_operation_count", 0)
        }
    )
    placement_nodes = {
        item["placement_id"]: item["node_id"]
        for item in after_runtime["placements"]
    }
    expected_working_nodes = {
        placement_nodes[placement_id]
        for placement_id in worked_placements
        if placement_id in placement_nodes
    }
    if len(expected_working_nodes) < 2 or not expected_working_nodes <= set(working_nodes):
        raise GateError("selected_placement_work_unproven")
    frame_deltas = {
        node_id: {
            "frames_sent": overlap_peers[node_id]["frames_sent"]
            - before_peers.get(node_id, {}).get("frames_sent", 0),
            "frames_received": overlap_peers[node_id]["frames_received"]
            - before_peers.get(node_id, {}).get("frames_received", 0),
            "applied_operation_count": overlap_peers[node_id]["applied_operation_count"]
            - before_peers.get(node_id, {}).get("applied_operation_count", 0),
        }
        for node_id in sorted(overlap_peers)
    }
    if any(
        frame_deltas[node_id]["frames_sent"] <= 0
        or frame_deltas[node_id]["frames_received"] <= 0
        or frame_deltas[node_id]["applied_operation_count"] <= 0
        for node_id in expected_working_nodes
    ):
        raise GateError("selected_placement_movement_unproven")
    in_flight_kv = {
        node_id: overlap_peers[node_id]["active_kv_state_count"]
        - before_peers.get(node_id, {}).get("active_kv_state_count", 0)
        for node_id in overlap_peers
    }
    kv_deltas = {
        node_id: after_peers[node_id]["active_kv_state_count"]
        - before_peers.get(node_id, {}).get("active_kv_state_count", 0)
        for node_id in after_peers
    }

    final_requests = _request_map(after_runtime)
    if any(item["request_id"] not in final_requests for item in accepted):
        raise GateError("terminal_request_record_missing")

    return {
        "protocol": "mycelium.a5_product_positive_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "deployment_id": after_runtime["deployment_id"],
        "deployment_epoch": after_runtime["deployment_epoch"],
        "topology_generation": after_runtime["topology_version"],
        "model_id": binding.get("model_id"),
        "resolved_commit": binding.get("resolved_commit"),
        "manifest_digest": binding.get("manifest_digest"),
        "qualification_digest": binding.get("qualification_digest"),
        "path_manifest_digest": binding.get("path_manifest_digest"),
        "graph_digest": after_runtime["graph_digest"],
        "request_ids": sorted(request_ids),
        "distinct_tracks": True,
        "track_placements": {
            item["request_id"]: list(track_placements[index])
            for index, item in enumerate(accepted)
        },
        "replica_placement_ids": replica_placement_ids,
        "worked_placements": worked_placements,
        "overlap": {
            "active_request_ids": sorted(overlap["queue"]["active_request_ids"]),
            "maximum_active_requests": overlap["queue"]["maximum_active_requests"],
            "proof_owner_protocol": overlap["protocol"],
            "placement_reservation_deltas": overlap_reservation_deltas,
        },
        "streams": [
            {
                key: value
                for key, value in summaries[index].items()
                if key != "terminal_at_monotonic_s"
            }
            for index in range(2)
        ],
        "peer_frame_deltas": frame_deltas,
        "working_nodes": working_nodes,
        "in_flight_stage_local_kv_state_deltas": in_flight_kv,
        "stage_local_kv_state_deltas": {
            node_id: kv_deltas[node_id] for node_id in sorted(kv_deltas)
        },
        "cleanup_zero_delta": all(delta == 0 for delta in kv_deltas.values()),
        "final_queue": after_runtime["queue"],
        "final_placement_reservations": {
            item["placement_id"]: item["active_reservations"]
            for item in after_runtime["placements"]
        },
    }


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    # 64 tokens keeps the overlap window observable when one track runs on a
    # slower replica host; the incumbent track can finish 32 tokens before
    # the replica request is admitted.
    parser.add_argument("--maximum-new-tokens", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.maximum_new_tokens <= 128:
        raise SystemExit("--maximum-new-tokens must be in [2, 128]")
    report = run_gate(args.base_url, maximum_new_tokens=args.maximum_new_tokens)
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
