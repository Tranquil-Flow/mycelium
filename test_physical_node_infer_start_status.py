# SPDX-License-Identifier: AGPL-3.0-only
"""``infer_start`` never reports a failed prefill as a started request.

The node answers ``infer_start`` with a signed ``inference_started``
observation.  That observation is success-shaped, so it may only carry a
request that is decoding, already completed, or cancelled by its owner.  A
request whose prefill ended ``FAILED`` executed no usable work and must raise a
named node error instead of returning ``FAILED`` with an empty output that a
caller could read as an empty completion.
"""

from __future__ import annotations

import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from mycelium_router.fakes import ManualClock
from mycelium_router.serialization import path_manifest_to_dict
from physical_inference_node import NodeCommandError, PhysicalNodeService
from test_router_numpy_qwen_kv import _case
from test_router_numpy_replay_locked_path import (
   _MAX_NEW_TOKENS,
   _PROMPT,
   _manifest,
   _mesh,
   _ports,
)


def _request_payload(request_id: str) -> dict[str, object]:
   return {
      "request_id": request_id,
      "prompt_token_ids": list(_PROMPT),
      "max_new_tokens": _MAX_NEW_TOKENS,
      "expected_new_tokens": _MAX_NEW_TOKENS,
      "qos_class": "interactive",
      "admitted_at": 0.0,
      "target_ttft_ms": 1_000.0,
      "target_tpot_ms": 100.0,
      "target_tokens_per_second": 10.0,
      "sampling_seed": 0,
      "generation_config_digest": "sha256:" + "c" * 64,
   }


def _service(*, router, runtime, graph) -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service.state = "RUNNING"
   service.router = router
   service.runtime = runtime
   service.graph = graph
   service.transport = None
   service.command_timeout = 5.0
   service._clock = ManualClock()
   service._sinks = {}
   service._control_lock = threading.RLock()
   service._request_controls = {}
   service._pending_cancellations = {}
   service.retired = []
   service._retire_legacy_terminal_path = service.retired.append
   service._signed_result = lambda event, details: {
      "event": event,
      "details": details,
   }
   return service


def _locked_start(case, *, chunk_size: int):
   ports = _ports(case, "complete_context_replay")
   routers = _mesh(case, ports, chunk_size=chunk_size)
   service = _service(
      router=routers["node-a"],
      runtime=ports["node-a"],
      graph=case.graph,
   )
   # Product control validation (deadlines, publisher identity) is covered by
   # its own suites; this test targets what happens after the locked prefill.
   service._validated_request_control = lambda data, **_: dict(data)
   request_id = f"request-node-replay-{chunk_size}"
   manifest = _manifest(case, request_id)
   manifest_data = path_manifest_to_dict(manifest)
   digest = "sha256:" + hashlib.sha256(
      json.dumps(manifest_data, sort_keys=True, separators=(",", ":")).encode()
   ).hexdigest()
   control = {
      "path_id": manifest.path_id,
      "path_attempt": manifest.path_attempt,
      "path_digest": digest,
      "cancellation_generation": 0,
   }
   payload = {
      "request": _request_payload(request_id),
      "control": control,
      "path_manifest": manifest_data,
   }
   return service, routers["node-a"], request_id, payload


@pytest.fixture(scope="module")
def qwen_case(tmp_path_factory):
   return _case(tmp_path_factory.mktemp("node-replay"), "qwen2")


def test_replay_locked_start_reports_a_decoding_request(qwen_case):
   service, router, request_id, payload = _locked_start(qwen_case, chunk_size=0)

   result = service._infer_start(payload)

   assert result["event"] == "inference_started"
   assert result["details"]["status"] == "DECODING"
   assert router.request_status(request_id) == "DECODING"
   # Replay produces the first token by a complete-context decode.
   assert router.decode_one_distributed(request_id)
   assert len(service._sinks[request_id].token_ids) == 1


def test_failed_locked_prefill_raises_instead_of_returning_failed(qwen_case):
   service, router, request_id, payload = _locked_start(qwen_case, chunk_size=2)

   with pytest.raises(NodeCommandError) as raised:
      service._infer_start(payload)

   assert raised.value.code == "inference_start_failed"
   assert router.request_status(request_id) == "FAILED"


class _TerminalRouter:
   def __init__(self, status: str) -> None:
      self.status = status

   def start_distributed_prefill(self, request, sink, **_):
      return request.request_id

   def request_status(self, request_id):
      return self.status

   def get_request(self, request_id):
      hop = SimpleNamespace(placement_id="placement-000")
      return SimpleNamespace(
         manifest=SimpleNamespace(
            path_id="path-legacy",
            path_attempt=0,
            ordered_hops=(hop,),
         )
      )


def test_legacy_failed_start_retires_the_path_and_raises():
   service = _service(
      router=_TerminalRouter("FAILED"),
      runtime=SimpleNamespace(decode_mode="complete_context_replay"),
      graph=None,
   )

   with pytest.raises(NodeCommandError) as raised:
      service._infer_start({"request": _request_payload("request-legacy-failed")})

   assert raised.value.code == "inference_start_failed"
   assert service.retired == ["request-legacy-failed"]


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
def test_legacy_terminal_success_statuses_are_still_reported(status):
   """Negative control: completed and owner-cancelled starts stay observable."""

   service = _service(
      router=_TerminalRouter(status),
      runtime=SimpleNamespace(decode_mode="complete_context_replay"),
      graph=None,
   )
   request_id = f"request-legacy-{status.lower()}"

   result = service._infer_start({"request": _request_payload(request_id)})

   assert result["event"] == "inference_started"
   assert result["details"]["status"] == status
   assert service.retired == [request_id]
