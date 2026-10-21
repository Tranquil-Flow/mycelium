# Qualification evidence-diff inspector

`mycelium_qualification_diff` compares two explicitly supplied qualification
evidence bundles. It validates bundle integrity, performs a deterministic
structural or digest comparison, and returns canonical JSON bytes.

This is an inspection-only component. It does not execute qualification,
contact nodes, inspect live processes, read the filesystem, or confer any
readiness state.

## Claim boundary

> read-only structural and digest diff of supplied qualification evidence bytes
> only; qualification semantics, physical execution, route readiness, and
> release readiness are not evaluated or granted

Every successful report sets:

```json
{"inspection_only":true,"qualification_evaluated":false,"release_ready":false,"route_ready":false}
```

An identical result means only that the validated supplied bytes have no
reported structural or digest differences. It is not evidence of physical
execution, qualification, route readiness, or release readiness.

## API

```python
from mycelium_qualification_diff import inspect_evidence_diff

report_bytes = inspect_evidence_diff(
    baseline_manifest_bytes,
    baseline_files,
    candidate_manifest_bytes,
    candidate_files,
)
```

Each manifest and each file body must have exact type `bytes`; subclasses and
other bytes-like objects are rejected. Each file mapping must have exact type
`dict[str, bytes]`. The inspector uses only these in-memory arguments. It has no
CLI and never resolves or opens a manifest path.

The function returns UTF-8 canonical JSON as exact `bytes`: keys sorted,
compact separators, non-ASCII text preserved, non-finite numbers rejected, and
no trailing newline. Validation failures raise `EvidenceDiffError`; its `code`
and string form contain only a stable machine-readable error code.

## Manifest contract

Both manifests must use protocol
`mycelium.route_qualification_evidence_manifest.v1` and contain exactly:

```json
{
  "evidence_class": "physical_qualification",
  "file_count": 1,
  "files": [
    {
      "path": "run/qualification.json",
      "sha256": "sha256:<64 lowercase hexadecimal characters>",
      "size_bytes": 123
    }
  ],
  "protocol": "mycelium.route_qualification_evidence_manifest.v1",
  "run_id": "qualification-run-id",
  "total_size_bytes": 123
}
```

`evidence_class` is either `physical_qualification` or
`synthetic_test_fixture`. File entries must be sorted by path and paths must be
unique. Declared counts, lengths, total bytes, exact file set, and SHA-256
digests must agree with the supplied file mapping.

Manifests and `.json` evidence documents must already be canonical JSON.
Duplicate object keys are rejected at every depth. Validation occurs only over
supplied bytes; paths are treated as lexical evidence identifiers and are never
used for filesystem traversal.

## Lexical path rules

A path must be a non-empty exact `str` and must:

- be relative, with `/` separators;
- contain no empty, `.` or `..` component;
- contain no backslash, repeated slash, leading or trailing whitespace,
  control character, or DEL;
- fit all path and component bounds below.

These rules do not authorize path access. The package imports no filesystem,
network, clock, worker, qualification-runtime, router, or transport module.

## Bounds

Bounds are fail-closed and reported in every successful result.

| Resource | Bound |
|---|---:|
| manifests per comparison | 2 |
| bytes per manifest | 524,288 |
| files per bundle | 256 |
| JSON documents per bundle | 128 |
| bytes per file | 4,194,304 |
| total file bytes per bundle | 33,554,432 |
| UTF-8 bytes per evidence path | 512 |
| path components | 32 |
| UTF-8 bytes per path component | 128 |
| JSON container/value depth | 48 |
| JSON nodes per bundle | 100,000 |
| reported changes | 1,024 |

The manifest byte limit is separate from each bundle's total evidence-file byte
limit. A comparison exceeding any bound fails without returning a partial
report.

## Comparison model

Canonical `.json` documents are compared recursively:

- object keys in sorted order;
- arrays by position;
- scalar or type differences as changes.

Non-JSON files are compared by exact bytes and represented by SHA-256 digest.
Whole-file additions and removals are likewise represented by digest. Array
comparison is intentionally positional; inserting an element can therefore
produce changed positions plus a final addition or removal. The inspector does
not infer semantic identity or qualification meaning from array members.

Changes are classified as `added`, `removed`, or `changed`. Every change has a
stable category-derived code, such as `ENDPOINTS_CHANGED`, and includes only:

- category and change code;
- before/after SHA-256 value digests, where applicable;
- SHA-256 document-path and structural-location digests.

Required classification domains are:

- identity;
- deployment epoch;
- topology version;
- model, commit, and manifest identity;
- endpoints;
- processes;
- tensors;
- load proofs;
- signatures;
- execution graph;
- transport;
- timing;
- parity;
- KV ownership;
- negative runs;
- source provenance.

Unrecognized structural locations use `other`; top-level manifest metadata may
use `manifest`. Classification is deterministic and descriptive only. It does
not validate the semantics or authenticity of evidence values.

## Disclosure model

Reports disclose fixed protocol and claim-boundary text, bounds, counts, byte
sizes, stable codes, and SHA-256 digests. Raw evidence paths, structural keys,
identifiers, endpoints, signatures, document values, and non-JSON file bytes
are not emitted. `values_disclosed` is always `false`.

Digests are pseudonymous fingerprints, not encryption. Operators should treat a
report as potentially correlatable metadata and protect it according to the
sensitivity of the underlying evidence.

## Report shape

The report protocol is `mycelium.qualification_evidence_diff.v1`. Its main
fields are:

```text
baseline, bounds, candidate, changes, claim_boundary, identical,
input_integrity_validated, inspection_only, protocol,
qualification_evaluated, release_ready, route_ready, summary,
values_disclosed
```

`input_integrity_validated=true` means only that each supplied manifest is
canonical and internally agrees with its supplied in-memory file mapping. It
does not authenticate the source, validate signatures, or establish that the
bytes came from a physical qualification run.
