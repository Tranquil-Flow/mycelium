#!/usr/bin/env python3
"""Exercise the M16 concurrent admission gate through the product API.

The report deliberately excludes prompts, decoded token payloads, cookies, and
CSRF material. It retains only public request identifiers, lifecycle states,
queue/resource projections, event counts, and physical route counters.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class GateError(RuntimeError):
    """Raised when the physical gate cannot prove a required invariant."""


class ProductSession:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self._bootstrap = self._json("GET", "/api/v1/bootstrap")
        self._qualification = self._json("GET", "/api/v1/qualification/current")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
        timeout: float = 120.0,
    ) -> urllib.response.addinfourl:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if mutate:
            session = self._bootstrap["session"]
            headers[session["csrf_header"]] = session["csrf_token"]
            headers["Origin"] = self.base_url
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8_192).decode("utf-8", "replace")
            raise GateError(f"{method} {path} failed: {exc.code} {detail}") from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
    ) -> dict[str, Any]:
        with self._request(method, path, body=body, mutate=mutate) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise GateError(f"{method} {path} returned a non-object")
        return document

    def submit(self, *, qos_class: str, maximum_new_tokens: int) -> dict[str, Any]:
        profile = (
            "interactive_chat_v1" if qos_class == "interactive" else "sustained_batch_v1"
        )
        return self._json(
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": f"M16 physical gate {qos_class} request",
                "max_new_tokens": maximum_new_tokens,
                "qualification": self._qualification["binding"],
                "workload_profile_id": profile,
                "qos_class": qos_class,
            },
            mutate=True,
        )

    def cancel(self, accepted: dict[str, Any]) -> dict[str, Any]:
        return self._json("DELETE", accepted["cancel_path"], mutate=True)

    def stream_summary(self, accepted: dict[str, Any]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        terminal: str | None = None
        with self._request("GET", accepted["event_path"], timeout=180.0) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "strict").rstrip("\r\n")
                if not line.startswith("event: "):
                    continue
                event_type = line[7:]
                counts[event_type] = counts.get(event_type, 0) + 1
                if event_type in {"completed", "cancelled", "failed"}:
                    terminal = event_type
        return {
            "request_id": accepted["request_id"],
            "event_counts": dict(sorted(counts.items())),
            "terminal_event": terminal,
        }


def public_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=10.0) as response:
        document = json.load(response)
    if not isinstance(document, dict):
        raise GateError(f"{path} returned a non-object")
    return document


def wait_for(predicate, *, timeout: float, interval: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = predicate()
        if latest:
            return latest
        time.sleep(interval)
    raise GateError(f"timed out waiting for gate state; latest={latest!r}")


def request_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["request_id"]: item for item in status["requests"]}


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
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


def run_gate(base_url: str) -> dict[str, Any]:
    sessions = [ProductSession(base_url) for _ in range(3)]
    before = public_json(base_url, "/__mycelium/live-status")
    before_runtime = public_json(base_url, "/__mycelium/m16-runtime-status")
    if before_runtime["queue"]["depth"] != 0:
        raise GateError("M16 queue was not empty before the gate")

    accepted_active = sessions[0].submit(qos_class="batch", maximum_new_tokens=16)
    stream_results: dict[str, dict[str, Any]] = {}
    stream_errors: list[str] = []

    def attach(name: str, index: int, accepted: dict[str, Any]) -> None:
        try:
            stream_results[name] = sessions[index].stream_summary(accepted)
        except BaseException as exc:  # report thread failures through the gate result
            stream_errors.append(f"{name}:{type(exc).__name__}:{exc}")

    active_thread = threading.Thread(
        target=attach, args=("active_batch", 0, accepted_active), daemon=True
    )
    active_thread.start()
    wait_for(
        lambda: (
            status
            if (status := public_json(base_url, "/__mycelium/m16-runtime-status"))[
                "queue"
            ]["active_request_id"]
            == accepted_active["request_id"]
            else None
        ),
        timeout=10.0,
    )

    accepted_batch = sessions[1].submit(qos_class="batch", maximum_new_tokens=8)
    accepted_interactive = sessions[2].submit(
        qos_class="interactive", maximum_new_tokens=8
    )
    batch_thread = threading.Thread(
        target=attach, args=("queued_batch", 1, accepted_batch), daemon=True
    )
    interactive_thread = threading.Thread(
        target=attach,
        args=("queued_interactive", 2, accepted_interactive),
        daemon=True,
    )
    batch_thread.start()
    interactive_thread.start()

    concurrent = wait_for(
        lambda: (
            status
            if (status := public_json(base_url, "/__mycelium/m16-runtime-status"))[
                "queue"
            ]["depth"]
            == 2
            and status["queue"]["batch_depth"] == 1
            and status["queue"]["interactive_depth"] == 1
            else None
        ),
        timeout=10.0,
    )
    resources_during = {
        item["placement_id"]: item["active_reservations"]
        for item in concurrent["placements"]
    }
    if set(resources_during.values()) != {3}:
        raise GateError(f"full-path reservations not observed: {resources_during}")

    priority = wait_for(
        lambda: (
            status
            if (status := public_json(base_url, "/__mycelium/m16-runtime-status"))[
                "queue"
            ]["active_request_id"]
            == accepted_interactive["request_id"]
            and request_map(status)[accepted_batch["request_id"]]["phase"] == "queued"
            else None
        ),
        timeout=90.0,
        interval=0.2,
    )
    cancelled_at = time.monotonic()
    cancel_response = sessions[1].cancel(accepted_batch)
    cancelled = wait_for(
        lambda: (
            status
            if (
                request := request_map(
                    status := public_json(
                        base_url, "/__mycelium/m16-runtime-status"
                    )
                ).get(accepted_batch["request_id"])
            )
            and request["phase"] == "cancelled"
            and request["reservation_count"] == 0
            else None
        ),
        timeout=10.0,
    )
    cancellation_release_ms = (time.monotonic() - cancelled_at) * 1_000.0

    for thread in (active_thread, batch_thread, interactive_thread):
        thread.join(timeout=180.0)
        if thread.is_alive():
            raise GateError(f"stream thread did not terminate: {thread.name}")
    if stream_errors:
        raise GateError("; ".join(stream_errors))

    after_runtime = wait_for(
        lambda: (
            status
            if (status := public_json(base_url, "/__mycelium/m16-runtime-status"))[
                "queue"
            ]["depth"]
            == 0
            and status["queue"]["active_request_id"] is None
            and all(item["active_reservations"] == 0 for item in status["placements"])
            else None
        ),
        timeout=10.0,
    )
    after = public_json(base_url, "/__mycelium/live-status")
    requests = request_map(after_runtime)
    return {
        "protocol": "mycelium.m16_physical_gate.v1",
        "claim": "concurrent_physical_observed",
        "deployment_id": after_runtime["deployment_id"],
        "graph_digest": after_runtime["graph_digest"],
        "topology_version": after_runtime["topology_version"],
        "request_ids": {
            "active_batch": accepted_active["request_id"],
            "queued_batch": accepted_batch["request_id"],
            "queued_interactive": accepted_interactive["request_id"],
        },
        "before_counters": before["counters"],
        "after_counters": after["counters"],
        "concurrent_queue": concurrent["queue"],
        "concurrent_active_reservations": resources_during,
        "priority_observation": {
            "active_request_id": priority["queue"]["active_request_id"],
            "queued_batch_phase": request_map(priority)[accepted_batch["request_id"]][
                "phase"
            ],
        },
        "cancellation": {
            "response": cancel_response,
            "release_latency_ms": cancellation_release_ms,
            "terminal_phase": request_map(cancelled)[accepted_batch["request_id"]][
                "phase"
            ],
            "reservation_count": request_map(cancelled)[accepted_batch["request_id"]][
                "reservation_count"
            ],
        },
        "terminal_requests": {
            name: {
                "phase": requests[summary["request_id"]]["phase"],
                "qos_class": requests[summary["request_id"]]["qos_class"],
                "admission_latency_ms": (
                    requests[summary["request_id"]]["queued_at_monotonic_s"]
                    - requests[summary["request_id"]]["admitted_at_monotonic_s"]
                )
                * 1_000.0,
                "queue_wait_ms": requests[summary["request_id"]]["queue_wait_ms"],
                "lifecycle_ms": (
                    requests[summary["request_id"]]["terminal_at_monotonic_s"]
                    - requests[summary["request_id"]]["admitted_at_monotonic_s"]
                )
                * 1_000.0,
                **summary,
            }
            for name, summary in sorted(stream_results.items())
        },
        "final_queue": after_runtime["queue"],
        "final_active_reservations": {
            item["placement_id"]: item["active_reservations"]
            for item in after_runtime["placements"]
        },
        "batch_claim": after_runtime["batch_state"],
        "claim_boundary": after_runtime["claim_boundary"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8794")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_gate(args.base_url)
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
