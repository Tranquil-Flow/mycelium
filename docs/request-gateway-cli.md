# Authenticated inference request CLI

Status: production client for the isolated request-gateway session contract. Local tests do not establish distributed or physical-route readiness.

## Privacy defaults

- The bearer credential comes from an environment variable, never a command-line argument.
- Prompt text comes from standard input, not an argument visible in process listings.
- The CLI does not log prompt text, token text, token IDs, credentials, qualification internals, or endpoint details.
- Generated token text is written only to standard output.

Use a dedicated request-gateway credential. Do not reuse an Observatory credential.

## Invocation

```sh
export MYCELIUM_REQUEST_GATEWAY_TOKEN='set-out-of-band'
printf '%s' 'Your private prompt' |
  python3.14 -m mycelium_request_gateway.cli \
    --base-url http://127.0.0.1:8081 \
    --max-new-tokens 64
```

Arguments:

- `--base-url`: request-gateway origin with no path, query, or fragment;
- `--max-new-tokens`: integer passed to the versioned submission contract;
- `--token-env`: alternate environment-variable name, default `MYCELIUM_REQUEST_GATEWAY_TOKEN`.

For non-loopback deployments, the client requires `https`. Plain `http` is accepted only for loopback hosts such as `127.0.0.1`, `::1`, or `localhost`. Redirects are disabled so a bearer credential cannot cross origins.

## Production flow

The CLI has no fixture-only token path. `stream_prompt()` uses the `GatewayClient` production interface shared by the HTTP and in-process adapters:

1. fetch current qualification through `GET /v1/qualification/current`;
2. submit prompt plus the exact returned binding through `POST /v1/inference`;
3. consume `mycelium.request_event.v1` events through the request's SSE path;
4. write only authenticated `token` event text;
5. stop only on one `completed`, `cancelled`, or `failed` terminal event.

The local `ServiceGatewayClient` uses the same `RequestGatewayService` as ASGI and exists for embedding and deterministic tests. `HTTPGatewayClient` is the command-line default.

## Disconnect and resume

The CLI tracks the sequence of the last event it successfully applied. On a transient stream-open or transport disconnect, it reconnects with that sequence as `Last-Event-ID`. The server resumes strictly after it, preventing duplicate output. Sequence gaps, changed request IDs, malformed events, expired cursors, authentication failures, and non-transient server errors fail closed instead of retrying.

Default reconnect budget is three. A request continues server-side while its stream is disconnected; use the authenticated DELETE endpoint to cancel explicitly.

## Exit behavior

- `0`: completed terminal event;
- `2`: cancelled terminal event or local invocation/configuration error;
- `1`: admission, stream, backend, or protocol failure.

Error output contains stable public codes only. It does not print server exception strings or response bodies.

## Required authority wiring

The gateway process must inject a qualifier-owned `QualificationSource` returning the already-validated current `RouteQualificationV1`. The CLI cannot create, deserialize, or promote a `route_ready` qualification. Until that authority adapter and a physically accepted route exist, successful local fixture tests remain local contract evidence only.
