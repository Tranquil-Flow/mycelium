# A13 macOS — managed run-profile admission slice

## Scope

This document records the smallest canonical **target-owned run-profile
admission** slice delivered for the macOS A13 demo.  The slice lives at:

* `mycelium_a13_macos/managed_provisioning.py` — new owner-side admission
  module (owned).
* `tests/a13_macos/test_managed_provisioning.py` — real inert consumer
  tests (owned).
* `docs/handover/A13-MACOS-PROVISIONING.md` — this handover (owned).
* `outputs/a13-macos-evidence/provisioning-managed-admission-green.log`
  and `provisioning-managed-admission-green-verbose.log` — pytest
  receipts (owned).

It is bounded exactly to the **pre-tensor admission decision** the owner
must make before chaining into the existing canonical chain.  It does not
load tensors, fabricate readiness, mutate the seed, sign offers, or call
any registry runner.  It is *strictly stricter* than
`DemoAuthority.plan_assignments()` and *strictly less* than
`prepare_registry_launch()`.

## What the slice composes (does not reimplement)

The slice consumes existing canonical producers read-only:

| Producer | Anchor | What the slice reads |
|---|---|---|
| `DemoAuthority.status` | `mycelium_a13_macos/authority.py:538` | current identity, membership state, lease state, capability state, contribution flag |
| `DemoAuthority.coordinator.members` | `mycelium_seed/__init__.py` | per-member identity, generation, lifecycle, lease expiry |
| `DemoAuthority._metadata.target_node_id` | `mycelium_a13_macos/authority.py:285-294` | owner identity marker used to bind grant membership |
| `ModelContract` / `RESOURCE_PROPOSAL` | `mycelium_a13_macos/serving.py:38-77` | exact model identity, runtime tuple, bounded budgets |
| `PreparedSource` | `mycelium_a13_macos/serving.py:98-112` | digest/path/mode-bound source artifact already compiled by `prepare_local_source` |

It does **not** call:

* `compile_layer_assignments` (parent owns assignment compilation).
* `LocalCandidatePreparer.__call__` (parent owns operator-plan building).
* `prepare_registry_launch` (parent owns the validated registry launch).
* `RegistryLaunch.run` (parent owns the model-executing boundary).
* `MemberArtifactTransport` / `build_member_artifact_runtime` (parent
  owns the staging transport).
* `mycelium_a13_macos/managed_control.py` / `worker.py` (sibling owns
  the target mailbox/worker).
* `scripts/build_qwen_live_route.py` /
  `scripts/stage_live_route.py` (parent owns producer / staging scripts).

The slice does **not** edit, stage, commit, push, open a public listener,
or run a model.  It does **not** impersonate the existing
`mycelium_physical_runner/config.py` SSH/`local` peer schema.  It is a
**preparation gate**, not a transport adapter.

## API surface (callable from the parent)

```python
from mycelium_a13_macos.managed_provisioning import (
    ADMISSION_PROTOCOL,            # "mycelium.a13_run_profile_admission.v1"
    ADMITTED_RUN_MODES,            # frozenset({"managed_local"})
    ProvisioningAdmissionError,    # stable .code on every fail-closed branch
    RunProfile,                    # caller-supplied owner intent
    RunProfileAdmission,           # digest/path/mode-bound admission record
    admit_run_profile,             # profile -> admission (fail-closed)
    verify_run_profile_admission,  # admission -> None (recompute + freshness)
    next_execution_boundary,       # static marker -> next parent call(s)
)
```

### `RunProfile` (caller-supplied)

Closed tuple of:

* `run_mode: str` — must be `"managed_local"` (the only admitted mode).
* `expected_model_id: str` — must equal `CANDIDATE_MODEL.model_id`.
* `expected_resolved_commit: str` — must equal `CANDIDATE_MODEL.resolved_commit`.
* `expected_manifest_digest: str` — must equal `prepared.manifest_digest`.
* `source_root: str` — must equal the resolved `prepared.source_root`.
* `runtime_backend / runtime_dtype / runtime_quantization: str` — must
  equal the `prepared.model.runtime_*` tuple.
* `grant_member_node_ids: tuple[str, str]` — distinct, owner + second
  member, both currently enrolled `mac_mlx_iroh`.
* `grant_host_ids: tuple[str, str]` — distinct, must equal
  `grant_member_node_ids` for `managed_local`.
* `network_download_bytes / additional_disk_bytes / max_combined_rss_bytes /
  max_wall_seconds / run_count: int` — must be `≤ RESOURCE_PROPOSAL.*`.
