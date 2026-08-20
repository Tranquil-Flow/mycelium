#!/usr/bin/env python3
"""Run the A4 overlapping-request/cancellation gate through the product HTTP API.

The output is a bounded privacy-reduced observation, not an A4 qualification. It
contains no prompt, decoded text, token IDs, cookies, CSRF values, hostnames,
addresses, paths, command lines, or exception strings.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


class GateError(RuntimeError):
    """Stable gate failure without response bodies or private material."""


class ProductSession:
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
                "prompt": f"A4 physical concurrency gate request {label}",
                "max_new_tokens": maximum_new_tokens,
                "qualification": self._qualification["binding"],
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            mutate=True,
        )

    def cancel(self, accepted: dict[str, Any]) -> dict[str, Any]:
        return self.json("DELETE", accepted["cancel_path"], mutate=True)

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
        # Stream may close without a terminal event when the route's scoped
        # liveness interrupts a request and the gateway refuses to publish a
        # terminal (fail-closed cleanup-unproven path). Surface that as a
        # stream-closed outcome with no terminal.
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

    def cross_session_stream_denied(self, event_path: str) -> bool:
        try:
            with self.request("GET", event_path, timeout=5.0):
                return False
        except GateError as error:
            return error.args[0] in {
                "http_get_401",
                "http_get_403",
                "http_get_404",
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


def _request_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["request_id"]: item for item in status["requests"]}


def _zero_live_resources(status: dict[str, Any]) -> bool:
    queue = status["queue"]
    return (
        queue["depth"] == 0
        and queue["active_request_ids"] == []
        and all(item["active_reservations"] == 0 for item in status["placements"])
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
    before = public_json(base_url, "/__mycelium/live-status")
    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    if before.get("route_alive") is not True or not _zero_live_resources(before_runtime):
        raise GateError("route_not_clean_and_alive")

    accepted = (
        sessions[0].submit(label="one", maximum_new_tokens=maximum_new_tokens),
        sessions[1].submit(label="two", maximum_new_tokens=maximum_new_tokens),
    )
    if accepted[0]["request_id"] == accepted[1]["request_id"]:
        raise GateError("duplicate_request_identity")
    if not sessions[1].cross_session_stream_denied(accepted[0]["event_path"]):
        raise GateError("cross_session_stream_visible")

    summaries: dict[int, dict[str, Any]] = {}
    failures: list[str] = []

    def stream(index: int) -> None:
        try:
            summaries[index] = sessions[index].stream_summary(accepted[index])
        except GateError as error:
            failures.append(str(error))
        except BaseException:
            failures.append("stream_worker_failed")

    threads = tuple(
        threading.Thread(target=stream, args=(index,), daemon=True)
        for index in range(2)
    )
    for thread in threads:
        thread.start()

    request_ids = {item["request_id"] for item in accepted}
    overlapping = wait_for(
        lambda: (
            status
            if request_ids
            <= set(
                (status := public_json(
                    base_url,
                    "/__mycelium/runtime/admission-status",
                ))["queue"]["active_request_ids"]
            )
            else None
        ),
        timeout=30.0,
    )
    cancellation_started = time.monotonic()
    cancellation_response = sessions[0].cancel(accepted[0])

    for thread in threads:
        thread.join(timeout=240.0)
        if thread.is_alive():
            raise GateError("stream_worker_join_timeout")
    if failures:
        raise GateError("stream_worker_failed")
    if summaries[0]["terminal"] != "cancelled":
        raise GateError("cancelled_request_terminal_invalid")
    if summaries[1]["terminal"] != "completed":
        raise GateError("unrelated_request_terminal_invalid")
    cancellation_total_ms = (
        summaries[0]["terminal_at_monotonic_s"] - cancellation_started
    ) * 1_000.0
    if not 0 <= cancellation_total_ms <= 2_000:
        raise GateError("cancellation_total_bound_exceeded")

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
    after = public_json(base_url, "/__mycelium/live-status")
    requests = _request_map(after_runtime)
    if any(request_id not in requests for request_id in request_ids):
        raise GateError("terminal_request_record_missing")
    return {
        "protocol": "mycelium.a4_product_positive_observation.v1",
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
        "overlap": {
            "active_request_ids": sorted(overlapping["queue"]["active_request_ids"]),
            "maximum_active_requests": overlapping["queue"]["maximum_active_requests"],
            "placement_reservations": {
                item["placement_id"]: item["active_reservations"]
                for item in overlapping["placements"]
            },
        },
        "cancellation": {
            "accepted_status": cancellation_response.get("status"),
            "total_observation_to_terminal_ms": cancellation_total_ms,
            "within_total_bound": True,
        },
        "streams": [
            {
                key: value
                for key, value in summaries[index].items()
                if key != "terminal_at_monotonic_s"
            }
            for index in range(2)
        ],
        "cross_session_stream_denied": True,
        "before_counters": before["counters"],
        "after_counters": after["counters"],
        "final_queue": after_runtime["queue"],
        "final_placement_reservations": {
            item["placement_id"]: item["active_reservations"]
            for item in after_runtime["placements"]
        },
        "runtime_batch_mode": after_runtime["batch_state"],
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
    parser.add_argument("--maximum-new-tokens", type=int, default=32)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.maximum_new_tokens <= 128:
        raise SystemExit("--maximum-new-tokens must be in [2, 128]")
    report = run_gate(
        args.base_url,
        maximum_new_tokens=args.maximum_new_tokens,
    )
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
