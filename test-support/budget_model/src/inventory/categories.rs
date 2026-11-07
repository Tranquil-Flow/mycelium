// SPDX-License-Identifier: AGPL-3.0-only
//! Conservative predicates over pinned source rows. Unmatched rows remain pending.
use super::Candidate;

pub fn disposition(category: &str) -> &'static str {
    match category {
        "packaged-unit-test" => "test-only",
        "fixture-data" => "fixture-only",
        _ => "not-a-bound",
    }
}

/// A predicate is evidence for this category, not permission to retire its code.
pub fn accepts(category: &str, row: &Candidate) -> bool {
    match category {
        "reviewed-nonfinite-sentinel" => {
            row.kind == "assignment"
                && crate::inventory::keyed_lines(
                    include_str!("../../data/python-nonfinite-sentinels.tsv"),
                    &row.key,
                )
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 11
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && !f[10].is_empty()
                        && match f[9] {
                            "unbounded-default" => row.expression == "float('inf')",
                            "never-renewed-state" => row.expression == "float('-inf')",
                            _ => false,
                        }
                })
        }
        "reviewed-absent-quantity" | "reviewed-domain-action" => {
            let (evidence, allowed) = if category == "reviewed-absent-quantity" {
                (
                    include_str!("../../data/python-absent-quantities.tsv"),
                    matches!(
                        row.kind.as_str(),
                        "parameter" | "assignment" | "keyword" | "dict-field"
                    ),
                )
            } else {
                (
                    include_str!("../../data/python-domain-actions.tsv"),
                    row.kind == "unbounded-call",
                )
            };
            allowed
                && row.expression == "None"
                && evidence.lines().skip(1).any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 11
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && !f[10].is_empty()
                        && if category == "reviewed-absent-quantity" {
                            matches!(
                                f[9],
                                "unknown-quantity"
                                    | "unset-state"
                                    | "default-selection"
                                    | "omitted-constraint"
                                    | "omitted-metadata"
                                    | "unbounded-default"
                            )
                        } else {
                            matches!(f[9], "PublicBootstrapPolicy.acquire" | "Gate.join")
                        }
                })
        }
        "reviewed-missing-local-timeout" => {
            row.kind == "unbounded-call"
                && row.expression == "None"
                && crate::inventory::keyed_lines(
                    include_str!("../../data/python-missing-timeouts.tsv"),
                    &row.key,
                )
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 14
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && f[9] == "no-local-timeout"
                        && matches!(
                            f[10],
                            "threading.Lock"
                                | "threading.RLock"
                                | "threading.Condition"
                                | "threading.BoundedSemaphore"
                                | "threading.Barrier"
                                | "threading.Event"
                                | "threading.Thread"
                                | "asyncio.Event"
                                | "concurrent.futures.Future"
                        )
                        && f[11].parse::<usize>().is_ok_and(|n| n > 0)
                        && !f[12].is_empty()
                        && !f[13].is_empty()
                })
        }
        "reviewed-nonwaiting-operation" => {
            row.kind == "unbounded-call"
                && row.expression == "None"
                && crate::inventory::keyed_lines(
                    include_str!("../../data/python-nonwaiting-calls.tsv"),
                    &row.key,
                )
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 15
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && matches!(f[9], "nonwaiting-event-poll" | "nonwaiting-lock-attempt")
                        && !f[10].is_empty()
                        && f[11].parse::<usize>().is_ok_and(|n| n > 0)
                        && f[12]
                            .parse::<usize>()
                            .ok()
                            .zip(f[11].parse::<usize>().ok())
                            .is_some_and(|(end, start)| end >= start)
                        && f[13].len() == 64
                        && f[13].bytes().all(|b| b.is_ascii_hexdigit())
                        && !f[14].is_empty()
                })
        }
        "reviewed-policy-readout" => crate::inventory::keyed_lines(
            include_str!("../../data/python-policy-readouts.tsv"),
            &row.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 11
                && f[0] == row.key
                && f[1] == row.path
                && f[2].parse::<usize>() == Ok(row.line)
                && f[3].parse::<usize>() == Ok(row.column)
                && f[4] == row.scope
                && f[5] == row.symbol
                && f[6] == row.expression
                && f[7] == row.kind
                && f[8] == row.source
                && !f[10].is_empty()
                && match f[9] {
                    "policy-readout" | "evidence-result" => row.kind == "dict-field",
                    "constructed-policy-readout" => {
                        matches!(row.kind.as_str(), "keyword" | "assignment")
                    }
                    "modeled-cost-input" => {
                        matches!(row.kind.as_str(), "parameter" | "keyword")
                    }
                    "modeled-cost-readout" => {
                        matches!(row.kind.as_str(), "dict-field" | "assignment" | "keyword")
                    }
                    _ => false,
                }
        }),
        "reviewed-inactive-state" => {
            matches!(row.expression.as_str(), "None" | "0" | "0.0")
                && matches!(row.kind.as_str(), "assignment" | "dict-field" | "keyword")
                && crate::inventory::keyed_lines(
                    include_str!("../../data/python-inactive-states.tsv"),
                    &row.key,
                )
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 11
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && f[9] == "inactive-policy-state"
                        && !f[10].is_empty()
                })
        }
        "reviewed-structural-slot" => crate::inventory::keyed_lines(
            include_str!("../../data/python-structural-slots.tsv"),
            &row.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 11
                && f[0] == row.key
                && f[1] == row.path
                && f[2].parse::<usize>() == Ok(row.line)
                && f[3].parse::<usize>() == Ok(row.column)
                && f[4] == row.scope
                && f[5] == row.symbol
                && f[6] == row.expression
                && f[7] == row.kind
                && f[8] == row.source
                && matches!(
                    f[9],
                    "structural-predicate"
                        | "evidence-shape"
                        | "identity-label"
                        | "evidence-container"
                        | "schema-label"
                )
                && !f[10].is_empty()
        }),
        "reviewed-state-comparison" => {
            row.kind == "comparison"
                && [" is True", " is False", " is not True", " is not False"]
                    .iter()
                    .any(|s| row.expression.ends_with(s))
                && crate::inventory::keyed_lines(
                    include_str!("../../data/python-state-comparisons.tsv"),
                    &row.key,
                )
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 11
                        && f[0] == row.key
                        && f[1] == row.path
                        && f[2].parse::<usize>() == Ok(row.line)
                        && f[3].parse::<usize>() == Ok(row.column)
                        && f[4] == row.scope
                        && f[5] == row.symbol
                        && f[6] == row.expression
                        && f[7] == row.kind
                        && f[8] == row.source
                        && f[9] == "boolean-state-predicate"
                        && !f[10].is_empty()
                })
        }
        "reviewed-observation-slot" => crate::inventory::keyed_lines(
            include_str!("../../data/python-observation-slots.tsv"),
            &row.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 11
                && f[0] == row.key
                && f[1] == row.path
                && f[2].parse::<usize>() == Ok(row.line)
                && f[3].parse::<usize>() == Ok(row.column)
                && f[4] == row.scope
                && f[5] == row.symbol
                && f[6] == row.expression
                && f[7] == row.kind
                && f[8] == row.source
                && !f[10].is_empty()
                && matches!(
                    f[9],
                    "elapsed-result" | "clock-sample" | "reported-measurement"
                )
        }),
        "reviewed-nonscalar-slot" => crate::inventory::keyed_lines(
            include_str!("../../data/python-nonscalar-slots.tsv"),
            &row.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 11
                && f[0] == row.key
                && f[1] == row.path
                && f[2].parse::<usize>() == Ok(row.line)
                && f[3].parse::<usize>() == Ok(row.column)
                && f[4] == row.scope
                && f[5] == row.symbol
                && f[6] == row.expression
                && f[7] == row.kind
                && f[8] == row.source
                && !f[10].is_empty()
                && (matches!(
                    f[9],
                    "record-handle"
                        | "path-identity"
                        | "platform-metadata"
                        | "filesystem-handle"
                        | "callable"
                        | "schema-label"
                        | "measurement-container"
                        | "completed-result"
                        | "status-projection"
                ) || (f[9] == "embedded-program"
                    && row.kind == "assignment"
                    && row.scope == "<module>"
                    && (row.expression.starts_with('\'') || row.expression.starts_with('"'))))
        }),
        "reviewed-state-field" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && crate::inventory::keyed_lines(
                include_str!("../../data/python-state-fields.tsv"),
                &row.path,
            )
            .any(|line| {
                let fields: Vec<_> = line.split('\t').collect();
                fields.len() == 5
                    && fields[0] == row.path
                    && fields[1] == row.scope
                    && fields[2] == row.symbol
                    && matches!(
                        fields[3],
                        "callback"
                            | "filesystem-handle"
                            | "platform-metadata"
                            | "message-identity"
                            | "status-projection"
                            | "observation-state"
                    )
                    && !fields[4].is_empty()
            })
        }
        "diagnostic-field" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && crate::inventory::keyed_lines(
                include_str!("../../data/python-diagnostic-fields.tsv"),
                &row.path,
            )
            .any(|line| {
                let fields: Vec<_> = line.split('\t').collect();
                fields.len() == 3 && fields[0] == row.path && fields[1] == row.symbol
            })
        }
        "string-join" => {
            row.kind == "positional-call"
                && row.symbol.strip_suffix(".join").is_some_and(string_literal)
        }
        "path-join" => {
            row.kind == "positional-call"
                && matches!(
                    row.symbol.as_str(),
                    "os.path.join" | "posixpath.join" | "shlex.join" | "os.pathsep.join"
                )
        }
        "boolean-state" => {
            matches!(row.expression.as_str(), "True" | "False")
                && matches!(
                    row.kind.as_str(),
                    "assignment" | "keyword" | "dict-field" | "parameter"
                )
                && !["timeout", "interval", "ttl", "max_bytes", "maxlen"]
                    .contains(&row.symbol.as_str())
        }
        "install-release-data" => {
            let symbol = row.symbol.to_ascii_lowercase();
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && (symbol.contains("install") || symbol.contains("release"))
                && !symbol.ends_with("_s")
                && ![
                    "timeout",
                    "deadline",
                    "_ms",
                    "seconds",
                    "ttl",
                    "retain",
                    "retention",
                    "interval",
                    "retry",
                    "retries",
                    "max",
                    "limit",
                    "cap",
                    "window",
                    "size",
                    "bytes",
                    "age",
                    "grace",
                    "reserve",
                ]
                .iter()
                .any(|word| symbol.contains(word))
                && !row.expression.contains("\\n")
        }
        "clock-provider" => {
            matches!(
                row.symbol.as_str(),
                "clock_ms" | "clock_unix_ms" | "_clock_unix_ms" | "recovery_clock_ms"
            ) && row.kind == "parameter"
                && row.expression == "None"
                && row.source.contains("Callable")
        }
        "clock-observation" => {
            matches!(row.kind.as_str(), "dict-field" | "keyword" | "assignment")
                && matches!(
                    row.symbol.as_str(),
                    "recorded_at_utc_ms"
                        | "recorded_at_unix_ms"
                        | "collected_at_unix_ms"
                        | "observed_at_unix_ms"
                        | "sampled_at_unix_ms"
                        | "created_at_unix_ms"
                        | "generated_at_unix_ms"
                        | "now_unix_ms"
                )
                && matches!(
                    row.expression.as_str(),
                    "int(time.time() * 1000)"
                        | "int(time.time() * 1000.0)"
                        | "int(self._clock_ms())"
                        | "int(clock_unix_ms())"
                        | "self._clock_ms()"
                        | "now_unix_ms"
                        | "now"
                )
        }
        "absent-measurement" => {
            row.expression == "None"
                && matches!(
                    row.kind.as_str(),
                    "dict-field" | "keyword" | "parameter" | "assignment"
                )
                && matches!(
                    row.symbol.as_str(),
                    "rtt_ms"
                        | "warm_rtt_ms"
                        | "jitter_ms"
                        | "last_ack_rtt_ms"
                        | "draft_queue_ms"
                        | "draft_compute_ms"
                        | "transfer_ms"
                        | "speculative_ms"
                        | "elapsed_s"
                        | "measured_rtt_ms"
                        | "serialize_ms"
                        | "deserialize_ms"
                        | "popen_ms"
                        | "mean_ms"
                        | "latest_ms"
                        | "rtt_seconds"
                        | "offset_seconds"
                        | "self.last_rtt_seconds"
                )
        }
        "protocol-label" => {
            string_literal(&row.expression)
                && !row.expression.contains("\\n")
                && (row.symbol.ends_with("PROTOCOL")
                    || row.symbol.ends_with("PROTOCOL_V2")
                    || row.symbol.ends_with("REASON")
                    || row.symbol.starts_with("REASON_")
                    || matches!(
                        row.symbol.as_str(),
                        "deadline_outcome"
                            | "lease_state"
                            | "window.state"
                            | "lease_scope"
                            | "PAIRING_WINDOW_PREFIX"
                            | "_OUTBOUND_RETRY_DIAGNOSTIC"
                            | "EXPIRED"
                            | "DEADLINE_EXCEEDED"
                            | "COMMAND_DEADLINE"
                            | "LEASE_AT_RISK"
                            | "F_EXPIRED"
                    ))
        }
        "schema-unit-label" => {
            row.kind == "dict-field"
                && string_literal(&row.expression)
                && matches!(
                    row.expression.as_str(),
                    "'number'"
                        | "'signed_number'"
                        | "'count'"
                        | "'seconds'"
                        | "'positive'"
                        | "'enum:throttle_behavior'"
                )
                && matches!(
                    row.path.as_str(),
                    "mycelium_product_spine/contracts.py"
                        | "mycelium_platform_neutral/capability_report.py"
                        | "mycelium_gate_requirements/spec.py"
                )
        }
        "packaged-unit-test" => matches!(
            row.path.as_str(),
            "scripts/a13_operator/test_pyz_ledger_coverage.py"
                | "scripts/a13_operator/test_stream_step_watch.py"
                | "scripts/a13_operator/test_bootid_guard.py"
        ),
        "timestamp-context" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && matches!(
                row.symbol.as_str(),
                "observed_at_unix_ms"
                    | "now_unix_ms"
                    | "issued_at_unix_ms"
                    | "captured_at_unix_ms"
                    | "started_at_unix_ms"
                    | "updated_at_unix_ms"
                    | "generated_at_unix_ms"
                    | "evaluated_at_unix_ms"
                    | "self._observed_at_unix_ms"
                    | "last_progress_at_unix_ms"
                    | "created_at_unix_ms"
                    | "measured_at_unix_ms"
                    | "terminal_at_unix_ms"
                    | "completed_at_unix_ms"
                    | "qualified_at_unix_ms"
                    | "finished_at_unix_ms"
                    | "executed_at_unix_ms"
                    | "observed_at_unix_seconds"
                    | "recorded_at_unix_ms"
                    | "published_at_unix_ms"
                    | "bound_at_unix_ms"
                    | "self._last_observed_at_unix_ms"
                    | "responded_at_unix_ms"
                    | "authorized_at_unix_ms"
                    | "emitted_at_unix_ms"
                    | "source_authorized_at_unix_ms"
                    | "recovery_authorized_at_unix_ms"
                    | "recovered_at_unix_ms"
                    | "self._opened_at_unix_ms"
                    | "self._now_unix_ms"
                    | "replica_now_unix_ms"
                    | "revoked_at_unix_ms"
                    | "acknowledged_at_unix_ms"
                    | "incident_observed_at_unix_ms"
                    | "claimed_at_unix_ms"
                    | "sampled_at_unix_ms"
                    | "observed_at_ms"
                    | "now_ms"
                    | "issued_at_ms"
                    | "state.last_observed_ms"
                    | "failure_started_at_ms"
                    | "candidate_first_seen_unix_ms"
                    | "observed_at_monotonic_s"
                    | "observed_at_monotonic_ms"
                    | "cancel_observed_at_ms"
                    | "recovery_last_progress_unix_ms"
                    | "pending.recovery_last_progress_unix_ms"
                    | "cancellation_requested_at_ms"
                    | "cleanup_completed_at_ms"
                    | "state.cancellation_requested_at_ms"
                    | "last_fresh_ms"
                    | "last_observed_ms"
            )
        }
        "latency-observation" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && matches!(
                row.symbol.as_str(),
                "ttft_ms"
                    | "tpot_ms"
                    | "rtt_ms"
                    | "elapsed_ms"
                    | "p95_ttft_ms"
                    | "warm_rtt_ms"
                    | "elapsed_seconds"
                    | "draft_compute_ms"
                    | "p95_tpot_ms"
                    | "decode_transfer_ms"
                    | "neighbor_rtt_ms"
                    | "cold_rtt_ms"
                    | "draft_queue_ms"
                    | "warm_target_tpot_p50_ms"
                    | "warm_target_only_p95_tpot_ms"
                    | "prefill_compute_ms"
                    | "decode_compute_ms"
                    | "decode_ms"
                    | "rtt_p95_seconds"
                    | "warm_target_tpot_p95_ms"
                    | "rtt_seconds"
                    | "compute_ms"
                    | "standby_receipt_rtt_ms"
                    | "baseline_tpot_ms"
                    | "candidate_tpot_ms"
                    | "connect_rtt_ema_ms"
                    | "rtt_p95_ms"
                    | "self._encode_seconds"
                    | "self._decode_seconds"
                    | "elapsed_s"
                    | "receiver_queue_ms"
                    | "ttft_s"
                    | "self.last_rtt_seconds"
                    | "state.last_ack_rtt_ms"
                    | "state.standby_receipt_rtt_ms"
                    | "last_ack_rtt_ms"
                    | "transport_rtt_to_standby_ms"
                    | "decode_samples_ms"
                    | "decode_network_ms"
                    | "decode_loopback_ms"
                    | "decode_bottleneck_ms"
                    | "active_compute_elapsed_ms"
                    | "request_elapsed_ms"
                    | "decode_seconds"
                    | "decode_step_ms"
                    | "reference_p95_ttft_ms"
                    | "queue_ms"
                    | "self._queue_ms"
                    | "self._compute_ms"
                    | "serial_elapsed_ms"
                    | "concurrent_elapsed_ms"
                    | "normalized_two_request_elapsed_ms"
                    | "target_only_tpot_ms"
                    | "draft_tpot_ms"
                    | "target_only_tpot_median_ms"
                    | "ttft_p95_ms"
                    | "replay_tpot_ms"
                    | "kv_tpot_ms"
                    | "tpot_delta_ms"
                    | "draft_ms"
                    | "round_ms"
                    | "pending_draft_ms"
                    | "paused_ms"
            )
        }
        "planner-cost" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && matches!(
                row.symbol.as_str(),
                "cost_ms"
                    | "selected_cost_ms"
                    | "loss_penalty_ms"
                    | "default_rtt_ms"
                    | "DEFAULT_FALLBACK_RTT_MS"
                    | "default_fallback_rtt_ms"
                    | "propagation_km_per_ms"
                    | "prefill_loopback_ms"
                    | "prefill_transfer_ms"
            )
        }
        "fixture-data" => {
            matches!(row.kind.as_str(), "dict-field" | "keyword" | "parameter")
                && (row.scope.contains("compatibility_fixture")
                    || row.scope.contains("_fixture")
                    || row.path == "scripts/generate_contract_fixtures.py")
                && !row.symbol.contains("timeout")
                && !row.symbol.contains("poll_interval")
        }
        "window-identity" => {
            matches!(
                row.kind.as_str(),
                "assignment" | "keyword" | "dict-field" | "parameter"
            ) && matches!(
                row.symbol.as_str(),
                "window_id"
                    | "self.window_id"
                    | "_WINDOW_ID"
                    | "_WINDOW_ID_RE"
                    | "WINDOWS_PEER_CLASS"
                    | "windows_version"
                    | "windows_build"
                    | "expected_windows_version"
                    | "expected_windows_build"
                    | "windows_member_name"
                    | "windows_member_sha256"
                    | "windows_package_root"
                    | "CREATE_NO_WINDOW"
            )
        }
        "windows-path-grammar" => {
            row.path == "layer_assignment.py"
                && matches!(
                    row.symbol.as_str(),
                    "_WINDOWS_RESERVED_NAMES"
                        | "_WINDOWS_FORBIDDEN_CHARACTERS"
                        | "windows_path"
                        | "windows_path.parts"
                        | "part.split('.', 1)[0].rstrip(' ').upper()"
                )
        }
        "http-status-code" => {
            row.path == "mycelium_a13_macos/pairing.py"
                && row.kind == "dict-field"
                && row.symbol.starts_with("pairing_window_")
                && matches!(row.expression.as_str(), "403" | "404" | "409" | "410")
        }
        _ => false,
    }
}

