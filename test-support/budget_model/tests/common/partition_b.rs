// SPDX-License-Identifier: AGPL-3.0-only
//! Exact partition-B family witnesses. These prove static operand/default
//! provenance, not finite caller inputs, runtime mutation safety or measured p99.
use budget_model::inventory::{Bound, Candidate};
use std::collections::BTreeMap;

fn exact(f: &[&str], c: &Candidate) -> bool {
    f.len() == 9
        && f[0] == c.key
        && f[1] == c.path
        && f[2].parse::<usize>() == Ok(c.line)
        && f[3].parse::<usize>() == Ok(c.column)
        && f[4] == c.scope
        && f[5] == c.symbol
        && f[6] == c.expression
        && f[7] == c.kind
        && f[8] == c.source
}

fn source_hash(path: &str) -> Option<&'static str> {
    include_str!("../../data/python-current-source-delta.tsv")
        .lines()
        .skip(1)
        .find_map(|line| {
            let f: Vec<_> = line.split('\t').collect();
            (f.first() == Some(&path))
                .then(|| f.get(2).copied())
                .flatten()
        })
        .or_else(|| {
            include_str!("../../data/python-scope.tsv")
                .lines()
                .skip(1)
                .find_map(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    (f.first() == Some(&path))
                        .then(|| f.get(1).copied())
                        .flatten()
                })
        })
}

