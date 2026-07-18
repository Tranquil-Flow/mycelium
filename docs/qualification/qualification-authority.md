# Qualification current-record authority

`mycelium_qualification.QualificationAuthority` closes the request gateway's
qualification-source composition gap without moving qualification authority
into the gateway.

## Boundary

The authority is an in-memory, qualifier-owned current-record source. It:

- snapshots evidence bytes and the manifest before verification;
- calls `qualify_route` itself rather than accepting caller-constructed records;
- publishes only a `RouteQualificationV1` returned by that validation;
- exposes the exact immutable object through `current()` for the gateway's
  read-only `QualificationSource` protocol;
- retains no evidence files or protected edits;
- stores at most one current record;
- expires the record at the validated route challenge's exact
  `valid_until_unix_ms` boundary;
- drops the record and fails closed on wall-clock rollback;
- rejects a late concurrent qualification if a newer deployment epoch,
  topology version, or issue time already won publication;
- supports compare-and-swap removal by exact `qualification_id`.

The authority is deliberately memory-only. Process restart starts with no
current route. There is no persistence fallback, record deserializer, generic
`publish()` method, or route-readiness override.

## Composition

```python
from mycelium_qualification import QualificationAuthority
from mycelium_request_gateway.init import create_request_gateway_application

source = QualificationAuthority(clock_unix_ms=trusted_unix_clock)
source.qualify_and_publish(
    evidence_files=evidence_files,
    evidence_manifest=evidence_manifest,
    verify_gossip_signature=verify_gossip_signature,
    verify_load_proof_signature=verify_load_proof_signature,
)
application = create_request_gateway_application(
    qualification_source=source,
    router=router,
    codec=codec,
    bearer_token=bearer_token,
)
```

The signature verifiers and clock remain explicit injected dependencies. The
request gateway only captures and revalidates the authority's current exact
record; it does not parse evidence or promote a route.

## Verification

Focused coverage lives in `tests/qualification_authority/` and includes empty
startup, exact-object publication, gateway consumption, exact expiry,
transactional failure, idempotent replay, stale concurrent completion,
compare-and-swap drop, clock rollback, and boolean-clock rejection.

## Claim boundary

The tests use deterministic in-memory evidence shaped like physical evidence.
They do not establish physical transport, two-Mac execution, fresh-machine
bootstrap, release readiness, or route readiness for this checkout. Local
evidence only; `route_ready=false` for the integrated MVP until separately
authorized physical qualification passes.
