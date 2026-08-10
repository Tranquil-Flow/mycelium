from __future__ import annotations

from http.cookiejar import CookieJar
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
import uuid

import pytest

from mycelium_live.route import FakeLiveRoute, PhysicalLiveRoute, RouteCounters
from mycelium_live.supervisor import (
    STARTUP_OUTPUT,
    STARTUP_PROMPT,
    _DiscardSink,
    build_live_stack,
    create_server,
)
from mycelium_qualification import issue_live_route_qualification
from physical_inference_qualification import PeerIdentity, _peer_process_argv


PLAN = Path(
    "/Users/evinova-self/.hermes/missions/mycelium-distributed-inference-mvp"
    "/evidence/g4-live/w8-mvp-live-533d107-20260809t091035z/operator-plan.json"
)
STATIC_ROOT = Path(__file__).resolve().parents[2] / "ui" / "web" / "dist"
FORBIDDEN_RESPONSE_MARKERS = ("FakeLiveRoute", "fixture", "4599,3329,2506,5145")


def _require_physical(route: Any) -> None:
    if bool(getattr(route, "is_simulated", True)):
        raise AssertionError("physical_acceptance_rejects_simulated_route")


def test_physical_acceptance_gate_rejects_fake_route() -> None:
    with pytest.raises(AssertionError, match="rejects_simulated"):
        _require_physical(FakeLiveRoute(scripted_tokens=STARTUP_OUTPUT))


