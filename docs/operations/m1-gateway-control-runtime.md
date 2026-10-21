# M1 gateway / control runtime — loopback origin composition

**Status:** landed (lane `v2/m1-gateway-control-runtime-r1`).
**Why:** `https://mycelium.now` answered HTTP 502 because the tunnel was up and
**no origin listened**. The ingress is `mycelium.now -> http://127.0.0.1:4351`,
nothing was bound to 4351, and `control.mycelium.now` has neither a DNS record
nor an ingress rule. Every runtime piece existed; the composition did not.

This page documents the composition that makes an origin **listen and serve end
to end on loopback**: ports, environment, start/stop, and how to verify it. It is
a *stub* runtime — the backend is a deterministic echo, not a model.

## 1. What it starts

| Listener | Module (landed) | Port |
| --- | --- | --- |
| public origin (the tunnel ingress target) | `mycelium_demo/public_origin_proxy.py` (`PublicOriginProxyServer`) | `--public-port`, default **4351**, env `MYCELIUM_PUBLIC_ORIGIN_PORT` |
| gateway upstream (session API + SSE) | `mycelium_request_gateway/asgi.py` (`RequestGatewayASGIApplication`) on the stdlib ASGI host in `mycelium_demo/gateway_control_runtime.py` | `--gateway-port`, default **0** (ephemeral), env `MYCELIUM_GATEWAY_UPSTREAM_PORT` |
| control-origin mailbox | `mycelium_a13_macos/control_origin.py` (`ControlOriginServer`) | `--control-port`, default **4352**, env `MYCELIUM_CONTROL_ORIGIN_PORT` |
| cold route restart | `mycelium_live/supervisor.py` (`ColdRouteRestartOwner`) | in-process, no socket |

Routing: the public origin forwards to the gateway upstream and injects
`X-Mycelium-Authenticated-User` + `X-Mycelium-Proxy-Capability` (it strips the
client `Authorization` header), so the upstream authenticates the *proxy hop*
with the owner-private capability instead of trusting the network. The
composition maps the public route surface onto the gateway's session API:

```
POST   /api/v1/inference                      -> POST   /v1/inference
GET    /api/v1/inference/<id>/events          -> GET    /v1/inference/<id>/events
DELETE /api/v1/inference/<id>/cancel          -> DELETE /v1/inference/<id>
GET    /api/v1/qualification/current          -> GET    /v1/qualification/current
GET    /api/v1/recovery/status                -> GET    /v1/recovery/status
```

A submission is admitted by `RequestGatewayService`, the stub backend emits
tokens, and the token events leave the socket as SSE (`text/event-stream`,
chunked upstream / close-delimited through the proxy). The stream is
acknowledged per event, so `Last-Event-ID` resume works exactly as the gateway
implements it.

## 2. Ports must stay configurable

Every port is an argument **and** an environment variable. The defaults exist so
that the production ingress (`127.0.0.1:4351`) needs no flag; they are never the
only accepted value:

```bash
--public-port / MYCELIUM_PUBLIC_ORIGIN_PORT      (default 4351)
--gateway-port / MYCELIUM_GATEWAY_UPSTREAM_PORT  (default 0 = ephemeral)
--control-port / MYCELIUM_CONTROL_ORIGIN_PORT    (default 4352)
--state-root / MYCELIUM_GATEWAY_RUNTIME_STATE_ROOT
--request-bearer-token-env NAME / MYCELIUM_REQUEST_GATEWAY_TOKEN  (NAME defaults to MYCELIUM_REQUEST_GATEWAY_TOKEN)
```

## 3. Production invocation

The composition is loopback-only and touches nothing outside itself: **no
launchd install/load/bootout/unload/kickstart/signal, no Cloudflare or DNS or
tunnel edit, no announcement**. Starting it does not by itself publish anything —
the apex/control cutover is owner-authorized and is not part of this lane.

```bash
# from the repository root, with the lane interpreter
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  ~/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python \
  -m mycelium_demo.gateway_control_runtime \
    --public-port 4351 \
    --control-port 4352 \
    --state-root "$HOME/.mycelium/m1-gateway-runtime" \
    --capability-file /absolute/path/to/owner-private-64-hex-capability
```

* Prints one JSON readiness line (`mycelium.gateway_control_runtime.ready.v1`)
  with the resolved ports, then serves until `SIGINT`/`SIGTERM`.
