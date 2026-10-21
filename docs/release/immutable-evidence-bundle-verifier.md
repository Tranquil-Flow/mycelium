# Immutable release-evidence bundle verifier runbook

## Scope and claim boundary

`mycelium_release_bundle` is a deterministic, verification-only reader for an
already-existing immutable release-evidence directory. It checks canonical
serialization, SHA-256 pins, exact file inventory, provenance bindings, and
declared gate presence.

It does **not**:

- collect, generate, sign, repair, copy, or promote evidence;
- call the route qualifier or evaluate `RouteQualificationV1` semantics;
- contact a peer, endpoint, remote host, model, or inference runtime;
- grant physical qualification, route readiness, or release readiness.

Every result, including a structurally valid result, contains:

- `verification_only=true`;
- `qualification_evaluated=false`;
- `physical_evidence_accepted=false`;
- `route_ready=false`;
- `release_ready=false`.

`ok=true` means only that this static verification pass completed without a
finding. It is not an acceptance or readiness signal.

## Invocation

Run from the repository root:

```sh
python3.14 -m mycelium_release_bundle verify /path/to/existing-bundle
```

Pin the manifest to a SHA-256 value obtained from an independent trusted
channel:

```sh
python3.14 -m mycelium_release_bundle verify /path/to/existing-bundle \
  --expected-manifest-sha256 'sha256:<64 lowercase hex characters>'
```

Exit status is `0` only when static verification returns `ok=true`; otherwise
it is `1`. Standard output is one canonical JSON document plus one newline.
Normal execution writes nothing to standard error. Repeated runs over unchanged
inputs produce byte-identical output: no clock, hostname, absolute bundle path,
timing, random value, or input bytes enter the result.

Never derive the trusted expected digest from the same untrusted bundle during
the verification decision. That would check consistency, not provenance.

## Directory and manifest contract

The bundle root contains exactly one unlisted control file named
`release-evidence-manifest.json` plus every artifact listed in the manifest.
Added files, missing files, added directories, symlinks, and non-regular inputs
fail closed.

The manifest protocol is:

`mycelium.immutable_release_evidence_bundle.v1`

The top-level object has exactly:

- `protocol`;
- `body`;
- `body_sha256`.

`body_sha256` is `sha256:` followed by the lowercase SHA-256 of the canonical
UTF-8 JSON bytes of `body`.

The body has exactly:

- `bundle_id`;
- `evidence_class`: `physical_qualification` or `synthetic_test_fixture`;
- `synthetic_fixture`: Boolean matching the evidence class;
- `file_count`;
- `total_size_bytes`;
- `files`;
- `declared_gates`;
- `bindings`.

A synthetic bundle ID starts with `synthetic-test-fixture:`. Every bundle ID is
an ASCII identifier of at most 128 characters: a lowercase alphanumeric first
character followed only by lowercase alphanumerics, `.`, `_`, `:`, or `-`.
Every synthetic file entry has `synthetic_fixture=true`; every synthetic JSON
artifact also has top-level `evidence_class="synthetic_test_fixture"` and
`synthetic_fixture=true`. Any synthetic artifact or binding that claims
`route_ready=true` or `release_ready=true` is rejected. Synthetic declarations
never reduce `missing_physical_inputs`.

### Canonical JSON

Manifest and JSON artifacts use UTF-8, sorted object keys, compact separators,
no insignificant whitespace, no trailing newline, no duplicate object keys,
and no NaN or infinity. The equivalent serialization operation is:

```python
json.dumps(
    value,
    allow_nan=False,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
```

The verifier exposes `canonical_json_bytes` only as a serialization primitive;
it exposes no physical-evidence builder, signer, or promotion operation.

### File entries

Each `files` item has exactly:

- `path`;
- `size_bytes`;
- `sha256`;
- `media_type`;
- `synthetic_fixture`.

Entries are sorted by path. Supported media types are `application/json` and
`application/vnd.mycelium.dependency-lock`. Current hard limits are 4 MiB for
the manifest, 16 MiB per artifact, 256 MiB total declared artifact bytes, and
4,096 artifacts.

Allowed top-level directories are:

- `control/`
- `model/`
- `provenance/`
- `qualification/`
- `release/`
- `router/`
- `run/`
- `runtime/`

Paths must be NFC-normalized relative POSIX paths. Absolute paths, Windows
paths, backslashes, empty/dot/dot-dot components, repeated separators, control
characters, exact duplicates, normalized duplicates, and case-fold collisions
are rejected. Path components naming credentials, private keys, prompts,
tokens, activations, KV caches, or private endpoints are rejected.

The verifier inventories the directory before reading, opens components with
no-follow semantics, compares named and opened inode identity, checks metadata
before and after each bounded read, and inventories again before returning.
Missing, added, modified, oversized, unreadable, replaced, or concurrently
changed inputs fail closed.

## Declared gates and exact bindings

`declared_gates` is sorted by `gate`; gate names are unique. Each declaration
has exactly `gate` and a sorted, unique, non-empty `evidence_paths` list. Every
listed path must be a manifest file entry.

Supported physical-input gate names are:

