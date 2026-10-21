# SPDX-License-Identifier: AGPL-3.0-only
"""A14 serve-2 (f9a8d961 r1): a FailureReport must say why, where and when.

r1's final node sent a ``FailureReport`` ~1.2 s after it dispatched prefill
chunk 5 and the entry failed ``route_qualify`` with ``inference_start_failed``.
Neither node's evidence bundle, stderr tail nor transport history carried the
report's ``reason``, the hop that failed or the chunk: the frame history kept
only the frame kind (``FailureReport``) and the cause chain only the node's
error code.  The root cause could only be inferred.

* Each node's transport ``diagnostic_state`` (the evidence bundle's
  ``transport``) keeps the last FailureReports it sent and received: reason,
  scope, failing placement, chunk/step index, peer and delivery outcome - never
  a request or path id.
* Both nodes print one ``MYCELIUM-FAILURE-REPORT`` line per report (kept by the
  bundle's redaction), so the reason reaches each node's stderr tail.
* A failed ``infer_start`` prints ``MYCELIUM-INFERENCE-START-FAILED`` naming the
  entry's last received report (reason, source node, stage, placement, chunk)
  before ``inference_start_failed`` is raised; the route's ``route_qualify``
  cause chain carries it as that error's ``node_stderr_tail``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import physical_inference_node as node_module
from mycelium_live.supervisor import _exception_chain_document
from mycelium_node.process import _RemoteNodeError
from mycelium_router.contracts import FailureReport
from mycelium_router.wire import encode_frame
from physical_inference_node import NodeCommandError
from test_iroh_prefill_send_lock_stall import _Router
from test_iroh_prefill_stall_recovery import (  # noqa: F401 - fixture re-export
   _Wire,
   _close_quiesced,
   _transport,
   _wait_for,
   scaled_policy,
)

_REQUEST_ID = "request-a14-serve-2-secret-id"
_PATH_ID = "path-a14-serve-2-secret-id"


def _report(**overrides: Any) -> FailureReport:
   values: dict[str, Any] = {
      "request_id": _REQUEST_ID,
      "path_id": _PATH_ID,
      "path_attempt": 0,
      "token_index": 5,
      "scope": "PLACEMENT",
      "reason": "kv_lease_expired",
      "placement_id": "placement-node-2-stage-1",
      "node_id": "node-b",
   }
   values.update(overrides)
   return FailureReport(**values)


def test_both_nodes_record_the_failure_report_reason_hop_and_chunk(
   scaled_policy: Any,
) -> None:
   del scaled_policy
   wire = _Wire()
   entry = _transport(wire, "node-a")
   final = _transport(wire, "node-b")
   entry_router, final_router = _Router(), _Router()
   entry.bind_router(entry_router)
   final.bind_router(final_router)
   observed: dict[str, list[tuple[str, dict[str, Any]]]] = {"node-a": [], "node-b": []}
   for node_id, transport in (("node-a", entry), ("node-b", final)):
      transport.failure_report_observer = (
         lambda direction, fields, *, request_id=None, _node=node_id: observed[_node].append(
            (direction, dict(fields))
         )
      )
   entry.start()
   final.start()
   try:
      final.send_router_frame(encode_frame(_report()), destination_node_id="node-a")
      assert _wait_for(lambda: bool(entry_router.failures), 3.0)

      expected = {
         "scope": "PLACEMENT",
         "reason": "kv_lease_expired",
         "placement_id": "placement-node-2-stage-1",
         "token_index": 5,
         "node_id": "node-b",
      }
      (sent,) = final.diagnostic_state()["failure_reports"]
      assert {key: sent[key] for key in expected} == expected
      assert (sent["direction"], sent["peer"], sent["outcome"]) == (
         "sent",
         "node-a",
         "confirmed",
      )
      (received,) = entry.diagnostic_state()["failure_reports"]
      assert {key: received[key] for key in expected} == expected
      assert (received["direction"], received["peer"]) == ("received", "node-b")
      assert isinstance(received["age_ms"], int)

      assert observed["node-b"] == [("sent", {"peer": "node-a", **expected})]
      assert observed["node-a"] == [("received", {"peer": "node-b", **expected})]
      for transport in (entry, final):
         state = json.dumps(transport.diagnostic_state())
         assert _REQUEST_ID not in state and _PATH_ID not in state
   finally:
      _close_quiesced(entry, final)


def test_an_unshaped_reason_is_reduced_to_a_digest_not_echoed(scaled_policy: Any) -> None:
   del scaled_policy
   wire = _Wire()
   entry = _transport(wire, "node-a")
   final = _transport(wire, "node-b")
   entry.bind_router(_Router())
   final.bind_router(_Router())
   entry.start()
   final.start()
   try:
      odd = "reason with spaces /Users/someone 10.0.0.1"
      final.send_router_frame(
         encode_frame(_report(reason=odd)), destination_node_id="node-a"
      )
      assert _wait_for(lambda: bool(entry.diagnostic_state()["failure_reports"]), 3.0)
      (received,) = entry.diagnostic_state()["failure_reports"]
      assert received["reason"].startswith("sha256:")
      assert odd not in json.dumps(entry.diagnostic_state())
   finally:
      _close_quiesced(entry, final)


def test_the_failure_report_line_survives_evidence_redaction(capsys) -> None:
   node_module._print_failure_report_event(
      "received",
      {
         "peer": "node-2",
         "scope": "PLACEMENT",
         "reason": "kv_lease_expired",
         "placement_id": "placement-node-2-stage-1",
         "token_index": 5,
         "node_id": "node-2",
      },
      request_id=_REQUEST_ID,
   )
   line = capsys.readouterr().err.strip()
   assert line == (
      "MYCELIUM-FAILURE-REPORT direction=received peer=node-2 scope=PLACEMENT "
      "reason=kv_lease_expired placement=placement-node-2-stage-1 token_index=5 "
      "node=node-2"
   )
   assert _REQUEST_ID not in line
   assert node_module.redact_evidence_text(line).strip() == line


# -- the failed start names its cause, and the route's cause chain carries it ------


@pytest.fixture(scope="module")
def qwen_case(tmp_path_factory):
   from test_router_numpy_qwen_kv import _case

   return _case(tmp_path_factory.mktemp("failure-report-diag"), "qwen2")


def test_a_failed_start_names_the_failure_report_and_the_cause_chain_carries_it(
   qwen_case, capsys
) -> None:
   from test_physical_node_infer_start_status import _locked_start

   # Complete-context replay refuses a partial prefill chunk at the first
   # stage (``prefill_chunk_requires_kv_continuity``): a runtime failure
   # reported to the entry as a FailureReport, exactly like r1's.
   service, router, request_id, payload = _locked_start(qwen_case, chunk_size=2)
   capsys.readouterr()
   with pytest.raises(NodeCommandError) as raised:
      service._infer_start(payload)
   assert raised.value.code == "inference_start_failed"

   cause = router.entry.last_failure_report(request_id)
   assert cause is not None
   assert cause["reason"] == "prefill_chunk_requires_kv_continuity"
   assert cause["applied"] is True
   stage = qwen_case.graph.stages[0]
   assert (cause["stage_id"], cause["placement_id"]) == (
      stage.stage_id,
      stage.placements[0].placement_id,
   )
   assert cause["token_index"] == 0

   lines = [
      line
      for line in capsys.readouterr().err.splitlines()
      if line.startswith("MYCELIUM-INFERENCE-START-FAILED ")
   ]
   assert lines == [
      "MYCELIUM-INFERENCE-START-FAILED status=FAILED cause=failure_report "
      f"source={cause['source_node_id']} scope={cause['scope']} "
      "reason=prefill_chunk_requires_kv_continuity "
      f"stage={stage.stage_id} placement={stage.placements[0].placement_id} "
      "token_index=0 path_attempt=0 applied=1"
   ]
   assert request_id not in lines[0]

   # The route raises the node's code with the node's stderr tail as detail;
   # the serve diagnostic's cause chain is built from exactly that.
   remote = _RemoteNodeError(
      "inference_start_failed",
      detail="node node-0 stderr (tail):\n" + lines[0],
   )
   (link,) = _exception_chain_document(remote)
   assert link["code"] == "inference_start_failed"
   assert "reason=prefill_chunk_requires_kv_continuity" in link["node_stderr_tail"]
   assert "token_index=0" in link["node_stderr_tail"]


def test_a_start_that_failed_without_a_report_says_so(capsys) -> None:
   from test_physical_node_infer_start_status import _TerminalRouter, _request_payload, _service
   from types import SimpleNamespace

   service = _service(
      router=_TerminalRouter("FAILED"),
      runtime=SimpleNamespace(decode_mode="complete_context_replay"),
      graph=None,
   )
   with pytest.raises(NodeCommandError):
      service._infer_start({"request": _request_payload("request-no-report")})
   assert "MYCELIUM-INFERENCE-START-FAILED status=FAILED cause=none" in capsys.readouterr().err
