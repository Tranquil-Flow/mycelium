#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Seal the A2 cold/warm acquisition and post-fault physical product gate."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_swarm_artifacts import validate_acquisition_ledger


GATE_PROTOCOL = "mycelium.a2_product_gate.v1"
DEFAULT_PROMPT = "Reply with the single word Paris."
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BINDING_FIELDS = (
    "model_id",
    "model_revision",
    "representation",
    "representation_digest",
    "placement_id",
    "stage_id",
    "layer_start",
    "layer_end_exclusive",
    "total_bytes",
)


class GateError(RuntimeError):
    """A stable A2 product-gate failure."""


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class ProductSession:
    """A browser-equivalent, cookie- and CSRF-bound product session."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.bootstrap = self.json("GET", "/api/v1/bootstrap")
        self.qualification = self.json("GET", "/api/v1/qualification/current")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
        timeout: float = 300.0,
    ) -> Any:
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        if mutate:
            session = self.bootstrap["session"]
            headers[session["csrf_header"]] = session["csrf_token"]
            headers["Origin"] = self.base_url
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read(16_384).decode("utf-8", "replace")
            raise GateError(f"{method} {path} failed:{exc.code}:{detail}") from exc

    def json(self, method: str, path: str) -> dict[str, Any]:
        with self.request(method, path) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise GateError(f"{path}:non_object_response")
        return document

    def infer(self, *, prompt: str, maximum_new_tokens: int) -> dict[str, Any]:
        with self.request(
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": prompt,
                "max_new_tokens": maximum_new_tokens,
                "qualification": self.qualification["binding"],
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            mutate=True,
        ) as response:
            accepted = json.load(response)
        if not isinstance(accepted, dict):
            raise GateError("inference_acceptance_invalid")

        request = urllib.request.Request(
            urllib.parse.urljoin(
                self.base_url + "/", str(accepted["event_path"]).lstrip("/")
            ),
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        events: list[dict[str, Any]] = []
        with self.opener.open(request, timeout=300.0) as response:
            data_lines: list[str] = []
            for raw in response:
                line = raw.decode("utf-8", "strict").rstrip("\r\n")
                if line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "" and data_lines:
                    event = json.loads("\n".join(data_lines))
                    if not isinstance(event, dict):
                        raise GateError("inference_event_invalid")
                    events.append(event)
                    data_lines = []
                    if event.get("type") in {"completed", "cancelled", "failed"}:
                        break
        output = "".join(
            str(event.get("text", ""))
            for event in events
            if event.get("type") == "token"
        )
        return {
            "request_id": accepted.get("request_id"),
            "event_types": [event.get("type") for event in events],
            "terminal_state": events[-1].get("type") if events else None,
            "output": output,
            "output_token_count": sum(event.get("type") == "token" for event in events),
        }


def _binding(status: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(status[field] for field in _BINDING_FIELDS)


def _cold_ready(status: dict[str, Any], minimum_sources: int) -> bool:
    positive_sources = [
        source for source in status["sources"] if source["verified_bytes"] > 0
    ]
    return (
        status["state"] == "ready"
        and status["cached_verified_bytes"] == 0
        and status["transferred_verified_bytes"] == status["total_bytes"]
        and status["missing_bytes"] == 0
        and status["origin_bytes"] == 0
        and status["verified_chunk_count"] == status["chunk_count"]
        and status["eligible_source_count"] >= minimum_sources
        and len(positive_sources) >= minimum_sources
        and sum(source["verified_bytes"] for source in positive_sources)
        == status["total_bytes"]
        and status["promotion_digest"] is not None
    )


def _warm_ready(status: dict[str, Any]) -> bool:
    return (
        status["state"] == "ready"
        and status["cached_verified_bytes"] == status["total_bytes"]
        and status["transferred_verified_bytes"] == 0
        and status["missing_bytes"] == 0
        and status["origin_bytes"] == 0
        and status["verified_chunk_count"] == status["chunk_count"]
        and status["resumed_chunk_count"] == status["chunk_count"]
        and status["duplicate_bytes_prevented"] >= status["total_bytes"]
        and status["promotion_digest"] is not None
    )


def _select_pair(
    history: list[dict[str, Any]],
    *,
    model_id: str,
    stage_id: str,
    minimum_sources: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    candidates = [
        status
        for status in history
        if status["model_id"] == model_id and status["stage_id"] == stage_id
    ]
    for warm in reversed(candidates):
        if not _warm_ready(warm):
            continue
        for cold in reversed(candidates):
            if (
                _cold_ready(cold, minimum_sources)
                and cold["generation"] < warm["generation"]
                and cold["terminal_at_unix_ms"] <= warm["started_at_unix_ms"]
                and _binding(cold) == _binding(warm)
            ):
                return cold, warm
    return None, None


def _record(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "acquisition_id": status["acquisition_id"],
        "generation": status["generation"],
        "model_revision": status["model_revision"],
        "representation": status["representation"],
        "assignment_id": status["assignment_id"],
        "placement_id": status["placement_id"],
        "stage_id": status["stage_id"],
        "layer_start": status["layer_start"],
        "layer_end_exclusive": status["layer_end_exclusive"],
        "total_bytes": status["total_bytes"],
        "cached_verified_bytes": status["cached_verified_bytes"],
        "transferred_verified_bytes": status["transferred_verified_bytes"],
        "origin_bytes": status["origin_bytes"],
        "duplicate_bytes_prevented": status["duplicate_bytes_prevented"],
        "verified_chunk_count": status["verified_chunk_count"],
        "chunk_count": status["chunk_count"],
        "sources": status["sources"],
        "manifest_digest": status["manifest_digest"],
        "assignment_digest": status["assignment_digest"],
        "representation_digest": status["representation_digest"],
        "promotion_digest": status["promotion_digest"],
        "started_at_unix_ms": status["started_at_unix_ms"],
        "terminal_at_unix_ms": status["terminal_at_unix_ms"],
    }


def evaluate(
    *,
    ledger_document: object,
    before: dict[str, Any],
    after: dict[str, Any],
    inference: dict[str, Any],
    model_id: str,
    stage_id: str,
    minimum_sources: int,
    prompt: str,
) -> dict[str, Any]:
    ledger = validate_acquisition_ledger(ledger_document)
    cold, warm = _select_pair(
        ledger["history"],
        model_id=model_id,
        stage_id=stage_id,
        minimum_sources=minimum_sources,
    )
    before_counters = before.get("counters", {})
    after_counters = after.get("counters", {})
    sent_delta = int(after_counters.get("frames_sent", 0)) - int(
        before_counters.get("frames_sent", 0)
    )
    received_delta = int(after_counters.get("frames_received", 0)) - int(
        before_counters.get("frames_received", 0)
    )
    applied_delta = int(after_counters.get("applied_operation_count", 0)) - int(
        before_counters.get("applied_operation_count", 0)
    )
    stages = after.get("stages", [])
    expected_stage = any(
        isinstance(stage, dict) and stage.get("stage_id") == stage_id
        for stage in stages
    )
    output = str(inference.get("output", ""))
    recent = after.get("recent_inferences", [])
    latest = recent[-1] if isinstance(recent, list) and recent else None
    peer_deltas = (
        latest.get("peer_counter_deltas", []) if isinstance(latest, dict) else []
    )
    deltas_by_node = {
        delta.get("node_id"): delta
        for delta in peer_deltas
        if isinstance(delta, dict) and isinstance(delta.get("node_id"), str)
    }
    every_stage_advanced = bool(stages) and all(
        isinstance(stage, dict)
        and isinstance(stage.get("node_id"), str)
        and stage["node_id"] in deltas_by_node
        and int(deltas_by_node[stage["node_id"]].get("frames_sent", 0)) > 0
        and int(deltas_by_node[stage["node_id"]].get("frames_sent", 0))
        == int(deltas_by_node[stage["node_id"]].get("frames_received", 0))
        and int(deltas_by_node[stage["node_id"]].get("applied_operation_count", 0)) > 0
        for stage in stages
    )
    route_identity = after.get("route_identity_digest")
    deployment_id = after.get("deployment_id")
    request_id = inference.get("request_id")
    checks = {
        "ordinary_ledger_has_matching_cold_warm_pair": cold is not None
        and warm is not None,
        "cold_is_zero_origin_multi_source_full_transfer": cold is not None
        and _cold_ready(cold, minimum_sources),
        "warm_is_zero_origin_zero_transfer_full_reuse": warm is not None
        and _warm_ready(warm),
        "cold_and_warm_bind_same_assignment_scope": cold is not None
        and warm is not None
        and _binding(cold) == _binding(warm),
        "route_is_live_non_simulated_and_non_fatal": before.get("route_alive") is True
        and after.get("route_alive") is True
        and before.get("simulated") is False
        and after.get("simulated") is False
        and before_counters.get("fatal") is None
        and after_counters.get("fatal") is None,
        "route_identity_and_deployment_remained_stable": before.get(
            "route_identity_digest"
        )
        == route_identity
        and isinstance(route_identity, str)
        and _SHA256.fullmatch(route_identity) is not None
        and before.get("deployment_id") == after.get("deployment_id")
        and isinstance(deployment_id, str)
        and bool(deployment_id)
        and before.get("stages") == after.get("stages"),
        "expected_model_and_acquired_stage_are_serving": before.get("model_id")
        == model_id
        and after.get("model_id") == model_id
        and expected_stage
        and len(stages) >= 2,
        "physical_frame_and_operation_counters_advanced": sent_delta > 0
        and sent_delta == received_delta
        and applied_delta > 0,
        "latest_inference_advanced_every_serving_stage": every_stage_advanced,
        "fresh_product_inference_completed": inference.get("terminal_state")
        == "completed"
        and isinstance(request_id, str)
        and bool(request_id)
        and inference.get("output_token_count", 0) > 0
        and bool(output.strip()),
        "fixed_answer_is_correct": output.strip().rstrip(".").casefold() == "paris",
    }
    document: dict[str, Any] = {
        "protocol": GATE_PROTOCOL,
        "generated_at_unix_ms": int(time.time() * 1_000),
        "passed": all(checks.values()),
        "model_id": model_id,
        "recipient_stage_id": stage_id,
        "minimum_source_count": minimum_sources,
        "source_ledger_digest": _digest(ledger),
        "live_before_digest": _digest(before),
        "live_after_digest": _digest(after),
        "cold": _record(cold),
        "warm": _record(warm),
        "route": {
            "route_identity_digest": route_identity,
            "deployment_id": deployment_id,
            "stage_count": len(stages),
            "stage_ids": [stage.get("stage_id") for stage in stages],
            "frames_sent_delta": sent_delta,
            "frames_received_delta": received_delta,
            "applied_operation_count_delta": applied_delta,
            "latest_inference": latest,
        },
        "inference": {
            "request_id": inference.get("request_id"),
            "prompt": prompt,
            "prompt_digest": _digest(prompt),
            "output": output,
            "output_digest": _digest(output),
            "output_token_count": inference.get("output_token_count"),
            "terminal_state": inference.get("terminal_state"),
            "event_types": inference.get("event_types"),
        },
        "checks": checks,
        "claim_boundary": (
            "Executed after the documented A2 fault exercises; proves the ordinary "
            "product ledger's matching physical cold/warm recipient records and one "
            "fresh post-fault inference on the same live route."
        ),
    }
    document["evidence_digest"] = _digest(document)
    return document


def run(
    *,
    base_url: str,
    model_id: str,
    stage_id: str,
    minimum_sources: int,
    prompt: str,
    maximum_new_tokens: int,
) -> dict[str, Any]:
    session = ProductSession(base_url)
    before = session.json("GET", "/__mycelium/live-status")
    inference = session.infer(prompt=prompt, maximum_new_tokens=maximum_new_tokens)
    after = session.json("GET", "/__mycelium/live-status")
    ledger = session.json("GET", "/__mycelium/artifacts/acquisitions")
    return evaluate(
        ledger_document=ledger,
        before=before,
        after=after,
        inference=inference,
        model_id=model_id,
        stage_id=stage_id,
        minimum_sources=minimum_sources,
        prompt=prompt,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--minimum-sources", type=int, default=2)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--maximum-new-tokens", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.minimum_sources <= 64:
        raise GateError("minimum_sources_out_of_range")
    if not 1 <= args.maximum_new_tokens <= 32:
        raise GateError("maximum_new_tokens_out_of_range")
    result = run(
        base_url=args.base_url,
        model_id=args.model_id,
        stage_id=args.stage_id,
        minimum_sources=args.minimum_sources,
        prompt=args.prompt,
        maximum_new_tokens=args.maximum_new_tokens,
    )
    _write_private_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
