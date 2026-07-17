# Mycelium Weight Provisioning Demo V1

Status: layer-artifact provisioning MVP ready for a constrained demo; not a distributed-inference MVP. Cold/warm authenticated HTTP provisioning is proven across two physical Macs.

## Scope

This demo proves only the layer-artifact provisioning path:

1. Resolve a Hugging Face revision to an immutable commit.
2. Compile an architecture-aware, content-digested model manifest.
3. Accept either an explicit half-open V2 route or the allocator's inclusive V1 route through one upgrade boundary.
4. Compile per-peer assignments containing only the upstream Safetensors shards needed to cover each peer's layers.
5. Let each peer fetch its assigned files directly from Hugging Face.
6. Verify file size, SHA-256, Safetensors header validity, exact tensor-key coverage, and expected layer prefixes.
7. Emit assignment-bound artifact-verification reports.
8. Audit all reports without claiming that the runtime route is active.

This demo does not load layers into MLX/PyTorch, run a stage probe, or perform distributed inference. Therefore `route_ready` remains `false` by design.

## Implementation

Core files:

- `route_contract.py`: V1 inclusive range to V2 half-open conversion and route validation.
- `model_adapters.py`: architecture-specific tensor-prefix and layer-count adapters.
- `model_manifest.py`: pure manifest compiler plus pinned Hugging Face resolver.
- `layer_assignment.py`: minimal covering-shard assignment compiler.
- `weight_provisioning.py`: direct Hub fetch, artifact verification, report emission, and coordinator audit.
- `demo_weight_provisioning.py`: `orchestrate`, `provision`, and `audit` CLI.
- `provisioning_transport.py`: bearer-authenticated HTTP assignment pull, durable peer report submission/resubmission, persisted coordinator reports, restart recovery, and status CLI.

Tests:

- `test_route_contract.py`
- `test_model_adapters.py`
- `test_model_manifest.py`
- `test_layer_assignment.py`
- `test_weight_provisioning.py`
- `test_demo_weight_provisioning.py`
- `test_provisioning_transport.py`

Supported adapter families currently include:

- `model.layers.<N>`: Llama, Mistral, Mixtral, Qwen 2/3, Gemma 1/2, Gemma 3 text.
- `h.<N>`: GPT-2 and Bloom.
- `transformer.h.<N>`: Falcon.
- `model.language_model.layers.<N>`: multimodal Gemma 3 with nested `text_config` layer count.
- `encoder.layer.<N>`: BERT, included for manifest experiments but not a distributed decoder-runtime claim.

## Live fixture

Repository:

`bumblebee-testing/tiny-random-GPT2Model-sharded`

Resolved commit:

`4fca22a84867aacca5dcf7317144782ea1807e1a`

Manifest digests:

- `sha256:ab146610744700d6c39e116d692352926cba770940dfd5a9535450c19335c9bb` when `requested_revision` is `main`.
- `sha256:75a58126d140ab3b945d80482314a92d6047fa163d37c2b37d0bfc4c420ddf46` in the hardened HTTP run, where `requested_revision` is the immutable commit itself.

The manifest includes `requested_revision`, so those two manifests intentionally have different canonical digests while resolving to the same upstream commit and file hashes.

Checkpoint shape:

- 5 decoder blocks.
- 3 Safetensors shards.
- 337,852 bytes across all three weight shards.
- Shard 1 contains the token embedding and is not assigned in this decoder-only demo.

Assignments:

| Peer | Half-open range | Assigned files | Assigned bytes |
|---|---:|---|---:|
| `m4pro` | `[0, 3)` | `model-00002-of-00003.safetensors` | 150,404 |
| `evis-macbook-pro-1` | `[3, 5)` | shards 2 and 3 | 206,660 |

Layer 3 crosses a physical shard boundary: its `ln_1` tensors remain in shard 2 while its other tensors are in shard 3. The laptop assignment therefore correctly includes both files. This is the behavior V1 must preserve: exact layer coverage using whole upstream files, with unavoidable shard-level overfetch.

Generated artifacts:

`/Users/evinova-self/Projects/mycelium/demo-runs/gpt2-two-peer-v1`

Important files:

- `model-manifest.json`
- `route-plan-v2.json` (historical filename; this sealed bundle predates the namespace split)
- `assignment-m4pro.json`
- `assignment-evis-macbook-pro-1.json`
- `report-m4pro-cold.json`
- `report-m4pro-warm-post-review.json`

## M4 Pro result

Cold cache:

- Network bytes: 150,404
- Cache-hit bytes: 0
- File SHA-256 matched upstream LFS metadata.
- Safetensors header contained 39 tensors.
- All 36 tensors assigned to layers 0 through 2 were present.
- `ready_for_load: true`
- `route_ready: false`

Warm restart with `--local-files-only`:

- Network bytes: 0
- Cache-hit bytes: 150,404
- All checks repeated successfully from the local cache.

Verification gates:

- 93/93 unit tests pass on the M4 Pro.
- Python compile gate passes.
- Ruff passes on all provisioning-related source and test modules. Project-wide Ruff still reports 22 pre-existing findings in unrelated original snapshot files; this provisioning work does not rewrite those files merely to make the global count green.
- Original 33-file snapshot checksum manifest remains valid.
- No token, API key, password, or secret pattern appears in the Python sources.
- Project still has no Git metadata; no branch was created or contaminated.

## Reproduce orchestration

From `/Users/evinova-self/Projects/mycelium` on the M4 Pro:

```sh
python3 demo_weight_provisioning.py orchestrate \
  --repo bumblebee-testing/tiny-random-GPT2Model-sharded \
  --revision main \
  --node 'm4pro,0,3,/Users/evinova-self/Projects/mycelium/.demo-cache/gpt2-m4pro' \
  --node 'evis-macbook-pro-1,3,5,/Users/evinova/Projects/mycelium/.demo-cache/gpt2-laptop' \
  --out-dir '/Users/evinova-self/Projects/mycelium/demo-runs/gpt2-two-peer-v1-next' \
  --metadata-cache '/Users/evinova-self/Projects/mycelium/.demo-cache/coordinator-metadata'
```

The coordinator downloads only `config.json` and `model.safetensors.index.json` while compiling the manifest. It does not need the full model. New orchestration bundles write the compact route as `manual-provisioning-route-v1.json` with protocol `mycelium.manual_provisioning_route.v1`; `mycelium.route_plan.v2` is reserved for the product Planner contract.

## Provision one peer

```sh
python3 demo_weight_provisioning.py provision \
  --assignment /absolute/path/to/assignment-NODE.json \
  --report /absolute/path/to/report-NODE.json
```

Warm/offline verification:

```sh
python3 demo_weight_provisioning.py provision \
  --assignment /absolute/path/to/assignment-NODE.json \
  --report /absolute/path/to/report-NODE-warm.json \
  --local-files-only
```

Each assignment deliberately contains the peer's own absolute cache root. The coordinator must use the path valid on that target peer; it must not reinterpret the peer path on the coordinator host.

## Audit after all peers report

```sh
python3 demo_weight_provisioning.py audit \
  --route demo-runs/gpt2-two-peer-v1/route-plan-v2.json \
  --assignment demo-runs/gpt2-two-peer-v1/assignment-m4pro.json \
  --assignment demo-runs/gpt2-two-peer-v1/assignment-evis-macbook-pro-1.json \
  --report demo-runs/gpt2-two-peer-v1/report-m4pro-cold.json \
  --report demo-runs/gpt2-two-peer-v1/report-evis-macbook-pro-1-cold.json \
  --out demo-runs/gpt2-two-peer-v1/provisioning-audit.json
```

A successful provisioning audit means all assigned artifacts are ready for a runtime loader. It still emits `route_ready: false`; runtime load proofs and a coordinator challenge remain separate gates.

## No-SSH HTTP flow

The hardened live bundle is:

`/Users/evinova-self/Projects/mycelium/demo-runs/gpt2-two-peer-http-v1-hardened`

Its historical `deployment.json` uses only relative references (`model-manifest.json`, the legacy-named `route-plan-v2.json`, and per-node assignment filenames). New bundles use `manual-provisioning-route-v1.json`. Copying the complete directory to another parent and loading it there succeeds. Peer cache roots remain absolute target-owned paths inside assignments and are never resolved by the coordinator.

Create a temporary bearer token with owner-only permissions:

```sh
umask 077
python3 -c 'import secrets; open("/tmp/mycelium-provisioning.token", "w").write(secrets.token_urlsafe(48) + "\n")'
```

Start the coordinator on a trusted LAN address. Loopback is used here for the one-host proof:

```sh
python3 provisioning_transport.py coordinator \
  --deployment demo-runs/gpt2-two-peer-http-v1-hardened/deployment.json \
  --host 127.0.0.1 \
  --port 49872 \
  --token-file /tmp/mycelium-provisioning.token \
  --report-dir demo-runs/gpt2-two-peer-http-v1-hardened/http-reports \
  --quiet
```