fn shape(rule: &str, aux: &str, c: &Candidate, t: &Candidate) -> bool {
    // Only reviewed cross-file forwards may leave the candidate's file.
    let cross_file = matches!(
        rule,
        "sole-constructor-forward"
            | "constructed-field-forward"
            | "sole-raise-forward"
            | "caught-exception-field"
            | "configured-fallback-constant"
            | "sole-caller-forward"
            | "sole-caller-clamped-parameter"
            | "command-identity-deadline-field"
            | "hop-collection-window"
            | "route-local-default"
            | "authorized-cleanup-deadline"
            | "config-ceiling-comparison"
            | "invite-ttl-forward"
    );
    if (!cross_file && c.path != t.path) || c.key == t.key {
        return false;
    }
    match rule {
        "forwarded-deadline-keyword" | "forwarded-deadline-remaining" => {
            // Exact edges in the pinned iroh send path: each `deadline` is a never-rebound
            // parameter whose callers were enumerated over the whole scope.
            const KEYWORD_EDGES: &[(usize, usize)] = &[
                (2156, 1770),
                (2181, 2156),
                (2184, 2156),
                (2190, 2156),
                (2235, 2190),
                (2644, 1806),
                (2644, 2184),
            ];
            const REMAINING_EDGES: &[(usize, usize)] = &[
                (2101, 2156),
                (2233, 2235),
                (2240, 2235),
                (1909, 1721),
                (2642, 2644),
                (2649, 2644),
            ];
            let iroh = "mycelium_router/transports/iroh.py";
            let remaining = rule == "forwarded-deadline-remaining";
            c.path == iroh
                && t.path == iroh
                && c.kind == "keyword"
                && t.kind == "keyword"
                && t.symbol == "deadline"
                && t.expression == "deadline"
                && if remaining {
                    aux == "self._remaining"
                        && c.symbol == "timeout"
                        && c.expression == "self._remaining(deadline)"
                        && REMAINING_EDGES.contains(&(c.line, t.line))
                } else {
                    c.symbol == "deadline"
                        && c.expression == "deadline"
                        && KEYWORD_EDGES.contains(&(c.line, t.line))
                        && match aux.strip_prefix("dual:") {
                            None => aux == "sole" && c.line != 2644,
                            Some(other) => {
                                c.line == 2644
                                    && other
                                        .split(':')
                                        .nth(1)
                                        .and_then(|n| n.parse::<usize>().ok())
                                        .is_some_and(|n| {
                                            n != t.line && KEYWORD_EDGES.contains(&(c.line, n))
                                        })
                            }
                        }
                }
        }
        "sole-caller-forward" => {
            // The whole-scope census found one external caller; aux = parameter.
            let edges: [(&str, &str, &str, &str, &str); 2] = [
                (
                    "mycelium_host_platform/__init__.py",
                    "read_owner_file",
                    "mycelium_node/__main__.py",
                    "_win32_canonical_document",
                    "_MAX_JOIN_BUNDLE_BYTES",
                ),
                (
                    "mycelium_a13_linux/package_manifest.py",
                    "build_linux_package_manifest",
                    "scripts/build_a13_linux.py",
                    "finalize_linux_bundle",
                    "issued + 90 * 86400",
                ),
            ];
            c.kind == "keyword"
                && t.kind == "keyword"
                && c.symbol == aux
                && c.expression == aux
                && t.symbol == aux
                && matches!(aux, "max_bytes" | "expires_at")
                && edges.iter().any(|(cp, cs, tp, ts, te)| {
                    c.path == *cp
                        && c.scope == *cs
                        && t.path == *tp
                        && t.scope == *ts
                        && t.expression == *te
                })
        }
        "state-field-snapshot" => {
            // aux = the exact writer keys of the slots-dataclass field (constructor keyword,
            // attribute stores and inactive default), enumerated over the pinned AST.
            const LIVE: &str = "mycelium_live/liveness.py:362:12:assignment:state.next_keepalive_due_ms,mycelium_live/liveness.py:694:12:assignment:state.next_keepalive_due_ms";
            const CMD: &str = "mycelium_live/command_controller.py:861:12:assignment:state.cleanup_deadline_ms,mycelium_live/command_controller.py:272:4:assignment:cleanup_deadline_ms";
            c.kind == "keyword"
                && t.kind != "comparison"
                && match c.path.as_str() {
                    "mycelium_live/liveness.py" => {
                        aux == LIVE
                            && c.scope == "TrafficAwareLivenessDetector._snapshot"
                            && c.symbol == "next_keepalive_due_ms"
                            && c.expression == "state.next_keepalive_due_ms"
                            && t.line == 263
                            && t.kind == "keyword"
                            && t.symbol == "next_keepalive_due_ms"
                    }
                    "mycelium_live/command_controller.py" => {
                        aux == CMD
                            && c.scope == "CommandController._snapshot"
                            && c.symbol == "cleanup_deadline_ms"
                            && c.expression == "state.cleanup_deadline_ms"
                            && t.line == 471
                            && t.symbol == "state.cleanup_deadline_ms"
                    }
                    _ => false,
                }
        }
        "admitted-deadline-ceiling" => {
            // Every returning branch of the clamp is <= DEADLINE_MAXIMUM_MS: floor/ceiling/within
            // for finite inputs, and the remaining-deadline branch only when remaining < admitted.
            c.path == "mycelium_speculative_decode.py"
                && t.path == c.path
                && t.scope == "<module>"
                && t.kind == "assignment"
                && t.symbol == "DEADLINE_MAXIMUM_MS"
                && match (
                    c.scope.as_str(),
                    c.kind.as_str(),
                    c.symbol.as_str(),
                    c.expression.as_str(),
                ) {
                    (
                        "admit_proposal_deadline",
                        "dict-field",
                        "admitted_deadline_ms",
                        "float(remaining)",
                    ) => aux == "remaining-below-admitted",
                    (
                        "admit_proposal_deadline",
                        "dict-field",
                        "admitted_deadline_ms",
                        "float(admitted)",
                    ) => aux == "clamped-admitted",
                    (
                        "open_target_verification_transaction",
                        "dict-field" | "keyword",
                        "deadline_ms",
                        "admitted_ms",
                    ) => aux == "transaction-admitted-ms",
                    _ => false,
                }
        }
        "sole-caller-monotonic-offset" => {
            c.path == "two_process_runtime_qualification.py"
                && c.scope == "_spawn_assignment_loads"
                && c.kind == "assignment"
                && c.symbol == "deadline"
                && aux == "timeout_seconds"
                && c.expression == format!("time.monotonic() + float({aux})")
                && t.kind == "keyword"
                && t.symbol == aux
                && t.expression == aux
                && t.line == 757
        }
        "sole-caller-number-input" => {
            // check_qualification_age_bound has one caller (check_composition :1248-1251).
            c.path == "mycelium_deadlines.py"
                && c.scope == "check_qualification_age_bound"
                && c.kind == "assignment"
                && t.kind == "keyword"
                && t.symbol == aux
                && t.expression == aux
                && c.expression == format!("_number({aux}, '{aux}')")
                && matches!(
                    (c.line, t.line, aux),
                    (1181, 1249, "qualification_max_age_seconds")
                        | (1182, 1250, "requalification_interval_seconds")
                )
        }
        "sole-caller-clamped-parameter" => {
            // HopArrivalTracker.partners_due has one caller (relay.py :2477-2480).
            c.path == "mycelium_router/hop_join.py"
                && c.scope == "HopArrivalTracker.partners_due"
                && c.kind == "assignment"
                && c.symbol == "window"
                && aux == "window_seconds"
                && c.expression == format!("max(0.0, float({aux}))")
                && t.path == "mycelium_router/relay.py"
                && t.kind == "keyword"
                && t.symbol == aux
                && t.scope == "RelayEngine.hop_join_partners_due"
        }
        "validated-config-field" | "half-ttl-refresh" => {
            // load_artifact_source_agent validates every numeric_bounds field (:384-394) before
            // use; `config` is a private dict copy with no item store, and the helper
            // _availability_snapshot receives that same validated dict from its two call sites.
            let p = "mycelium_live/artifact_agent.py";
            c.path == p
                && t.path == p
                && match (rule, aux) {
                    ("validated-config-field", "ttl-ms") => {
                        c.scope == "load_artifact_source_agent"
                            && c.kind == "assignment"
                            && c.symbol == "ttl_ms"
                            && c.expression == "config['advertisement_ttl_seconds'] * 1000"
                            && t.kind == "dict-field"
                            && t.symbol == "advertisement_ttl_seconds"
                            && t.line == 386
                    }
                    ("validated-config-field", "rate-keyword") => {
                        c.scope == "load_artifact_source_agent"
                            && c.kind == "keyword"
                            && c.symbol == "maximum_bytes_per_second"
                            && c.expression == "config['max_bytes_per_second']"
                            && t.kind == "dict-field"
                            && t.symbol == "max_bytes_per_second"
                            && t.line == 388
                    }
                    ("validated-config-field", "rate-snapshot") => {
                        c.scope == "_availability_snapshot"
                            && c.kind == "dict-field"
                            && c.symbol == "max_bytes_per_second"
                            && c.expression == "config['max_bytes_per_second']"
                            && t.kind == "dict-field"
                            && t.symbol == "max_bytes_per_second"
                            && t.line == 388
                    }
                    ("half-ttl-refresh", "now") | ("half-ttl-refresh", "reconciled_at") => {
                        c.kind == "assignment"
                            && c.symbol == "refresh_after_unix_ms"
                            && c.expression == format!("{aux} + max(250, ttl_ms // 2)")
                            && matches!(
                                c.scope.as_str(),
                                "load_artifact_source_agent"
                                    | "load_artifact_source_agent.reconcile"
                            )
                            && t.scope == "load_artifact_source_agent"
                            && t.kind == "assignment"
                            && t.symbol == "ttl_ms"
                            && t.line == 501
                    }
                    _ => false,
                }
        }
        "sole-constructor-field-forward" => {
            // BatchCollection is a frozen dataclass with one construction site (:896-914).
            c.path == "mycelium_router/a10_batch_admission.py"
                && c.scope == "BatchCollection.proposal"
                && c.kind == "keyword"
                && c.symbol == "maximum_bytes"
                && aux == "BatchCollection|maximum_bytes"
                && c.expression == "self.maximum_bytes"
                && t.kind == "keyword"
                && t.scope == "RuntimeBatchAdmission._collection"
                && t.symbol == "maximum_bytes"
                && t.line == 913
        }
        "command-identity-deadline-field" => {
            // CommandIdentity (frozen) is constructed only at router_port.py:835 (the reviewed
            // `issued_at_ms + 3_600_000` offset) and :2625 (the recovery parameter, whose sole
            // caller :1739 forwards `source_command.absolute_deadline_ms`); the only
            // `replace(...)` rewrites touch the cancellation/publisher generations.
            matches!(
                c.path.as_str(),
                "mycelium_live/router_port.py" | "mycelium_live/command_controller.py"
            ) && t.path == "mycelium_live/router_port.py"
                && t.line == 847
                && t.kind == "keyword"
                && t.symbol == "absolute_deadline_ms"
                && t.scope == "LiveRouterPort._admit_serialized"
                && match (
                    c.scope.as_str(),
                    c.kind.as_str(),
                    c.symbol.as_str(),
                    c.expression.as_str(),
                ) {
                    (
                        "LiveRouterPort._admit_serialized" | "LiveRouterPort._run_route",
                        "dict-field",
                        "absolute_deadline_ms",
                        "identity.absolute_deadline_ms",
                    ) => aux == "identity-field",
                    (
                        "LiveRouterPort._command_identity_document",
                        "dict-field",
                        "absolute_deadline_ms",
                        "identity.absolute_deadline_ms",
                    ) => aux == "identity-field",
                    (
                        "LiveRouterPort._a6_test_only_source_fault_binding",
                        "dict-field",
                        "absolute_deadline_ms",
                        "command.absolute_deadline_ms",
                    ) => aux == "pending-command-field",
                    (
                        "LiveRouterPort.recover_full_context",
                        "keyword",
                        "absolute_deadline_ms",
                        "source_command.absolute_deadline_ms",
                    ) => aux == "pending-command-field",
                    (
                        "LiveRouterPort._register_recovery_command",
                        "keyword",
                        "absolute_deadline_ms",
                        "absolute_deadline_ms",
                    ) => aux.starts_with("sole-caller:") && c.line == 2637,
                    // The controller only ever receives CommandIdentity values (its own
                    // `replace` rewrites change generations), so its readers inherit the field.
                    (
                        "CommandController._expire_if_due",
                        "assignment",
                        "effective_deadline",
                        "state.identity.absolute_deadline_ms",
                    ) => c.line == 842 && aux == "state-identity-field",
                    (
                        "CommandController._expire_if_due",
                        "assignment",
                        "effective_deadline",
                        "min(effective_deadline, state.cleanup_deadline_ms)",
                    ) => c.line == 844 && aux.starts_with("min-tightens:"),
                    (
                        "CommandController._identity_payload",
                        "dict-field",
                        "absolute_deadline_ms",
                        "identity.absolute_deadline_ms",
                    ) => c.line == 891 && aux == "identity-field",
                    _ => false,
                }
        }
        "cancel-deadline-key" => {
            // cancel_request (:4079-4126 pinned) refuses any deadline above
            // `cancellation_started_at + _MAX_CANCELLATION_AND_CLEANUP_SECONDS` before storing
            // it, so the `cancellation_deadline` key of an active-request dict holds a
            // value under the reviewed cancel-and-cleanup allowance (its only other writer is the
            // reviewed `None` initialiser). Readers inherit that relation; the injected
            // `authorize_cleanup` callback results are not claimed here.
            let route = "mycelium_live/route.py";
            const KEY_READ: &str = "active.get('cancellation_deadline')";
            let rest = c.scope.strip_prefix("PhysicalLiveRoute.");
            c.path == route
                && t.path == route
                && rest.is_some()
                && match (
                    c.line,
                    rest.unwrap_or_default(),
                    c.kind.as_str(),
                    c.symbol.as_str(),
                ) {
                    (4103, "cancel_request", "assignment", "active['cancellation_deadline']") => {
                        c.expression == "float(deadline_monotonic_s)"
                            && aux == "validated-ceiling"
                            && t.line == 4095
                    }
                    (4119, "cancel_request", "assignment", "deadline") => {
                        c.expression == "float(active['cancellation_deadline'])"
                            && aux == "dict-key-read"
                            && t.line == 4103
                    }
                    (4175, "cancel_request.cancel_node", "assignment", "fanout_deadline") => {
                        c.expression == "deadline" && aux == "closure-read" && t.line == 4119
                    }
                    (4177, "cancel_request.cancel_node", "assignment", "fanout_deadline") => {
                        c.expression == "deadline - _CLEANUP_FALLBACK_RESERVE_SECONDS"
                            && aux == "closure-tightened"
                            && t.line == 4119
                    }
                    (4196, "cancel_request.cancel_node", "keyword", "deadline_monotonic_s") => {
                        c.expression == "fanout_deadline"
                            && aux.starts_with("dual:")
                            && t.line == 4175
                    }
                    (4379, "_install_observed_owner_cancellation", "assignment", "deadline") => {
                        c.expression == KEY_READ && aux == "dict-key-read" && t.line == 4103
                    }
                    (
                        4382,
                        "_install_observed_owner_cancellation",
                        "assignment",
                        "observed_route_deadline",
                    ) => c.expression == "float(deadline)" && aux == "typed-read" && t.line == 4379,
                    (4404, "_install_observed_owner_cancellation", "assignment", "deadline") => {
                        c.expression
                            == "None if active is None or active.get('cancellation_requested') is not True else active.get('cancellation_deadline')"
                            && aux == "dict-key-read"
                            && t.line == 4103
                    }
                    (
                        4434,
                        "_authorize_failed_request_cleanup",
                        "assignment",
                        "existing_deadline",
                    ) => {
                        c.expression
                            == "None if active is None else active.get('cancellation_deadline')"
                            && aux == "dict-key-read"
                            && t.line == 4103
                    }
                    (
                        4446,
                        "_authorize_failed_request_cleanup",
                        "assignment",
                        "proposed_deadline",
                    ) => {
                        c.expression
                            == "float(existing_deadline) if existing_deadline is not None else failure_observed_at + _MAX_CANCELLATION_AND_CLEANUP_SECONDS"
                            && aux.starts_with("offset:")
                            && t.line == 4434
                    }
                    (5238, "infer", "assignment", "cancellation_deadline") => {
                        c.expression
                            == "self._active_route_requests[request_id].get('cancellation_deadline')"
                            && aux == "dict-key-read"
                            && t.line == 4103
                    }
                    _ => false,
                }
        }
        "cleanup-reconcile-forward" => {
            // _submit_cleanup_reconciliation_locked and _reconcile_request_cleanup are called
            // only at :3875 (schedule), :3904 (executor submit) and :4006 (resubmit); the
            // resubmit passes the same final deadline and, as the only non-None retry hint,
            // `min(final_deadline, time.monotonic() + _CLEANUP_RECEIPT_POLL_SECONDS)`.
            let route = "mycelium_live/route.py";
            let submit = "PhysicalLiveRoute._submit_cleanup_reconciliation_locked";
            let reconcile = "PhysicalLiveRoute._reconcile_request_cleanup";
            c.path == route
                && t.path == route
                && match (c.line, c.scope.as_str(), c.kind.as_str(), c.symbol.as_str()) {
                    (3886, s, "keyword", "final_deadline_monotonic_s") if s == submit => {
                        c.expression == "final_deadline_monotonic_s"
                            && aux.starts_with("callers:")
                            && t.line == 3857
                            && t.scope == "PhysicalLiveRoute._schedule_cleanup_reconciliation"
                    }
                    (3988, s, "keyword", "final_deadline_monotonic_s") if s == reconcile => {
                        c.expression == "final_deadline_monotonic_s"
                            && aux == "resubmit"
                            && t.line == 3886
                    }
                    (3889, s, "keyword", "retry_not_before_monotonic_s") if s == submit => {
                        c.expression == "retry_not_before_monotonic_s"
                            && aux.starts_with("defaults:")
                            && t.line == 3991
                            && t.scope == reconcile
                    }
                    (3919, s, "positional-call", "stop.wait") if s == reconcile => {
                        c.expression
                            == "max(0.0, min(retry_not_before_monotonic_s, final_deadline_monotonic_s) - time.monotonic())"
                            && aux == "retry-below-poll"
                            && t.line == 3889
                    }
                    _ => false,
                }
        }
        "hop-collection-window" => {
            // `Router.hop_collection_window_seconds` (router.py:209, its only definition) returns
            // `relay.batch_controller.config.live_hop_collection_window_seconds`, the reviewed
            // RouterConfig field (dataclass default 0.002; composers may override it).
            // `_hop_collection_deadline` has exactly one non-reset writer (:3756).
            let iroh = "mycelium_router/transports/iroh.py";
            c.path == iroh
                && match (c.line, c.scope.as_str(), c.kind.as_str(), c.symbol.as_str()) {
                    (3755, "IrohTransport._queue_hop_for_batch", "assignment", "window") => {
                        c.expression == "max(0.0, float(router.hop_collection_window_seconds()))"
                            && aux == "sole-implementation:router.py:209"
                            && t.path == "mycelium_router/contracts.py"
                            && t.line == 171
                            && t.kind == "assignment"
                            && t.symbol == "live_hop_collection_window_seconds"
                    }
                    (
                        3756,
                        "IrohTransport._queue_hop_for_batch",
                        "assignment",
                        "self._hop_collection_deadline",
                    ) => {
                        c.expression == "time.monotonic() + window"
                            && aux == "relative-offset"
                            && t.path == iroh
                            && t.line == 3755
                    }
                    (3773, "IrohTransport._dispatch_wait_seconds", "assignment", "deadline")
                    | (3897, "IrohTransport._service_queued_hops", "assignment", "deadline") => {
                        c.expression == "self._hop_collection_deadline"
                            && aux.starts_with("writers:")
                            && t.path == iroh
                            && t.line == 3756
                    }
                    _ => false,
                }
        }
        "route-local-default" => {
            let route = "mycelium_live/route.py";
            c.path == route
                && match (c.line, c.scope.as_str(), c.kind.as_str(), c.symbol.as_str()) {
                    (
                        2451,
                        "PhysicalLiveRoute._prove_external_cancellation",
                        "dict-field",
                        "absolute_deadline_ms",
                    ) => {
                        c.expression == "int(deadline * 1000)"
                            && aux == "sole-local-writer"
                            && t.path == route
                            && t.line == 2445
                            && t.kind == "assignment"
                            && t.symbol == "deadline"
                    }
                    (2783, "PhysicalLiveRoute._release_health_probe", "assignment", "floor_ms") => {
                        c.expression
                            == "int(float(getattr(self._sessions.get(node_id), 'idle_probe_floor_seconds', 0.0) or 0.0) * 1000)"
                            && aux == "session-attribute-default-zero"
                            && t.path == "mycelium_a13_macos/control_session.py"
                            && t.line == 565
                            && t.symbol == "idle_probe_floor_seconds"
                    }
                    _ => false,
                }
        }
        "stage-pack-lease-ttl" => {
            // `_stage_pack_authority_ttl_ms` returns `ttl_ms` only after refusing
            // `ttl_ms > _STAGE_PACK_MAX_AUTHORITY_TTL_MS` (:1151), and `_acquire_stage_packs`
            // either uses the warm-only floor (:1443) or a preflight value that must equal the
            // helper's recomputation (:1461). Every consumer is bounded by the ceiling.
            let lp = "mycelium_live/local_preparer.py";
            c.path == lp
                && t.path == lp
                && match (c.line, c.kind.as_str(), c.symbol.as_str()) {
                    (1381, "assignment", "ttl_ms") => {
                        c.scope == "LocalCandidatePreparer._preflight_local_acquisition_storage"
                            && c.expression
                                .starts_with("self._stage_pack_authority_ttl_ms(")
                            && aux == "helper-return-ceiling"
                            && t.line == 42
                            && t.symbol == "_STAGE_PACK_MAX_AUTHORITY_TTL_MS"
                    }
                    (1403, "keyword", "lease_ttl_ms") => {
                        c.scope == "LocalCandidatePreparer._preflight_local_acquisition_storage"
                            && c.expression == "ttl_ms"
                            && aux == "sole-writer"
                            && t.line == 1381
                    }
                    (1449, "assignment", "lease_ttl_ms") => {
                        c.scope == "LocalCandidatePreparer._acquire_stage_packs"
                            && c.expression == "preflight.get('lease_ttl_ms')"
                            && aux == "validated-equal-to-helper"
                            && t.line == 1381
                    }
                    (1469, "assignment", "stage_pack_expires_at_unix_ms") => {
                        c.scope == "LocalCandidatePreparer._acquire_stage_packs"
                            && c.expression == "issued_at_unix_ms + lease_ttl_ms"
                            && aux.starts_with("writers:")
                            && t.line == 1449
                    }
                    (1472, "keyword", "lease_expires_at_unix_ms") => {
                        c.scope == "LocalCandidatePreparer._acquire_stage_packs"
                            && c.expression == "stage_pack_expires_at_unix_ms"
                            && aux == "sole-writer"
                            && t.line == 1469
                    }
                    (1554, "keyword", "expires_at_unix_ms") => {
                        c.scope == "LocalCandidatePreparer._acquire_stage_packs"
                            && c.expression == "stage_pack_expires_at_unix_ms"
                            && aux == "sole-writer"
                            && t.line == 1469
                    }
                    _ => false,
                }
        }
        "authorized-cleanup-deadline" => {
            // The injected `authorize_cleanup` callback has two production producers
            // (router_port.py:1064 and :1790); both return `_request_command_cancellation(...)`,
            // whose results are the controller's `cleanup_deadline_ms / 1000` (validated in
            // CommandController.cancel to lie within `observed_at_ms + _interruption_and_cleanup_ms`,
            // :426-431) or `time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS`.
            // CommandController() is built with defaults (router_port.py:533), i.e.
            // MAX_INTERRUPTION_AND_CLEANUP_MS = COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS =
            // int(CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1000) (deadlines.py:234), so every result is
            // within one reviewed cancel-and-cleanup allowance of a monotonic sample.
            const ROUTE: &str = "mycelium_live/route.py";
            const PORT: &str = "mycelium_live/router_port.py";
            const INSTALL: &str = "PhysicalLiveRoute._install_observed_owner_cancellation";
            const FAILED: &str = "PhysicalLiveRoute._authorize_failed_request_cleanup";
            const INFER: &str = "PhysicalLiveRoute.infer";
            // (path, line, scope, kind, symbol, expression prefix/exact, mode, writer lines)
            type Site = (
                &'static str,
                usize,
                &'static str,
                &'static str,
                &'static str,
                &'static str,
                &'static str,
                &'static [usize],
            );
            const SITES: &[Site] = &[
                (
                    ROUTE,
                    4395,
                    INSTALL,
                    "assignment",
                    "cancellation_deadline",
                    "authorize_cleanup(time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS)",
                    "producer",
                    &[],
                ),
                (
                    ROUTE,
                    4398,
                    INSTALL,
                    "keyword",
                    "deadline_monotonic_s",
                    "cancellation_deadline",
                    "forward",
                    &[4395],
                ),
                (
                    ROUTE,
                    4451,
                    FAILED,
                    "assignment",
                    "cleanup_deadline",
                    "authorize_cleanup(proposed_deadline)",
                    "producer",
                    &[],
                ),
                (
                    ROUTE,
                    4454,
                    FAILED,
                    "keyword",
                    "deadline_monotonic_s",
                    "cleanup_deadline",
                    "forward",
                    &[4451],
                ),
                (
                    ROUTE,
                    5217,
                    INFER,
                    "assignment",
                    "stop_cleanup_deadline",
                    "authorize_cleanup(stop_cleanup_deadline)",
                    "producer",
                    &[],
                ),
                (
                    ROUTE,
                    5223,
                    INFER,
                    "keyword",
                    "deadline_monotonic_s",
                    "stop_cleanup_deadline",
                    "forward",
                    &[5213, 5217],
                ),
                (
                    ROUTE,
                    5230,
                    INFER,
                    "assignment",
                    "cancellation_deadline",
                    "self._install_observed_owner_cancellation(request_id, authorize_cleanup=authorize_cleanup)",
                    "forward",
                    &[4395, 4404],
                ),
                (
                    ROUTE,
                    5244,
                    INFER,
                    "assignment",
                    "cancellation_deadline",
                    "authorize_cleanup(float(cancellation_deadline))",
                    "producer",
                    &[],
                ),
                (
                    ROUTE,
                    5250,
                    INFER,
                    "keyword",
                    "deadline_monotonic_s",
                    "float(cancellation_deadline)",
                    "forward",
                    &[5230, 5238, 5242, 5244],
                ),
                (
                    ROUTE,
                    5264,
                    INFER,
                    "assignment",
                    "cleanup_deadline",
                    "authorize_cleanup(cleanup_deadline)",
                    "producer",
                    &[],
                ),
                (
                    ROUTE,
                    5267,
                    INFER,
                    "keyword",
                    "deadline_monotonic_s",
                    "cleanup_deadline",
                    "forward",
                    &[5262, 5264, 5399, 5405],
                ),
                (
                    ROUTE,
                    5273,
                    INFER,
                    "keyword",
                    "deadline_monotonic_s",
                    "cleanup_deadline",
                    "forward",
                    &[5262, 5264, 5399, 5405],
                ),
                (
                    ROUTE,
                    5405,
                    INFER,
                    "assignment",
                    "cleanup_deadline",
                    "self._authorize_failed_request_cleanup(request_id, failure_observed_at=failure_observed_at, authorize_cleanup=authorize_cleanup)",
                    "forward",
                    &[4451, 4395, 4404],
                ),
                (
                    ROUTE,
                    5418,
                    INFER,
                    "keyword",
                    "deadline_monotonic_s",
                    "cleanup_deadline",
                    "forward",
                    &[5262, 5264, 5399, 5405],
                ),
                (
                    PORT,
                    2160,
                    "LiveRouterPort._interrupt_recovery_at_deadline",
                    "assignment",
                    "cancellation_deadline",
                    "self._request_command_cancellation(pending, deadline_monotonic_s=time.monotonic() + _MAX_CANCELLATION_AND_CLEANUP_SECONDS)",
                    "producer",
                    &[],
                ),
                (
                    PORT,
                    2172,
                    "LiveRouterPort._interrupt_recovery_at_deadline",
                    "keyword",
                    "deadline_monotonic_s",
                    "cancellation_deadline",
                    "forward",
                    &[2160],
                ),
                (
                    PORT,
                    2477,
                    "LiveRouterPort.cancel_with_deadline",
                    "assignment",
                    "cancellation_deadline",
                    "self._request_command_cancellation(pending, deadline_monotonic_s=deadline_monotonic_s)",
                    "producer",
                    &[],
                ),
                (
                    PORT,
                    2492,
                    "LiveRouterPort.cancel_with_deadline",
                    "keyword",
                    "deadline_monotonic_s",
                    "cancellation_deadline",
                    "forward",
                    &[2477],
                ),
            ];
            t.path == "mycelium_deadlines.py"
                && t.line == 188
                && t.kind == "assignment"
                && t.symbol == "CANCEL_AND_CLEANUP_BUDGET_SECONDS"
                && SITES.iter().any(
                    |(path, line, scope, kind, symbol, expression, mode, writers)| {
                        c.path == *path
                            && c.line == *line
                            && c.scope == *scope
                            && c.kind == *kind
                            && c.symbol == *symbol
                            && c.expression == *expression
                            && match *mode {
                                "forward" => {
                                    let mut found: Vec<usize> = aux
                                        .strip_prefix("writers:")
                                        .unwrap_or_default()
                                        .split(',')
                                        .filter_map(|key| key.split(':').nth(1)?.parse().ok())
                                        .collect();
                                    let mut expected_lines = writers.to_vec();
                                    found.sort_unstable();
                                    expected_lines.sort_unstable();
                                    found == expected_lines
                                }
                                "producer" => aux == "producer-chain",
                                _ => false,
                            }
                    },
                )
        }
        "config-ceiling-comparison" => {
            // HopScheduler is built only from a RouterConfig (m16_runtime.py:394, relay.py:236,
            // router.py:35); `maximum_pending_bytes` has the one dataclass default
            // (contracts.py:160) and composers may override it. The comparison enforces that field.
            c.path == "mycelium_router/scheduler.py"
                && c.scope == "HopScheduler.enqueue"
                && c.kind == "comparison"
                && c.symbol == "self._queued_payload_bytes + payload_bytes"
                && c.expression
                    == "self._queued_payload_bytes + payload_bytes > max(0, self.config.maximum_pending_bytes)"
                && aux == "router-config-field"
                && t.path == "mycelium_router/contracts.py"
                && t.line == 160
                && t.kind == "assignment"
                && t.symbol == "maximum_pending_bytes"
        }
        "invite-ttl-forward" => {
            // create_invite refuses `not 30 <= ttl_seconds <= 300` (supervisor.py:514) before the
            // only call of `mint_native_invite`, which reaches the registry method (:906) and the
            // route method (:6841) with the same keyword; nothing else in the packaged scope calls
            // either method.
            match (c.path.as_str(), c.line) {
                ("mycelium_live/supervisor.py", 513) => {
                    c.scope == "LiveSwarmCoordinator.create_invite"
                        && c.kind == "assignment"
                        && c.symbol == "ttl_seconds"
                        && c.expression == "document.get('expires_in_seconds')"
                        && aux == "validated-before-use"
                        && t.path == c.path
                        && t.kind == "comparison"
                        && t.expression == "30 <= ttl_seconds <= 300"
                        && t.scope == c.scope
                        && t.line > c.line
                }
                ("mycelium_live/registry.py", 921) => {
                    c.scope == "LiveDeploymentRegistry.mint_native_invite"
                        && c.kind == "keyword"
                        && c.symbol == "ttl_seconds"
                        && c.expression == "ttl_seconds"
                        && aux == "sole-caller:supervisor"
                        && t.path == "mycelium_live/supervisor.py"
                        && t.line == 520
                        && t.kind == "keyword"
                        && t.symbol == "ttl_seconds"
                }
                ("mycelium_live/route.py", 6829) => {
                    c.scope == "PhysicalLiveRoute.mint_native_invite"
                        && c.kind == "keyword"
                        && c.symbol == "ttl_seconds"
                        && c.expression == "ttl_seconds"
                        && aux == "sole-forwarder:registry"
                        && t.path == "mycelium_live/registry.py"
                        && t.line == 921
                }
                _ => false,
            }
        }
        "sole-raise-forward" => {
            // aux = Class|local. BackpressureError is raised only at scheduler.py:54/:60 with
            // `retry_after`, whose sole store is scheduler.py:52 (max(0.001, config field)).
            c.path == "mycelium_router/scheduler.py"
                && c.scope == "BackpressureError.__init__"
                && c.kind == "assignment"
                && c.symbol == "self.retry_after_seconds"
                && c.expression == "retry_after_seconds"
                && aux == "BackpressureError|retry_after"
                && t.path == "mycelium_router/contracts.py"
                && t.scope == "RouterConfig"
                && t.kind == "assignment"
                && t.symbol == "backpressure_retry_after_seconds"
        }
        "caught-exception-field" => {
            c.path == "mycelium_router/relay.py"
                && c.kind == "keyword"
                && c.symbol == "retry_after_seconds"
                && aux == "BackpressureError|error"
                && c.expression == "error.retry_after_seconds"
                && t.path == "mycelium_router/scheduler.py"
                && t.scope == "BackpressureError.__init__"
                && t.symbol == "self.retry_after_seconds"
        }
        "configured-fallback-constant" => {
            c.path == "mycelium_live/route.py"
                && c.scope == "admit_peer_cleanup_bound"
                && c.kind == "assignment"
                && c.symbol == "reserve"
                && aux == "reserve_ms"
                && t.path == c.path
                && t.scope == "<module>"
                && t.kind == "assignment"
                && t.symbol == "_CLEANUP_FALLBACK_RESERVE_SECONDS"
                && c.expression == format!("int({} * 1000) if {aux} is None else {aux}", t.symbol)
        }
        "fault-deadline-min" => {
            t.kind == "comparison"
                && c.path == "mycelium_live/route.py"
                && c.kind == "comparison"
                && c.symbol == "int(time.monotonic() * 1000)"
                && aux == "fault"
                && t.expression
                    == "self.deadline_monotonic_ms > int(time.monotonic() * 1000) + 60000"
                && matches!(
                    (c.scope.as_str(), c.expression.as_str()),
                    (
                        "PhysicalLiveRoute._install_test_only_transport_fault",
                        "int(time.monotonic() * 1000) >= min(fault.deadline_monotonic_ms, active.get('absolute_deadline_ms', 0))"
                    ) | (
                        "PhysicalLiveRoute._send_request_command",
                        "int(time.monotonic() * 1000) < min(fault.deadline_monotonic_ms, active.get('absolute_deadline_ms', 0))"
                    )
                )
        }
        "sole-constructor-forward" => {
            // aux = Class|parameter. The pinned whole-scope AST census found exactly one
            // construction site, in the gateway application, passing a config field.
            let Some((class, parameter)) = aux.split_once('|') else {
                return false;
            };
            let home = match class {
                "ProductSessionStore" => "mycelium_ui_gateway/sessions.py",
                "BoundedUpstreamClient" | "BoundedSSEProxy" => "mycelium_ui_gateway/upstream.py",
                _ => return false,
            };
            c.path == home
                && c.kind == "assignment"
                && c.scope == format!("{class}.__init__")
                && c.symbol == format!("self._{parameter}")
                && c.expression == parameter
                && t.path == "mycelium_ui_gateway/app.py"
                && t.scope == "ProductGatewayASGIApplication.__init__"
                && t.kind == "keyword"
                && t.symbol == parameter
                && t.expression.starts_with("config.")
        }
        "constructor-field-clock-offset" => {
            c.path == "mycelium_ui_gateway/sessions.py"
                && c.scope == "ProductSessionStore.open"
                && c.kind == "keyword"
                && c.symbol == "expires_at_unix_ms"
                && t.scope == "ProductSessionStore.__init__"
                && t.symbol == "self._ttl_seconds"
                && aux == "now_ms"
                && c.expression == format!("{aux} + {} * 1000", t.symbol)
        }
        "constructor-field-wait" => {
            c.path == "mycelium_ui_gateway/upstream.py"
                && c.kind == "keyword"
                && c.symbol == "timeout"
                && t.scope == "BoundedSSEProxy.__init__"
                && t.symbol == "self._startup_timeout_seconds"
                && c.scope.starts_with("BoundedSSEProxy.")
                && matches!(
                    c.scope.as_str(),
                    "BoundedSSEProxy.forward" | "BoundedSSEProxy._collect_error_response"
                )
                && aux == "asyncio.wait_for"
                && c.expression == t.symbol
        }
        "loop-clock-offset" => {
            c.path == "mycelium_ui_gateway/upstream.py"
                && c.scope == "BoundedSSEProxy._relay_started_stream"
                && c.kind == "assignment"
                && c.symbol == "deadline"
                && t.scope == "BoundedSSEProxy.__init__"
                && t.symbol == "self._stream_timeout_seconds"
                && aux == "loop"
                && c.expression == format!("{aux}.time() + {}", t.symbol)
        }
        "loop-remaining-timeout" => {
            c.path == "mycelium_ui_gateway/upstream.py"
                && c.scope == "BoundedSSEProxy._relay_started_stream"
                && c.scope == t.scope
                && t.line < c.line
                && t.kind == "assignment"
                && t.symbol == "deadline"
                && t.expression.starts_with("loop.time() + ")
                && aux == "remaining"
                && c.kind == "keyword"
                && c.symbol == "timeout"
                && c.expression == aux
        }
        "constructed-field-forward" => {
            c.path == "mycelium_ui_gateway/app.py"
                && c.scope == "ProductGatewayASGIApplication._bootstrap"
                && c.kind == "dict-field"
                && c.symbol == "expires_at_unix_ms"
                && aux == "session"
                && c.expression == format!("{aux}.expires_at_unix_ms")
                && t.path == "mycelium_ui_gateway/sessions.py"
                && t.scope == "ProductSessionStore.open"
                && t.kind == "keyword"
                && t.symbol == "expires_at_unix_ms"
        }
        "validated-integral-range" => {
            t.kind == "comparison"
                && c.scope == t.scope
                && c.line < t.line
                && c.kind == "assignment"
                && aux == c.symbol
                && t.expression == format!("30 <= {aux} <= 86400")
                && c.expression == "document['expires_in_seconds']"
        }
        "registered-composition-default" => {
            c.path == "mycelium_deadlines.py"
                && t.scope == "<module>"
                && t.symbol == "route_cleanup_fanout_window_ms"
                && t.expression == "derive_route_cleanup_fanout_window_ms()"
                && match aux {
                    "signature-defaults" => {
                        c.scope == "derive_advertised_maximum_total_cleanup_ms"
                            && c.expression
                                == "derive_route_cleanup_fanout_window_ms(budget_seconds=budget_seconds, fallback_reserve_seconds=fallback_reserve_seconds)"
                    }
                    "none-fallbacks" => {
                        c.scope == "check_composition"
                            && c.expression
                                == "derive_route_cleanup_fanout_window_ms(budget_seconds=budget, fallback_reserve_seconds=reserve)"
                    }
                    _ => false,
                }
        }
        "composition-budget-fallback" => {
            c.path == "mycelium_deadlines.py"
                && c.scope == "check_composition"
                && c.kind == "keyword"
                && c.symbol == "budget_seconds"
                && aux == "budget"
                && c.expression == aux
                && t.scope == "<module>"
                && t.symbol == "CANCEL_AND_CLEANUP_BUDGET_SECONDS"
        }
        "initial-resend-consumption" => {
            c.path == "mycelium_router/transports/iroh.py"
                && c.scope == "IrohTransport._acquire_send_operation"
                && c.kind == "assignment"
                && c.symbol == "windows"
                && c.expression == "0"
                && aux == "windows"
                && t.scope == "PrefillStallPolicy"
                && t.symbol == "max_resends"
        }
        "tail-retention-count" => {
            c.path == "mycelium_speculative_decode.py"
                && c.scope == "circuit_breaker_decision"
                && t.scope == "<module>"
                && t.symbol == "CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES"
                && aux == "observed"
                && c.expression == format!("{aux}[-{}:]", t.symbol)
        }
        "dispatch-helper-cap" => {
            c.path == "mycelium_router/transports/iroh.py"
                && c.scope == "IrohTransport._dispatch_loop"
                && t.scope == "IrohTransport.__init__"
                && t.symbol == "self.poll_interval_seconds"
                && aux == "self._dispatch_wait_seconds"
                && c.expression == format!("{aux}()")
        }
        "minimum-offset-reference" => {
            let Some((ceiling, origin)) = aux.split_once('|') else {
                return false;
            };
            t.kind == "assignment"
                && (t.scope == "<module>" || t.scope.ends_with(".__init__"))
                && c.expression == format!("min({ceiling}, {origin} + {})", t.symbol)
        }
        "helper-clock-upper-cap" => {
            c.path == "mycelium_live/member_transport.py"
                && c.scope == "MemberArtifactTransport._reconcile_source_bundles"
                && t.scope == "<module>"
                && t.symbol == "_AVAILABILITY_RECONCILE_MAX_SECONDS"
                && aux == "_availability_reconcile_timeout_seconds"
                && c.expression == format!("time.monotonic() + {aux}(manifest['total_size_bytes'])")
        }
        "postvalidated-cleanup-branch" => {
            c.path == "mycelium_live/command_controller.py"
                && c.scope == "CommandController.cancel"
                && c.symbol == "state.cleanup_deadline_ms"
                && t.scope == "CommandController.__init__"
                && t.symbol == "self._interruption_and_cleanup_ms"
                && aux == "requested_cleanup_deadline_ms"
                && c.expression == aux
        }
        "clock-policy-default" => {
            let Some((origin, receiver)) = aux.split_once('|') else {
                return false;
            };
            c.path == "mycelium_live/liveness.py"
                && t.scope == "LivenessPolicy"
                && t.kind == "assignment"
                && t.symbol == "idle_keepalive_ms"
                && origin == "observed_at_ms"
                && receiver == "self.policy"
                && c.expression == format!("{origin} + {receiver}.{}", t.symbol)
        }
        "guarded-scalar-reference" | "guarded-wallclock-offset" => {
            t.kind == "comparison"
                && c.scope == t.scope
                && t.line < c.line
                && t.expression == format!("30 <= {aux} <= 300")
                && if rule == "guarded-scalar-reference" {
                    c.expression == aux
                } else {
                    c.expression == format!("int((time.time() + {aux}) * 1000)")
                }
        }
        "same-default-expression" => {
            c.path == "mycelium_router/transports/iroh.py"
                && c.scope == "IrohTransport.__init__"
                && c.scope == t.scope
                && t.kind == "keyword"
                && t.symbol == "maxsize"
                && t.line < c.line
                && aux == "queue_capacity"
                && c.expression == aux
                && t.expression == aux
        }
        "nonnegative-byte-remainder" => {
            c.path == "mycelium_router/transports/iroh.py"
                && t.scope == "<module>"
                && t.symbol == "_TRACE_ENTRY_BYTES"
                && match aux {
                    "identity_budget" => {
                        c.expression == aux
                            && matches!(
                                c.scope.as_str(),
                                "IrohTransport._send_or_dispatch"
                                    | "IrohTransport._remote_trace_entries"
                            )
                    }
                    "receipt_prefix" => {
                        c.scope == "IrohTransport._remote_trace_entries"
                            && c.expression
                                == "_TRACE_ENTRY_BYTES - len(receipt_prefix.encode('utf-8'))"
                    }
                    _ => false,
                }
        }
        "float-monotonic-parameter" => {
            t.kind == "parameter"
                && c.scope == t.scope
                && c.expression == format!("time.monotonic() + float({})", t.symbol)
        }
        "guarded-monotonic-parameter" => {
            t.kind == "comparison"
                && c.scope == t.scope
                && t.line < c.line
                && t.expression.starts_with(&format!("0 < {aux} <= "))
                && c.expression == format!("time.monotonic() + float({aux})")
        }
        "remaining-from-deadline" => {
            t.kind == "assignment"
                && c.scope == t.scope
                && t.line < c.line
                && (t.expression.starts_with("time.monotonic() + ")
                    || t.expression
                        .starts_with("min(deadline_monotonic_s, time.monotonic() + "))
                && match aux {
                    "remaining" => c.expression == "remaining",
                    "self._remaining" => c.expression == format!("self._remaining({})", t.symbol),
                    _ => false,
                }
        }
        "connect-wait" => {
            c.path == "mycelium_iroh_sidecar/client.py"
                && c.scope == t.scope
                && t.kind == "assignment"
                && t.line < c.line
                && t.symbol == "selected_timeout"
                && t.expression == "self._timeout if timeout is None else timeout"
                && matches!(c.expression.as_str(), "wait" | "time.monotonic() + wait")
        }
        "clamped-local-cap" => {
            c.path == "mycelium_iroh_sidecar/client.py"
                && t.scope == "<module>"
                && t.kind == "assignment"
                && match (aux, t.symbol.as_str()) {
                    ("connect_budget", "PEER_CONNECT_DEADLINE_SECONDS") => {
                        c.scope == "SidecarClient.connect_peer" && c.expression == aux
                    }
                    ("maximum_wait", "CONTROL_CALL_TIMEOUT_CEILING_SECONDS") => {
                        c.scope == "SidecarClient.control_call"
                            && (c.expression == aux
                                || c.expression == format!("time.monotonic() + {aux}"))
                    }
                    _ => false,
                }
        }
        "clock-field-offset" => {
            let Some((origin, receiver)) = aux.split_once('|') else {
                return false;
            };
            c.path == "mycelium_live/router_port.py"
                && t.scope == "RecoveryPhaseDeadlines"
                && t.kind == "assignment"
                && matches!(
                    t.symbol.as_str(),
                    "total_ms" | "qualification_ms" | "cutover_ms"
                )
                && matches!(receiver, "self._recovery_deadlines" | "deadlines")
                && c.expression == format!("{origin} + {receiver}.{}", t.symbol)
        }
        "nary-minimum-reference" => {
            let operands: Vec<_> = aux.split('|').collect();
            operands.len() == 2
                && t.scope == "<module>"
                && t.kind == "assignment"
                && c.expression == format!("min({}, {}, {})", t.symbol, operands[0], operands[1])
        }
        _ => false,
    }
}