// Resolve only exact reviewed module operands, never a numeric registry lookup.
fn constant_seconds_operand(
    row: &Candidate,
    key: &str,
    allow_import: bool,
) -> Option<(&'static str, u64)> {
    let line = crate::inventory::keyed_lines(include_str!("../../data/python-candidates.tsv"), key)
        .next()?;
    let f: Vec<_> = line.split('\t').collect();
    if f.len() != 9 || f[4] != "<module>" || f[7] != "assignment" {
        return None;
    }
    let local = f[1] == row.path && f[2].parse::<usize>().ok()? < row.line;
    let imported = allow_import
        && include_str!("../../data/python-import-bindings.tsv")
            .lines()
            .skip(1)
            .any(|line| {
                let edge: Vec<_> = line.split('\t').collect();
                edge.len() == 5
                    && edge[0] == row.path
                    && edge[1] == f[5]
                    && edge[2] == key
                    && edge[3] == f[5]
                    && edge[4]
                        .parse::<usize>()
                        .is_ok_and(|n| n > 0 && n <= row.line)
            });
    if !local && !imported {
        return None;
    }
    let millis = scaled_decimal(f[6], 1000)?;
    (millis > 0).then_some((f[5], millis))
}

/// Exact reviewed units; a clock construction maps only its relative offset.
pub fn reviewed_quantity(row: &Candidate, mode: &str) -> Option<(&'static str, u64)> {
    if mode == "helper-range-operand" {
        return helper_range_operand(row);
    }
    let f = crate::inventory::keyed_lines(
        include_str!("../../data/python-quantity-sites.tsv"),
        &row.key,
    )
    .map(|line| line.split('\t').collect::<Vec<_>>())
    .find(|f| {
        f.len() == 13
            && f[0] == row.key
            && f[1] == row.path
            && f[2].parse::<usize>() == Ok(row.line)
            && f[3].parse::<usize>() == Ok(row.column)
            && f[4] == row.scope
            && f[5] == row.symbol
            && f[6] == row.expression
            && f[7] == row.kind
            && f[8] == row.source
            && f[9] == mode
            && !f[12].is_empty()
    })?;
    if mode == "a-progress-envelope-bytes" {
        return (row.path == "mycelium_node/token_progress.py"
            && row.kind == "assignment"
            && row.scope == "<module>"
            && row.symbol == "MAX_PROGRESS_BYTES"
            && row.expression == "MAX_RECEIPT_BYTES + 1024"
            && f[10] == "bytes"
            && f[11] == "-")
            .then_some(("bytes", 1024 * 1024 + 1024));
    }
    if mode == "a-identity-sentinel-bytes" {
        return (row.path == "mycelium_node/identity.py"
            && row.scope == "_win32_load_existing"
            && row.kind == "keyword"
            && row.symbol == "max_bytes"
            && row.expression == "_KEY_BYTES + 1"
            && f[10] == "bytes"
            && f[11] == "-")
            .then_some(("bytes", 32 + 1));
    }
    if mode == "validated-deque-ceiling" {
        let parts: Vec<_> = f[11].split('|').collect();
        if parts.len() != 4
            || row.kind != "keyword"
            || row.symbol != "maxlen"
            || f[10] != "entries"
            || (row.expression != parts[1] && row.expression != parts[2])
        {
            return None;
        }
        let literal = match parts[0] {
            "positive" => parts[3]
                .strip_prefix(&format!(
                    "_positive_integer('{}', {}, maximum=",
                    parts[1], parts[1]
                ))?
                .strip_suffix(')')?,
            "range" => {
                let (left, maximum) = parts[3].rsplit_once(" <= ")?;
                if left != format!("1 <= {}", parts[1]) && left != format!("0 <= {}", parts[1]) {
                    return None;
                }
                maximum
            }
            _ => return None,
        };
        let value = literal.parse::<u64>().ok()?;
        return (value > 0).then_some(("entries", value));
    }
    if mode == "immediate-relative-expiry" {
        if !matches!(row.kind.as_str(), "assignment" | "keyword" | "dict-field")
            || f[10] != "milliseconds"
            || row.expression != f[11]
            || !matches!(
                f[11],
                "now" | "now_unix_ms" | "observed_at" | "int(instant * 1000)"
            )
        {
            return None;
        }
        // Exact source slot adds no allowance to its own observation/activation base.
        // Zero is the relative offset, never a fixed Unix epoch or unlimited validity.
        return Some(("milliseconds", 0));
    }
    if mode == "comparison-constant-product" {
        if row.kind != "comparison" || f[10] != "bytes" {
            return None;
        }
        let line =
            crate::inventory::keyed_lines(include_str!("../../data/python-candidates.tsv"), f[11])
                .next()?;
        let t: Vec<_> = line.split('\t').collect();
        if t.len() != 9
            || t[1] != row.path
            || t[4] != "<module>"
            || t[7] != "assignment"
            || t[2].parse::<usize>().ok()? >= row.line
        {
            return None;
        }
        let product = row.expression.strip_prefix(&format!("{} > ", row.symbol))?;
        let (factor, name) = product.split_once(" * ")?;
        if name != t[5] {
            return None;
        }
        let base = t[6]
            .parse::<u64>()
            .ok()
            .or_else(|| checked_integer_operand(t[6]))?;
        let value = factor.parse::<u64>().ok()?.checked_mul(base)?;
        return (value > 0).then_some(("bytes", value));
    }
    if mode == "loop-sleep-ceiling" {
        if row.kind != "positional-call" || row.symbol != "time.sleep" || f[10] != "seconds" {
            return None;
        }
        let (initial, update) = f[11].split_once('|')?;
        let (cap, next) = call_pair(update, "min")?;
        let factor = next.strip_prefix(&format!("{} * ", row.expression))?;
        if scaled_decimal(factor, 1000)? == 0 {
            return None;
        }
        let compact = |s: &str| s.split_whitespace().collect::<String>();
        if compact(&row.source) != compact(&format!("time.sleep({0});{0}={update}", row.expression))
        {
            return None;
        }
        let value = scaled_decimal(initial, 1000)?.max(scaled_decimal(cap, 1000)?);
        return (value > 0).then_some(("milliseconds", value));
    }
    if mode == "conditional-count-ceiling" {
        if row.kind != "assignment" || f[10] != "entries" {
            return None;
        }
        let (yes, rest) = row.expression.split_once(" if ")?;
        let (condition, no) = rest.rsplit_once(" else ")?;
        if condition != f[11] {
            return None;
        }
        let value = yes.parse::<u64>().ok()?.max(no.parse::<u64>().ok()?);
        return (value > 0).then_some(("entries", value));
    }
    if mode == "positional-validation-upper" {
        if row.kind != "assignment" || f[10] != "bytes" || f[11] != "_bounded_int" {
            return None;
        }
        let args = row
            .expression
            .strip_prefix("_bounded_int(")?
            .strip_suffix(')')?;
        let (prefix, maximum) = args.rsplit_once(", ")?;
        if !prefix.ends_with(", 1") {
            return None;
        }
        let value = maximum
            .parse::<u64>()
            .ok()
            .or_else(|| checked_integer_operand(maximum))?;
        return (value > 0).then_some(("bytes", value));
    }
    if mode == "minimum-clock-offset" {
        if !matches!(row.kind.as_str(), "assignment" | "keyword")
            || f[10] != "seconds"
            || f[11] != "now"
        {
            return None;
        }
        let rest = row.expression.strip_prefix("min(now + ")?;
        let (offset, _) = rest.split_once(", ")?;
        let value = scaled_decimal(offset, 1000)?;
        return (value > 0 && row.expression.ends_with(')')).then_some(("milliseconds", value));
    }
    if mode == "maximum-clock-offset" {
        // `max(<same retained attribute>, now + <integer ms>)`: the literal is the relative
        // allowance added to the trusted observation. The retained operand is caller/persisted
        // state and is deliberately not bounded or discharged by this arm.
        if row.kind != "assignment" || f[10] != "milliseconds" || f[11] != "now" {
            return None;
        }
        let rest = row.expression.strip_prefix("max(")?.strip_suffix(')')?;
        let (state, offset) = rest.split_once(", now + ")?;
        if state != row.symbol
            || !state.starts_with("self._")
            || !state
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.')
            || offset.is_empty()
            || !offset.bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        let value = offset.parse::<u64>().ok()?;
        return (value > 0).then_some(("milliseconds", value));
    }
    if mode == "seconds-ratio-product" {
        if row.kind != "assignment" || f[10] != "seconds" {
            return None;
        }
        let references: Vec<_> = f[11].split('|').collect();
        if references.len() != 2 {
            return None;
        }
        let (left, millis) = constant_seconds_operand(row, references[0], true)?;
        let factor_line = crate::inventory::keyed_lines(
            include_str!("../../data/python-candidates.tsv"),
            references[1],
        )
        .next()?;
        let factor: Vec<_> = factor_line.split('\t').collect();
        if factor.len() != 9
            || factor[1] != row.path
            || factor[4] != "<module>"
            || factor[7] != "assignment"
            || factor[2].parse::<usize>().ok()? >= row.line
            || row.expression != format!("{left} * {}", factor[5])
        {
            return None;
        }
        let numerator = scaled_decimal(factor[6], 1_000_000)?;
        if numerator == 0 || numerator > 1_000_000 {
            return None;
        }
        let scaled = millis.checked_mul(numerator)?;
        if !scaled.is_multiple_of(1_000_000) {
            return None;
        }
        return Some(("milliseconds", scaled / 1_000_000));
    }
    if matches!(mode, "constant-sum" | "constant-product") {
        if row.kind != "assignment" || row.scope != "<module>" || f[10] != "seconds" {
            return None;
        }
        let references: Vec<_> = f[11].split('|').collect();
        let (left, value) = constant_seconds_operand(row, references.first()?, false)?;
        let result = if mode == "constant-sum" && references.len() == 2 {
            let (right, other) = constant_seconds_operand(row, references[1], false)?;
            if row.expression != format!("{left} + {right}") {
                return None;
            }
            value.checked_add(other)?
        } else if mode == "constant-product" && references.len() == 1 {
            let factor = row
                .expression
                .strip_prefix(&format!("{left} * "))?
                .parse::<u64>()
                .ok()?;
            value.checked_mul(factor)?
        } else {
            return None;
        };
        return (result > 0).then_some(("milliseconds", result));
    }
    let literal = match mode {
        "literal" if f[11] == "-" => row.expression.as_str(),
        "environment-default-seconds" | "environment-clock-default-seconds"
            if row.kind == "assignment"
                && f[10] == "seconds"
                && !f[11].is_empty()
                && f[11]
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_') =>
        {
            // This extracts only the absent-key fallback, not an override ceiling.
            let expression = if mode == "environment-clock-default-seconds" {
                row.expression.strip_prefix("time.monotonic() + ")?
            } else {
                row.expression.as_str()
            };
            expression
                .strip_prefix(&format!("float(os.environ.get('{}', '", f[11]))?
                .strip_suffix("'))")?
        }
        "argv-default-seconds"
            if row.kind == "assignment" && f[10] == "seconds" && f[11] == "-" =>
        {
            row.expression
                .strip_prefix("float(sys.argv[1]) if len(sys.argv) > 1 else ")?
        }
        "clock-mapping-default-seconds"
            if row.kind == "assignment"
                && f[10] == "seconds"
                && f[11].contains(" + float(")
                && f[11].contains(".get(")
                && f[11].ends_with(", ") =>
        {
            row.expression.strip_prefix(f[11])?.strip_suffix("))")?
        }
        "optional-seconds"
            if row.kind == "keyword"
                && f[10] == "seconds"
                && !f[11].is_empty()
                && f[11]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_') =>
        {
            // None means no selected timer, not a zero-duration timer.
            row.expression
                .strip_suffix(&format!(" if {} else None", f[11]))?
        }
        "http-retry-after-seconds"
            if row.kind == "keyword"
                && row.symbol == "retry_after"
                && f[10] == "seconds"
                && f[11] == "-" =>
        {
            row.expression.strip_prefix('\'')?.strip_suffix('\'')?
        }
        "explicit-zero-default"
            if matches!(row.kind.as_str(), "parameter" | "dict-field")
                && matches!(row.expression.as_str(), "0" | "0.0")
                && f[11] == "-"
                && matches!(f[10], "bytes" | "seconds") =>
        {
            row.expression.as_str()
        }
        "get-default"
            if row.kind == "assignment" && f[11].contains(".get(") && f[11].ends_with(", ") =>
        {
            row.expression.strip_prefix(f[11])?.strip_suffix(')')?
        }
        "helper-zero-floor"
            if row.kind == "keyword"
                && row.symbol == "minimum"
                && row.expression == "0"
                && f[11] == "-"
                && matches!(f[10], "bytes" | "entries") =>
        {
            row.expression.as_str()
        }
        "integer-expression" if row.kind == "assignment" && f[11] == "-" => row.expression.as_str(),
        "integer-argument"
            if matches!(row.kind.as_str(), "keyword" | "dict-field") && f[11] == "-" =>
        {
            row.expression.as_str()
        }
        "signed-i64-ceiling"
            if row.kind == "assignment" && f[11] == "-" && row.expression == "(1 << 63) - 1" =>
        {
            row.expression.as_str()
        }
        "nonblocking-seconds"
            if row.kind == "positional-timeout"
                && row.expression == "0"
                && f[10] == "seconds"
                && f[11] == "-" =>
        {
            row.expression.as_str()
        }
        "range-floor" if row.kind == "comparison-lower" && f[11] == "-" => row.expression.as_str(),
        "range-zero-floor"
            if row.kind == "comparison-lower" && row.expression == "0" && f[11] == "-" =>
        {
            row.expression.as_str()
        }
        "zero-boundary"
            if row.kind == "comparison"
                && [" < ", " <= ", " > ", " >= "]
                    .iter()
                    .any(|op| f[11].ends_with(op)) =>
        {
            let literal = row.expression.strip_prefix(f[11])?;
            if !matches!(literal, "0" | "0.0") {
                return None;
            }
            literal
        }
        "positive-boundary" if row.kind == "comparison" && f[11].ends_with(" < ") => {
            let literal = row.expression.strip_prefix(f[11])?;
            if !matches!(literal, "1" | "2") {
                return None;
            }
            literal
        }
        "upper-guard" | "upper-integer-expression"
            if row.kind == "comparison"
                && [" < ", " <= ", " > ", " >= "]
                    .iter()
                    .any(|op| f[11].ends_with(op)) =>
        {
            row.expression.strip_prefix(f[11])?
        }
        "literal-floor" => {
            let (left, right) = call_pair(&row.expression, "max")?;
            match f[11] {
                "0" => left,
                "1" => right,
                _ => return None,
            }
        }
        "retention-tail" if row.kind == "slice" && f[10] == "entries" && f[11] == "-" => {
            row.expression.strip_prefix('-')?.strip_suffix(':')?
        }
        "clock-offset" | "clock-integer-offset"
            if !f[11].is_empty() && matches!(f[10], "seconds" | "milliseconds") =>
        {
            let (base, offset) = row.expression.rsplit_once(" + ")?;
            if base != f[11] {
                return None;
            }
            offset
        }
        _ => return None,
    };
    let (unit, scale) = match f[10] {
        "seconds" => ("milliseconds", 1000),
        "milliseconds" => ("milliseconds", 1),
        "bytes" => ("bytes", 1),
        "bytes-per-second" => ("bytes-per-second", 1),
        "entries" => ("entries", 1),
        _ => return None,
    };
    let value = if mode == "signed-i64-ceiling" {
        (1_u64.checked_shl(63)?)
            .checked_sub(1)?
            .checked_mul(scale)?
    } else if matches!(
        mode,
        "upper-integer-expression"
            | "integer-expression"
            | "integer-argument"
            | "clock-integer-offset"
    ) {
        checked_integer_operand(literal)?.checked_mul(scale)?
    } else {
        scaled_decimal(literal, scale)?
    };
    (value > 0
        || matches!(
            mode,
            "nonblocking-seconds"
                | "zero-boundary"
                | "range-zero-floor"
                | "helper-zero-floor"
                | "explicit-zero-default"
        ))
    .then_some((unit, value))
}

