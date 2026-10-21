# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the phone control channel's signed-observation golden.

The phone answers ``mycelium.physical_node_control.v1`` commands with
observations signed by its member key (``native/mobile_core/src/
node_control.rs``). This script builds the observations a stage-only phone
returns for a fixed session (``hello``, phone-profile ``configure``,
``start``, ``bind_request_control``, ``stop``), signs each with the real
Python ``Ed25519EvidenceSigner`` over the same fixed key, and checks every one
with the controller's own acceptance rules (the 13-field observation schema
and ``build_ed25519_verifier``). The Rust node must reproduce every signed
result byte for byte (``native/mobile_core/tests/phone_node_control.rs``).

Run from the repository root:
    python3 -B scripts/generate_phone_node_control_golden.py [--check]
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_qualification.signing import Ed25519EvidenceSigner, build_ed25519_verifier  # noqa: E402

OUTPUT = ROOT / "contracts" / "phone-node-control-golden" / "index.json"
SCHEMA = "mycelium.phone_node_control.golden.v1"
CONTROL = "mycelium.physical_node_control.v1"
OBSERVATION = "mycelium.physical_node_observation.v1"
SEED = bytes([9]) * 32
IDENTITY = {"node_id": "node-phone", "host_id": "host-phone", "process_id": "process-1", "endpoint_id": "endpoint-phone"}
RUN = {"run_id": "run-1", "deployment_id": "dep-1"}
#: physical_inference_qualification.py: the fields a controller accepts.
OBSERVATION_FIELDS = {
    "protocol", "event", "monotonic_ns", "run_id", "deployment_id", "node_id", "host_id",
    "process_id", "endpoint_id", "peer_generation", "state", "route_ready", "details",
}


def _signer() -> Ed25519EvidenceSigner:
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    return Ed25519EvidenceSigner(
        endpoint_id=IDENTITY["endpoint_id"], _private_key=key, _public_key_bytes=key.public_key().public_bytes_raw()
    )


def _command(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"protocol": CONTROL, "command_id": f"c-{command}", **RUN, "command": command, "payload": payload}


def _signed(signer, event: str, state: str, peer_generation: int, monotonic_ns: int, details: dict[str, Any]) -> dict[str, Any]:
    statement = {
        "protocol": OBSERVATION, "event": event, "monotonic_ns": monotonic_ns, **RUN, **IDENTITY,
        "peer_generation": peer_generation, "state": state, "route_ready": False, "details": details,
    }
    signed = {"observation": statement, "signature": signer.sign(statement), "verification_key": signer.public_key_record()}
    # The controller's acceptance rules.
    assert set(statement) == OBSERVATION_FIELDS
    assert signed["signature"]["signer_endpoint_id"] == IDENTITY["endpoint_id"]
    assert build_ed25519_verifier([signed["verification_key"]])(canonical_json_bytes(statement), dict(signed["signature"])) is True
    return signed


def _ok(command_id: str, result: Any) -> dict[str, Any]:
    return {"protocol": CONTROL, "command_id": command_id, "node_id": IDENTITY["node_id"], "ok": True, "route_ready": False, "result": result}


def generate(check: bool = False) -> dict[str, Any]:
    signer = _signer()
    endpoint_addr = {"id": "endpoint-phone", "addrs": []}
    control = {
        "deployment_id": "dep-1", "deployment_epoch": 1, "qualification_digest": "sha256:q", "command_id": "cmd",
        "publisher_generation": 0, "absolute_deadline_ms": 10, "request_attempt": 0, "path_id": "p",
        "path_attempt": 0, "path_digest": "sha256:p", "topology_generation": 1, "cancellation_generation": 0,
    }
    peer = {"node_id": "node-mac-final", "endpoint_id": "endpoint-final", "endpoint_addr": {"id": "endpoint-final", "addrs": []}, "generation": 3}
    configure = {"assignment_id": "assignment-1", "stage_id": "stage-phone", "pack_digest": "sha256:pack", "manifest_digest": "sha256:graph", "load_generation": 1}
    steps = [
        (_command("hello", {}), _ok("c-hello", {**RUN, **IDENTITY, "peer_generation": 0, "state": "NEW", "route_ready": False, "protocol": CONTROL, "boot_id": "boot-1"})),
        (_command("configure", configure), _ok("c-configure", _signed(signer, "configured", "CONFIGURED", 0, 7, {
            "assignment_id": "assignment-1", "stage_id": "stage-phone", "manifest_digest": "sha256:graph",
            "endpoint_addr": endpoint_addr, "runtime_mode": "complete_context_replay",
            "phone_stage_pack_digest": "sha256:pack", "load_generation": 1,
        }))),
        (_command("start", {"peer": peer, "local_generation": 5}), _ok("c-start", _signed(signer, "started", "RUNNING", 5, 7, {
            "peer_node_id": "node-mac-final", "peer_node_ids": ["node-mac-final"], "local_generation": 5,
        }))),
        (_command("bind_request_control", {"request_id": "r", "control": control}), _ok("c-bind_request_control", _signed(signer, "request_control_bound", "RUNNING", 5, 7, {
            "request_id": "r", "control_digest": "sha256:" + hashlib.sha256(canonical_json_bytes(control)).hexdigest(),
        }))),
        (_command("infer_start", {}), {"protocol": CONTROL, "command_id": "c-infer_start", "node_id": "node-phone", "ok": False, "route_ready": False, "error": {"code": "stage_only_node"}}),
        (_command("stop", {}), _ok("c-stop", {"state": "STOPPING", "final_observation": _signed(signer, "stopping", "STOPPING", 5, 7, {})})),
    ]
    index = {
        "schema": SCHEMA,
        "seed_hex": SEED.hex(),
        "identity": {**IDENTITY, "boot_id": "boot-1"},
        "monotonic_ns": 7,
        "endpoint_addr": endpoint_addr,
        "stage": {"assignment_id": "assignment-1", "stage_id": "stage-phone", "pack_digest": "sha256:pack", "backend": "mobile-rust-qwen2"},
        "steps": [
            {"command": command, "response_canonical": canonical_json_bytes(response).decode("utf-8")}
            for command, response in steps
        ],
    }
    rendered = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if check:
        if OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit("phone node control golden is stale; regenerate it")
        return index
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    return index


if __name__ == "__main__":
    result = generate(check="--check" in sys.argv[1:])
    print(f"phone node control golden: {len(result['steps'])} steps")