fn quantity_value(mode: &str, c: &Candidate) -> Option<(&'static str, u64)> {
    match (mode, c.path.as_str(), c.expression.as_str()) {
        (
            "fault-window-offset",
            "mycelium_live/route.py",
            "self.deadline_monotonic_ms > int(time.monotonic() * 1000) + 60000",
        ) => Some(("milliseconds", 60_000)),
        ("retained-message-characters", "mycelium_live/route.py", ":128") => Some(("entries", 128)),
        ("report-row-limit", "mycelium_router/analyze.py", ":8") => Some(("entries", 8)),
        ("embedded-script-block-size", "mycelium_live/member_transport.py", e)
            if e.matches("1_048_576").count() == 2
                && ["timeout", "sleep", "wait", "deadline"]
                    .iter()
                    .all(|word| !e.contains(word)) =>
        {
            Some(("bytes", 1_048_576))
        }
        ("schema-range-maximum", "mycelium_live/artifact_agent.py", "(1, 86400)") => {
            Some(("milliseconds", 86_400_000))
        }
        ("schema-range-maximum", "mycelium_live/artifact_agent.py", "(1, 2 ** 63 - 1)") => {
            Some(("bytes-per-second", 9_223_372_036_854_775_807))
        }
        (
            "closed-release-retry-series",
            "mycelium_a7_entry_controller.py",
            "_RELEASE_RETRY_SECONDS * (attempt + 1)",
        ) => Some(("milliseconds", (3 - 1) * 50)),
        (
            "local-wire-payload",
            "mycelium_iroh_sidecar/client.py",
            "OPERATIONAL_MAX_FRAME_BYTES + _ENDPOINT_ID_BYTES + 2 * _CONFIRMED_GENERATION_BYTES",
        ) => Some(("bytes", 16 * 1024 * 1024 + 32 + 2 * 8)),
        (
            "local-wire-record",
            "mycelium_iroh_sidecar/client.py",
            "_HEADER.size + _LOCAL_MAX_PAYLOAD_BYTES + _TAG_BYTES",
        ) => {
            // Standard big-endian >BBQ16sI, not native struct alignment.
            Some((
                "bytes",
                (1 + 1 + 8 + 16 + 4) + (16 * 1024 * 1024 + 32 + 2 * 8) + 32,
            ))
        }
        (
            "active-liveness-remainder",
            "mycelium_live/route.py",
            "mycelium_deadlines.LIVENESS_ACTIVE_FAILURE_DETECTION_MS - int(_ACTIVE_HEALTH_PROBE_DEADLINE_SECONDS * 1000) - _ACTIVE_HEALTH_TICK_MARGIN_MS",
        ) => Some(("milliseconds", 2000 - 500 - 100)),
        (
            "minimum-wallclock-literal",
            "mycelium_live/a6_recovery.py",
            "min(checkpoint.expiry_unix_ms, now + 30000)",
        )
        | (
            "minimum-wallclock-literal",
            "mycelium_live/artifact_transport.py",
            "min(grant_expiry, now + 30000)",
        ) => Some(("milliseconds", 30_000)),
        ("bounded-block-size", "mycelium_live/artifact_transport.py", "len(block)") => {
            Some(("bytes", 64 * 1024))
        }
        (
            "cancellation-pool-cap",
            "mycelium_router/transports/iroh.py",
            "cancellation_client_count",
        ) => Some(("entries", 4)),
        ("dispatch-queue-default", "mycelium_router/transports/iroh.py", "queue_capacity + 1") => {
            Some(("entries", 128 + 1))
        }
        ("frame-queue-default", "mycelium_router/transports/iroh.py", "queue_capacity") => {
            Some(("entries", 128))
        }
        (
            "receiver-join-default",
            "mycelium_router/transports/iroh.py",
            "max(1.0, self.poll_interval_seconds * 4)",
        ) => Some(("milliseconds", 1_000)),
        (
            "shutdown-join-allowance",
            "mycelium_router/transports/iroh.py",
            "max(0.0, shutdown_cleanup_deadline + _DELIVERY_CANCEL_SHUTDOWN_HANDOFF_SECONDS - time.monotonic())",
        ) => Some(("milliseconds", 200 + 50)),
        (
            "receive-socket-default-envelope",
            "mycelium_iroh_sidecar/client.py",
            "max(self._timeout, remaining + 0.5)",
        ) => Some(("milliseconds", 5_500)),
        _ => None,
    }
}