// An exact source witness preserves the helper's inclusive integer range contract.
fn helper_range_operand(row: &Candidate) -> Option<(&'static str, u64)> {
    let f = crate::inventory::keyed_lines(
        include_str!("../../data/python-helper-ranges.tsv"),
        &row.key,
    )
    .map(|line| line.split('\t').collect::<Vec<_>>())
    .find(|f| f.first() == Some(&row.key.as_str()))?;
    if f.len() != 20
        || f[1] != row.path
        || f[2].parse::<usize>() != Ok(row.line)
        || f[3].parse::<usize>() != Ok(row.column)
        || f[4] != row.scope
        || f[5] != row.symbol
        || f[6] != row.expression
        || f[7] != row.kind
        || f[8] != row.source
        || row.kind != "keyword"
        || !matches!(row.symbol.as_str(), "minimum" | "maximum")
        || f[18] != row.symbol
        || !f[12].starts_with("self.")
        || f[19].is_empty()
        || !matches!(
            f[9],
            "mycelium_router/a10_batch_admission.py"
                | "mycelium_router/a10_member_isolation.py"
                | "mycelium_router/a10_runtime_batch.py"
        )
        || !f[10].parse::<usize>().is_ok_and(|n| n > 0)
        || f[11]
            != "def _bounded_int( value: object, *, code: str, minimum: int, maximum: int, ) -> int: if ( isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum ): _reject(code) return value"
        || !f[15].parse::<usize>().is_ok_and(|n| n > 0)
        || !f[14].ends_with(".py")
    {
        return None;
    }
    if row.expression != f[13]
        && (!row
            .expression
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b == b'_' || b.is_ascii_digit())
            || row.expression.is_empty()
            || f[16] != format!("{} = {}", row.expression, f[13]))
    {
        return None;
    }
    let value = f[13]
        .parse::<u64>()
        .ok()
        .or_else(|| checked_integer_operand(f[13]))?;
    let unit = match f[17] {
        "bytes" if f[12].ends_with("_bytes") => "bytes",
        "entries" if !f[12].ends_with("_bytes") => "entries",
        _ => return None,
    };
    Some((unit, value))
}

