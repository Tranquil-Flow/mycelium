A4 qualification install design (corrected 2026-08-19)
=========================================================

## Root constraint

`qualification_digest` (mycelium_request_gateway.contracts.qualification_digest)
hashes the ENTIRE canonical RouteQualificationV1 record, including
`qualification_id` which is itself a hash over the random startup request_id
(`startup-<uuid4>`) and the wall-clock issued_at_unix_ms. Therefore:

  * A pre-sealed A4 qualification file can NEVER install into a fresh serve
    (digest mismatch by construction; set_a4_qualification rejects).
  * The A4 document must be built IN-SESSION at startup, bound to the serve's
    own just-issued qualification digest.

## Identity fields across serve restarts (empirically verified)

  Stable (bind gate artifacts to the deployment): deployment_id,
  deployment_epoch, model_id, resolved_commit, manifest_digest, graph_digest.
  Per-session (volatile): qualification_digest, path_manifest_digest.

## Serving flow (final)

  1. Operator runs the physical gates (positive / data-plane / qualification /
     shutdown) against a running serve, producing artifacts on disk.
  2. Operator restarts the serve with four evidence flags:
       --a4-positive-observation FILE            (repeatable)
       --a4-negative-data-plane-observation FILE (repeatable)
       --a4-negative-qualification-observation FILE
       --a4-negative-shutdown-observation FILE
  3. The supervisor loads + validates every artifact:
       * protocol match per artifact kind
       * passed is True (negatives) / qualification_claim False + terminals
         completed+cancelled + within_total_bound True (positive)
       * positive artifacts' stable identity digests equal the route's live
         execution graph digests
  4. Supervisor builds mycelium.product_concurrency_liveness_qualification.v1
     in-session: qualification_digest = digest of ITS OWN qualification;
     evidence_digest = canonical digest over all supplied artifacts.
  5. route.set_a4_qualification(document) -> concurrency_liveness_qualification
     becomes eligible -> product projector transport+runtime readiness become
     ready -> UI unified-evidence gate opens live inference.

## Artifact kinds (verified protocols)

  mycelium.a4_product_positive_observation.v1
    - qualification_claim must be False, promotion_authorized False (owner
      promotion is this install act itself)
    - streams: >=1 completed and >=1 cancelled terminal, within_total_bound
  mycelium.a4_product_negative_data_plane.v1
    - passed True, scoped incident + bounded cleanup + healthy-peer release
  mycelium.a4_product_negative_qualification_observation.v1
    - passed True, 409 zero-delta rejection
  mycelium.a4_product_negative_shutdown_observation.v1
    - passed True, bounded SIGTERM shutdown

## Files

  mycelium_live/supervisor.py   _install_a4_qualification_from_evidence(...)
  mycelium_demo/cli.py          four --a4-* flags, forward to supervisor
  tests/a4_acceptance/test_install.py  deterministic RED->GREEN tests

## Removed

  The earlier file-based --a4-qualification-file pre-seal design (digest trap)
  and scripts/seal_a4_qualification.py in its pre-seal form.