fn validate_quantity(row: &Bound, all: &BTreeMap<String, Candidate>) -> bool {
    let Some(claim) = row.review.strip_prefix("partition-b-quantity:") else {
        return false;
    };
    let mode = claim.split(';').next().unwrap_or("");
    let budget = if mode == "bounded-block-size" {
        &row.relation
    } else {
        &row.budget
    };
    let Some(c) = all.get(&row.key) else {
        return false;
    };
    let mut witnesses = include_str!("../../data/python-partition-b-quantities.tsv")
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect::<Vec<_>>())
        .filter(|f| f.first().is_some_and(|key| *key == row.key));
    let Some(f) = witnesses.next() else {
        return false;
    };
    if f.len() != 16
        || witnesses.next().is_some()
        || !exact(&f[..9], c)
        || f[9] != mode
        || f[13].is_empty()
        || source_hash(&c.path) != Some(f[14])
        || f[15] != budget_model::inventory::CENSUS_SOURCE_SHA
        || *budget != f[12]
    {
        return false;
    }
    let Some((unit, value)) = quantity_value(mode, c) else {
        return false;
    };
    f[10] == unit
        && f[11].parse::<u64>() == Ok(value)
        && row.unit == unit
        && if mode == "bounded-block-size" {
            row.value.is_none() && row.budget.is_empty() && row.disposition == "derived"
        } else {
            row.value == Some(value) && row.disposition.is_empty()
        }
        && row.path == c.path
        && row.line == c.line
        && row.column == c.column
        && row.scope == c.scope
        && row.symbol == c.symbol
        && row.expression == c.expression
        && budgets::BudgetId::from_name(budget).is_some_and(|id| {
            let exact_value = match id.entry().value {
                budgets::table::Value::Storage(n) => unit == "bytes" && n.get() == value,
                budgets::table::Value::Count(n) => unit == "entries" && n.get() == value,
                budgets::table::Value::Duration(n) => unit == "milliseconds" && n.millis() == value,
                budgets::table::Value::BytesPerSecond(n) => {
                    unit == "bytes-per-second" && n.get() == value
                }
                _ => false,
            };
            exact_value
                && matches!(id.entry().provenance, budgets::table::Provenance::Source(s)
                if s == format!("{}:{} @ ef9ae61d", c.path, c.line))
        })
}