// Only integer products or one shift: no Python eval, division or wrapping.
fn checked_integer_operand(expression: &str) -> Option<u64> {
    if let Some((base, shift)) = expression.split_once(" << ") {
        let base = base.parse::<u64>().ok()?;
        let shift = shift.parse::<u32>().ok()?;
        return base.checked_mul(2_u64.checked_pow(shift)?);
    }
    if !expression.contains(" * ") {
        return None;
    }
    expression.split(" * ").try_fold(1_u64, |product, factor| {
        // A factor is a plain integer or one `base ** exponent` power (checked, no eval).
        let value = match factor.split_once(" ** ") {
            Some((base, exponent)) => base
                .parse::<u64>()
                .ok()?
                .checked_pow(exponent.parse::<u32>().ok()?)?,
            None => factor.parse::<u64>().ok()?,
        };
        product.checked_mul(value)
    })
}

/// Only finite, integral canonical values are admitted; no float rounding.
pub fn numeric_literal(row: &Candidate) -> Option<(&'static str, u64)> {
    if !matches!(
        row.kind.as_str(),
        "assignment" | "keyword" | "dict-field" | "parameter" | "cli-default"
    ) {
        return None;
    }
    let symbol = row.symbol.to_ascii_lowercase().replace('-', "_");
    let unit = if symbol == "maximum_bytes_per_second" {
        ("bytes-per-second", 1)
    } else if symbol.contains("bytes")
        && !symbol.contains("_per_")
        && ["max", "limit", "reserve"]
            .iter()
            .any(|s| symbol.contains(s))
    {
        ("bytes", 1)
    } else if matches!(
        symbol.as_str(),
        "_max_worker_replacements"
            | "baseline_window"
            | "_max_windows"
            | "max_requests"
            | "max_prepared_requests"
            | "max_commands"
            | "maximum_records"
            | "max_retained_checkpoints"
            | "max_buffered_events"
            | "__max_buffered_events"
            | "max_records"
            | "max_records_per_peer_per_second"
            | "max_peer_history"
            | "max_job_history"
            | "default_max_join_attempts_per_invite"
            | "max_attempts"
            | "max_resends"
            | "poll_attempts"
            | "history_limit"
            | "maximum_entries"
            | "maximum_retries_per_chunk"
            | "maximum_concurrent_requests"
            | "max_concurrent_requests"
            | "maximum_recovery_attempts"
            | "max_pending_requests"
            | "max_requests_per_second"
            | "maximum_intervals"
            | "maximum_runtime_batch_size"
            | "kv_release_log_limit"
            | "default_terminal_kv_retention_entries"
            | "max_retained"
            | "snapshot_interval_window"
            | "snapshot_batch_observation_window"
            | "snapshot_outbound_trace_window"
            | "snapshot_scoped_event_window"
            | "snapshot_scoped_failure_window"
            | "snapshot_hop_trace_window"
            | "circuit_breaker_consecutive_deadlines"
            | "circuit_breaker_rolling_window_cycles"
            | "_retained_source_failures_max"
    ) {
        ("entries", 1)
    } else if symbol.contains("unix")
        || symbol.contains("_at")
        || symbol.contains("absolute_deadline_ms")
        || symbol.contains("fraction")
        || symbol.contains("multiplier")
        || symbol.contains("ratio")
    {
        return None;
    } else if (symbol.ends_with("_seconds")
        || symbol.ends_with("_s")
        || matches!(
            symbol.as_str(),
            "_request_ttl_max"
                | "_window_ttl_min"
                | "_window_ttl_max"
                | "report_interval"
                | "poll_interval"
                | "__heartbeat_interval"
                | "__cooldown"
                | "__observation_window"
                | "skew"
                | "timeout"
                | "server.timeout"
                | "agent.server.timeout"
                | "self._server.timeout"
                | "self._outbound_retry_backoff"
        ))
        && ![
            "rtt",
            "observed",
            "total_backoff",
            "encode",
            "decode",
            "elapsed",
            "intersection",
        ]
        .iter()
        .any(|s| symbol.contains(s))
    {
        ("milliseconds", 1000)
    } else if symbol.ends_with("_ms")
        && !symbol.contains("_per_")
        && ![
            "rtt", "jitter", "cost", "penalty", "compute", "transfer", "queue", "measured",
            "observed",
        ]
        .iter()
        .any(|s| symbol.contains(s))
    {
        ("milliseconds", 1)
    } else if symbol == "audit_retention_days" {
        ("milliseconds", 86_400_000)
    } else {
        return None;
    };
    let value = if let Some((base, shift)) = row.expression.split_once(" << ") {
        let base = base.parse::<u64>().ok()?;
        let shift = shift.parse::<u32>().ok()?;
        base.checked_mul(2_u64.checked_pow(shift)?)?
            .checked_mul(unit.1)?
    } else if row.expression.contains('*') {
        let mut product = 1_u64;
        for factor in row.expression.split(" * ") {
            let (base, exponent) = factor.split_once(" ** ").unwrap_or((factor, "1"));
            let base = base.parse::<u64>().ok()?;
            let exponent = exponent.parse::<u32>().ok()?;
            product = product.checked_mul(base.checked_pow(exponent)?)?;
        }
        product.checked_mul(unit.1)?
    } else {
        scaled_decimal(&row.expression, unit.1)?
    };
    // Zero is often state initialization or a sentinel; it needs separate review.
    (value > 0).then_some((unit.0, value))
}

