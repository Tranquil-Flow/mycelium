# Physical qualification preflight

## Status and claim boundary

`mycelium_physical_preflight` is a read-only validator and inert execution-plan
generator for a later, separately authorized two-Mac qualification.

Every successful output states:

```json
{"physical_qualification_executed":false,"release_ready":false,"route_ready":false}
```

The tool does not establish route or release readiness. It does not open SSH
connections or sockets, probe either host, transfer files, provision software,
start inference, read token bytes, clean files, or execute any generated phase.
The output is a declarative checklist, not a runner.

## Invocation

From the repository root:

```bash
python3.13 -m mycelium_physical_preflight operator-plan.json > execution-plan.json
```

Exit codes:

- `0`: validated; stdout contains one canonical execution-plan object.
- `2`: rejected; stdout contains one redacted canonical error object.

Stderr remains empty. Errors contain only a stable code and JSON pointer:

```json
{"error":{"code":"missing_field","pointer":"/hosts/0/staging_root"},"ok":false}
```

The CLI accepts exactly one regular, non-symlink input file. It reads that file
read-only with final-component no-follow protection where the platform supports
it. It does not accept credentials or qualification commands as arguments.

Library entry point:

```python
from pathlib import Path
from mycelium_physical_preflight import canonical_json_bytes, validate_and_generate

encoded = Path("operator-plan.json").read_bytes()
value = validate_and_generate(encoded, source_tree_root=Path.cwd())
output = canonical_json_bytes(value)
```

`source_tree_root` is used only for local source-file and path-boundary checks.
It is never included in success or error output.

## Canonical JSON contract

Input protocol:

```text
mycelium.physical_qualification_operator_plan.v1
```

Output protocol:

```text
mycelium.physical_qualification_execution_plan.v1
```

The input must already be canonical UTF-8 JSON:

- object keys sorted lexicographically;
- compact separators: `,` and `:` with no surrounding whitespace;
- `ensure_ascii=false` behavior;
- no duplicate object keys;
- no `NaN`, positive infinity, or negative infinity;
- no trailing newline or other bytes;
- maximum size 1 MiB.

A canonical renderer for an in-memory operator object is:

```python
json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
```

Success and error output use the same encoding plus exactly one trailing
newline. Equal input bytes and equal source-tree metadata produce
byte-identical output. `operator_plan_digest` is the SHA-256 digest of the exact
validated input bytes.

## Strict operator schema

All listed fields are required. Unknown fields are rejected at every object
level. JSON booleans are not accepted where integers are required.

### Root object

| Field | Contract |
|---|---|
| `protocol` | Exact operator protocol above |
| `plan_id` | Lowercase immutable slug |
| `authorization_statement` | Exact generated statement described below |
| `hosts` | Exactly two entries: coordinator first, peer second |
| `coordinator` | Exact address, port, and coordinator host binding |
| `identities` | Immutable deployment/model/route identity object |
| `source_files` | Sorted, unique, explicit repository-relative regular files |
| `run_matrix` | Exact cold and warm contracts |
| `decode_parity` | Exact eight-step parity contract |
| `negative_tests` | Exact fail-closed test list |
| `evidence` | Copyback and verification requirements |
| `cleanup` | Run-scoped cleanup requirements |
| `abort_conditions` | Exact abort-condition list |
| `rollback` | Exact rollback policy |

### Hosts

Each host requires:

```text
role
host_name
ssh_user
staging_root
token_file_path
endpoint_id
expected_generation
assignment_id
assignment_digest
evidence_copyback_destination
```

Additional constraints:

- host order and roles are exactly `coordinator`, then `peer`;
- host names, EndpointIDs, and assignment IDs are nonempty and pairwise unique;
- SSH users are explicit, syntactically safe, and never `root`;
- expected generations are positive integers;
- assignment digests are lowercase `sha256:` digests.

### Coordinator

The object contains exactly:

```text
host_name
address
port
```