/// A dual-caller forward needs the other caller reviewed with the same relation.
fn second_caller_ok(rule: &str, aux: &str, rows: &[Bound], expected: &str) -> bool {
    if rule == "state-field-snapshot" {
        // Every enumerated writer is reviewed; derived writers carry the same relation and
        // the inactive default is the exact reviewed reset slot.
        return aux.split(',').all(|key| {
            rows.iter().any(|b| {
                b.key == key
                    && if b.disposition == "derived" {
                        (if b.budget.is_empty() {
                            &b.relation
                        } else {
                            &b.budget
                        }) == expected
                    } else {
                        b.review.starts_with("category:reviewed-inactive-state")
                    }
            })
        });
    }
    if rule == "authorized-cleanup-deadline" {
        // The controller allowance is the route allowance only through the reviewed formula
        // rows, and every named writer must carry the same relation.
        let formula = rows.iter().any(|b| {
            b.path == "mycelium_deadlines.py"
                && b.line == 234
                && b.expression == "int(CANCEL_AND_CLEANUP_BUDGET_SECONDS * 1000)"
                && b.relation == "PyCommandInterruptionCleanup"
        }) && rows.iter().any(|b| {
            b.path == "mycelium_live/command_controller.py"
                && b.line == 25
                && b.expression == "mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS"
                && b.relation == "PyCommandInterruptionCleanup"
        });
        if !formula {
            return false;
        }
        return match aux.strip_prefix("writers:") {
            None => true,
            Some(keys) => keys.split(',').all(|key| {
                rows.iter().any(|b| {
                    b.key == key
                        && matches!(
                            b.path.as_str(),
                            "mycelium_live/route.py" | "mycelium_live/router_port.py"
                        )
                        && (if b.budget.is_empty() {
                            &b.relation
                        } else {
                            &b.budget
                        }) == expected
                })
            }),
        };
    }
    if rule == "stage-pack-lease-ttl" {
        // The expiry offset covers two writers of lease_ttl_ms: the warm-only floor row must be
        // reviewed and, numerically, fit under the validated ceiling.
        let Some(key) = aux.strip_prefix("writers:") else {
            return true;
        };
        let millis = |name: &str| match budgets::BudgetId::from_name(name)?.entry().value {
            budgets::table::Value::Duration(span) => Some(span.millis()),
            _ => None,
        };
        return rows.iter().any(|b| {
            b.key == key
                && b.path == "mycelium_live/local_preparer.py"
                && b.line == 1443
                && b.disposition == "derived"
                && matches!((millis(&b.relation), millis(expected)), (Some(floor), Some(ceiling)) if floor <= ceiling)
        });
    }
    if rule == "hop-collection-window" {
        let Some(keys) = aux.strip_prefix("writers:") else {
            return true;
        };
        let iroh = "mycelium_router/transports/iroh.py";
        let writers: Vec<_> = keys.split(',').collect();
        return writers.len() == 4
            && [974, 3751, 3895, 3904].iter().all(|line| {
                writers.iter().any(|key| {
                    rows.iter().any(|b| {
                        b.key == *key
                            && b.path == iroh
                            && b.line == *line
                            && b.disposition == "not-a-bound"
                            && b.review.starts_with("category:reviewed-inactive-state")
                    })
                })
            });
    }
    if rule == "cleanup-reconcile-forward" {
        let route = "mycelium_live/route.py";
        let related = |key: &str, line: usize| {
            rows.iter().any(|b| {
                b.key == key
                    && b.path == route
                    && b.line == line
                    && (if b.budget.is_empty() {
                        &b.relation
                    } else {
                        &b.budget
                    }) == expected
            })
        };
        if let Some(key) = aux.strip_prefix("callers:") {
            return related(key, 3988);
        }
        if let Some(keys) = aux.strip_prefix("defaults:") {
            // Omitted-constraint None markers of both retry-hint parameters.
            let markers: Vec<_> = keys.split('|').collect();
            return markers.len() == 2
                && [(markers[0], 3870), (markers[1], 3912)]
                    .iter()
                    .all(|(key, line)| {
                        rows.iter().any(|b| {
                            b.key == *key
                                && b.path == route
                                && b.line == *line
                                && b.disposition == "not-a-bound"
                                && b.semantic == "omitted-constraint"
                        })
                    });
        }
        return true;
    }
    if rule == "cancel-deadline-key" {
        // A second branch/writer must be reviewed with the same relation.
        let (key, line) = match (aux.strip_prefix("dual:"), aux.strip_prefix("offset:")) {
            (Some(key), _) => (key, 4177),
            (None, Some(key)) => (key, 174),
            (None, None) => return true,
        };
        return rows.iter().any(|b| {
            b.key == key
                && b.path == "mycelium_live/route.py"
                && b.line == line
                && (if b.budget.is_empty() {
                    &b.relation
                } else {
                    &b.budget
                }) == expected
        });
    }
    if rule == "command-identity-deadline-field" {
        // The recovery constructor parameter is only as good as its one reviewed caller.
        let (required, path, line) = match (
            aux.strip_prefix("sole-caller:"),
            aux.strip_prefix("min-tightens:"),
        ) {
            (Some(key), _) => (key, "mycelium_live/router_port.py", 1745),
            (None, Some(key)) => (key, "mycelium_live/command_controller.py", 842),
            (None, None) => return true,
        };
        return rows.iter().any(|b| {
            b.key == required
                && b.path == path
                && b.line == line
                && (if b.budget.is_empty() {
                    &b.relation
                } else {
                    &b.budget
                }) == expected
        });
    }
    if rule != "forwarded-deadline-keyword" {
        return true;
    }
    match aux.strip_prefix("dual:") {
        None => true,
        Some(key) => rows.iter().any(|b| {
            b.key == key
                && (if b.budget.is_empty() {
                    &b.relation
                } else {
                    &b.budget
                }) == expected
        }),
    }
}

