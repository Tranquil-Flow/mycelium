# SPDX-License-Identifier: AGPL-3.0-only
"""``verifier-product``: the A10 verifier matrix driven through the product dispatcher.

The chain is real except the physical route and hosts: the driver's HTTP client ->
the grant-gated control -> ``LiveRouterPort.a10_target_verify`` (exclusive
admission) -> a scripted route that hands the command to
``PhysicalNodeService.dispatch`` exactly as the approved route method does -> the
node handler -> the in-process MLX target of ``mlx_case``.  Local composition only;
never physical evidence.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import json
import stat
from types import SimpleNamespace

import pytest

from mycelium_a10_verify_control import (
   A10VerifyControl,
   EXACT_FLEET_GRANT,
   GRANT_ENVIRONMENT,
)
from mycelium_live.route import FakeLiveRoute
from mycelium_live.router_port import LiveRouterPort
from physical_inference_node import NODE_CONTROL_PROTOCOL, PhysicalNodeService
from scripts import run_a10_physical_gate as gate
from tests.a10_deterministic.test_a10_product_verifier import ColocatedRuntime
from tests.a10_deterministic.test_runtime_target_verifier import mlx_case  # noqa: F401

DIGESTS = {
   "--runtime-digest": "sha256:" + "1" * 64,
   "--model-digest": "sha256:" + "2" * 64,
   "--representation-digest": "sha256:" + "3" * 64,
}
GRAPH = SimpleNamespace(
   deployment_id="deployment-a10-product",
   stages=(SimpleNamespace(placements=(SimpleNamespace(node_id="node-0"),)),),
)


class NodeRoute(FakeLiveRoute):
   """Carries the command to the node service like PhysicalLiveRoute.a10_target_verify."""

   def __init__(self, node: PhysicalNodeService) -> None:
      super().__init__(scripted_tokens=(1,))
      self.node = node
      self.sequence = 0

   def a10_target_verify(self, node_id, *, operation, deadline_monotonic_s):
      self.sequence += 1
      return self.node.dispatch({
         "protocol": NODE_CONTROL_PROTOCOL, "command_id": f"cmd-{self.sequence}",
         "run_id": "run-a10", "deployment_id": "deployment-a10",
         "command": "a10_target_verify", "payload": dict(operation)})


@pytest.fixture
def product(mlx_case, monkeypatch):
   runtime = ColocatedRuntime(mlx_case)
   node = object.__new__(PhysicalNodeService)
   node.state, node.runtime = "RUNNING", runtime
   node.run_id, node.deployment_id = "run-a10", "deployment-a10"
   route = NodeRoute(node)
   route.open()
   port = LiveRouterPort(route=route, execution_graph=GRAPH)
   control = A10VerifyControl(grant=EXACT_FLEET_GRANT, dispatch=port.a10_target_verify,
                              product_port=8787, port=0)
   control.start()
   monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
   yield SimpleNamespace(control=control, vocab=runtime.stages[0].vocab_size)
   control.close()


def _argv(product, out, **overrides):
   values = {
      "--control-url": product.control.status()["url"], "--node-id": "node-0",
      **DIGESTS, "--backend": "mlx", "--vocab-size": str(product.vocab),
      "--widths": "1,3,6", "--short-prefix": "2,3", "--long-prefix": "7,8",
      "--operations": "102", "--out": str(out),
   }
   values.update(overrides)
   return ["verifier-product", *[item for pair in values.items() for item in pair]]


def test_the_matrix_runs_through_the_product_dispatcher_and_is_decided(product, tmp_path, capsys):
   out = tmp_path / "verifier-product"
   assert gate.main(_argv(product, out)) == 0
   printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
   assert printed["matrix_passed"] is True
   # Local composition of a tiny model: the decider sees product-dispatched live
   # records, so nothing blocks; this is still not physical evidence.
   assert printed["blockers"] == []
   operations = json.loads((out / "verifier-operations.json").read_text("utf-8"))
   assert len(operations) == 102
   assert {record["execution_path"] for record in operations} == {"product_dispatcher"}
   negatives = json.loads((out / "verifier-negatives.json").read_text("utf-8"))
   assert [item["case_id"] for item in negatives] == [
      "stale_prefix", "unsupported_generation_policy", "position_bound",
      "cancellation_during_verification"]
   summary = json.loads((out / "verifier-summary.json").read_text("utf-8"))
   assert summary["subcommand"] == "verifier-product"
   assert summary["execution_path"] == "product_dispatcher"
   assert stat.S_IMODE(out.stat().st_mode) == 0o700
   for name in ("verifier-operations.json", "verifier-negatives.json",
                "verifier-qualification.json", "verifier-summary.json"):
      assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
   assert product.control.status()["dispatched_count"] == 106


def test_the_driver_refuses_without_the_grant_or_a_loopback_control(product, tmp_path, monkeypatch, capsys):
   monkeypatch.delenv(GRANT_ENVIRONMENT)
   assert gate.main(_argv(product, tmp_path / "a")) == 2
   assert "explicit_fleet_grant_required" in capsys.readouterr().err
   monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
   for url in ("http://10.0.0.1:9/a10/target-verify", "https://127.0.0.1:9/a10/target-verify",
               "http://127.0.0.1:9/other"):
      assert gate.main(_argv(product, tmp_path / "b", **{"--control-url": url})) == 2
      assert "control_url_invalid" in capsys.readouterr().err
   assert product.control.status()["dispatched_count"] == 0


def test_a_refused_operation_stops_the_run_without_a_decision(product, tmp_path, capsys):
   out = tmp_path / "refused"
   assert gate.main(_argv(product, out, **{"--node-id": "node-9"})) == 2
   assert "product_verification_refused:a10_verify_node_not_in_deployment" in capsys.readouterr().err
   assert not (out / "verifier-qualification.json").exists()