* `--status-only` prints the full status document and exits without serving.
* `--no-control-origin` starts the public origin and gateway only.
* `--capability-file` should point at a mode-0600 file holding 64 lowercase hex
  characters (the same format `public_origin_proxy.load_proxy_capability`
  enforces). When omitted, a fresh capability is generated in memory for the
  process lifetime — fine for loopback development, wrong for a long-lived
  cutover, where a durable file is the point.

Stop it by stopping the process (`Ctrl-C`, or the supervisor that started it).
Do **not** stop it by touching the tunnel, and do not signal the running
`now.mycelium.*` agents — this runtime is not one of them.

### Interfaces it exposes

* `GET /healthz` — request-gateway health on the public surface.
* `GET /v1/route/status` (gateway port only) — backend, `route_ready`, probe
  count and the `cold_route_restart` projection.
* `GET <control port>/healthz` with `Host: control.mycelium.now` or a loopback
  host — managed-control origin health (`mycelium.managed_control.origin_health.v1`).
* `POST <control port>/managed-control/{enqueue,poll,ack,result}` — the mailbox,
  behind the signed managed-control envelope. Serving an actual command needs a
  seeded owner/target member; the composition only exposes the origin.

## 4. Cold route restart behaviour

`ColdRouteRestartOwner` is both the gateway's qualification source and the
liveness gate. When the serving route dies, the next submission is refused
immediately with the *named* error — it never hangs and never replays the
request that discovered the dead route:

| condition | error code | HTTP |
| --- | --- | --- |
| route lost, restart in progress | `cold_route_restart_in_progress` | 503 + `Retry-After: 1` |
| restart budget spent | `cold_route_restart_budget_exhausted` | 503 |
| replacement route could not be opened | `cold_route_restart_failed` | 503 |

The owner has already retired the dead route and opened, qualified and published
its replacement by the time the error is returned, so the *next* request is
served by the new generation (see the transcript: `generation` 1 → 2).

## 5. Stub inventory — what is *not* real here

* **Backend**: `EchoStubBackend` echoes the prompt's whitespace-delimited words
  as token events. No model, no Router, no KV, no physical route.
