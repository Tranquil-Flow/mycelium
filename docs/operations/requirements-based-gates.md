# Requirements-based gates

Owner direction (2026-10-13): devices are incidental. A gate verifies a FEATURE on
whatever eligible devices are present. This page describes the harness in
`mycelium_gate_requirements/` that replaces hard-coded hosts in gate tooling.

## 1. Gate spec (`mycelium.gate_requirements_spec.v1`)

`mycelium_gate_requirements/specs/<gate>.v1.json`, one per gate (A1, A5, A6, A7,
A10, A11, A12, A14). Each spec declares:

- `model`: model id, weight bytes and layer count (the source of the byte figure is named);
- `resource_policy`: usable memory = `memory_bytes * usable_memory_fraction - memory_reserve_bytes`
  for reports that give total RAM; reports that give a sustainable figure use it directly;
  a role's memory need is its model share `* (1 + kv_headroom_fraction)`, its disk need is
  the share plus `disk_reserve_bytes`;
- `freshness.max_report_age_seconds`;
- `roles`: name, `count {min,max}`, `fit` (`none`, `full_model`, `{fraction_of_model}`,
  `{layers}`), `requirements` (any of `peer_classes`, `os_families`, `architectures`,
  `backends`, `min_usable_memory_bytes`, `min_free_disk_bytes`, `max_link_rtt_ms`,
  `min_link_bytes_per_second`, `thermal_states`, `power_sources`, `report_protocols`),
  `may_share_member_with` (roles are on distinct members unless listed here) and
  `same_backend_as`;
- `cases`: each case id with the minimum role counts it needs (A6
  `exhausted_recovery_attempts` needs two successors);
- `evidence`: the fields every case record and every binding entry must carry.

The spec digest is sha256 over canonical JSON, stable under key order and
whitespace. `specs/index.v1.json` pins every digest; `load_pinned_spec` refuses a
drifted file. After editing a spec run `scripts/pin_gate_requirement_specs.py`
(and `--check` in CI-like runs).

## 2. Role binding (`mycelium.gate_role_binding.v1`)

`binding.bind_roles(spec, members=..., reports=..., now=...)`:

- members: `SeedCoordinator.members()` projections or `seed_members` rows
  (`members_from_seed_database`, read-only);
- reports: signed `mycelium.a13_capability_link_report.v1` (A13 join flow: RAM, free
  disk, MLX probe, power/thermal, member-to-owner relay link) or
  `mycelium.membership.capability_report.v2` (only `native_observed` families count).
  Each report is verified against the key digest the seed holds for that member, its
  node id and generation, and its freshness;
- a member is current only in `NEW`/`CONFIGURED`/`RUNNING` with a fresh lease;
- candidates are ranked by usable memory (desc), link RTT (asc), member digest; a
  bounded backtracking search fills roles in spec order. The result depends only on
  the inventory, not on input order.

Refusals are `GateRequirementsUnsatisfied` with `requirements_unsatisfied:<role>`
and, per member digest, every unmet requirement (`member_not_current`,
`capability_report_missing|expired|signature_invalid|generation_mismatch`,
`peer_class_not_allowed`, `os_family_not_allowed`, `architecture_not_allowed`,
`backend_not_allowed`, `usable_memory_below_requirement`,
`free_disk_below_requirement`, `link_measurement_missing`, `link_rtt_above_bound`,
`link_throughput_below_bound`, `thermal_state_not_allowed`,
`power_source_not_allowed`), or `requirements_unsatisfied:joint_assignment` with
the first role the search could not fill.

CLI: `scripts/bind_gate_roles.py --gate A6 --seed-db <state.sqlite3> --reports <dir>
--binding-out <private> --receipt-out <public>`; exit 2 on refusal.

## 3. Evidence

The binding is owner-private (it names node ids). Published evidence uses
`evidence.public_receipt(binding)`: per role, device class (peer class, OS family,
architecture, backends, chip, memory class in GiB, report protocol), capability
digest and member digest. `assert_host_free` rejects IP addresses, `user@host`,
home/volume paths, `.local`/tailnet names and any forbidden value taken from the
seed (node ids, endpoint ids and addresses, incarnations, serials).
`check_case_evidence(spec, record)` checks a case record has the spec's fields and
no host identifier.

## 4. Signed control verbs (fault injection)

`control.issue_control_request` signs `mycelium.gate_control_request.v1`
(`stop_process`, `kill_process`, `partition_link`, `heal_link`,
`advance_generation`) for one bound role member. The member's own
`MemberControlAgent` executes it only if the issuer is the pinned owner key, the
signature verifies, node and generation match, a standing `ControlGrant` covers the
gate id, binding digest and verb, the request is inside its validity window and was
never seen. It answers with a member-signed `mycelium.gate_control_receipt.v1`
(outcome and exit status, no pid or host). `public_control_receipt` is the
host-free projection for published evidence.

`LocalProcessExecutor` + `LocalControlChannel` are the local multi-process
implementation. The member-side transport over the managed control channel does not
exist yet, so gate scripts keep their SSH paths, marked LEGACY.

## 5. Gate scripts

| Script | Requirements-based path | Legacy path kept |
|---|---|---|
| `run_a6_recovery_gate.py`, `build_a6_execution_plan.py` | plan carries `role_binding`; placements = bound entry + successors | `LEGACY_EXPECTED_PLACEMENTS` / `--placements` |
| `run_a7_kv_successor_gate.py` | `--role-binding`: entry/source/standby from the binding; plan pins binding and spec digests and device classes | `LEGACY_TOPOLOGY` |
| `build_a7_standby_plan.py` | `--role-binding` standby | `--standby-node`, `LEGACY_STANDBY_NODE` |
| `run_a5_replica_loss_gate.py` | `run_gate(fault=SignedReplicaKill(...))` | `LegacySshSidecarKill`, `--ssh-host/--ssh-key` |
| `run_a4_negative_data_plane_gate.py` | `--role-binding` (faulted = final, healthy = entry) | `--faulted-node/--healthy-node`, legacy SSH defaults |
| `run_a4_qualification_gate.py` | `write_completion_record(placement_nodes=...)` | `LEGACY_A4_PLACEMENT_NODES` |
| `stage_live_route.py` | `--python` defaults to the running interpreter | explicit `--python` |
| `build_qwen_live_route.py` | `--role-binding` checks topology order and backends; template backend from the node's declared `runtime_backend` | index rule (first MLX, rest NumPy) when undeclared |
| `mycelium_seed/route_assignment.py` | `--gate-role-binding/--gate-role` via `member_for_gate_role` | `newest_android_member` |
| `mycelium_a13_linux/platform_admission.py` | declared target is the Linux device class | explicit `device_label` |