* `freshness_window_seconds: int` — positive.
* `expected_artifact_cache_roots: dict[str, str]` — exactly one absolute
  POSIX path per grant member.

### `admit_run_profile(profile, prepared, authority, *, clock=None, require_capability=True)`

Reads canonical owner state, validates every marker, emits
`RunProfileAdmission`.  Failures raise
`ProvisioningAdmissionError(code=...)` with a stable string code; the
exception message is the code itself so the slice never leaks private
material.  `require_capability=False` is a documented inert-test seam
that skips the `capability_state == "self_report_accepted"` and
`contribution_enabled is True` checks; production callers must leave it
`True`.

### `RunProfileAdmission`

Frozen dataclass containing every marker the parent must chain into
`prepare_registry_launch`:

* `protocol` — `ADMISSION_PROTOCOL`.
* `admitted_run_mode` — `"managed_local"`.
* `profile_digest` — `sha256:...` of the canonical profile envelope.
* `admission_digest` — `sha256:...` of the canonical admission envelope.
* `expected_manifest_digest` — exact manifest digest pinned to source.
* `source_root` — exact normalized path pinned to source.
* `runtime_backend / runtime_dtype / runtime_quantization` — exact runtime.
* `grant_member_node_ids / grant_host_ids` — exact grant tuple.
* `network_download_bytes / additional_disk_bytes /
  max_combined_rss_bytes / max_wall_seconds / run_count` — exact budget
  tuple.
* `freshness_window_seconds` — exact freshness window.
* `owner_swarm_id / owner_seed_key_digest / owner_membership_generation /
  owner_lease_expires_at` — current canonical owner identity markers.
* `decision_clock` — epoch seconds when admission was issued.
* `resource_proposal_digest` — `sha256:...` of `RESOURCE_PROPOSAL`,
  pinning the admission to the exact bounded proposal.
* `chain` — tuple of canonical calls consumed (audit trail).

### `verify_run_profile_admission(admission, *, clock=None)`

Recomputes the admission digest from the recorded envelope and checks:

* protocol marker is `ADMISSION_PROTOCOL`;
* run mode is admitted;
* every marker matches the canonical envelope;
* (if `clock` is supplied) the freshness window has not elapsed and the
  lease has not expired.

Returns `None` on success; raises `ProvisioningAdmissionError` with a
stable code on any drift.

### `next_execution_boundary()`

Returns the explicit minimal parent insertion map the parent integration
must perform:

```python
{
    "admit_run_profile": (
        "prepare_registry_launch",
        "RegistryLaunch.run",
    ),
    "RunProfileAdmission.profile_digest": (
        "operator_plan_binding.profile_digest",
        "operator_plan.member_digest",
    ),
    "RunProfileAdmission.expected_manifest_digest": (
        "prepare_registry_launch.prepared.manifest_digest",
        "operator_plan.graph.manifest_digest",
    ),
    "RunProfileAdmission.grant_host_ids": (
        "operator_plan.controller.peers.host_id",
    ),
    "RunProfileAdmission.grant_member_node_ids": (
        "operator_plan.membership_snapshot.member_node_id",
    ),
}
```

This map is published, not invoked.  It is the slice's contract for
"exact minimal parent insertion" — the parent code reads this and
inserts the listed canonical calls in order; no foreign file is edited
by this slice.

## Tests

`tests/a13_macos/test_managed_provisioning.py` — **29 passed in 5.98 s**
(see `provisioning-managed-admission-green.log`).  Coverage:

* **Positive** — full admission binds digest, path, mode, owner state;
  admission is deterministic and recomputable; chain marker documents
  parent insertions; admission envelope contains only canonical markers.
* **Negative (parameterized 19 cases)** — wrong run mode, wrong model id,
  wrong resolved commit, wrong manifest digest, wrong source root, wrong
  runtime backend / dtype / quantization, owner not in grant, duplicate
  member in grant, non-POSIX cache root, single-host grant, excessive
  network/disk/RSS/wall budget, run-count mismatch, freshness window
  zero, stopped authority.
* **Negative (named)** — revoked target member, stopped authority,
  tampered digest, NaN clock, past freshness, mutated marker set,
  tampered manifest digest pin, source-root mismatch.