/// Configured offset only; never the runtime absolute monotonic timestamp.
pub fn monotonic_allowance(row: &Candidate) -> Option<u64> {
    if !matches!(row.kind.as_str(), "assignment" | "keyword" | "dict-field") {
        return None;
    }
    let literal = row.expression.strip_prefix("time.monotonic() + ")?;
    let millis = scaled_decimal(literal, 1000)?;
    (millis > 0).then_some(millis)
}

/// Literal upper operand of a reviewed seconds-valued min expression.
pub fn clamped_allowance(row: &Candidate) -> Option<u64> {
    if !matches!(
        row.kind.as_str(),
        "assignment" | "keyword" | "dict-field" | "positional-call"
    ) {
        return None;
    }
    let expression = row
        .expression
        .strip_prefix("time.monotonic() + ")
        .unwrap_or(&row.expression);
    let (left, right) = call_pair(expression, "min")?;
    let (literal, dynamic) = match (scaled_decimal(left, 1000), scaled_decimal(right, 1000)) {
        (Some(n), None) => (n, right),
        (None, Some(n)) => (n, left),
        _ => return None,
    };
    // Multiple positive constants need a multi-operand policy review, not this rule.
    if literal == 0 || dynamic.is_empty() || dynamic.bytes().any(|b| matches!(b, b'1'..=b'9')) {
        return None;
    }
    Some(literal)
}

