# SPDX-License-Identifier: AGPL-3.0-only
"""The A10 batch-policy switch, from the gate's mode command to every node.

Failure modes this suite catches (one per test, by name):

  test_refuses_to_mount_without_the_exact_grant
  test_refuses_a_proxied_or_public_product
  test_refuses_an_invalid_or_product_port
      A switch reachable without the fleet grant, behind a proxy or public
      origin, or on the product origin itself.
  test_forwards_one_well_formed_policy_and_returns_the_dispatcher_result
      A well-formed switch not reaching the product dispatcher, or reaching it
      altered.
  test_refuses_any_request_without_grant_path_method_or_with_forwarding
  test_refuses_a_malformed_body_before_dispatch
      Anything but an exact loopback, granted, well-formed policy reaching the
      dispatcher.
  test_a_dispatch_refusal_leaves_only_a_stable_code
      Exception text leaking from a refused switch.
  test_router_port_switches_every_deployment_node_serialized_with_admission
      A switch that misses a node, runs while a request is being admitted, or
      accepts a receipt for another policy.
  test_router_port_refuses_a_closed_or_dead_route_and_a_bad_policy
      A switch applied to a route that is not open.
  test_owner_forwards_only_to_the_ready_route
      A switch reaching a route that is restarting or gone.
  test_mount_hands_requests_to_the_product_dispatcher
  test_supervisor_main_forwards_the_port_only_when_requested
  test_demo_serve_forwards_the_port_to_live_but_fixture_rejects_it
      The serve not mounting the control when asked, or mounting it unasked.
  test_client_switches_and_verifies_every_node_receipt
  test_client_sets_and_verifies_the_candidate_window
  test_client_fails_on_a_refusal_a_partial_or_a_wrong_cap
      The gate's mode command succeeding although some node did not switch, or
      did not take the requested collection window.
  test_forwards_a_candidate_window_to_the_dispatcher
  test_the_window_bound_agrees_everywhere
      A window that the control, the router port and the node bound differently.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a10_batch_policy_control import (
   CONTROL_PATH,
   A10BatchPolicyControl,
   A10BatchPolicyControlRefused,
)
from mycelium_a10_verify_control import EXACT_FLEET_GRANT, GRANT_ENVIRONMENT
from mycelium_demo import cli
from mycelium_live import supervisor as supervisor_module
from mycelium_live.router_port import LiveRouterPort
from scripts import a10_batch_policy as client


def _post(port, body, *, grant=EXACT_FLEET_GRANT, path=CONTROL_PATH, method="POST",
          extra_headers=(), content_type="application/json"):
   raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
   lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}",
            f"Content-Type: {content_type}", f"Content-Length: {len(raw)}",
            "Connection: close"]
   if grant is not None:
      lines.append(f"X-Mycelium-A10-Fleet-Grant: {grant}")
   lines.extend(extra_headers)
   head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
   with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
      sock.sendall(head + raw)
      reply = bytearray()
      while chunk := sock.recv(65536):
         reply.extend(chunk)
   status = int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])
   return status, json.loads(bytes(reply).split(b"\r\n\r\n", 1)[1])


class Dispatcher:
   def __init__(self, result=None, error=None):
      self.calls = []
      self.result = result or {"policy": "baseline", "nodes": {"node-0": {
         "maximum_runtime_batch_size": 1}}}
      self.error = error

   def __call__(self, policy, *, timeout_s, **kwargs):
      self.calls.append((policy, timeout_s, *kwargs.values()))
      if self.error is not None:
         raise self.error
      return self.result


def _control(dispatcher=None, **overrides):
   values = {"grant": EXACT_FLEET_GRANT, "dispatch": dispatcher or Dispatcher(),
             "product_port": 8787, "port": 0}
   values.update(overrides)
   control = A10BatchPolicyControl(**values)
   control.start()
   return control


@pytest.mark.parametrize("grant", [None, "", "fleet granted to a10", EXACT_FLEET_GRANT + " "])
def test_refuses_to_mount_without_the_exact_grant(grant):
   with pytest.raises(A10BatchPolicyControlRefused, match="explicit_fleet_grant_required"):
      A10BatchPolicyControl(grant=grant, dispatch=Dispatcher(), product_port=8787)


@pytest.mark.parametrize("overrides", [{"public_origin": "https://mycelium.now"},
                                       {"trusted_https_proxy": True}])
def test_refuses_a_proxied_or_public_product(overrides):
   with pytest.raises(A10BatchPolicyControlRefused, match="requires_direct_loopback_product"):
      A10BatchPolicyControl(grant=EXACT_FLEET_GRANT, dispatch=Dispatcher(),
                            product_port=8787, **overrides)


@pytest.mark.parametrize(("port", "code"), [(80, "port_invalid"), (70_000, "port_invalid"),
                                            (8787, "port_is_product_origin")])
def test_refuses_an_invalid_or_product_port(port, code):
   with pytest.raises(A10BatchPolicyControlRefused, match=code):
      A10BatchPolicyControl(grant=EXACT_FLEET_GRANT, dispatch=Dispatcher(),
                            product_port=8787, port=port)


def test_forwards_a_candidate_window_to_the_dispatcher():
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   try:
      status, _document = _post(control.port,
                                {"policy": "candidate", "timeout_s": 30, "window_ms": 40})
      assert status == 200
      assert dispatcher.calls == [("candidate", 30, 40)]
   finally:
      control.close()


def test_the_window_bound_agrees_everywhere():
   import physical_inference_node
   from mycelium_a10_batch_policy_control import MAXIMUM_WINDOW_MS
   from mycelium_live import router_port

   assert (MAXIMUM_WINDOW_MS == router_port.A10_MAXIMUM_COLLECTION_WINDOW_MS
           == physical_inference_node.A10_MAXIMUM_COLLECTION_WINDOW_MS == 250)


def test_forwards_one_well_formed_policy_and_returns_the_dispatcher_result():
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   try:
      status, document = _post(control.port, {"policy": "baseline", "timeout_s": 30})
      assert status == 200
      assert document == {"result": dispatcher.result}
      assert dispatcher.calls == [("baseline", 30)]
      mounted = control.status()
      assert mounted["mounted"] is True
      assert mounted["dispatched_count"] == 1
      assert mounted["last_policy"] == "baseline"
      assert mounted["qualification_claim"] is False
   finally:
      control.close()
   assert control.status()["mounted"] is False


@pytest.mark.parametrize(
   ("kwargs", "expected"),
   [
      ({"grant": None}, 403),
      ({"grant": "FLEET GRANTED TO A9"}, 403),
      ({"path": "/a10/target-verify"}, 403),
      ({"method": "PUT"}, 403),
      ({"extra_headers": ("X-Forwarded-For: 10.0.0.1",)}, 403),
      ({"extra_headers": ("Forwarded: for=10.0.0.1",)}, 403),
      ({"content_type": "text/plain"}, 403),
   ],
)
def test_refuses_any_request_without_grant_path_method_or_with_forwarding(kwargs, expected):
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   try:
      status, document = _post(control.port, {"policy": "baseline", "timeout_s": 30}, **kwargs)
      assert (status, document) == (expected, {"code": "a10_batch_policy_control_refused"})
      assert dispatcher.calls == []
   finally:
      control.close()


@pytest.mark.parametrize(
   "body",
   [
      b"not json",
      {"policy": "off", "timeout_s": 30},
      {"policy": "baseline"},
      {"policy": "baseline", "timeout_s": True},
      {"policy": "baseline", "timeout_s": "30"},
      {"policy": "baseline", "timeout_s": 30, "node_id": "node-0"},
      ["baseline"],
      {"policy": "baseline", "timeout_s": 30, "window_ms": 40},
      {"policy": "candidate", "timeout_s": 30, "window_ms": 251},
      {"policy": "candidate", "timeout_s": 30, "window_ms": -1},
      {"policy": "candidate", "timeout_s": 30, "window_ms": 4.5},
      {"policy": "candidate", "timeout_s": 30, "window_ms": True},
      {"policy": "candidate", "timeout_s": 30, "window_ms": "40"},
   ],
)
def test_refuses_a_malformed_body_before_dispatch(body):
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   try:
      assert _post(control.port, body) == (400, {"code": "a10_batch_policy_request_invalid"})
      assert dispatcher.calls == []
   finally:
      control.close()


@pytest.mark.parametrize(
   ("error", "expected"),
   [
      (RuntimeError("a10_batch_policy_route_unsupported"),
       (409, {"code": "a10_batch_policy_route_unsupported"})),
      (RuntimeError("Traceback: /private/path secret"),
       (500, {"code": "a10_batch_policy_failed"})),
   ],
)
def test_a_dispatch_refusal_leaves_only_a_stable_code(error, expected):
   control = _control(Dispatcher(error=error))
   try:
      assert _post(control.port, {"policy": "candidate", "timeout_s": 30}) == expected
   finally:
      control.close()


class _Route:
   def __init__(self, *, alive=True, cap=None, refuse=None):
      self.alive = alive
      self.cap = cap
      self.refuse = refuse
      self.calls = []
      self.admitting = None

   def is_alive(self):
      return self.alive

   def a10_batch_policy(self, node_id, *, policy, deadline_monotonic_s):
      self.calls.append((node_id, policy))
      if self.refuse == node_id:
         raise RuntimeError("a10_batch_policy_node_unavailable")
      cap = self.cap if self.cap is not None else (1 if policy == "baseline" else 20)
      window = float(policy.split("@", 1)[1]) if "@" in policy else 2.0
      return {"protocol": "mycelium.a10_batch_policy_receipt.v1", "policy": policy,
              "maximum_runtime_batch_size": cap, "collection_window_ms": window}


def _port(route):
   port = object.__new__(LiveRouterPort)
   port._route = route
   port._graph = SimpleNamespace(stages=(
      SimpleNamespace(placements=(SimpleNamespace(node_id="node-2"),)),
      SimpleNamespace(placements=(SimpleNamespace(node_id="node-0"),
                                  SimpleNamespace(node_id="node-2"))),
   ))
   port._admission_dispatch_lock = threading.Lock()
   port._lock = threading.Lock()
   port._closed = False
   return port


def test_router_port_switches_every_deployment_node_serialized_with_admission():
   route = _Route()
   port = _port(route)
   assert port.a10_batch_policy("baseline", timeout_s=30) == {
      "policy": "baseline",
      "window_ms": None,
      "nodes": {"node-0": {"maximum_runtime_batch_size": 1, "collection_window_ms": 2.0},
                "node-2": {"maximum_runtime_batch_size": 1, "collection_window_ms": 2.0}},
   }
   assert route.calls == [("node-0", "baseline"), ("node-2", "baseline")]

   # A candidate window travels inside the policy string the route carries.
   route.calls.clear()
   assert port.a10_batch_policy("candidate", timeout_s=30, window_ms=40) == {
      "policy": "candidate",
      "window_ms": 40,
      "nodes": {"node-0": {"maximum_runtime_batch_size": 20, "collection_window_ms": 40.0},
                "node-2": {"maximum_runtime_batch_size": 20, "collection_window_ms": 40.0}},
   }
   assert route.calls == [("node-0", "candidate@40"), ("node-2", "candidate@40")]
   for window_ms in (-1, 251, 4.5, True):
      with pytest.raises(ValueError, match="invalid_a10_collection_window"):
         port.a10_batch_policy("candidate", timeout_s=30, window_ms=window_ms)
   with pytest.raises(ValueError, match="invalid_a10_collection_window"):
      port.a10_batch_policy("baseline", timeout_s=30, window_ms=40)

   class _IgnoredWindow(_Route):
      def a10_batch_policy(self, node_id, *, policy, deadline_monotonic_s):
         return {"policy": policy, "maximum_runtime_batch_size": 20,
                 "collection_window_ms": 2.0}

   with pytest.raises(RuntimeError, match="a10_batch_policy_response_invalid"):
      _port(_IgnoredWindow()).a10_batch_policy("candidate", timeout_s=30, window_ms=40)

   # The switch holds the admission lock: no request is admitted mid-switch.
   port._admission_dispatch_lock.acquire()
   finished = threading.Event()
   worker = threading.Thread(
      target=lambda: (port.a10_batch_policy("candidate", timeout_s=30), finished.set()))
   worker.start()
   assert not finished.wait(0.2)
   port._admission_dispatch_lock.release()
   assert finished.wait(5)
   worker.join(5)

   with pytest.raises(RuntimeError, match="a10_batch_policy_node_unavailable"):
      _port(_Route(refuse="node-2")).a10_batch_policy("baseline", timeout_s=30)

   class _WrongPolicy(_Route):
      def a10_batch_policy(self, node_id, *, policy, deadline_monotonic_s):
         return {"policy": "candidate", "maximum_runtime_batch_size": 20}

   with pytest.raises(RuntimeError, match="a10_batch_policy_response_invalid"):
      _port(_WrongPolicy()).a10_batch_policy("baseline", timeout_s=30)


def test_router_port_refuses_a_closed_or_dead_route_and_a_bad_policy():
   with pytest.raises(RuntimeError, match="route_not_open"):
      _port(_Route(alive=False)).a10_batch_policy("baseline", timeout_s=30)
   closed = _port(_Route())
   closed._closed = True
   with pytest.raises(RuntimeError, match="route_not_open"):
      closed.a10_batch_policy("baseline", timeout_s=30)
   with pytest.raises(ValueError, match="invalid_a10_batch_policy"):
      _port(_Route()).a10_batch_policy("off", timeout_s=30)
   with pytest.raises(ValueError, match="a10_batch_policy_deadline_invalid"):
      _port(_Route()).a10_batch_policy("baseline", timeout_s=0)
   with pytest.raises(RuntimeError, match="a10_batch_policy_route_unsupported"):
      _port(SimpleNamespace(is_alive=lambda: True)).a10_batch_policy(
         "baseline", timeout_s=30)


def test_owner_forwards_only_to_the_ready_route():
   owner = object.__new__(supervisor_module.ColdRouteRestartOwner)
   owner._lock = threading.Lock()
   route = _Route()
   owner._current = route
   owner._state = "ready"
   receipt = owner.a10_batch_policy("node-0", policy="baseline", deadline_monotonic_s=1.0)
   assert receipt["maximum_runtime_batch_size"] == 1
   assert route.calls == [("node-0", "baseline")]
   owner._state = "restarting"
   with pytest.raises(RuntimeError, match="route_not_open"):
      owner.a10_batch_policy("node-0", policy="baseline", deadline_monotonic_s=1.0)


def _mount(router):
   return supervisor_module._mount_a10_batch_policy_control(
      stack=SimpleNamespace(router=router), product_port=8787, control_port=0,
      public_origin=None, trusted_https_proxy=False)


def test_mount_hands_requests_to_the_product_dispatcher(monkeypatch):
   monkeypatch.delenv(GRANT_ENVIRONMENT, raising=False)
   with pytest.raises(RuntimeError,
                      match="startup_a10_batch_policy_control_refused:explicit_fleet"):
      _mount(SimpleNamespace(a10_batch_policy=Dispatcher()))
   monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
   with pytest.raises(RuntimeError,
                      match="startup_a10_batch_policy_control_dispatcher_unavailable"):
      _mount(None)
   dispatcher = Dispatcher()
   control = _mount(SimpleNamespace(a10_batch_policy=dispatcher))
   try:
      assert _post(control.port, {"policy": "baseline", "timeout_s": 30}) == (
         200, {"result": dispatcher.result})
      assert dispatcher.calls == [("baseline", 30)]
   finally:
      control.close()


def test_supervisor_main_forwards_the_port_only_when_requested(tmp_path: Path, monkeypatch):
   calls: list[dict] = []
   monkeypatch.setattr(supervisor_module, "run_physical_server",
                       lambda **kwargs: calls.append(kwargs) or 0)
   base = ["--operator-plan", str(tmp_path / "one.json"),
           "--seed-state-root", str(tmp_path / "seed")]
   assert supervisor_module.main(base) == 0
   assert "a10_batch_policy_control_port" not in calls[-1]
   assert supervisor_module.main([*base, "--a10-batch-policy-control-port", "8790"]) == 0
   assert calls[-1]["a10_batch_policy_control_port"] == 8790
   with pytest.raises(SystemExit):
      supervisor_module.main([*base, "--a10-batch-policy-control-port", "80"])
   with pytest.raises(SystemExit):
      supervisor_module.main([*base, "--operator-plan", str(tmp_path / "two.json"),
                              "--a10-batch-policy-control-port", "8790"])


def test_demo_serve_forwards_the_port_to_live_but_fixture_rejects_it(tmp_path: Path):
   calls: list[list[str]] = []
   argv = ["serve", "--mode", "live", "--operator-plan", str(tmp_path / "plan.json"),
           "--seed-state-root", str(tmp_path / "seed"),
           "--a10-batch-policy-control-port", "8790"]
   assert cli.main(argv, live_server_main=lambda a: calls.append(list(a)) or 0,
                   environ={}) == 0
   assert calls[0][-2:] == ["--a10-batch-policy-control-port", "8790"]
   with pytest.raises(SystemExit):
      cli.main(["serve", "--mode", "fixture", "--a10-batch-policy-control-port", "8790"],
               environ={})


def test_client_sets_and_verifies_the_candidate_window(monkeypatch, capsys):
   result = {"policy": "candidate", "window_ms": 40, "nodes": {
      "node-0": {"maximum_runtime_batch_size": 20, "collection_window_ms": 40.0}}}
   dispatcher = Dispatcher(result=result)
   control = _control(dispatcher)
   try:
      monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
      assert client.main(["candidate", "--port", str(control.port), "--window-ms", "40"]) == 0
      assert json.loads(capsys.readouterr().out) == result
      assert dispatcher.calls == [("candidate", 30.0, 40)]
      # A node reporting another window fails the mode command.
      dispatcher.result = {"policy": "candidate", "window_ms": 40, "nodes": {
         "node-0": {"maximum_runtime_batch_size": 20, "collection_window_ms": 2.0}}}
      with pytest.raises(SystemExit, match="a10_batch_policy_response_invalid"):
         client.main(["candidate", "--port", str(control.port), "--window-ms", "40"])
   finally:
      control.close()


def test_client_switches_and_verifies_every_node_receipt(monkeypatch, capsys):
   result = {"policy": "candidate", "nodes": {"node-0": {"maximum_runtime_batch_size": 20},
                                              "node-2": {"maximum_runtime_batch_size": 20}}}
   dispatcher = Dispatcher(result=result)
   control = _control(dispatcher)
   try:
      monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
      assert client.main(["candidate", "--port", str(control.port), "--timeout", "30"]) == 0
      assert json.loads(capsys.readouterr().out) == result
      assert dispatcher.calls == [("candidate", 30.0)]
      for bad in (["baseline", "--window-ms", "40"], ["candidate", "--window-ms", "251"],
                  ["candidate", "--window-ms", "-1"]):
         with pytest.raises(SystemExit, match="--window-ms is valid only with candidate"):
            client.main([*bad, "--port", str(control.port)])
      monkeypatch.delenv(GRANT_ENVIRONMENT)
      with pytest.raises(SystemExit, match="explicit_fleet_grant_required"):
         client.main(["candidate", "--port", str(control.port)])
   finally:
      control.close()


@pytest.mark.parametrize(
   ("dispatcher", "code"),
   [
      (Dispatcher(error=RuntimeError("a10_batch_policy_node_unavailable")),
       "a10_batch_policy_refused:a10_batch_policy_node_unavailable"),
      (Dispatcher(result={"policy": "baseline", "nodes": {}}),
       "a10_batch_policy_response_invalid"),
      (Dispatcher(result={"policy": "candidate", "nodes": {
         "node-0": {"maximum_runtime_batch_size": 1}}}),
       "a10_batch_policy_response_invalid"),
      (Dispatcher(result={"policy": "baseline", "nodes": {
         "node-0": {"maximum_runtime_batch_size": 20}}}),
       "a10_batch_policy_response_invalid"),
   ],
)
def test_client_fails_on_a_refusal_a_partial_or_a_wrong_cap(dispatcher, code):
   control = _control(dispatcher)
   try:
      with pytest.raises(SystemExit, match=code):
         client.switch_policy(port=control.port, policy="baseline", timeout_s=30,
                              grant=EXACT_FLEET_GRANT)
   finally:
      control.close()
   with pytest.raises(SystemExit, match="a10_batch_policy_control_unreachable"):
      client.switch_policy(port=1, policy="baseline", timeout_s=1, grant=EXACT_FLEET_GRANT)