/// Proposal-local guard: canonical library glue remains coordinator-owned.
pub fn validate_rows(rows: &[Bound]) -> Result<(), Vec<String>> {
    budget_model::inventory::validate(rows)?;
    // The census is immutable: index it once per test binary instead of per call.
    static CENSUS: std::sync::OnceLock<Result<BTreeMap<String, Candidate>, String>> =
        std::sync::OnceLock::new();
    let all = CENSUS
        .get_or_init(|| {
            Ok(budget_model::inventory::candidates()?
                .into_iter()
                .map(|c| (c.key.clone(), c))
                .collect())
        })
        .as_ref()
        .map_err(|error| vec![error.clone()])?;
    let errors: Vec<_> = rows
        .iter()
        .filter(|row| row.review.starts_with("partition-b-"))
        .filter(|row| !validate(row, rows, all))
        .map(|row| format!("invalid-partition-b-proof: {}", row.key))
        .collect();
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

pub fn validate(row: &Bound, rows: &[Bound], all: &BTreeMap<String, Candidate>) -> bool {
    if row.review.starts_with("partition-b-fixture:") {
        let Some(c) = all.get(&row.key) else {
            return false;
        };
        return c.path == "mycelium_router/a10_fault_negatives.py"
            && c.kind == "keyword"
            && c.symbol == "deadline_at"
            && matches!(
                (c.scope.as_str(), c.expression.as_str()),
                ("_member", "1000.0") | ("_verification_request", "1.0")
            )
            && row.review.split(';').next() == Some("partition-b-fixture:cpu-fault-input")
            && row.disposition == "fixture-only"
            && row.unit == "not-applicable"
            && row.budget.is_empty()
            && row.value.is_none()
            && row.path == c.path
            && row.line == c.line
            && row.column == c.column
            && row.scope == c.scope
            && row.symbol == c.symbol
            && row.expression == c.expression
            && include_str!("../../data/python-partition-b-fixtures.tsv")
                .lines()
                .skip(1)
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 12
                        && exact(&f[..9], c)
                        && !f[9].is_empty()
                        && source_hash(&c.path) == Some(f[10])
                        && f[11] == budget_model::inventory::CENSUS_SOURCE_SHA
                });
    }
    if row.review.starts_with("partition-b-readout:") {
        let Some(c) = all.get(&row.key) else {
            return false;
        };
        return matches!(
            (
                c.path.as_str(),
                c.scope.as_str(),
                c.kind.as_str(),
                c.symbol.as_str(),
                c.expression.as_str()
            ),
            (
                "mycelium_a6_case_observations.py",
                "derive_bindings",
                "assignment",
                "deadline",
                "settled.deadline_unix_ms"
            ) | (
                "mycelium_a6_case_observations.py",
                "CaseObservationRecorder._settle",
                "keyword",
                "deadline_unix_ms",
                "executed.deadline_unix_ms"
            )
        ) && row.review.split(';').next()
            == Some("partition-b-readout:observation-envelope-copy")
            && row.disposition == "not-a-bound"
            && row.unit == "not-applicable"
            && row.budget.is_empty()
            && row.value.is_none()
            && row.path == c.path
            && row.line == c.line
            && row.column == c.column
            && row.scope == c.scope
            && row.symbol == c.symbol
            && row.expression == c.expression
            && include_str!("../../data/python-partition-b-readouts.tsv")
                .lines()
                .skip(1)
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 13
                        && exact(&f[..9], c)
                        && f[9] == "observation-envelope-copy"
                        && !f[10].is_empty()
                        && source_hash(&c.path) == Some(f[11])
                        && f[12] == budget_model::inventory::CENSUS_SOURCE_SHA
                });
    }
    if row.review.starts_with("partition-b-quantity:") {
        return validate_quantity(row, all);
    }
    let Some(claim) = row.review.strip_prefix("partition-b-family:") else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    let Some(c) = all.get(&row.key) else {
        return false;
    };
    let mut witnesses = include_str!("../../data/python-partition-b-families.tsv")
        .lines()
        .skip(1)
        .map(|line| line.split('\t').collect::<Vec<_>>())
        .filter(|f| f.first().is_some_and(|key| *key == row.key));
    let Some(f) = witnesses.next() else {
        return false;
    };
    if f.len() != 23
        || witnesses.next().is_some()
        || f[18] != rule
        || f[20].is_empty()
        || f[22] != budget_model::inventory::CENSUS_SOURCE_SHA
        || source_hash(&c.path) != Some(f[21])
        || !exact(&f[..9], c)
    {
        return false;
    }
    let Some(t) = all.get(f[9]) else {
        return false;
    };
    let Some(target) = rows.iter().find(|b| b.key == t.key) else {
        return false;
    };
    let expected = if target.budget.is_empty() && target.disposition == "derived" {
        &target.relation
    } else {
        &target.budget
    };
    exact(&f[9..18], t)
        && shape(rule, f[19], c, t)
        && second_caller_ok(rule, f[19], rows, expected)
        && !expected.is_empty()
        && row.relation == *expected
        && row.path == c.path
        && row.line == c.line
        && row.column == c.column
        && row.scope == c.scope
        && row.symbol == c.symbol
        && row.expression == c.expression
        && row.disposition == "derived"
        && row.budget.is_empty()
        && row.value.is_none()
        && row.unit
            == match rule {
                "same-default-expression"
                | "initial-resend-consumption"
                | "tail-retention-count" => "entries",
                "nonnegative-byte-remainder" => "bytes",
                "sole-constructor-forward"
                | "sole-caller-forward"
                | "validated-config-field"
                | "half-ttl-refresh"
                | "sole-constructor-field-forward"
                | "config-ceiling-comparison" => target.unit.as_str(),
                _ => "milliseconds",
            }
        && row.unit == target.unit
}
