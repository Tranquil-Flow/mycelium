# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-gated A10 verification control on a direct loopback listener.

The control exists only with the exact fleet grant, refuses a proxied or public
product, requires the grant again on every request, refuses forwarding headers,
and hands each well-formed request to the product dispatcher unchanged.  A
refusal carries only a stable code.  Scripted dispatcher; never evidence.
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from mycelium_a10_verify_control import (
   CONTROL_PATH,
   EXACT_FLEET_GRANT,
   GRANT_ENVIRONMENT,
   STATUS_PROTOCOL,
   A10VerifyControl,
   A10VerifyControlRefused,
   grant_from_environment,
)

OPERATION = {"protocol": "mycelium.a10_product_verification_operation.v1", "kind": "negative"}


class Dispatcher:
   def __init__(self, result=("negative", {"case_id": "stale_prefix"}), error=None):
      self.result = result
      self.error = error
      self.calls: list[tuple[str, Any, float]] = []

   def __call__(self, node_id, operation, *, timeout_s):
      self.calls.append((node_id, operation, timeout_s))
      if self.error is not None:
         raise self.error
      return self.result


def _post(port: int, body: object, *, grant: str | None = EXACT_FLEET_GRANT,
          path: str = CONTROL_PATH, method: str = "POST",
          extra: str = "") -> tuple[int, dict]:
   raw = json.dumps(body).encode("utf-8")
   head = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
           "Connection: close\r\n" + extra)
   if grant is not None:
      head += f"X-Mycelium-A10-Fleet-Grant: {grant}\r\n"
   with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
      sock.sendall((head + "\r\n").encode("ascii") + raw)
      reply = bytearray()
      while chunk := sock.recv(65536):
         reply.extend(chunk)
   status = int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])
   return status, json.loads(bytes(reply).split(b"\r\n\r\n", 1)[1] or b"{}")


def _control(dispatcher=None, **overrides) -> A10VerifyControl:
   options = dict(grant=EXACT_FLEET_GRANT, dispatch=dispatcher or Dispatcher(),
                  product_port=8787, port=0)
   options.update(overrides)
   return A10VerifyControl(**options)


@pytest.mark.parametrize("grant", [None, "", "FLEET GRANTED TO A6", EXACT_FLEET_GRANT + " "])
def test_refuses_to_mount_without_the_exact_grant(grant):
   with pytest.raises(A10VerifyControlRefused, match="explicit_fleet_grant_required"):
      _control(grant=grant)


@pytest.mark.parametrize("overrides", [{"public_origin": "https://mycelium.now"},
                                       {"trusted_https_proxy": True}])
def test_refuses_a_proxied_or_public_product(overrides):
   with pytest.raises(A10VerifyControlRefused, match="requires_direct_loopback_product"):
      _control(**overrides)


@pytest.mark.parametrize("port, code", [(80, "port_invalid"), (8787, "port_is_product_origin"),
                                        (True, "port_invalid")])
def test_refuses_an_invalid_or_product_port(port, code):
   with pytest.raises(A10VerifyControlRefused, match=code):
      _control(port=port)


def test_grant_is_read_only_from_the_exact_environment_value():
   assert grant_from_environment({GRANT_ENVIRONMENT: EXACT_FLEET_GRANT}) == EXACT_FLEET_GRANT
   assert grant_from_environment({GRANT_ENVIRONMENT: "fleet granted to a10"}) is None
   assert grant_from_environment({}) is None


def test_forwards_one_well_formed_request_and_returns_the_dispatcher_result():
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   control.start()
   try:
      status = control.status()
      assert status["protocol"] == STATUS_PROTOCOL and status["mounted"] is True
      assert status["url"] == f"http://127.0.0.1:{control.port}{CONTROL_PATH}"
      assert len(status["lifetime_id"]) == 64
      code, body = _post(control.port, {"node_id": "node-0", "operation": OPERATION,
                                        "timeout_s": 60})
      assert code == 200
      assert body == {"kind": "negative", "result": {"case_id": "stale_prefix"}}
      assert dispatcher.calls == [("node-0", OPERATION, 60)]
      assert control.status()["dispatched_count"] == 1
   finally:
      control.close()
   assert control.status()["mounted"] is False


@pytest.mark.parametrize("kwargs, expected", [
   ({"grant": None}, 403),
   ({"grant": "FLEET GRANTED TO A6"}, 403),
   ({"path": "/a10/other"}, 403),
   ({"method": "PUT"}, 403),
   ({"extra": "X-Forwarded-For: 10.0.0.1\r\n"}, 403),
   ({"extra": "Forwarded: for=10.0.0.1\r\n"}, 403),
])
def test_refuses_any_request_without_grant_path_method_or_with_forwarding(kwargs, expected):
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   control.start()
   try:
      code, body = _post(control.port, {"node_id": "node-0", "operation": OPERATION,
                                        "timeout_s": 60}, **kwargs)
      assert code == expected and body == {"code": "a10_verify_control_refused"}
      assert dispatcher.calls == []
   finally:
      control.close()


@pytest.mark.parametrize("body", [
   [], {"node_id": "node-0", "operation": OPERATION},
   {"node_id": "", "operation": OPERATION, "timeout_s": 60},
   {"node_id": "node-0", "operation": [], "timeout_s": 60},
   {"node_id": "node-0", "operation": OPERATION, "timeout_s": "60"},
   {"node_id": "node-0", "operation": OPERATION, "timeout_s": 60, "extra": True},
])
def test_refuses_a_malformed_body_before_dispatch(body):
   dispatcher = Dispatcher()
   control = _control(dispatcher)
   control.start()
   try:
      assert _post(control.port, body) == (400, {"code": "a10_verify_request_invalid"})
      assert dispatcher.calls == []
   finally:
      control.close()


@pytest.mark.parametrize("error, expected", [
   (RuntimeError("a10_verify_route_busy"), (409, {"code": "a10_verify_route_busy"})),
   (RuntimeError("a10_product_result_invalid"), (409, {"code": "a10_product_result_invalid"})),
   (RuntimeError("private detail with spaces"), (500, {"code": "a10_verify_failed"})),
   (TimeoutError("command_deadline_exceeded"), (409, {"code": "command_deadline_exceeded"})),
])
def test_a_dispatch_refusal_leaves_only_a_stable_code(error, expected):
   control = _control(Dispatcher(error=error))
   control.start()
   try:
      assert _post(control.port, {"node_id": "node-0", "operation": OPERATION,
                                  "timeout_s": 60}) == expected
   finally:
      control.close()