Before all reports arrive, `status` emits `state: pending_reports`, `all_reports_received: false`, and exits with code 3. It exits 0 only after a complete successful audit; a completed failed audit exits 2.

Each peer fetches its assignment, verifies/downloads locally, atomically persists its report, and only then POSTs the report:

```sh
python3 provisioning_transport.py peer \
  --coordinator-url http://127.0.0.1:49872 \
  --deployment-id 1724a5f6-c527-411f-aa6a-aade96767b90 \
  --node-id http-peer-a \
  --token-file /tmp/mycelium-provisioning.token \
  --report demo-runs/gpt2-two-peer-http-v1-hardened/local-report-http-peer-a.json \
  --local-files-only

python3 provisioning_transport.py peer \
  --coordinator-url http://127.0.0.1:49872 \
  --deployment-id 1724a5f6-c527-411f-aa6a-aade96767b90 \
  --node-id http-peer-b \
  --token-file /tmp/mycelium-provisioning.token \
  --report demo-runs/gpt2-two-peer-http-v1-hardened/local-report-http-peer-b.json \
  --local-files-only
```

If the POST fails after provisioning, retry from the durable report without downloading or verifying again:

```sh
python3 provisioning_transport.py resubmit \
  --coordinator-url http://127.0.0.1:49872 \
  --deployment-id 1724a5f6-c527-411f-aa6a-aade96767b90 \
  --node-id http-peer-a \
  --token-file /tmp/mycelium-provisioning.token \
  --report demo-runs/gpt2-two-peer-http-v1-hardened/local-report-http-peer-a.json
```

Observed hardened run:

- Both logical peers used warm caches: 0 network bytes; 150,404 and 206,660 cache-hit bytes.
- A persisted peer-A report was resubmitted idempotently without provisioning.
- Final status: `state: verified`, `all_reports_received: true`, and `all_assignments_verified: true`.
- `route_ready` remained `false`, as required at the artifact-only gate.
- Restarting the coordinator recovered both persisted reports and the successful audit.
- No SSH or Tailscale participated in assignment delivery or report return.

## Two-physical-machine qualification

The transport no longer requires SSH or Tailscale as protocol dependencies. A peer needs only:

1. Network access to the coordinator HTTP endpoint.
2. Outbound HTTPS access to Hugging Face for a cold cache.
3. The deployment ID, node ID, and bearer token.

Physical qualification completed on two Macs connected to the same phone hotspot, with the provisioning endpoint carried over their Tailscale addresses:

- Evidence: `demo-runs/gpt2-two-physical-http-v1/physical-machine-qualification.json`.
- Deployment: `5bf418bb-2735-42a8-b9d1-5c4f3546f02b`, epoch 1.
- Coordinator: `m4pro` (`100.84.252.4:49874`).
- Remote peer: `evis-macbook-pro-1` (`100.126.111.123`).
- Observed Tailscale RTT before provisioning: 13 ms.
- Commit: `4fca22a84867aacca5dcf7317144782ea1807e1a`.
- Manifest: `sha256:75a58126d140ab3b945d80482314a92d6047fa163d37c2b37d0bfc4c420ddf46`.

| Physical peer | Range | Cold network bytes | Cold cache-hit bytes | Warm network bytes | Warm cache-hit bytes |
|---|---:|---:|---:|---:|---:|
| `m4pro` | `[0, 3)` | 150,404 | 0 | 0 | 150,404 |
| `evis-macbook-pro-1` | `[3, 5)` | 206,660 | 0 | 0 | 206,660 |

Both cold caches were absent before provisioning. Each machine fetched its assignment over authenticated HTTP and downloaded only its minimal covering upstream shard set directly from Hugging Face. Both warm `--local-files-only` runs reused the same pinned-commit caches with zero network-download bytes.

The coordinator reached `state: verified`, persisted both reports and the successful audit, then recovered the same verified state after a full stop/restart. Report-only resubmission from both machines remained idempotent after recovery. `ready_for_runtime_load` was true while `route_ready` remained false.

SSH was used only to launch the remote test command, install the temporary token, and copy evidence back; assignment delivery and report return used the provisioning HTTP protocol. Tailscale served as the trusted network path in this qualification, not as an application-layer requirement. For untrusted networks, terminate TLS in front of the coordinator; bearer-authenticated plaintext HTTP is suitable only for loopback, a trusted LAN, or a trusted overlay.

## Final cold-cache qualification

Final evidence directory:

`/Users/evinova-self/Projects/mycelium/demo-runs/gpt2-two-peer-http-v1-final-cold`