Every test exercises **real canonical owner state** (real local TLS seed,
real member enrollment, real synthetic manifest through the canonical
`compile_model_manifest` compiler, real coordinator member snapshot).
No model bytes are loaded.  No tensor-side-effect chain is invoked.  No
fabricated readiness flags are written.  The `require_capability=False`
seam is used to keep tests inert (no live MLX self-probe + HTTPS
round-trip); production callers must leave it at the default `True`.

## Exact minimal parent insertion

This slice does **not** modify any parent-owned canonical producer.
The smallest exact parent integration to chain this admission into the
existing managed path is:

1. **Caller (parent integration)** constructs a `RunProfile` from the
   owner-supplied intent and the existing `PreparedSource` already
   produced by `prepare_local_source`.
2. **Caller** invokes `admit_run_profile(profile, prepared, authority)`.
3. **Caller** invokes `verify_run_profile_admission(admission)` before
   invoking the existing canonical `prepare_registry_launch(...)` /
   `RegistryLaunch.run(approval=..., port=...)`.
4. **Caller** passes `admission.expected_manifest_digest`,
   `admission.source_root`, `admission.runtime_*`, `admission.grant_*`,
   `admission.profile_digest` as the only allowed overrides to
   `prepare_registry_launch`.

That is the entire parent integration.  No foreign canonical file is
edited; no new producer is forked.

## Exact next execution boundary

| Owner output | Next parent call (existing canonical) |
|---|---|
| `RunProfileAdmission.admission_digest` | `verify_run_profile_admission(admission, clock=now)` |
| `RunProfileAdmission.expected_manifest_digest` | `prepare_registry_launch(prepared=prepared, operator_plan=...)` |
| `RunProfileAdmission.source_root` | `prepare_registry_launch.prepared.source_root` (already pinned via `prepared`) |
| `RunProfileAdmission.grant_host_ids` | `operator_plan.controller.peers[*].host_id` |
| `RunProfileAdmission.grant_member_node_ids` | `operator_plan.membership_snapshot.member_node_id` |
| `RunProfileAdmission.profile_digest` | `operator_plan.binding.profile_digest` (parent-defined, currently absent) |

The last row identifies a **missing parent-owned producer contract**:
the operator-plan builder currently does not carry a `profile_digest`
field binding the owner admission into the canonical plan.  This is
deliberately not invented here — the parent owns the operator-plan
schema and must decide whether to extend
`mycelium_physical_runner/config.py:_controller` with a
`profile_digest` peer field, or to thread the admission through
`bind_operator_plan_to_seed.py`.  Until the parent commits to that
contract, the `profile_digest` is recorded in the admission but not yet
cross-checked against the operator plan; the digest, manifest and host
markers still gate admission and so still prevent silent marker
substitution between preparation and launch.

## What is intentionally NOT in this slice

* No producer or operator-plan builder.  Parent owns
  `LocalCandidatePreparer` / `build_qwen_live_route.py`.
* No target mailbox/worker.  Sibling owns `managed_control.py` /
  `worker.py`.
* No stage-pack acquisition.  Parent owns `MemberArtifactTransport` /
  `build_member_artifact_runtime.py`.
* No registry launch.  Parent owns `RegistryLaunch` /
  `prepare_registry_launch`.
* No live-route session factory threading into
  `PhysicalLiveRoute.from_operator_plan`.  Parent owns
  `_qualified_runtime` / `run_registry_server` integration.
* No public bootstrap, notarization, signing, or DNS work.
* No actual model load, conversion, inference, fleet, SSH, or >1 GB
  download.  The slice is source-only and inert.

## Ground rules observed

* No commit, push, PR, or `git add`.  Slice files remain untracked.
* No funds, ENS, paid inference, or organizer credentials.
* No seed-database edits; canonical `SqliteSeedState` is read-only
  through `coordinator.members()`.
* No protected hackathon workbench / W6 / A / B / C modifications.
* No public listener, tunnel, DNS, or publication.
* No actual model tensor load / conversion / inference.  HF pinned
  source is referenced but the slice never touches it.
* No >1 GB download.
* Fresh local temp fixtures only (`tmp_path`).
* No recursive delegation; this slice ran in a single subagent process.

## Status

**Local implementation:** GREEN — 29 tests pass, RED baseline captured.

**Whole-A13 readiness:** NOT CLAIMED.  This slice closes one row of the
managed chain (owner admission before tensor side effects).  C4
(managed native serving) and C5 (real user inference) require the
parent-owned operator-plan producer / mailbox session / managed
controller threading work tracked in
`outputs/a13-macos-evidence/managed-chain-map.md` and
`outputs/a13-macos-evidence/owner-session-integration-map.md`.
