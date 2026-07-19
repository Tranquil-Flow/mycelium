# Mycelium product gateway

## Scope

`mycelium_ui_gateway` is the browser-facing, same-origin composition boundary for the product UI. It adapts existing Observatory, request-gateway, and injected swarm-coordinator interfaces without acquiring their authority.

This lane proves local composition only. It does not prove public deployment security, route readiness, physical distributed inference, or multi-host reachability.

## Authority

- Observatory operations exposed here are read-only GET/SSE projections.
- Inference submission, event streaming, resume, and cancellation remain request-gateway operations.
- Qualification is validated and projected literally. The gateway never synthesizes `route_ready=true`.
- Swarm operations invoke an injected `SwarmCoordinator`; they never mutate Router state.

## Browser endpoints

All endpoints are same-origin under `/api/v1/`:

- `GET /api/v1/bootstrap`
- `GET /api/v1/observatory/snapshot`
- `GET /api/v1/observatory/events`
- `GET /api/v1/qualification/current`
- `POST /api/v1/inference`
- `GET /api/v1/inference/{request_id}/events`
- `DELETE /api/v1/inference/{request_id}/cancel`
- `GET /api/v1/swarm/status`
- `POST /api/v1/swarm/invites`
- `POST /api/v1/swarm/join`
- `POST /api/v1/swarm/leave`

Query strings and websocket upgrades are rejected. Private responses carry `Cache-Control: no-store` and defensive response headers.

## Session and CSRF boundary

A bootstrap request creates or resumes a bounded, process-local session. The session identifier is set only in an `HttpOnly; SameSite=Strict; Path=/` cookie and becomes `Secure` under TLS. State-changing requests require both the session cookie and the bootstrap-provided `X-Mycelium-CSRF` value, plus an exact same-origin `Origin` header.

Sessions, request ownership, response sizes, request bodies, SSE frames, queue depth, and stream duration are bounded. Restarting the gateway destroys all product sessions.

## Credentials and privacy

Upstream service credentials are injected server-side. Browser headers, cookies, origins, query strings, and arbitrary upstream response headers are never forwarded. Upstream credentials are checked against browser-visible bodies, errors, and stream frames before emission. Stable public error codes contain no exception text or private endpoint.

Prompt and generated output exist only in bounded request/stream memory. The gateway does not write them to logs, URLs, storage, or metrics.

## Deployment defaults

`GatewayConfig()` binds conceptually to loopback and accepts only loopback clients/hosts. Non-loopback configuration fails closed unless HTTPS origin configuration and an explicit access policy are supplied. No product behavior requires or inspects Tailscale, private DNS, or private IP topology.

## Injected applications

Construct `ProductGatewayASGIApplication` with:

- `GatewayConfig`
- an Observatory ASGI application
- a request-gateway ASGI application
- a `SwarmCoordinator`
- server-side request-gateway credential
- optional distinct Observatory credential
- explicit access policy for non-loopback serving

The gateway is framework-free ASGI and can be hosted by an ASGI server selected by deployment code.

## Verification

```bash
python3.14 -m pytest -q tests/ui_gateway
PYTHONPYCACHEPREFIX=/tmp/mycelium-ui-gateway-pycache \
  python3.14 -m compileall -q mycelium_ui_gateway tests/ui_gateway
git diff --check
```

Expected claim boundary: local browser-facing composition and contract validation only. Qualifier-owned accepted evidence remains the sole route-readiness authority.
