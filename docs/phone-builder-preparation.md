# Phone preparation: assignment, provenance and runtime boundaries

This is preparation, not a route-ready, component-complete, L2 or physical claim.
All live issuance/application, APK approval, transfer and physical work remain
OWNER-fired. Never run these operations against live authority as a test.

## Validated issuance

1. Run the existing builder preparation-only flow. Keep its compiled phone
   assignment JSON, signed native stage manifest and intended topology together.
2. The owner issues the route using the existing `mycelium_seed.route_assignment`
   `issue` command and its normal authority, peer, controller and artifact-source
   arguments, **plus `--preparation-assignment PATH`**, pointing at that compiled
   phone assignment. The production API equivalent is
   `issue_route_assignment(..., preparation_assignment=assignment)`.
3. Issuance validates the deterministic assignment ID, protocol, native normalized
   runtime, node, model/revision, range and tensor coverage against the verified
   signed pack. It atomically stores the complete assignment with the route row
   and uses its deterministic ID. It refuses a simultaneous `id_source` override.
   Ordinary non-preparation issuance remains available, but is not sufficient for
   builder authorization; replacing a route without a preparation clears that
   binding. Do not repair old records by editing SQLite or signed observations.
4. Apply that exact issued ID through the ordinary seed/native route protocol.
   Configure the actual native process using the unchanged compiled profile.
   Authorize only with its original signed configured/snapshot records and an
   independently approved build receipt digest. The issuer and final builder
   reprojection compare the configured ID to the current applied route ID.
   A replacement ID invalidates the earlier configured record even for the same
   pack, key, incarnation, membership generation and controller.

## Native provenance limitation

Native `359ee19` explicitly identifies `manifest_digest` and `load_generation` as
**controller claims**, not independently observed native load authority. The
builder preserves and requires that limitation. It independently binds the graph
manifest identity through the seed-stored compiled assignment, and generation
through the seed route generation. `preparation_binding.provenance` names this
source, rather than relabeling those claims as native observations. Full builder
binding also reconstructs the pack from exact source tensors/config and compares
it, and compares the complete compiled assignment to the seed binding.

A missing/legacy preparation binding or missing/altered provenance descriptor
refuses; it is not silently qualified. Native applied pack/backend/build/golden
fields retain their actual native meaning. Physical qualification must still
consume the original signed records, current authority and its own independent
distributed challenge. This change does not grant that qualification.

## Freshness and authority

The issuer owns the clock; request JSON cannot specify it. A callable `clock`
argument is solely an injected local dependency for deterministic controls. Time
is sampled after `BEGIN IMMEDIATE` and again after verification, immediately
before successful commit/release. Member lease and heartbeat, applied-evidence
recency, route expiry and authorization expiry must still be valid at that final
check. The last guarded check while holding the transaction is the publication
linearization point. Issuer/reprojection do not mutate authority rows. Existing
initializers can still create/migrate schemas; logical row nonmutation is not a
claim of byte-for-byte SQLite nonmutation.

## Packed runtime

The existing transfer file/byte limits are unchanged. Large closures are still
transferred in one archive, but a stdlib-only bootstrap verifies the archive hash,
exact unique member set, names/types, sizes and per-member hashes before importing
anything from it. It extracts into a private temporary filesystem tree, capped at
4096 members/256 MiB uncompressed, and cleans it on normal exit. Traversal,
symlinks, duplicate/extra/missing/corrupt members refuse. SIGKILL is not normal-exit
cleanup. The launcher itself and archive remain protected by the transfer manifest.

`physical_inference_node._runtime_build_digest` reads the five real source files
from this tree. Missing bytes now fail with `runtime_build_source_unavailable`,
never a fingerprint containing synthetic missing markers. Module-relative resource
lookup uses the same tree. Template-declared non-Python resources are included
inside the archive and beside it for explicit command-line resource paths.
Reachable macOS lookup controls cover:

- `.build-native/mycelium-iroh-sidecar` via `mycelium_a13_macos.native_runtime`;
- `.build-native/mycelium-resource-probe` and
  `mycelium_a13_macos/native/resource_probe.m` via `resources`.

Those lookup tests use explicit tiny resource fixtures, not proof that optional
native helpers were built. Actual lifecycle tests use a real separately supplied
loopback sidecar, not the stub locator resource, and execute emitted node bytes
with `-I` and no checkout `PYTHONPATH` in desktop-only and mixed topologies.

## Reproduction and boundaries

Run `tests/seed_coordinator/test_phone_review_controls.py`,
`tests/live/test_runtime_archive.py` and `tests/live/test_runtime_lifecycle.py`
through the lead's serialized `run_focused.py`, alongside the existing builder,
seed, stage-pack, host, qualification and strict-plan regression files.

Invoke `tests/iroh_conformance/run_phone_builder_native.py OUTSIDE_SOURCE_DIR`
only through `run_bounded.py`, with the retained lane `CARGO_TARGET_DIR`. It exports
committed `2fd3cff` native carrier/contracts plus `359ee19` mobile core into an owned
fixture; no parent or worker source is changed. The adapted committed composition
fixture uses the native test constructor (including `control_transition`), the real
control server, native Qwen runtime and unmodified signed configure/snapshot/stop.
Its source identity is the exact committed fixture tree. It exercises production
seed issuance/applied acknowledgment and builder authorization/reprojection.

The fixture directly installs the exact applied seed row into native state; it does
not test phone acquisition/onboarding or seed HTTP. Model, membership, receipt/APK
artifact and desktop challenge environment are synthetic. No genuine APK build,
physical installation, thermal/energy qualification, distributed token challenge,
roaming, remote staging or owner approval is inferred. The parent must admit the
combined host/native source, regenerate/repin artifacts and run its own approved
full composition/L2/physical gates. Source data deadline and 42 C policy are unchanged.