/// Balanced two-argument syntax only; no Python execution or name resolution.
pub(super) fn call_pair<'a>(expression: &'a str, call: &str) -> Option<(&'a str, &'a str)> {
    let prefix = match call {
        "min" => "min(",
        "max" => "max(",
        _ => return None,
    };
    let inner = expression.strip_prefix(prefix)?.strip_suffix(')')?;
    let mut stack = Vec::new();
    let mut quote = None;
    let mut escaped = false;
    let mut comma = None;
    for (i, byte) in inner.bytes().enumerate() {
        if let Some(q) = quote {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == q {
                quote = None;
            }
            continue;
        }
        match byte {
            b'\'' | b'"' => quote = Some(byte),
            b'(' => stack.push(b')'),
            b'[' => stack.push(b']'),
            b'{' => stack.push(b'}'),
            b')' | b']' | b'}' if stack.pop() != Some(byte) => return None,
            b',' if stack.is_empty() && comma.replace(i).is_some() => return None,
            _ => {}
        }
    }
    if quote.is_some() || !stack.is_empty() {
        return None;
    }
    let comma = comma?;
    let left = inner[..comma].trim();
    let right = inner[comma + 1..].trim();
    (!left.is_empty() && !right.is_empty()).then_some((left, right))
}

pub(super) fn scaled_decimal(text: &str, scale: u64) -> Option<u64> {
    if text.is_empty() || !text.bytes().all(|c| c.is_ascii_digit() || c == b'.') {
        return None;
    }
    let (whole, fraction) = text.split_once('.').unwrap_or((text, ""));
    if whole.is_empty() || (text.contains('.') && fraction.is_empty()) {
        return None;
    }
    let whole = whole.parse::<u64>().ok()?;
    let digits = u32::try_from(fraction.len()).ok()?;
    let denominator = 10_u64.checked_pow(digits)?;
    let numerator = if fraction.is_empty() {
        0
    } else {
        fraction.parse::<u64>().ok()?
    };
    let scaled = numerator.checked_mul(scale)?;
    if !scaled.is_multiple_of(denominator) {
        return None;
    }
    whole.checked_mul(scale)?.checked_add(scaled / denominator)
}