`host_name` must equal the coordinator host entry. Address must be a valid IP
literal or DNS name and cannot be loopback, link-local, multicast, reserved,
unspecified, or `localhost`. Port must be an unprivileged integer from 1024
through 65535.

### Immutable identities

The object contains exactly:

```text
deployment_id
deployment_epoch
topology_generation
model_id
resolved_commit
model_manifest_digest
route_id
route_plan_digest
execution_graph_digest
assignment_bundle_digest
```

`model_id` is a repository-style `owner/model` identity, never a URL or mutable
revision query. `resolved_commit` is a lowercase 40- or 64-hex commit identity.
All four digest fields are lowercase SHA-256 identities. Epoch and generation
values are positive integers.

### Source files

`source_files` must be a nonempty, lexicographically sorted, duplicate-free
array. Every item must be an explicit canonical repository-relative path:

- no absolute path;
- no `.` or `..` component;
- no hidden component or credential-like source name;
- no glob, wildcard, or directory copy;
- target must currently exist under the source tree as a regular, non-symlink
  file;
- source-tree and file traversal uses read-only, close-on-exec, no-follow,
  descriptor-relative opens with device/inode identity comparisons;
- the source-tree identity is rechecked after validation, and descriptors close
  on every success and failure path.

The generated `stage_explicit_files` phase contains one declarative staging
record per host and per listed file. No unlisted file is implied.

## Path fail-closed policy

All staging, token-indirection, and evidence-copyback values must be canonical
absolute POSIX paths. Empty, missing, relative, `//`-rooted, dot-normalized,
shallow, control-character-bearing, source-tree-overlapping, and unsafe paths
are rejected.

A staging root must have this exact Mac user-home suffix:

```text
/Users/{ssh_user}/mycelium-physical-qualification/{plan_id}/{host_name}
```

A token path must be a strict descendant of that host's staging root and begin
with `.credentials/` relative to the staging root. It names a file location;
token bytes are forbidden in the plan and output.

An evidence destination must end with:

```text
mycelium-physical-qualification-evidence/{plan_id}/{host_name}
```

The two staging roots cannot be equal or ancestor/descendant. Evidence
copyback destinations cannot overlap either staging root or each other.
Staging, token, and copyback paths cannot overlap the source tree lexically or
after local resolution.

The validator performs `lstat`-style checks on every path component visible on
the machine running preflight and rejects any visible symlink component. It
never probes a remote host. Therefore a path component that exists only on a
future remote host is not claimed to have been inspected. The generated plan
requires the same checks again host-locally before any later side effect; any
missing path value or failed host-local revalidation is an abort condition.
This separation preserves both fail-closed operation and the prohibition on
physical host probing during preflight.

## Authorization statement

The statement is not free-form. It must byte-match this template after values
are substituted from the same plan:

```text
I explicitly authorize a later Mycelium physical qualification between {coordinator_host} as SSH user {coordinator_user} and {peer_host} as SSH user {peer_user}; stage only the declared source_files under {coordinator_staging_root} and {peer_staging_root}; use only token-file indirection at {coordinator_token_file_path} and {peer_token_file_path}; bind the coordinator to {address}:{port}; copy evidence to {coordinator_copyback} and {peer_copyback}; then apply the declared cleanup, rollback, and abort conditions. This statement authorizes only a later operator run; this validator performs no physical qualification.
```

Changing a named host, user, staging root, token path, coordinator address or
port, or copyback destination without regenerating the statement fails closed.
The canonical operator-plan digest binds the complete source-file, identity,
run, parity, negative-test, evidence, cleanup, and rollback objects.

## Cold and warm matrix

The exact required matrix is:

```json
{"cold":{"cache_precondition":"absent","expected_network_bytes":"positive","local_files_only":false},"warm":{"cache_precondition":"same_pinned_assignment","expected_network_bytes":"zero","local_files_only":true}}
```

Cold and warm evidence remains distinct. Warm qualification requires the same
pinned assignment, local-files-only loading, and zero expected network bytes.

## Eight-step decode/parity contract

Required values:

```text
decode_steps = 8
mode = stage_local_kv
oracle = independently_loaded_monolithic
token_match = exact
require_single_token_decode = true
require_no_full_prefix = true
activation_abs_tolerance > 0 and <= 1
final_logits_abs_tolerance > 0 and <= 1
```

`per_step_evidence` must be this exact ordered list:

```text
active_cache_snapshots
child_tensor_ownership
distributed_and_reference_tokens
input_token_and_position
max_numeric_error_and_tolerance
stage_digests_and_cache_lengths
```

The generated plan has exactly eight records indexed `0` through `7`. Each
record retains `route_ready=false` and `release_ready=false`.

## Required negative tests

The array is exact and lexicographically ordered:

```text
assignment_identity_mismatch
authorization_statement_mismatch
dropped_peer
endpoint_generation_mismatch
expired_reservation
full_model_fallback
missing_tensor
route_identity_mismatch
sequence_replay
simulator_participation
staging_root_symlink
stale_proof
synthetic_timing
token_file_inline_value
wrong_endpoint
wrong_revision
```

Every generated negative-test action expects `fail_closed`. Unexpected
acceptance aborts the later run.

## Evidence, cleanup, rollback, and abort

`evidence.copyback_order` must equal coordinator host then peer host. These are
all required `true`:

```text
preserve_distinct_cold_and_warm
require_immutable_manifest
verify_before_cleanup
```

Every cleanup flag is required `true`:

```text
remove_staging_roots
remove_token_files
stop_run_scoped_processes
require_coordinator_port_free
preserve_verified_copyback
```

Rollback is exact:

```text
scope = run_scoped_only
order = stop_then_copy_partial_evidence_then_cleanup_if_copyback_verified
preserve_remote_evidence_on_copyback_failure = true
require_reauthorization_after_abort = true
```

Abort conditions are exact and ordered:

```text
authorization_changed
coordinator_bind_failure
credential_indirection_failure
evidence_copyback_failure
identity_mismatch
negative_test_unexpected_acceptance
parity_mismatch
path_revalidation_failure
peer_generation_mismatch
source_revision_mismatch
```

## Generated phase order

A successful result contains these inert phases in fixed order:

1. `authorization_gate`
2. `host_local_path_revalidation`
3. `stage_explicit_files`
4. `credential_file_preflight`
5. `coordinator_start_and_pending_status`
6. `cold_runs`
7. `warm_offline_runs`
8. `coordinator_restart_and_report_resubmission`
9. `stage_local_kv_prefill`
10. `eight_step_decode_parity`
11. `negative_tests`
12. `evidence_copyback_and_verification`
13. `run_scoped_cleanup`
14. `abort_and_rollback`

They are data records only. The preflight package deliberately has no SSH,
socket, HTTP, subprocess, copy, provisioning, inference, deletion, or cleanup
implementation.

## Credential rejection and redaction

Allowed credential representation is only `token_file_path`. Inline token,
password, secret, cookie, authorization-header, private-key, API-key, bearer,
credential-byte, URI-userinfo, and secret-bearing CLI forms fail closed.
High-confidence token formats and private-key markers are rejected even when
placed in an otherwise ordinary identity field.

Errors never echo rejected values or input paths. Success output contains the
operator's declared host staging/token/copyback paths but never the absolute
local source worktree path. Token files are named but never opened or read.

## Later operator responsibility

Do not treat a successful preflight as permission or evidence that physical
qualification occurred. Before a later run, an operator must separately:

1. review and explicitly authorize the exact canonical plan;
2. verify both host identities, SSH users, EndpointIDs, generations, immutable
   model/assignment/route identities, and coordinator bind scope;
3. re-run path checks locally on each named host without following symlinks;
4. verify token-file ownership, regular-file type, no-follow status, and mode
   `0600` without exposing token bytes;
5. execute phases under a separately reviewed runner;
6. abort on every declared mismatch;
7. verify immutable evidence copyback before run-scoped cleanup;
8. keep route and release readiness false unless the separate qualification
   authority validates complete physical evidence.

None of those physical actions are implemented or performed by this tool.