* **Qualification**: `load_stub_qualification()` issues a `route_ready=True`
  record from the repository's own `synthetic_test_fixture` document set run
  through the real `qualify_route` with test-only signature verifiers. It is a
  **local contract proof, not physical evidence** (`evidence_class` is inherited
  from the fixture, and the composition's status document says so). Production
  publishes the live route owner's qualification instead.
* **Control origin**: the mailbox is real (SQLite + `SeedCoordinator`), but no
  owner/target member is seeded, so an end-to-end signed control command is not
  exercised here.
* **`GET /v1/recovery/status`** returns 503 `recovery_projection_unavailable`:
  the stub backend does not implement `recovery_public_projection`.

## 6. Known seams (precise, not silently worked around)

1. **Advertised session paths vs public route surface.** The gateway's 202 body
   advertises `stream_path`/`cancel_path` as `/v1/inference/...`
   (`mycelium_request_gateway/asgi.py:275-276`), and the public origin blocks
   `/v1/inference*` by design
   (`mycelium_demo/public_origin_proxy.py:452-453`). A public client must use the
   `/api/v1/...` pair returned by §1 instead. In production this does not arise:
   the thing behind the proxy is `mycelium_ui_gateway`'s product application,
   which serves and advertises `/api/v1/...` natively (`mycelium_ui_gateway/app.py:63,743-744`).
2. **`HTTPGatewayClient` and owner-locked sessions — repaired 2026-09-27.** `service.py`
   locks each session to the token minted by `session_token_source`
   (`_require_session_owner`, called by `subscribe()` and `cancel()`), and
   `asgi.py` requires that token on `/events` and on `DELETE
   /v1/inference/{request_id}` -- `_session_token` reads exactly the
   `x-mycelium-session` header. `client.py::HTTPGatewayClient` now captures
   `session_token` from the 202 acceptance document and echoes it as
   `X-Mycelium-Session` on both follow-up requests, the same owner token
   `mycelium_ui_gateway` forwards (`mycelium_ui_gateway/app.py:489-490`); the
   client also parses the gateway's own SSE id dialect,
   `id: {publisher_generation}:{sequence}` (`asgi.py:146-152`), which it
   previously read as a bare sequence. The gateway's scoping is unchanged: a
   subscriber that does not present the session's owner token is still refused
   with 409 `session_owner_mismatch` before a single event is streamed.
   Guarded by
   `tests/gateway/test_gateway_control_runtime.py::test_repo_http_client_streams_an_owner_locked_session`
   (RED whenever the header stops being sent, or when the id dialect regresses)
   and
   `...::test_owner_locked_session_still_rejects_a_stream_that_omits_the_session_token`
   (the gateway-side half, RED if the owner check is ever relaxed). The
   pre-repair behaviour was recorded here as a finding and asserted by
   `test_repo_http_client_cannot_stream_an_owner_locked_session`; that test
   asserted the *defect*, not a contract, and was replaced by the two above.
3. **`HTTPGatewayClient` resume cursor shape (open).** `cli.py` resumes a
   dropped stream by passing an integer `last_event_id`, and the client writes
   it straight into `Last-Event-ID` (`mycelium_request_gateway/client.py::events`).
   The gateway parses that header as `{publisher_generation}:{sequence}`
   (`asgi.py::_last_event_id`; it rejects any single-part value and any
   `generation < 1`), so a resumed stream is refused with 409
   `invalid_last_event_id`. Measured on the stub runtime, same request and same
   owner token, only the cursor shape changing: `Last-Event-ID: 0` -> HTTP 409
   `{"error": "invalid_last_event_id"}`, `Last-Event-ID: <generation>:0` -> HTTP
   200 `text/event-stream` resuming at the next event. Fixing it needs a
   cursor-shape decision (the public `GatewayClient.events(last_event_id: int)`
   signature cannot express the generation), so it is recorded, not silently
   worked around.
4. **Cosmetic**: the proxy forwards the upstream `Server`/`Date` headers in
   addition to its own, so a proxied response carries them twice.

## 7. Verification

```bash
# focused suite (4-8s, loopback only, stub backend)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  ~/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python \
  -m pytest -q -p no:cacheprovider tests/gateway/test_gateway_control_runtime.py

# manual capture: real sockets, real SSE bytes, cold-route error (prints transcript)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  ~/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python \
  tests/gateway/capture_loopback_sse.py 14351 14352 /tmp/m1-gateway-capture
```

Captured transcript (ports 14351 / 50611 / 14352; the public port is a flag, so
`127.0.0.1:4351` was never bound during this run):

```
== listening sockets ==
public-origin     127.0.0.1:14351 connect_ex=0
gateway-upstream  127.0.0.1:50611 connect_ex=0
control-origin    127.0.0.1:14352 connect_ex=0
== POST /api/v1/inference through the public origin ==
HTTP 202
{"cancel_path": "/v1/inference/m1-1-c0b04519211d", "request_id": "m1-1-c0b04519211d",
 "session_token": "<session-token>",
 "stream_path": "/v1/inference/m1-1-c0b04519211d/events"}
== raw SSE response headers (bytes on the wire) ==
'HTTP/1.1 200 OK\r\nServer: MyceliumPublicOriginProxy/1 \r\n...\r\ncontent-type: text/event-stream; charset=utf-8\r\ncache-control: no-store\r\nx-accel-buffering: no\r\nConnection: close'
== raw SSE blocks as they arrive (backend held after 1 token) ==
'id: 1:0\nevent: accepted\ndata: {"protocol":"mycelium.request_event.v1","publisher_generation":1,"request_id":"m1-1-c0b04519211d","sequence":0,"type":"accepted"}'
'id: 1:1\nevent: token\ndata: {...,"sequence":1,"text":"alpha ","token_index":0,"type":"token"}'
    -> token arrived while the backend is still held (backend.emitted_tokens=1, release set=False)
'id: 1:2\nevent: token\ndata: {...,"sequence":2,"text":"beta ","token_index":1,"type":"token"}'
'id: 1:3\nevent: token\ndata: {...,"sequence":3,"text":"gamma","token_index":2,"type":"token"}'
'id: 1:4\nevent: completed\ndata: {...,"sequence":4,"type":"completed"}'
== dead route: kill the serving route, submit again ==
HTTP 503 after 0.005s
{"code": "cold_route_restart_in_progress",
 "cold_route_restart": {"attempts": 1, "budget": 1, "budget_exhausted": false,
   "generation": 2, "state": "ready"},
 "error": "cold_route_restart_in_progress", "retryable": true}
== successor request on the replacement route ==
HTTP 202 request_id=m1-2-2184fdbe5683
== runtime stopped without touching launchd, DNS, the tunnel or 127.0.0.1:4351 ==
```

## 8. Boundary reminder

`docs/operations/pilot-service-runbook.md` §0.3 lists port 4351 and the
`now.mycelium.*` launchd agents as never-touch. That runbook predates PLAN v2's
hackathon retirement, but the caution stands: this lane composes and proves the
runtime on loopback and hands the actual apex/control cutover to the owner. No
launchd, DNS, tunnel, publication or synthetic load was involved in any run
recorded above.