Deployment identity:

- Deployment: `95160ff3-9f37-457c-8558-a781008e8673`, epoch 1.
- Commit: `4fca22a84867aacca5dcf7317144782ea1807e1a`.
- Manifest: `sha256:75a58126d140ab3b945d80482314a92d6047fa163d37c2b37d0bfc4c420ddf46`.

Both peer cache roots were confirmed absent before the run. Each logical peer then fetched only its minimal covering upstream shard set directly from Hugging Face:

| Peer | Range | Cold network bytes | Cold cache-hit bytes | Warm network bytes | Warm cache-hit bytes |
|---|---:|---:|---:|---:|---:|
| `cold-peer-a` | `[0, 3)` | 150,404 | 0 | 0 | 150,404 |
| `cold-peer-b` | `[3, 5)` | 206,660 | 0 | 0 | 206,660 |

Final observations:

- Both cold reports passed file digest, Safetensors header, tensor-key, tensor-prefix, commit, manifest, range, and assignment checks.
- Coordinator reached `state: verified` with `all_assignments_verified: true`.
- Coordinator restart recovered both durable reports from the deployment/epoch namespace.
- Warm `--local-files-only` provisioning reused exactly the same assigned caches and commit with zero network bytes.
- `--force` archived both cold local reports before writing warm evidence; no prior proof was silently destroyed.
- `route_ready` remained `false`, preserving the boundary between artifact provisioning and runtime execution.
- The complete deployment bundle was copied to another directory and loaded successfully through relative references.

## Security-hardening re-verification (2026-07-16)

A focused review found and fixed four transport-boundary defects:

1. The peer HTTP client followed redirects while carrying its bearer header. Redirects now fail closed before the client can contact the redirect target.
2. The client accepted non-HTTP URL handlers, including `file://`. Coordinator endpoints now require an `http` or `https` URL with a hostname.
3. The coordinator CLI defaulted to all interfaces. Its safe default is now `127.0.0.1`; multi-host operation must deliberately pass a trusted bind address.
4. A hand-authored noncanonical deployment ID could escape the report root when used as a path component. The coordinator now validates a canonical UUID and non-negative integer epoch before creating storage.

Each fix was developed from a failing regression test. Fresh-cache authenticated HTTP provisioning then passed with the updated source:

- Evidence: `demo-runs/gpt2-two-peer-http-v1-security-reverify-20260716/local-security-reverification.json`.
- Evidence SHA-256: `86656749cdcba2b9e313f40e3effada1e3275a1e6edd6fca32620f89882baabe`.
- Deployment: `305a9644-a04f-46ac-8926-ce3d6c8214b4`, epoch 1.
- Commit: `4fca22a84867aacca5dcf7317144782ea1807e1a`.
- Manifest: `sha256:ab146610744700d6c39e116d692352926cba770940dfd5a9535450c19335c9bb`.
- `local-peer-a` `[0,3)`: 150,404 cold network bytes, then 150,404 warm cache-hit bytes and zero warm network bytes.
- `local-peer-b` `[3,5)`: 206,660 cold network bytes, then 206,660 warm cache-hit bytes and zero warm network bytes.
- Coordinator restart recovery and report-only idempotent resubmission both passed.
- Final audit: `all_assignments_verified=true`, `ready_for_runtime_load=true`, and `route_ready=false`.
- Full suite: 361 passed, 2 optional skips, 33 subtests; provisioning subset: 61 passed plus 11 subtests; Ruff and Bandit passed.
- Temporary bearer material and coordinator processes were removed after the run.

This re-verification used two logical peers on `m4pro`; it is not new two-physical-host evidence. Cross-host source staging was blocked by the execution safety gate after its authorization prompt timed out, so the earlier two-Mac qualification remains the latest physical evidence. The four fixes are transport-boundary hardening and preserve the existing wire contract, but the updated source should still receive another two-host run when explicit staging authorization is available.

## MVP decision

This implementation is good enough for a **layer-artifact provisioning MVP** under this explicit envelope:

- public or peer-authorized Hugging Face repositories;
- sharded Safetensors checkpoints supported by the current adapters;
- direct peer-to-Hub file downloads;
- minimal covering whole-shard assignment, not exact per-layer byte transfer;
- trusted LAN, trusted overlay, or loopback HTTP; otherwise external TLS termination;
- each peer owns its Hub credentials and writable absolute cache root;
- artifact readiness only, never runtime or route readiness.

Provisioning-MVP gates:

