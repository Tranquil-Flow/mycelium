#!/usr/bin/env python3
"""Capture and seal the M23 replay-versus-stage-local-KV physical gate."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


CAPTURE_PROTOCOL = "mycelium.m23_decode_capture.v1"
GATE_PROTOCOL = "mycelium.m23_heterogeneous_kv_gate.v1"
DEFAULT_PROMPT = "What is 6 plus 7? Answer with only the number."


class GateError(RuntimeError):
    """A stable physical gate failure."""


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
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


class ProductSession:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
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

    def json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
    ) -> dict[str, Any]:
        with self.request(method, path, body=body, mutate=mutate) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise GateError(f"{path}:non_object_response")
        return document

    def submit(self, prompt: str, maximum_new_tokens: int) -> dict[str, Any]:
        return self.json(
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
        )

    def stream(self, accepted: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"Accept": "text/event-stream"}
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", accepted["event_path"].lstrip("/")),
            headers=headers,
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
                        raise GateError("invalid_stream_event")
                    events.append(event)
                    data_lines = []
                    if event.get("type") in {"completed", "cancelled", "failed"}:
                        break
        if not events or events[-1].get("type") != "completed":
            raise GateError(f"inference_not_completed:{events[-1:]}")
        return events


def _public_json(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=30.0) as response:
        document = json.load(response)
    if not isinstance(document, dict):
        raise GateError(f"{path}:non_object_response")
    return document


def _peer_map(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(peer["node_id"]): peer for peer in status["peers"]}


def _counter_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    before_peers = _peer_map(before)
    after_peers = _peer_map(after)
    fields = (
        "frames_sent",
        "frames_received",
        "applied_operation_count",
        "prefill_operation_count",
        "prefill_input_token_count",
        "decode_operation_count",
        "decode_input_token_count",
        "activation_output_bytes",
    )
    return {
        node_id: {
            field: int(peer.get(field, 0))
            - int(before_peers.get(node_id, {}).get(field, 0))
            for field in fields
        }
        for node_id, peer in sorted(after_peers.items())
    }


def capture(
    *,
    base_url: str,
    expected_mode: str,
    output: Path,
    prompt: str,
    maximum_new_tokens: int,
) -> dict[str, Any]:
    session = ProductSession(base_url)
    before = _public_json(base_url, "/__mycelium/live-status")
    if before.get("decode_mode") != expected_mode:
        raise GateError(
            f"decode_mode_mismatch:{before.get('decode_mode')}:{expected_mode}"
        )
    accepted = session.submit(prompt, maximum_new_tokens)
    events = session.stream(accepted)
    after = _public_json(base_url, "/__mycelium/live-status")
    if after.get("decode_mode") != expected_mode:
        raise GateError("decode_mode_changed_during_capture")
    token_events = [event for event in events if event.get("type") == "token"]
    output_text = "".join(str(event.get("text", "")) for event in token_events)
    if not after.get("recent_inferences"):
        raise GateError("missing_recent_inference_timing")
    latest = after["recent_inferences"][-1]
    peers = _peer_map(after)
    document: dict[str, Any] = {
        "protocol": CAPTURE_PROTOCOL,
        "captured_at_unix_ms": int(time.time() * 1_000),
        "mode": expected_mode,
        "route": {
            "route_identity_digest": after["route_identity_digest"],
            "deployment_id": after["deployment_id"],
            "model_id": after["model_id"],
            "topology_version": after["topology_version"],
            "stages": after["stages"],
        },
        "request": {
            "request_id": accepted["request_id"],
            "prompt": prompt,
            "prompt_digest": _digest(prompt),
            "maximum_new_tokens": maximum_new_tokens,
            "output_text": output_text,
            "output_digest": _digest(output_text),
            "output_token_count": len(token_events),
            "event_types": [str(event.get("type")) for event in events],
        },
        "timing": latest,
        "counter_deltas": _counter_delta(before, after),
        "terminal_kv": {
            node_id: {
                "backend": peer["placements"][0]["runtime_backend"],
                "architecture": peer.get("architecture"),
                "active_state_count": int(peer.get("active_kv_state_count", 0)),
                "active_kv_bytes": int(peer.get("active_kv_bytes", 0)),
                "peak_kv_bytes": int(peer.get("peak_kv_bytes", 0)),
                "release_state": peer.get("release_state", "unknown"),
                "last_release_reason": peer.get("last_release_reason"),
            }
            for node_id, peer in sorted(peers.items())
        },
        "fatal": after["counters"]["fatal"],
    }
    document["capture_digest"] = _digest(document)
    _atomic_json(output, document)
    return document


def _read_capture(path: Path, expected_mode: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateError(f"capture_unreadable:{path}") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol") != CAPTURE_PROTOCOL
        or document.get("mode") != expected_mode
    ):
        raise GateError(f"capture_invalid:{path}")
    detached = dict(document)
    digest = detached.pop("capture_digest", None)
    if digest != _digest(detached):
        raise GateError(f"capture_digest_invalid:{path}")
    return document


def derive_operator_plan(
    *, base_path: Path, mode: str, output: Path
) -> dict[str, Any]:
    try:
        document = json.loads(base_path.read_text("utf-8"))
        controller = document["controller"]
        run_plan = controller["run_plan"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateError(f"operator_plan_unreadable:{base_path}") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol") != "mycelium.physical_runner_operator_plan.v1"
        or not isinstance(controller, dict)
        or not isinstance(run_plan, dict)
        or run_plan.get("protocol") != "mycelium.controller_run_plan.v1"
        or mode not in {"complete_context_replay", "stage_local_kv"}
    ):
        raise GateError("operator_plan_invalid")
    derived = json.loads(json.dumps(document))
    derived["controller"]["run_plan"]["decode_mode"] = mode
    derived["plan_id"] = f"{document['plan_id']}-m23-{mode}"
    _atomic_json(output, derived)
    return derived


def seal(*, replay_path: Path, kv_path: Path, output: Path) -> dict[str, Any]:
    replay = _read_capture(replay_path, "complete_context_replay")
    kv = _read_capture(kv_path, "stage_local_kv")
    same_fields = ("deployment_id", "model_id", "topology_version", "stages")
    same_route = all(replay["route"][field] == kv["route"][field] for field in same_fields)
    same_request = all(
        replay["request"][field] == kv["request"][field]
        for field in ("prompt_digest", "maximum_new_tokens")
    )
    output_parity = (
        replay["request"]["output_text"] == kv["request"]["output_text"]
        and replay["request"]["output_token_count"]
        == kv["request"]["output_token_count"]
    )
    kv_nodes = kv["counter_deltas"]
    decode_operation_counts = {
        int(delta["decode_operation_count"]) for delta in kv_nodes.values()
    }
    one_token_decode = (
        len(decode_operation_counts) == 1
        and next(iter(decode_operation_counts), 0) > 0
        and all(
            int(delta["decode_input_token_count"])
            == int(delta["decode_operation_count"])
            and int(delta["applied_operation_count"])
            == int(delta["prefill_operation_count"])
            + int(delta["decode_operation_count"])
            for delta in kv_nodes.values()
        )
    )
    physical_counters = all(
        int(delta["applied_operation_count"]) > 0
        and int(delta["frames_sent"]) > 0
        and int(delta["frames_received"]) > 0
        for delta in kv_nodes.values()
    )
    kv_cleanup = all(
        int(state["active_state_count"]) == 0
        and int(state["active_kv_bytes"]) == 0
        and int(state["peak_kv_bytes"]) > 0
        and state["release_state"] == "released"
        and state["last_release_reason"]
        in {"normal_completion", "cancellation", "cancelled"}
        for state in kv["terminal_kv"].values()
    )
    no_fatal = replay["fatal"] is None and kv["fatal"] is None
    replay_tpot = float(replay["timing"]["tpot_ms"])
    kv_tpot = float(kv["timing"]["tpot_ms"])
    replay_activation = sum(
        int(delta["activation_output_bytes"])
        for delta in replay["counter_deltas"].values()
    )
    kv_activation = sum(
        int(delta["activation_output_bytes"])
        for delta in kv["counter_deltas"].values()
    )
    performance_qualified = kv_tpot < replay_tpot
    gates = {
        "same_route_model_stages_hosts": same_route,
        "same_prompt_and_budget": same_request,
        "exact_output_parity": output_parity,
        "one_token_decode_every_stage": one_token_decode,
        "all_stages_advanced_physical_counters": physical_counters,
        "kv_active_then_terminally_released": kv_cleanup,
        "no_fatal_or_cleanup_failure": no_fatal,
        "measured_tpot_improvement": performance_qualified,
    }
    implemented = all(
        value
        for key, value in gates.items()
        if key != "measured_tpot_improvement"
    )
    document: dict[str, Any] = {
        "protocol": GATE_PROTOCOL,
        "generated_at_unix_ms": int(time.time() * 1_000),
        "replay_capture_digest": replay["capture_digest"],
        "kv_capture_digest": kv["capture_digest"],
        "gates": gates,
        "implemented": implemented,
        "performance_qualified": implemented and performance_qualified,
        "promotion_state": (
            "qualified"
            if implemented and performance_qualified
            else "implemented_not_performance_qualified"
            if implemented
            else "withheld"
        ),
        "measurements": {
            "replay_tpot_ms": replay_tpot,
            "kv_tpot_ms": kv_tpot,
            "tpot_delta_ms": kv_tpot - replay_tpot,
            "tpot_improvement_ratio": (
                (replay_tpot - kv_tpot) / replay_tpot if replay_tpot > 0 else 0.0
            ),
            "replay_activation_output_bytes": replay_activation,
            "kv_activation_output_bytes": kv_activation,
            "activation_byte_delta": kv_activation - replay_activation,
            "replay_total_ms": float(replay["timing"]["total_ms"]),
            "kv_total_ms": float(kv["timing"]["total_ms"]),
        },
        "claim_boundary": (
            "One fixed-prompt A/B on the same three physical hosts and stage allocation; "
            "performance qualification is limited to this measured route and workload."
        ),
    }
    document["evidence_digest"] = _digest(document)
    _atomic_json(output, document)
    if not implemented:
        raise GateError(f"m23_gate_withheld:{json.dumps(gates, sort_keys=True)}")
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--base-url", required=True)
    capture_parser.add_argument(
        "--mode",
        required=True,
        choices=("complete_context_replay", "stage_local_kv"),
    )
    capture_parser.add_argument("--output", type=Path, required=True)
    capture_parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    capture_parser.add_argument("--maximum-new-tokens", type=int, default=4)
    derive_parser = subparsers.add_parser("derive-plan")
    derive_parser.add_argument("--base", type=Path, required=True)
    derive_parser.add_argument(
        "--mode",
        required=True,
        choices=("complete_context_replay", "stage_local_kv"),
    )
    derive_parser.add_argument("--output", type=Path, required=True)
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--replay", type=Path, required=True)
    seal_parser.add_argument("--kv", type=Path, required=True)
    seal_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        if not 2 <= args.maximum_new_tokens <= 32:
            raise GateError("maximum_new_tokens_out_of_range")
        result = capture(
            base_url=args.base_url,
            expected_mode=args.mode,
            output=args.output,
            prompt=args.prompt,
            maximum_new_tokens=args.maximum_new_tokens,
        )
    elif args.command == "derive-plan":
        result = derive_operator_plan(
            base_path=args.base,
            mode=args.mode,
            output=args.output,
        )
    else:
        result = seal(replay_path=args.replay, kv_path=args.kv, output=args.output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
