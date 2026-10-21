# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback LOCAL-CONFORMANCE origin for the A10 two-session browser harness.

Test-only.  It exists so the gate-1 browser harness
(``ui/web/scripts/a10-browser-sessions.mjs``) can be exercised end to end on a
host without devices, a model or a route.  It is never physical evidence:

* the BUILT production SPA (``ui/web/dist``) is served as-is;
* ``/api/*`` is the real production product gateway
  (``mycelium_ui_gateway.create_product_gateway_application``) with its real
  bootstrap, session cookie, CSRF and proxying; behind it sit the real
  Observatory gateway and the real product-evidence application, fed with the
  repository's compatibility fixtures, and a *simulated* request gateway that
  decodes deterministic placeholder words on a shared tick (requests active on
  the same tick share a simulated batch reference);
* the qualification projections it publishes say ``physical_qualification``
  only because the UI refuses to enable inference otherwise; the origin's own
  start line and ``/__mycelium/live-status`` say ``simulated: true`` and
  ``evidence_class: local_conformance``, which the harness accepts only in
  ``A10_BROWSER_MODE=local_conformance`` and the A10 gate never accepts as
  gate-1 evidence.

Run: ``python3.14 -B tests/a10_browser/conformance_origin.py`` prints one JSON
start line ``{"origin": ..., "evidence_class": "local_conformance", ...}`` and
serves until stdin closes or the process is terminated.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
   sys.path.insert(0, str(ROOT))

from mycelium_request_gateway.contracts import (  # noqa: E402
   REQUEST_EVENT_PROTOCOL,
   REQUEST_EVENT_PROTOCOL_V2,
   StreamEvent,
)

EVIDENCE_CLASS = "local_conformance"
DEPLOYMENT_ID = "12345678-1234-5678-1234-567812345678"
TICK_SECONDS = 0.08
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _json_response(status: int, document: Any) -> tuple[int, bytes, bytes]:
   return status, b"application/json", json.dumps(document, sort_keys=True).encode("utf-8")


def gateway_qualification(now_ms: int) -> dict[str, Any]:
   """The request-gateway projection shape (tests/ui_gateway fixture), freshly issued."""

   return {
      "protocol": "mycelium.request_gateway.v1",
      "issued_at_unix_ms": now_ms,
      "evidence_class": "physical_qualification",
      "route_ready": True,
      "reason_codes": [],
      "binding": {
         "qualification_id": "local-conformance-qualification",
         "qualification_digest": DIGEST_A,
         "deployment_id": DEPLOYMENT_ID,
         "deployment_epoch": 1,
         "topology_version": 1,
         "model_id": "org/model",
         "resolved_commit": "local-conformance",
         "manifest_digest": DIGEST_B,
         "path_manifest_digest": DIGEST_C,
         "stage_load_proof_digests": [DIGEST_A],
      },
   }


def observatory_qualification(now_ms: int) -> dict[str, Any]:
   """The Observatory snapshot's route-qualification projection for the same binding."""

   projection = gateway_qualification(now_ms)
   return {
      "protocol": "mycelium.route_qualification.v1",
      "qualification_id": projection["binding"]["qualification_id"],
      "issued_at_unix_ms": now_ms,
      "evidence_class": projection["evidence_class"],
      "route_ready": True,
      "reason_codes": [],
      "binding": projection["binding"],
   }


class SimulatedDecoder:
   """Deterministic placeholder decoding on one shared tick (no model, no route)."""

   def __init__(self) -> None:
      self.lock = threading.Lock()
      self.requests: dict[str, dict[str, Any]] = {}
      self.counter = 0
      self.tick = 0
      self.monotonic_anchor = time.monotonic()
      self.wall_anchor = time.time()
      self.intervals: list[dict[str, Any]] = []
      # One qualification issuance per origin lifetime (a re-issue would make the
      # UI ask the operator to accept a changed binding); 1 h UI freshness bound.
      self.issued_at_ms = int(self.wall_anchor * 1000)

   def admit(self, submission: dict[str, Any]) -> str:
      with self.lock:
         self.counter += 1
         request_id = f"a10-local-{self.counter:04d}-{secrets.token_hex(4)}"
         self.requests[request_id] = {
            "max_new_tokens": int(submission["max_new_tokens"]),
            "protocol": REQUEST_EVENT_PROTOCOL_V2
            if submission.get("protocol") == "mycelium.request_gateway.v2"
            else REQUEST_EVENT_PROTOCOL,
            "ordinal": self.counter,
            "events": [],
            "done": False,
         }
         state = self.requests[request_id]
         state["events"].append(
            StreamEvent(request_id=request_id, sequence=0, kind="accepted", publisher_generation=1,
                        protocol=state["protocol"]).to_dict()
         )
         return request_id

   def cancel(self, request_id: str) -> bool:
      with self.lock:
         state = self.requests.get(request_id)
         if state is None or state["done"]:
            return False
         state["done"] = True
         state["events"].append(
            StreamEvent(request_id=request_id, sequence=len(state["events"]), kind="cancelled",
                        publisher_generation=1, protocol=state["protocol"]).to_dict()
         )
         return True

   def step(self) -> None:
      with self.lock:
         self.tick += 1
         active = [(rid, s) for rid, s in self.requests.items() if not s["done"]]
         batch_ref = f"simulated-batch:{self.tick}" if len(active) > 1 else ""
         started = time.monotonic()
         for request_id, state in active:
            index = sum(1 for event in state["events"] if event["type"] == "token")
            text = f" r{state['ordinal']}w{index}"
            event = StreamEvent(request_id=request_id, sequence=len(state["events"]), kind="token",
                                token_index=index, text=text, publisher_generation=1,
                                protocol=state["protocol"],
                                token_id=1000 + index if state["protocol"] == REQUEST_EVENT_PROTOCOL_V2 else None)
            state["events"].append(event.to_dict())
            from mycelium_router.a10_live_publication import pseudonym

            self.intervals.append(
               {
                  "interval_ref": f"interval:local-conformance:{self.tick}:{state['ordinal']}",
                  "request_ref": pseudonym("request", request_id),
                  "runtime_batch_ref": batch_ref,
                  "started_at": started,
                  "finished_at": started,
                  "stage_index": 0,
                  "phase": "decode",
               }
            )
            if index + 1 >= state["max_new_tokens"]:
               state["done"] = True
               state["events"].append(
                  StreamEvent(request_id=request_id, sequence=len(state["events"]), kind="completed",
                              publisher_generation=1, protocol=state["protocol"]).to_dict()
               )
         self.intervals = self.intervals[-256:]

   def events_after(self, request_id: str, sequence: int) -> tuple[list[dict[str, Any]], bool]:
      with self.lock:
         state = self.requests.get(request_id)
         if state is None:
            return [], True
         return [event for event in state["events"] if event["sequence"] > sequence], state["done"]

   def live_status(self) -> dict[str, Any]:
      with self.lock:
         intervals = [dict(item) for item in self.intervals]
      return {
         "protocol": "mycelium.live_route_status.v1",
         "evidence_class": EVIDENCE_CLASS,
         "simulated": True,
         "route_alive": True,
         "deployment_id": DEPLOYMENT_ID,
         "peers": [
            {
               "node_id": "local-conformance-node",
               "a10_publication": {
                  "protocol": "mycelium.a10_live_publication.v1",
                  "clock": {"monotonic_anchor": self.monotonic_anchor, "wall_anchor": self.wall_anchor},
                  "intervals": intervals,
               },
            }
         ],
      }


class SimulatedRequestGateway:
   """ASGI request-gateway double: qualification, submit, SSE events, cancel."""

   def __init__(self, decoder: SimulatedDecoder) -> None:
      self.decoder = decoder

   async def __call__(self, scope, receive, send) -> None:
      body = bytearray()
      while True:
         message = await receive()
         if message.get("type") == "http.disconnect":
            return
         body.extend(message.get("body", b""))
         if not message.get("more_body", False):
            break
      path, method = scope["path"], scope["method"]
      if path == "/v1/qualification/current" and method == "GET":
         return await self._send(send, *_json_response(200, gateway_qualification(self.decoder.issued_at_ms)))
      if path == "/v1/inference" and method == "POST":
         try:
            submission = json.loads(bytes(body))
            int(submission["max_new_tokens"])
         except Exception:  # noqa: BLE001
            return await self._send(send, *_json_response(400, {"error": "invalid_submission"}))
         request_id = self.decoder.admit(submission)
         return await self._send(
            send,
            *_json_response(
               202,
               {
                  "request_id": request_id,
                  "stream_path": f"/v1/inference/{request_id}/events",
                  "cancel_path": f"/v1/inference/{request_id}",
                  "session_token": secrets.token_hex(32),
               },
            ),
         )
      if path.startswith("/v1/inference/") and path.endswith("/events") and method == "GET":
         request_id = path[len("/v1/inference/"):-len("/events")]
         last = -1
         for name, value in scope.get("headers", []):
            if name.lower() == b"last-event-id":
               try:
                  last = int(value.decode().rsplit(":", 1)[-1])
               except ValueError:
                  last = -1
         await send({"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
         while True:
            events, done = self.decoder.events_after(request_id, last)
            for event in events:
               last = event["sequence"]
               frame = (
                  f"id: {event['publisher_generation']}:{event['sequence']}\nevent: {event['type']}\n"
                  f"data: {json.dumps(event, separators=(',', ':'), sort_keys=True)}\n\n"
               ).encode("utf-8")
               await send({"type": "http.response.body", "body": frame, "more_body": True})
            if done and not self.decoder.events_after(request_id, last)[0]:
               await send({"type": "http.response.body", "body": b"", "more_body": False})
               return
            await asyncio.sleep(TICK_SECONDS / 4)
      if path.startswith("/v1/inference/") and method == "DELETE":
         request_id = path[len("/v1/inference/"):]
         self.decoder.cancel(request_id)
         return await self._send(send, *_json_response(202, {"request_id": request_id, "status": "cancelling"}))
      return await self._send(send, *_json_response(404, {"error": "not_found"}))

   @staticmethod
   async def _send(send, status: int, media: bytes, body: bytes) -> None:
      await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", media)]})
      await send({"type": "http.response.body", "body": body, "more_body": False})


class NoSwarm:
   def status(self):
      return {"protocol": "mycelium.product_ui.swarm.v1", "native_nodes": [], "browser_workers": []}

   def __getattr__(self, name):
      raise RuntimeError("conformance_mutation_forbidden")


def build_application(static_root: Path, state_root: Path):
   """Compose the outer ASGI app; returns (app, decoder, observatory_runtime, republish)."""

   from mycelium_demo.product_servers import (
      _load_observatory_gateway,
      demo_observatory_bundle,
      demo_observatory_read_policy,
      publish_demo_observatory_bundle,
   )
   from mycelium_product_spine import ProductEvidenceApplication, ProductProjector
   from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application

   decoder = SimulatedDecoder()
   now = int(time.time() * 1000)
   route = json.loads((ROOT / "contracts/compatibility-fixtures/live-route-status-v1.json").read_text("utf-8"))
   route["route_alive"] = True
   qualification = json.loads(
      (ROOT / "contracts/compatibility-fixtures/route-qualification-v1.json").read_text("utf-8")
   )
   qualification.update(
      deployment_id=route["deployment_id"], model_id=route["model_id"],
      topology_version=route["topology_version"], route_ready=True, issued_at_unix_ms=now,
   )
   member_ids = sorted({stage["node_id"] for stage in route["stages"]})
   members = [
      dict(node_id=node, generation=1, incarnation="conformance", lease_expires_at=(now + 3_600_000) / 1000,
           last_liveness_at=now / 1000, lifecycle_state="RUNNING", peer_class="mac_mlx_iroh",
           runtime_capability=dict(runtime_backend="mlx", transport="iroh",
                                   activation_protocol="mycelium.router_wire.v1"),
           activation_eligible=True, endpoint_id="RAW-ENDPOINT-CANARY", endpoint_addrs=["192.0.2.23"])
      for node in member_ids
   ]

   def fresh_members():
      at = time.time()
      return [{**member, "last_liveness_at": at, "lease_expires_at": at + 3600} for member in members]

   def fresh_qualification():
      return {**qualification, "issued_at_unix_ms": int(time.time() * 1000)}

   product = ProductEvidenceApplication(
      projector=ProductProjector(pseudonym_salt=secrets.token_bytes(32)),
      membership_source=fresh_members,
      route_source=lambda: route,
      qualification_source=fresh_qualification,
      recovery_source=lambda: {
         "protocol": "mycelium.full_context_recovery_runtime.v1",
         "binding": {"deployment_id": route["deployment_id"], "deployment_epoch": 1},
         "requests": [],
         "breaker": {"state": "closed"},
      },
   )
   observatory = _load_observatory_gateway(
      state_path=state_root / "coherent-snapshot.json", read_policy=demo_observatory_read_policy(None)
   )
   cursor = {"value": 0}

   def publish_observatory() -> None:
      at = int(time.time() * 1000)
      cursor["value"] += 1
      publish_demo_observatory_bundle(
         observatory.publisher,
         demo_observatory_bundle(source_cursor=cursor["value"], observed_at_unix_ms=at,
                                 qualification=observatory_qualification(decoder.issued_at_ms)),
      )

   publish_observatory()
   gateway = create_product_gateway_application(
      config=GatewayConfig(),
      observatory_app=observatory.app,
      request_gateway_app=SimulatedRequestGateway(decoder),
      product_app=product,
      swarm_coordinator=NoSwarm(),
      request_gateway_bearer_token=secrets.token_urlsafe(32),
   )
   static_root = static_root.resolve()

   async def app(scope, receive, send):
      if scope["type"] != "http":
         return
      path = scope["path"]
      if path.startswith("/api/"):
         return await gateway(scope, receive, send)
      if path == "/__mycelium/live-status":
         return await SimulatedRequestGateway._send(send, *_json_response(200, decoder.live_status()))
      if path.startswith("/__mycelium/"):
         return await SimulatedRequestGateway._send(send, *_json_response(404, {"error": "not_found"}))
      relative = path.lstrip("/") or "index.html"
      target = (static_root / relative).resolve()
      if not target.is_relative_to(static_root) or not target.is_file():
         target = static_root / "index.html"
      media = (mimetypes.guess_type(target.name)[0] or "application/octet-stream").encode()
      await SimulatedRequestGateway._send(send, 200, media, target.read_bytes())

   return app, decoder, observatory, publish_observatory


class ConformanceOrigin:
   """Started loopback origin; ``origin`` is ``http://127.0.0.1:<port>``."""

   def __init__(self, static_root: Path | None = None) -> None:
      from mycelium_demo.gateway_control_runtime import LoopbackASGIServer

      static_root = static_root or ROOT / "ui" / "web" / "dist"
      if not (static_root / "index.html").is_file():
         raise RuntimeError("built_spa_missing")
      self._state = tempfile.TemporaryDirectory(prefix="a10-browser-conformance-")
      app, self.decoder, self._observatory, self._republish = build_application(
         static_root, Path(self._state.name)
      )
      self._server = LoopbackASGIServer(("127.0.0.1", 0), app)
      self._server.start()
      self._thread = self._server.serve_forever_thread()
      self._ticking = True
      self._ticker = threading.Thread(target=self._tick, name="a10-conformance-tick", daemon=True)
      self._ticker.start()
      self.origin = f"http://127.0.0.1:{self._server.port}"

   def _tick(self) -> None:
      ticks = 0
      while self._ticking:
         time.sleep(TICK_SECONDS)
         self.decoder.step()
         ticks += 1
         if ticks % 25 == 0:
            # Keep the Observatory projection current (freshness is time-bounded).
            self._republish()

   def close(self) -> None:
      self._ticking = False
      self._server.shutdown()
      self._server.server_close()
      close = getattr(self._observatory, "close", None)
      if callable(close):
         close()
      self._state.cleanup()

   def __enter__(self) -> "ConformanceOrigin":
      return self

   def __exit__(self, *_exc: object) -> None:
      self.close()


def main() -> int:
   with ConformanceOrigin() as origin:
      print(json.dumps({"origin": origin.origin, "evidence_class": EVIDENCE_CLASS, "simulated": True,
                        "physical_devices": 0}), flush=True)
      try:
         sys.stdin.read()
      except KeyboardInterrupt:
         pass
   return 0


if __name__ == "__main__":
   raise SystemExit(main())