fn string_literal(expression: &str) -> bool {
    let expression = expression.strip_prefix('b').unwrap_or(expression);
    let bytes = expression.as_bytes();
    if bytes.len() < 2 || !matches!(bytes[0], b'\'' | b'"') || bytes.last() != Some(&bytes[0]) {
        return false;
    }
    let quote = bytes[0];
    let mut escaped = false;
    for &byte in &bytes[1..bytes.len() - 1] {
        if escaped {
            escaped = false;
        } else if byte == b'\\' {
            escaped = true;
        } else if byte == quote || byte == b'\n' || byte == b'\r' {
            return false;
        }
    }
    !escaped
}

#[cfg(test)]
mod integer_operand_tests {
    use super::checked_integer_operand;
    #[test]
    fn checked_operand_grammar_rejects_overflow_and_nonconstant_syntax() {
        assert_eq!(
            checked_integer_operand("64 * 1024 * 1024"),
            Some(67_108_864)
        );
        assert_eq!(checked_integer_operand("1 << 30"), Some(1_073_741_824));
        for expression in [
            "1 << 64",
            "2 << 63",
            "18446744073709551615 * 2",
            "1 * policy",
            "1 * -2",
            "1 * 2.0",
            "1 * 2 + 3",
            "(1 << 3)",
            "1 << 2 << 3",
            "1 * ",
        ] {
            assert_eq!(checked_integer_operand(expression), None, "{expression}");
        }
    }
}
