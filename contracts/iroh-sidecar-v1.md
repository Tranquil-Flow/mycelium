# Mycelium authenticated iroh sidecar contract v1

Status: Phase 7 isolated contract; canonical base `dc9ca4c5e8f5bc73c2e3e9ae762afad45db25350`.

## Ownership boundary

This tranche owns only `native/iroh_transport/`, `mycelium_iroh_sidecar/`, isolated Phase 7 tests, and this contract. It does not alter Router semantics, Phase 6 qualification, UI, or `contracts/manifest.json`.

## Pinned transport

- Crate: `iroh = "=1.0.2"`.
- Custom ALPN: `mycelium.iroh.sidecar.v1`.
- Required, proven upstream APIs: `Endpoint::id`, `Connection::remote_id`, `Connection::open_bi`, `Connection::accept_bi`, `SendStream::reset`, and `RecvStream::stop`.
- Sources: iroh 1.0.2 official rustdoc and upstream integration tests:
  - <https://github.com/n0-computer/iroh/releases/tag/v1.0.2>
  - <https://docs.rs/iroh/1.0.2/iroh/endpoint/struct.Endpoint.html>
  - <https://docs.rs/iroh/1.0.2/iroh/endpoint/struct.Connection.html>
  - <https://docs.rs/iroh/1.0.2/iroh/endpoint/struct.SendStream.html>
  - <https://docs.rs/iroh/1.0.2/iroh/endpoint/struct.RecvStream.html>

## Local UDS boundary

- Socket parent mode: `0700`; socket mode: `0600`.
- Sidecar accepts only a peer with the same effective UID, checked from kernel peer credentials.
- Bootstrap secret is exactly 32 random bytes read once from an inherited pipe descriptor. It never appears in argv, environment, filesystem, logs, or protocol errors.
- Handshake: HMAC-SHA256 client and server proofs over domain-separated nonces. Session keys are HKDF-SHA256 output split into directional client-to-sidecar and sidecar-to-client keys.
- Every post-handshake record carries direction-specific HMAC-SHA256, a monotonically increasing 64-bit sequence number, a kind, a 128-bit message ID, and bounded payload. Sequence must equal the next expected value; replay and gaps fail closed.
- Local record kinds: send, receive poll, delivery, acknowledgement, cancellation, configure-peer, ping, and error.

## Remote iroh boundary

- Remote identity is authenticated by iroh and compared to the configured, pinned endpoint key on both dial and accept paths.
- Only the custom ALPN is accepted.
- Each transfer uses a bidirectional stream. Receiver validates strict canonical `mycelium.router_wire.v1` framing before queue admission, then responds with an acknowledgement on the same stream.
- Cancellation resets the send stream and stops the receive stream.
- Connection or stream failure keeps the outbound message pending and reconnects with bounded exponential backoff. Duplicate message IDs remain legal; receiver deduplicates queue admission and acknowledges them. Delivery is therefore at least once, not exactly once, for the lifetime of each sidecar process; this contract does not claim crash-durable disk spooling.

## Bounds and ingress

- Operational Router frame cap: 16 MiB, including canonical Router wire prefix, header, and payload.
- Outbound and inbound queues are independently bounded; saturation fails explicitly and never allocates an unbounded backlog.
- Every outbound UDS send and inbound iroh transfer passes the canonical Rust Router wire decoder before queue admission.

## Observability

Logs contain event names, coarse counts, and public endpoint IDs only. Bootstrap/session keys, HMACs, Router bodies, payloads, message IDs, paths, and full peer addresses are never logged.