def _json_request(
    opener: Any,
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    document: dict[str, Any] | None = None,
    csrf: tuple[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    body = None
    if document is not None:
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if csrf is not None:
        headers["Origin"] = base_url
        headers[csrf[0]] = csrf[1]
    request = Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _stream_events(opener: Any, base_url: str, path: str) -> list[dict[str, Any]]:
    request = Request(
        base_url + path,
        headers={"Accept": "text/event-stream"},
        method="GET",
    )
    events: list[dict[str, Any]] = []
    with opener.open(request, timeout=180) as response:
        data_lines: list[str] = []
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if line.startswith("data: "):
                data_lines.append(line[6:])
            elif line == "" and data_lines:
                event = json.loads("\n".join(data_lines))
                events.append(event)
                data_lines = []
                if event.get("type") in {"completed", "cancelled", "failed"}:
                    break
    return events


def _submit(
    opener: Any,
    base_url: str,
    csrf: tuple[str, str],
    qualification: dict[str, Any],
    prompt: str,
) -> tuple[str, list[dict[str, Any]]]:
    status, accepted = _json_request(
        opener,
        base_url,
        "/api/v1/inference",
        method="POST",
        csrf=csrf,
        document={
            "protocol": "mycelium.request_gateway.v1",
            "prompt": prompt,
            "max_new_tokens": 8,
            "qualification": qualification["binding"],
        },
    )
    assert status == 202, accepted
    events = _stream_events(opener, base_url, accepted["event_path"])
    assert events[-1]["type"] == "completed", events
    return accepted["request_id"], events


def _increased(before: RouteCounters, after: RouteCounters) -> bool:
    return (
        after.frames_sent > before.frames_sent
        and after.frames_received > before.frames_received
        and after.applied_operation_count > before.applied_operation_count
    )


def _counter_record(value: RouteCounters) -> dict[str, Any]:
    return {
        "frames_sent": value.frames_sent,
        "frames_received": value.frames_received,
        "applied_operation_count": value.applied_operation_count,
        "fatal": value.fatal,
    }


def _terminate_remote_worker(route: PhysicalLiveRoute, plan: dict[str, Any]) -> None:
    peers = tuple(PeerIdentity(**item) for item in plan["controller"]["peers"])
    remote = next(peer for peer in peers if peer.process_transport == "ssh")
    process_id = route.process_id(remote.node_id)
    terminate_tree = (
        "import os,signal,sys;"
        "pid=int(sys.argv[1]);"
        "children=[int(path.split('/')[-2]) for path in __import__('glob').glob('/proc/[0-9]*/stat') "
        "if open(path,encoding='utf-8').read().split()[3]==str(pid)];"
        "[os.kill(child,signal.SIGTERM) for child in children];"
        "os.kill(pid,signal.SIGTERM)"
    )
    capture = subprocess.run(
        _peer_process_argv(remote, ("python3", "-c", terminate_tree, str(process_id))),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert capture.returncode == 0, capture.stderr.decode("utf-8", errors="replace")


@pytest.mark.skipif(
    os.environ.get("MYCELIUM_PHYSICAL") != "1",
    reason="set MYCELIUM_PHYSICAL=1 to run against real devices",
)
def test_two_http_prompts_then_route_loss_rejects_third_request() -> None:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    route = PhysicalLiveRoute.from_operator_plan(PLAN)
    server = None
    server_thread = None
    evidence: list[dict[str, Any]] = []
    try:
        identity = route.open()
        _require_physical(route)
        startup_request_id = f"startup-{uuid.uuid4().hex}"
        startup = route.infer(
            STARTUP_PROMPT,
            max_new_tokens=len(STARTUP_OUTPUT),
            request_id=startup_request_id,
            sink=_DiscardSink(),
        )
        assert startup.token_ids == STARTUP_OUTPUT
        qualification_record = issue_live_route_qualification(
            route.live_attestation(request_id=startup_request_id),
            expected_prompt_token_ids=STARTUP_PROMPT,
            expected_output_token_ids=STARTUP_OUTPUT,
        )
        stack = build_live_stack(
            route=route,
            deployment_dir=Path(plan["controller"]["source_root"]) / "deployment",
            execution_graph=route.execution_graph,
            bearer_token=secrets.token_urlsafe(32),
        )
        stack.health.publish(qualification_record)
        server = create_server(
            app=stack.app,
            route=route,
            static_root=STATIC_ROOT,
            host="127.0.0.1",
            port=0,
        )
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        boot_status, bootstrap = _json_request(opener, base_url, "/api/v1/bootstrap")
        assert boot_status == 200
        csrf = (
            bootstrap["session"]["csrf_header"],
            bootstrap["session"]["csrf_token"],
        )
        qualification_status, qualification = _json_request(
            opener, base_url, "/api/v1/qualification/current"
        )
        assert qualification_status == 200
        worker_process_ids = {
            peer["node_id"]: route.process_id(peer["node_id"])
            for peer in plan["controller"]["peers"]
        }

        for label in ("amber", "violet"):
            prompt = f"M4 physical acceptance {label} {uuid.uuid4().hex}"
            before = route.counters()
            request_id, events = _submit(opener, base_url, csrf, qualification, prompt)
            after = route.counters()
            attestation = route.live_attestation(request_id=request_id)
            decoded = "".join(
                event["text"] for event in events if event.get("type") == "token"
            )
            record = {
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "request_id": request_id,
                "prompt_token_ids": attestation["prompt_token_ids"],
                "output_token_ids": attestation["output_token_ids"],
                "decoded_text": decoded,
                "before": _counter_record(before),
                "after": _counter_record(after),
            }
            assert _increased(before, after), record
            assert tuple(attestation["output_token_ids"]) != STARTUP_OUTPUT
            serialized = json.dumps({"events": events, "attestation": attestation})
            assert not any(marker in serialized for marker in FORBIDDEN_RESPONSE_MARKERS)
            evidence.append(record)

        assert evidence[0]["request_id"] != evidence[1]["request_id"]
        assert evidence[0]["prompt_sha256"] != evidence[1]["prompt_sha256"]
        assert route.is_alive() is True
        assert identity.deployment_id == qualification["binding"]["deployment_id"]
        assert worker_process_ids == {
            peer["node_id"]: route.process_id(peer["node_id"])
            for peer in plan["controller"]["peers"]
        }

        _terminate_remote_worker(route, plan)
        deadline = time.monotonic() + 15
        while route.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert route.is_alive() is False
        third_status, third_error = _json_request(
            opener,
            base_url,
            "/api/v1/inference",
            method="POST",
            csrf=csrf,
            document={
                "protocol": "mycelium.request_gateway.v1",
                "prompt": f"M4 dropped route {uuid.uuid4().hex}",
                "max_new_tokens": 1,
                "qualification": qualification["binding"],
            },
        )
        assert third_status == 409
        assert third_error["code"] == "route_dropped"
        evidence_path = Path(
            os.environ.get(
                "MYCELIUM_ACCEPTANCE_EVIDENCE",
                "/tmp/mycelium-m4-physical-acceptance.json",
            )
        )
        evidence_path.write_text(
            json.dumps(
                {
                    "protocol": "mycelium.m4_physical_acceptance.v1",
                    "route_identity": {
                        "deployment_id": identity.deployment_id,
                        "model_id": identity.model_id,
                        "resolved_commit": identity.resolved_commit,
                        "endpoint_ids": list(identity.endpoint_ids),
                    },
                    "requests": evidence,
                    "route_loss_error": third_error["code"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        evidence_path.chmod(0o600)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        route.close()
        route.cleanup()