| Gate | Required binding form |
| --- | --- |
| `source_commit` | exact 40-character lowercase source commit |
| `dependency_lock` | exact file SHA-256 |
| `assignment` | exact assignment identifier |
| `model_manifest` | exact model-manifest digest |
| `deployment` | exact deployment identifier |
| `deployment_epoch` | exact non-negative integer epoch |
| `path` | exact path identifier |
| `endpoint_id` | exact opaque EndpointID, not an address |
| `stage_load_proof` | exact stage/load-proof digest |
| `transport` | exact transport evidence digest |
| `parity` | exact parity evidence digest |
| `negative_run` | exact negative-run evidence digest |
| `qualification` | exact qualification-record digest |

`bindings` is sorted by `(kind, path, json_pointer)`. Each binding has exactly:

- `kind`: one supported gate name;
- `path`: a file listed for that gate;
- `json_pointer`: RFC 6901 pointer string, or `null`;
- `expected`: the exact expected value.

A `null` pointer binds `expected` to the listed file's SHA-256. A string pointer
binds `expected` to the exact canonical JSON value at that pointer. Missing
pointer targets, wrong JSON types, malformed escapes, array-index ambiguity,
value mismatches, duplicate bindings, unbound declarations, and undeclared
bindings fail closed.

This layer checks declared values and digests only. It does not independently
prove that a transport was physical, parity was correct, a negative run
occurred, signatures were valid, or qualification semantics passed. Those are
physical collection and qualifier responsibilities outside this verifier.

## Forbidden bundle material

Bundles are digest-and-provenance surfaces, not trace archives. The verifier
rejects paths, JSON fields, binding selectors, and high-confidence byte markers
for:

- private keys and credentials;
- passwords, bearer/access material, and API secrets;
- prompts and raw token IDs;
- raw activations or hidden states;
- KV/cache contents;
- runtime/private endpoint addresses.

JSON artifacts are schema-closed to fields from the current immutable evidence
manifest and frozen `RouteQualificationV1` contract. Unknown fields fail closed;
an extension field ending in `_digest` or `_digests` is admitted only when its
value is one or more canonical SHA-256 references. Opaque identifier fields are
bounded, contain no whitespace, and cannot be IP addresses or URLs. Reason
codes remain bounded identifier lists. `claim_boundary` accepts only the exact
current physical or synthetic frozen-contract text, preventing these metadata
surfaces from becoming free-text prompt or secret channels.

Digest-only fields such as `activation_digests`, `token_parity_digest`, and
`kv_ownership_digest` remain admissible. Opaque `endpoint_id` values remain
admissible; endpoint URLs and runtime addresses do not.

Machine-readable findings contain only stable codes and ordinal subjects such
as `artifact:0002` or `binding:0004`. They never include artifact bytes,
manifest values, filenames, bundle paths, exception text, or secret matches.

## Result interpretation

Primary fields:

- `ok`: static verifier execution had no finding;
- `checks`: completed static check groups;
- `manifest_sha256` and `body_sha256`: deterministic manifest identities;
- `observed`: declared and scanned counts only;
- `missing_physical_inputs`: required categories not represented by physical
  declarations; all categories for synthetic fixtures;
- `physical_input_inventory_complete`: all categories were declared by a
  structurally valid `physical_qualification` bundle, without implying truth or
  acceptance;
- readiness and acceptance fields: always false.

A consumer must never map `ok` or `physical_input_inventory_complete` to
`route_ready` or `release_ready`. Only the existing qualifier authority may
issue a qualified route record after its independent physical-evidence gates.
This verifier never invokes that authority.

## Adversarial verification matrix

The focused suite under `tests/release_bundle/` covers:

| Threat | Fail-closed check |
| --- | --- |
| noncanonical/duplicate JSON | canonical byte equality and duplicate-key rejection |
| manifest or artifact mutation | SHA-256 and exact byte-count checks |
| missing/added input | exact pre/post tree inventory |
| traversal/absolute/Windows path | canonical relative POSIX allowlist |
| normalized duplicate/case collision | NFC and case-fold collision maps |
| symlink/non-regular input | no-follow open and file-type checks |
| oversized/unreadable input | bounded reads and permission checks |
| concurrent replacement/change | inode and metadata snapshots around reads |
| stale or altered declaration | gate-to-path membership checks |
| wrong source/dependency/identity/gate value | exact file or JSON-pointer binding |
| credentials/model-private material | path, structured JSON, selector, and byte policy |
| secret-reflecting errors | code-and-ordinal-only output |
| synthetic promotion attempt | prominent markers and readiness-claim rejection |
| output nondeterminism | repeated-process byte comparison |

Run focused verification tests:

```sh
python3.14 -m pytest -q tests/release_bundle
```

## Physical inputs absent from checked-in fixtures

The checked-in tests create only in-memory/on-disk temporary
`synthetic_test_fixture` shapes. They do not contain or establish any physical
input. Therefore all remain absent:

- source commit provenance captured by a physical run;
- dependency-lock provenance captured by that run;
- physical assignment evidence;
- physical model-manifest binding;
- physical deployment identifier and epoch evidence;
- physical route/path evidence;
- authenticated physical EndpointIDs;
- physical stage and load-proof evidence;
- physical transport evidence;
- physical parity evidence;
- physical negative-run evidence;
- qualifier-issued physical qualification evidence.

No successful physical evidence bundle is checked in or fabricated by this
lane.
