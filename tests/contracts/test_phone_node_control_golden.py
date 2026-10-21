# SPDX-License-Identifier: AGPL-3.0-only
"""The phone control channel's signed-observation golden stays in step.

``scripts/generate_phone_node_control_golden.py`` signs the stage-only
phone's session observations with the real Python signer and checks each
against the controller's acceptance rules; regenerating must reproduce the
committed golden exactly, and ``native/mobile_core/tests/
phone_node_control.rs`` pins the phone to it byte for byte.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_regenerating_reproduces_the_committed_golden() -> None:
    spec = importlib.util.spec_from_file_location(
        "generate_phone_node_control_golden", ROOT / "scripts" / "generate_phone_node_control_golden.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    index = module.generate(check=True)
    commands = [step["command"]["command"] for step in index["steps"]]
    assert commands == ["hello", "configure", "start", "bind_request_control", "infer_start", "stop"]
    refused = json.loads(index["steps"][4]["response_canonical"])
    assert refused["ok"] is False and refused["error"] == {"code": "stage_only_node"}
    for step in index["steps"]:
        response = json.loads(step["response_canonical"])
        assert response["route_ready"] is False and response["node_id"] == "node-phone"