| Gate | Result |
|---|---|
| Immutable commit and canonical manifest identity | PASS |
| Explicit gap-free half-open route | PASS |
| One identity-bound assignment per peer | PASS |
| Architecture-aware minimal covering-shard allowlist | PASS |
| Cold two-peer direct download | PASS, two physical Macs over authenticated HTTP/Tailscale |
| Exact file and assigned-tensor verification | PASS |
| Durable authenticated assignment/report transport | PASS |
| Idempotent report-only retry | PASS |
| Warm-cache restart on same commit | PASS |
| Portable deployment bundle | PASS |
| Full recursive pytest suite | PASS: 361 tests, 2 optional skips, 33 subtests |
| Provisioning test suite | PASS: 61 tests + 11 subtests |
| Provisioning Ruff and Bandit gates | PASS |

Physical qualification verdict: **PASS for the constrained two-physical-machine layer-artifact provisioning MVP**.

The current recursive project-wide pytest run is fully green: 361 passed, 2 optional Zenoh tests skipped, and 33 subtests passed. The earlier unrelated UI boundary self-scan failure no longer reproduces. The current `unittest discover` runner still treats pytest's module-level `importorskip("zenoh")` as import errors; pytest remains the canonical recursive runner, and no gossip code was changed during this provisioning work.

This is **not yet a distributed-inference MVP**. Before making that broader claim, Mycelium still needs runtime tensor loading, assignment-bound layer-load proofs, deterministic stage probes, a full-route inference challenge, and stale-replan rejection. The layer-artifact provisioning path now has real two-host validation; that result does not imply a multi-host inference runtime. Broader format and reliability coverage—single-file Safetensors, PyTorch-bin checkpoints, partial/stalled transfers, disk exhaustion, gated-model auth failures, and concurrent same-cache downloads—belongs in the next hardening tranche rather than blocking this narrowly scoped layer-downloading MVP.

## Security and claim boundaries

Implemented now:

- Immutable 40-hex commit pinning.
- Canonical SHA-256 manifest identity.
- Exact file allowlists per assignment.
- Upstream file size and SHA-256 verification.
- Safe relative artifact path validation.
- Safetensors header and data-offset validation.
- Exact assigned tensor-key and prefix coverage.
- Absolute, assignment-bound peer cache roots preserved without coordinator-side path resolution.
- Relative deployment-artifact references, allowing a complete bundle directory to move intact.
- Route-to-assignment binding for model ID, layer ranges, manifest digest, and immutable commit.
- Bearer-authenticated HTTP assignment/report transport with no-store responses.
- Fail-closed coordinator URL handling: only HTTP(S), no automatic redirects while bearer credentials are present.
- Loopback-only coordinator binding by default; non-loopback exposure requires an explicit `--host`.
- Canonical deployment UUID and non-negative epoch validation before report-storage path construction.
- Durable peer reports written atomically and directory-synced before network submission, plus report-only resubmission.
- Crash-released advisory peer-report locks and evidence-preserving forced reprovision archives.
- Durable coordinator reports namespaced by deployment and epoch, restart recovery, idempotent retries, and repair of interrupted audit publication.
- Explicit pending/verified/failed status with protocol, deployment-identity, node-partition, and state-invariant validation.
- Strict integer-type checks that reject Python/JSON numeric type confusion in proof fields.
- Duplicate/extra assignment and report rejection during audit.
- Protocol and deployment-identity checks during audit.
- Fail-closed handling for range gaps, overlaps, checksum mismatches, missing tensors, missing metadata, and unsupported architectures.

Not implemented yet:

- Runtime tensor loading.
- Runtime backend identity proof beyond the assignment declaration.
- Stage forward-pass probe.
- Signed peer reports or coordinator nonce challenge.
- Route activation.
- Exact HTTP byte-range tensor downloads.
- Custom per-layer repacking.
- Single-file Safetensors and sharded PyTorch-bin manifests.
- Automatic gated-model token distribution; each authorized peer must use its own Hugging Face credentials.

## V1 conclusion

Direct peer download from Hugging Face satisfies the constrained layer-artifact provisioning MVP. The orchestrator does not need to hold or redistribute the full model: it can remain a control-plane coordinator that resolves immutable metadata, compiles assignments, and audits reports. This is the simpler V1 architecture and avoids making the orchestrator a bandwidth and storage bottleneck.

Whole-shard overfetch remains the principal limitation because Hugging Face download APIs operate at file granularity. Custom content-addressed per-layer packs remain the clean V2 optimization if measurements justify their conversion, hosting, integrity, and licensing complexity. Runtime execution remains a separate next milestone.
