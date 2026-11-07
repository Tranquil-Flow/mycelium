<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# L7 registry bindings and remaining declaration requests

Validation registry: merged preview 2 `d3973af9` (direct branch builds; no mirror).
Oracle sources: `f4e366e98a1f705dfaf182897044500d290dbd7f`.
This is an explicit partial binding inventory, **not** a completed L1 audit.

## Bound through actual registry accessors

- `ResourcePolicy.control_archive_bytes` → `ControlArchiveAllowance`.
- Safetensors header bytes → `PyMyceliumA13MacosLayerSplitMaxHeaderBytesL47`.
- `ResourcePolicy.staged_copy_factor` → `StagedCopyFactor`.
- `ResourcePolicy.slice_header_bytes` →
  `PyMyceliumA13MacosSliceAcquisitionMaxInlineBytesPerNodeL64`.
  This is a source-backed alias: `model_resources.py:77-79` explicitly names
  `slice_acquisition.MAX_INLINE_BYTES_PER_NODE`. A same-sized generic frame bound
  is not a substitute. All three reject differing policy values.
- Signed membership TTL → `PyMyceliumMembershipContractsMaxMessageTtlSecondsL30`;
  clock skew → `PyMyceliumMembershipContractsMaxClockSkewSecondsL31`.
  Tests capture exact runtime budget reads. These are not member lease/command
  TTLs or worker-expiry substitutes.
- Existing admitted wall authority: `OwnerChosenWall`, `MemberChosenWall`,
  `UnknownMemberWall`; compile ceiling: `Compile`. L6 composition additionally
  uses the current durable caps, retaining member/owner authority provenance.

## L1 requests (do not use coincidentally equal durations/bytes)

Supply typed source-bound rational/size/count declarations and the policy factory
that builds the currently explicit `SplitPolicy` / `ResourcePolicy` inputs:

| Source in pinned oracle | Requested policy |
| --- | --- |
| `layer_split.py:MIN_HOSTS, MAX_HOSTS` | Current 2..=8 host bounds |
| `layer_split.py:DEFAULT_MEMORY_FRACTION, DEFAULT_RUNTIME_OVERHEAD_BYTES, _SPEED_CLAMP_LOW, _SPEED_CLAMP_HIGH` | Split RAM fraction, runtime overhead, benchmark clamp ratios |
| `model_resources.py:RUNNER_LIMIT_DIVISOR, DISK_HEADROOM, GUARD_HEADROOM, LOAD_PEAK_FACTOR, GROUP_FIXED_BYTES` | Guard/disk/load-peak factors and fixed allocation |
| `model_resources.py:RESIDENT_FACTORS, DEMO_CONTEXT_TOKENS, MEMBER_PEAK_MEMORY_FRACTION, _MEMORY_CAP_GRANULARITY` | Runtime-quantization resident ratios, context, consent/RSS policy |
| `layer_split.py:48 _MAX_LAYERS`, `layer_split.py:46 _NODE_RE` | Source-backed 1024-layer header bound and 128-byte node-id limit; supply L1 counts/shared-validator ownership (not invented L7 policy) |

Artifact cache byte/index/chunk bounds and transport ingress maximum remain
explicit trusted caller inputs, not inferred runtime defaults. L1/L3/L9 must
supply the deployment policy to the cache/decoder factory. `LegacyGraphCosts`
remain explicit Python compatibility placeholders, not benchmark-derived costs.
Do not turn them into operational placement defaults without backend descriptors
and measured admitted profiles. Numeric format widths and unit conversions are
wire/representation constants, not lifecycle policy.

Restart `stop/start/safety` and stage/fetch/compile phase inputs remain typed caller
contracts. Test selections are synthetic registered bounds, **not measured p99
samples**. L6/L8/L9 still owe measurement admission and actual worker supervision;
this continuation does not invent a restart duration that makes the budget pass.
