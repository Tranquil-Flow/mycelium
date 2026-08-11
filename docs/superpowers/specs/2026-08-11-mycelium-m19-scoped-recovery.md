# Mycelium M19 Scoped Replanning and KV-Safe Recovery Specification

**Status:** implementation baseline
**Milestone:** M19

## 1. Outcome and claim boundary

M19 adds traffic-aware liveness, scoped replanning, and truthful request recovery to
the qualified M18 replica topology. At least one physical request must continue by an
explicitly qualified recovery mode and at least one must terminate explicitly when no
compatible successor exists.

M19 does not infer recovery from replica availability. A replica is only a candidate
until its model, revision, representation, graph, assignment, load proof, decode mode,
KV layout, membership generation, and fresh qualification match the recovery policy.

The two recovery labels are intentionally disjoint:

- `full_context_replay` reconstructs state from the original prompt plus committed
  token IDs on a newly qualified immutable path. It never claims KV transfer.
- `fenced_kv_successor` resumes from a monotonic replicated KV watermark only when
  source and successor expose compatible, independently qualified KV ownership.

`stage_local_kv` by itself does not prove successor continuity. Heterogeneous or
unknown layouts may use qualified replay or abort; they never inherit an MLX KV label.

## 2. Frozen authorities and contracts

M19 introduces three closed privacy-reduced documents:

- `mycelium.m19_liveness.v1`: detector-owned observations and scoped incidents;
- `mycelium.m19_recovery_plan.v1`: Planner-owned successor intent and hysteresis;
- `mycelium.m19_recovery_runtime.v1`: Router/runtime-owned attempt, checkpoint,
  cutover, cleanup, and terminal facts.

The documents bind one deployment ID/epoch, topology generation, model/revision,
representation digest, graph digest, membership generation, qualification IDs and
digests, request ID, old/new path IDs and attempts, and evidence generation/digest.
Unknown fields fail closed. Prompt text, decoded text, token IDs, tensors, KV bytes,
network addresses, credentials, hostnames, and raw device identity do not cross the
browser boundary.

Gossip/membership owns peer freshness. Transport owns receipts and active failure.
Planner owns successor intent. Runtime owns checkpoints, attempts, and cleanup.
Qualifier owns recovery readiness. Observatory and the product UI only project.

## 3. Liveness and incident semantics

Detector state is `fresh`, `suspect`, `quarantined`, `failed`, or `recovered`.
One missing receipt may only enter `suspect`; it cannot remove a peer or route.

Frozen local budgets:

- active transport failure detection: at most 2 seconds after a verified disconnect;
- idle keepalive interval: 5 seconds;
- suspect threshold: 2 consecutive missed keepalives;
- quarantine threshold: 3 consecutive misses and at least 15 seconds stale;
- recovery requires 2 consecutive fresh signed observations;
- incident retention: 256 records;
- detector scope count: at most 4,096 tracked subjects.

Incidents carry one of `request`, `edge`, `placement`, `peer`, or `deployment` scope.
They record detector source, first/last observation time, old/new generation, affected
track IDs, action, and terminal outcome. Receipt suppression, idle staleness, active
disconnect, generation conflict, and circuit-break rejection remain distinguishable.

## 4. Replanning and hysteresis

Replica-only or edge loss retains every still-legal qualified track. Join, capacity
drift, and departure produce deterministic candidate intent from one atomic evidence
generation. They do not mutate the incumbent route.

Provisioning may start only after three consecutive equivalent candidate generations
over at least 10 seconds. Emergency loss may bypass the time delay only when the
incumbent has no legal track; it may not bypass artifact, load, graph, challenge, or
qualification gates. Candidate identity is content-addressed and generation fenced.

Circuit-break policy permits at most 2 failed recovery attempts per logical request
and opens for 30 seconds after 3 successor failures in 60 seconds. An open breaker
aborts new recovery attempts with an explicit terminal reason.

## 5. Request checkpoint and recovery

Each accepted output token advances a monotonic committed-token watermark. A recovery
attempt binds the exact preceding attempt, committed count and digest, path generation,
and successor qualification. A stale, repeated, skipped, or regressed watermark is
rejected before successor work begins.

For `full_context_replay`, the successor receives the original encoded prompt plus the
committed generated token IDs as recovery prefill. Only tokens after the committed
watermark may reach the client. Parity is checked against an uninterrupted reference.

For `fenced_kv_successor`, source and successor must bind identical KV schema version,
model representation, layer range, attention layout, dtype, rope configuration,
decode mode, and checkpoint digest. Watermarks must be contiguous and acknowledged by
both owners. Any mismatch falls back to qualified replay when available, otherwise the
request aborts. It never silently restarts or duplicates output.

Failure before KV allocation releases reservations and may readmit from the original
prompt. Failure after KV allocation requires one of the two explicit modes. Every
terminal path releases source and successor KV, path reservations, capacity, pending
deliveries, and temporary checkpoint state exactly once.

## 6. Control-plane continuity

M19 reuses the M12 durable seed backup, restore, rotation, and corruption gates. It
does not create another coordinator authority. Durable reconciliation records retain
deployment/member generation, product event cursor, request attempt/watermark, and
terminal state.

After restart, each in-flight request is reconciled to exactly one of `resumed`,
`aborted`, or `already_terminal`. A stale process cannot publish a newer attempt or
qualification. Restart during idle, admission, provisioning, active decode, cutover,
and revocation must preserve one authority and one terminal history.

## 7. UI convergence

The existing eight workspaces remain the product surface:

- Incidents shows detector scope, failure point, old/new track and generation,
  recovery mode, KV outcome, replay/checkpoint action, cutover and terminal status.
- Network ghosts superseded routes and distinguishes candidate, loading, probing,
  active, resumed and aborted states without inventing topology.
- Plans shows deterministic candidate intent, hysteresis observations, retained
  tracks, rejected successors and circuit-break state.
- Readiness withholds successor readiness until artifact, load proof, graph,
  challenge and qualification generations all match.
- Inference request history shows attempt count, committed-token count, recovery mode,
  successor track and explicit terminal outcome without exposing token IDs.

Refresh, section navigation, Back/Forward, reconnect and tab-session continuity must
retain the same privacy-reduced evidence and terminal history.

## 8. Verification gate

M19 is complete only when all of the following pass:

1. Contract tests reject unknown/private fields, stale generation, watermark rollback,
   incompatible KV, duplicate terminal state, unqualified successor and breaker bypass.
2. Pure tests cover active versus idle detection, receipt suppression, scoped loss,
   surviving tracks, hysteresis, replay, KV successor, fallback and exact cleanup.
3. A physical two-host run proves one request continues through qualified full-context
   replay with token parity and no duplicate token delivery.
4. A physical negative run proves incompatible/no-successor termination and exposes no
   continuity claim.
5. Restart reconciliation proves one authority, monotonic cursor/attempt history and
   exactly one terminal result.
6. UI/browser checks cover all affected routes, refresh/reconnect, Back/Forward,
   accessibility, positive recovery and truthful abort.
7. Focused, contract-audit, frontend and full backend suites pass before the separate
   M19 commit.
