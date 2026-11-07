// SPDX-License-Identifier: AGPL-3.0-only
//! Reviewed lexical edges; not a transitive runtime-mutation/authority proof.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

fn exact_candidate(fields: &[&str], row: &Candidate) -> bool {
    fields.len() == 9
        && fields[0] == row.key
        && fields[1] == row.path
        && fields[2].parse::<usize>() == Ok(row.line)
        && fields[3].parse::<usize>() == Ok(row.column)
        && fields[4] == row.scope
        && fields[5] == row.symbol
        && fields[6] == row.expression
        && fields[7] == row.kind
        && fields[8] == row.source
}

fn configured_clock_offset(source: &Candidate, target: &Candidate) -> bool {
    source.key != target.key
        && source.kind == "assignment"
        && crate::inventory::keyed_lines(
            include_str!("../../data/python-config-clock-offsets.tsv"),
            &source.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            if f.len() != 22
                || !exact_candidate(&f[..9], source)
                || !exact_candidate(&f[9..18], target)
                || f[18] != "time.monotonic()"
                || f[20] != "monotonic-seconds"
                || f[21].is_empty()
                || source.expression != format!("{} + {}", f[18], f[19])
            {
                return false;
            }
            let Some((receiver, field)) = f[19].rsplit_once('.') else {
                return false;
            };
            !receiver.is_empty()
                && match target.kind.as_str() {
                    "assignment" => target.symbol.rsplit('.').next() == Some(field),
                    "cli-default" => target
                        .symbol
                        .strip_prefix("--")
                        .is_some_and(|flag| flag.replace('-', "_") == field),
                    _ => false,
                }
        })
}

fn configured_float_field(source: &Candidate, target: &Candidate) -> bool {
    source.key != target.key
        && target.kind == "assignment"
        && crate::inventory::keyed_lines(
            include_str!("../../data/python-config-float-bindings.tsv"),
            &source.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 20
                && exact_candidate(&f[..9], source)
                && exact_candidate(&f[9..18], target)
                && !f[18].is_empty()
                && !f[19].is_empty()
                && source.expression == format!("float({}.{})", f[18], target.symbol)
        })
}

fn contains_word(haystack: &str, word: &str) -> bool {
    haystack.match_indices(word).any(|(at, _)| {
        let before = haystack[..at].chars().next_back();
        let after = haystack[at + word.len()..].chars().next();
        !before.is_some_and(|c| c.is_alphanumeric() || c == '_')
            && !after.is_some_and(|c| c.is_alphanumeric() || c == '_')
    })
}

// Index only this additive witness table. Duplicate source witnesses fail closed;
// no source/target fields or historical lexical validation rules are relaxed.
static SOURCE_FLOWS: std::sync::LazyLock<BTreeMap<&'static str, Vec<&'static str>>> =
    std::sync::LazyLock::new(|| {
        let mut rows: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for line in include_str!("../../data/python-source-flow-edges.tsv")
            .lines()
            .skip(1)
        {
            if let Some((key, _)) = line.split_once('\t') {
                rows.entry(key).or_default().push(line);
            }
        }
        rows
    });

// Full source/target witnesses preserve reviewed helper and lexical context.
// A selected default is not a ceiling on environment or caller overrides.
fn reviewed_source_flow(rule: &str, source: &Candidate, target: &Candidate) -> bool {
    source.key != target.key
        && SOURCE_FLOWS
            .get(source.key.as_str())
            .filter(|rows| rows.len() == 1)
            .is_some_and(|rows| {
                let f: Vec<_> = rows[0].split('\t').collect();
                if f.len() != 21
                    || !exact_candidate(&f[..9], source)
                    || !exact_candidate(&f[9..18], target)
                    || match f[18] {
                        "reviewed-source-invite-batch-ceiling" => {
                            rule != if f[19] == "forward" {
                                "validated-required-ceiling"
                            } else {
                                "reviewed-clock-offset"
                            }
                        }
                        "reviewed-source-rotation-ceiling" => rule != "reviewed-clock-offset",
                        "reviewed-source-validated-document-expiry"
                        | "reviewed-source-decoder-timeout" => rule != "reviewed-helper-return",
                        "reviewed-source-mobile-validity" => {
                            rule != if f[19] == "clock" {
                                "reviewed-clock-offset"
                            } else {
                                "reviewed-helper-return"
                            }
                        }
                        "reviewed-source-optional-clock"
                        | "reviewed-source-expiry-alias"
                        | "reviewed-source-qualified-clock" => rule != "reviewed-clock-offset",
                        "reviewed-source-scaled-comparison"
                        | "reviewed-source-field-comparison" => rule != "reviewed-local-comparison",
                        "reviewed-source-nested-constructor" | "reviewed-source-tuple-field" => {
                            rule != "constructor-field"
                        }
                        "reviewed-source-range-return"
                        | "reviewed-source-validator-ceiling"
                        | "reviewed-source-verified-message-reread"
                        | "reviewed-source-cli-guarded-argument"
                        | "reviewed-source-validated-assignment"
                        | "reviewed-source-validated-assignment-constant"
                        | "reviewed-source-clamping-callee" => rule != "validated-required-ceiling",
                        "reviewed-source-clamped-deadline-return" => {
                            rule != "reviewed-helper-return"
                        }
                        "reviewed-source-tuple-member-alias"
                        | "reviewed-source-sole-caller-chain"
                        | "reviewed-source-subscript-alias"
                        | "reviewed-source-sole-constructor-forward"
                        | "reviewed-source-settings-callers"
                        | "reviewed-source-default-callable-forward"
                        | "reviewed-source-sole-constructor-field-forward"
                        | "reviewed-source-annotated-property-read"
                        | "reviewed-source-closure-sole-call"
                        | "reviewed-source-cross-module-callers-forward"

                        | "reviewed-source-retention-suffix"
                        | "reviewed-source-cell-readers"
                        | "reviewed-source-all-callers-forward" => rule != "resolved-optional-value",
                        "reviewed-source-validated-retention-slice"
                        | "reviewed-source-callee-validator-ceiling" => {
                            rule != "validated-required-ceiling"
                        }
                        "reviewed-source-cli-float-identity" => rule != "argparse-option",
                        "reviewed-source-interval-submultiple" => rule != "typed-config-field",
                        "reviewed-source-scaled-max-deadline"
                        | "reviewed-source-select-remaining-deadline"
                        | "reviewed-source-default-wait-callers" => rule != "reviewed-clock-offset",
                        "reviewed-source-all-callers-clock"
                        | "reviewed-source-interval-multiple"
                        | "reviewed-source-remaining-parameter" => {
                            rule != "reviewed-clock-offset"
                        }
                        "reviewed-source-sole-caller-clock"
                        | "reviewed-source-sole-caller-cli-clock"
                        | "reviewed-source-cli-guarded-clock" => rule != "reviewed-clock-offset",
                        "reviewed-source-sole-caller-forward" => rule != "resolved-optional-value",
                        "reviewed-source-tightening-minimum" => rule != "upper-cap-reference",
                        "reviewed-source-class-optional" | "reviewed-source-tuple-default" => {
                            rule != "resolved-optional-value"
                        }
                        "reviewed-source-helper-default"
                        | "reviewed-source-lexical-closure"
                        | "reviewed-source-local-alias"
                        | "reviewed-source-optional-alias" => rule != "resolved-optional-value",
                        "reviewed-source-validated-default" => {
                            rule != "validated-parameter-default"
                        }
                        "reviewed-source-positional-upper" => rule != "reviewed-helper-return",
                        "reviewed-source-integer-clock" | "reviewed-source-tightened-expiry" => {
                            rule != "reviewed-clock-offset"
                        }
                        "reviewed-source-comparison-sentinel" => {
                            rule != "comparison-reference-constructor-field"
                        }
                        "reviewed-source-walrus-remaining" | "reviewed-source-local-remaining" => {
                            rule != "remaining-monotonic-local"
                        }
                        "reviewed-source-helper-default-clock"
                        | "reviewed-source-conditional-clock" => rule != "reviewed-clock-offset",
                        "reviewed-source-seed-return-ceiling" => rule != "reviewed-helper-return",
                        _ => true,
                    }
                    || f[20].is_empty()
                {
                    return false;
                }
                match f[18] {
                    "reviewed-source-invite-batch-ceiling" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && target.symbol == "MAX_INVITE_TTL_SECONDS"
                            && match f[19] {
                                "forward" => source.expression == "ttl_seconds",
                                "clock" => source.expression == "issued_at + ttl_seconds",
                                _ => false,
                            }
                    }
                    "reviewed-source-rotation-ceiling" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && target.symbol == "_MAX_ROTATION_OVERLAP_SECONDS"
                            && match f[19] {
                                "seconds" => {
                                    source.expression == "instant + float(overlap_seconds)"
                                }
                                "milliseconds" => {
                                    source.expression
                                        == "int((instant + float(overlap_seconds)) * 1000)"
                                }
                                _ => false,
                            }
                    }
                    "reviewed-source-validated-document-expiry" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && target.symbol == "MAX_MESSAGE_TTL_SECONDS"
                            && source.expression == format!("_number({}['expires_at'])", f[19])
                            && source.scope == "_verify_envelope"
                    }
                    "reviewed-source-mobile-validity" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && target.symbol == "MAX_RUNG_VALIDITY_MS"
                            && match f[19] {
                                "call" => source.expression == "_validity_ms(validity_ms)",
                                "alias" => source.expression == "validity",
                                "clock" => source.expression == "now + _validity_ms(validity_ms)",
                                _ => false,
                            }
                    }
                    "reviewed-source-decoder-timeout" => {
                        source.path == target.path
                            && target.scope == "decode_run_request"
                            && target.kind == "comparison"
                            && target.expression.starts_with("0.1 <= float(timeout) <= ")
                            && match f[19] {
                                "tuple-2" => {
                                    source.symbol == "(argv, cwd, timeout, detach)"
                                        && source.expression == "decode_run_request(value)"
                                }
                                "timeout" => {
                                    source.kind == "keyword"
                                        && source.symbol == "timeout"
                                        && source.expression == "timeout"
                                }
                                _ => false,
                            }
                    }
                    "reviewed-source-optional-clock" => {
                        source.path == target.path
                            && target.symbol.starts_with("self.")
                            && source.expression
                                == format!(
                                    "self._clock() + ({} if {} is None else {})",
                                    target.symbol, f[19], f[19]
                                )
                    }
                    "reviewed-source-scaled-comparison" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.kind == "comparison"
                            && source.expression == format!("{} <= {} * 1000", f[19], target.symbol)
                    }
                    "reviewed-source-field-comparison" => {
                        source.path == target.path
                            && target.scope.ends_with(".__init__")
                            && source.expression == format!("{} > {}", f[19], target.symbol)
                    }
                    "reviewed-source-nested-constructor" => {
                        source.path == target.path
                            && target.symbol == f[19]
                            && source.expression == f[19]
                            && target
                                .scope
                                .strip_suffix(".__init__")
                                .is_some_and(|c| source.scope.starts_with(&format!("{c}.")))
                    }
                    "reviewed-source-range-return" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && target.kind == "comparison"
                            && target.line < source.line
                            && source.expression == format!("float({})", f[19])
                            && target
                                .expression
                                .starts_with(&format!("0.0 < float({}) <= ", f[19]))
                    }
                    // The validator itself rejects a lifetime above the same-file module
                    // ceiling. Exact full-candidate witnesses pin the source expiry read and
                    // the ceiling; this is a relative lifetime, never an absolute epoch.
                    "reviewed-source-validator-ceiling" => {
                        source.path == target.path
                            && source.kind == "assignment"
                            && source.scope != "<module>"
                            && target.kind == "assignment"
                            && target.scope == "<module>"
                            && target.symbol == f[19]
                            && matches!(source.symbol.as_str(), "expires_at" | "expires" | "lease")
                            && source.expression.contains("expires_at")
                    }
                    // The callee has exactly one shipped caller (git-grep evidence in the
                    // witness); a required parameter therefore has the reviewed call-site
                    // argument as its nominal provenance. Tests and other callers keep their
                    // own values; this is not a runtime cap.
                    "reviewed-source-sole-caller-clock" => {
                        source.path == target.path
                            && source.kind == "assignment"
                            && source.symbol == "deadline"
                            && target.kind == "keyword"
                            && source.scope != target.scope
                            && source.scope.rsplit('.').next() == Some(f[19])
                            && source.expression == format!("time.monotonic() + {}", target.symbol)
                    }
                    "reviewed-source-sole-caller-forward" => {
                        target.kind == "keyword"
                            && matches!(
                                source.kind.as_str(),
                                "keyword" | "dict-field" | "assignment"
                            )
                            && source.scope != target.scope
                            && source.scope.rsplit('.').next() == Some(f[19])
                            && source.expression == target.symbol
                    }
                    // CLI input rejected outside the comparison in the same function before
                    // it is forwarded; the comparison row owns the numeric ceiling.
                    "reviewed-source-cli-guarded-argument" => {
                        source.path == target.path
                            && source.kind == "keyword"
                            && f[19].starts_with("args.")
                            && source.expression == f[19]
                            && target.kind == "comparison"
                            && target.scope == source.scope
                            && target.line < source.line
                            && target.expression.contains(f[19])
                    }
                    // Raw document fields are checked by the reviewed comparison later in
                    // the same function (raising before use); the comparison row owns the
                    // numeric ceiling, so every unpacked member must appear in it.
                    "reviewed-source-validated-assignment" => {
                        source.path == target.path
                            && source.kind == "assignment"
                            && target.kind == "comparison"
                            && source.scope == target.scope
                            && source.line < target.line
                            && f[19] == "comparison"
                            && source
                                .symbol
                                .trim_matches(|c| c == '(' || c == ')')
                                .split(", ")
                                .all(|name| {
                                    !name.is_empty() && contains_word(&target.expression, name)
                                })
                    }
                    "reviewed-source-validated-assignment-constant" => {
                        source.path == target.path
                            && source.kind == "assignment"
                            && source.scope != "<module>"
                            && source.symbol == "(issued, expires)"
                            && target.scope == "<module>"
                            && target.kind == "assignment"
                            && target.symbol == f[19]
                    }
                    // `_admit_deadline` returns only values clamped to the reviewed
                    // DEADLINE_MAXIMUM_MS (or a smaller remaining deadline); the unset branch
                    // goes through sd.admit_proposal_deadline with the same clamp.
                    "reviewed-source-clamped-deadline-return" => {
                        source.path == "mycelium_speculative_decode_execution.py"
                            && target.path == "mycelium_speculative_decode.py"
                            && source.kind == "assignment"
                            && source.symbol == "(admitted_ms, clamp_reason, _deadline_evidence)"
                            && source.expression == "_admit_deadline(document, deadline_ms)"
                            && f[19] == "_admit_deadline"
                            && target.scope == "<module>"
                            && target.symbol == "DEADLINE_MAXIMUM_MS"
                    }
                    "reviewed-source-tuple-member-alias" => {
                        source.path == target.path
                            && matches!(source.kind.as_str(), "keyword" | "dict-field")
                            && f[19] == "admitted_ms"
                            && source.expression == "admitted_ms"
                            && target.kind == "assignment"
                            && target.symbol.starts_with("(admitted_ms, ")
                            && source.scope == target.scope
                            && source.line > target.line
                    }
                    "reviewed-source-clamping-callee" => {
                        source.path == target.path
                            && source.kind == "keyword"
                            && source.symbol == "deadline_ms"
                            && source.expression == "deadline_ms"
                            && source.scope != target.scope
                            && f[19] == target.scope
                            && ((target.kind == "assignment"
                                && target.symbol.starts_with("(admitted_ms, "))
                                || (target.kind == "keyword" && target.symbol == "deadline_ms"))
                    }
                    // The sole shipped constructor call passes a replay_limit already rejected
                    // unless it is an int in [1, 4096]; the newest-N slice keeps at most that.
                    "reviewed-source-validated-retention-slice" => {
                        source.path == "mycelium_product_spine/state.py"
                            && target.path == "mycelium_product_spine/app.py"
                            && source.kind == "assignment"
                            && source.symbol == "retained"
                            && source.expression
                                == "[validate_product_event(event) for event in events][-self._replay_limit:]"
                            && f[19] == "replay_limit"
                            && target.kind == "keyword"
                            && target.symbol == "maxlen"
                            && target.expression == "replay_limit"
                            && target.scope == "ProductEvidenceApplication.__init__"
                    }
                    "reviewed-source-retention-suffix" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && source.kind == "assignment"
                            && source.symbol == "retained"
                            && source.expression == "retained[1:]"
                            && f[19] == "retained"
                            && target.symbol == "retained"
                            && target.line < source.line
                            && target.expression.starts_with("[validate_product_event(")
                    }
                    "reviewed-source-cli-float-identity" => {
                        source.path == target.path
                            && source.kind == "keyword"
                            && f[19].starts_with("args.")
                            && source.expression == format!("float({})", f[19])
                            && target.kind == "cli-default"
                            && target.symbol
                                == format!("--{}", f[19].trim_start_matches("args.").replace('_', "-"))
                            && target.source.contains("type=float")
                    }
                    // Every shipped caller (git-grep evidence in the witness) passes the same
                    // reviewed argument expression; the catalogue must contain exactly the
                    // caller keywords listed in the operand.
                    "reviewed-source-all-callers-clock" | "reviewed-source-all-callers-forward" => {
                        source.path == target.path
                            && source.scope != target.scope
                            && target.kind == "keyword"
                            && match f[18] {
                                "reviewed-source-all-callers-clock" => {
                                    source.kind == "assignment"
                                        && source.symbol == "deadline"
                                        && source.expression
                                            == format!("time.monotonic() + {}", target.symbol)
                                }
                                _ => {
                                    source.kind == "dict-field"
                                        && source.symbol == target.symbol
                                        && source.expression == target.symbol
                                }
                            }
                            && super::candidates_ref().is_ok_and(|all| {
                                let mut lines: Vec<usize> = all
                                    .iter()
                                    .filter(|c| {
                                        c.path == target.path
                                            && c.kind == "keyword"
                                            && c.symbol == target.symbol
                                            && c.expression == target.expression
                                    })
                                    .map(|c| c.line)
                                    .collect();
                                lines.sort_unstable();
                                lines.len() > 1
                                    && lines
                                        .iter()
                                        .map(usize::to_string)
                                        .collect::<Vec<_>>()
                                        .join(",")
                                        == f[19]
                            })
                    }
                    // A one-cell list whose every subscript store is the same reviewed monotonic
                    // grace offset (full-AST scan in the witness); a read is None or that offset.
                    "reviewed-source-cell-readers" => {
                        source.path == target.path
                            && source.kind == "assignment"
                            && source.symbol == "deadline"
                            && source.expression == "termination_deadline[0]"
                            && target.symbol == "termination_deadline[0]"
                            && target.kind == "assignment"
                            && super::candidates_ref().is_ok_and(|all| {
                                let writers: Vec<_> = all
                                    .iter()
                                    .filter(|c| {
                                        c.path == target.path
                                            && c.kind == "assignment"
                                            && c.symbol == target.symbol
                                    })
                                    .collect();
                                let mut lines: Vec<usize> = writers.iter().map(|c| c.line).collect();
                                lines.sort_unstable();
                                !writers.is_empty()
                                    && writers.iter().all(|c| c.expression == target.expression)
                                    && lines
                                        .iter()
                                        .map(usize::to_string)
                                        .collect::<Vec<_>>()
                                        .join(",")
                                        == f[19]
                            })
                    }
                    // Two private helpers each with exactly one shipped call (git-grep evidence in
                    // the witness) forward the required timeout of the probe entry point.
                    "reviewed-source-sole-caller-chain" => {
                        source.path == "scripts/a13_operator/ssh_window_evidence.py"
                            && target.path == source.path
                            && source.kind == "keyword"
                            && source.symbol == "timeout"
                            && source.expression == "timeout"
                            && source.scope == "_record_one_canary_into"
                            && target.kind == "parameter"
                            && target.symbol == "timeout"
                            && target.scope == "probe_recorder"
                            && f[19] == "_record_one_canary_into>_probe_target_log_write"
                    }
                    // base + N * keepalive interval, N the reviewed single-writer 2-or-1 multiplier
                    // local to the same function; a relative schedule offset, not a lease lifetime.
                    "reviewed-source-interval-multiple" => {
                        source.path == "mycelium_seed/coordinator.py"
                            && target.path == source.path
                            && target.symbol == "self._keepalive_interval_seconds"
                            && target.kind == "assignment"
                            && f[19] == "heartbeat_intervals"
                            && matches!(source.symbol.as_str(), "next_heartbeat_due_at" | "member.next_heartbeat_due_at")
                            && (source.expression
                                == format!("now + {} * {}", f[19], target.symbol)
                                || source.expression
                                    == format!("committed_at + {} * {}", f[19], target.symbol))
                            && super::candidates_ref().is_ok_and(|all| {
                                let m: Vec<_> = all
                                    .iter()
                                    .filter(|c| {
                                        c.path == source.path
                                            && c.scope == source.scope
                                            && c.kind == "assignment"
                                            && c.symbol == f[19]
                                    })
                                    .collect();
                                m.len() == 1
                                    && m[0].expression.starts_with("2 if ")
                                    && m[0].expression.ends_with(" else 1")
                            })
                    }
                    "reviewed-source-subscript-alias" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && source.kind == "assignment"
                            && target.kind == "assignment"
                            && target.line < source.line
                            && source.symbol == format!("persisted_member['{}']", f[19])
                            && source.expression == f[19]
                            && target.symbol == f[19]
                    }
                    // A required keyword-only constructor parameter stored unchanged; the class has
                    // exactly one shipped construction (git-grep evidence in the witness).
                    "reviewed-source-sole-constructor-forward" => {
                        source.path == "scripts/run_a10_physical_gate.py"
                            && !target.path.is_empty()
                            && source.kind == "assignment"
                            && source.symbol == "self._timeout_s"
                            && source.scope == format!("{}.__init__", f[19])
                            && f[19] == "ProductVerifierClient"
                            && source.expression == "timeout_s"
                            && target.kind == "keyword"
                            && target.symbol == "timeout_s"
                    }
                    // The remaining part of one shared monotonic deadline passed by every caller.
                    "reviewed-source-remaining-parameter" => {
                        source.path == target.path
                            && source.kind == "keyword"
                            && source.symbol == "timeout"
                            && source.expression == "max(0.0, deadline - time.monotonic())"
                            && target.kind == "keyword"
                            && target.symbol == "deadline"
                            && target.expression.starts_with("time.monotonic() + ")
                            && super::candidates_ref().is_ok_and(|all| {
                                let mut lines: Vec<usize> = all
                                    .iter()
                                    .filter(|c| {
                                        c.path == target.path
                                            && c.kind == "keyword"
                                            && c.symbol == "deadline"
                                            && c.expression == target.expression
                                    })
                                    .map(|c| c.line)
                                    .collect();
                                lines.sort_unstable();
                                lines.len() > 1
                                    && lines
                                        .iter()
                                        .map(usize::to_string)
                                        .collect::<Vec<_>>()
                                        .join(",")
                                        == f[19]
                            })
                    }
                    // Exact re-reads of the expiry of a membership message that already passed
                    // `_validate_common` (relative `MAX_MESSAGE_TTL_SECONDS` check); the target is
                    // that validator's reviewed expiry row. Nothing else qualifies by shape.
                    "reviewed-source-verified-message-reread" => {
                        target.path == "mycelium_membership/contracts.py"
                            && target.scope == "_validate_common"
                            && target.kind == "assignment"
                            && target.symbol == "expires_at"
                            && matches!(
                                (
                                    source.path.as_str(),
                                    source.scope.as_str(),
                                    source.kind.as_str(),
                                    source.symbol.as_str(),
                                    source.expression.as_str(),
                                    f[19],
                                ),
                                (
                                    "mycelium_seed/coordinator.py",
                                    "SeedCoordinator._receive_member_message",
                                    "keyword",
                                    "heartbeat_expires_at",
                                    "float(message['expires_at'])",
                                    "verify_membership_message",
                                ) | (
                                    "mycelium_seed/coordinator.py",
                                    "SeedCoordinator._remember",
                                    "keyword",
                                    "expires_at",
                                    "float(message['expires_at'])",
                                    "verify_membership_message",
                                ) | (
                                    // Coordinator adjudication: both validators are reached only
                                    // through `validate_membership_message`, which runs
                                    // `_validate_common` on the same mapping first.
                                    "mycelium_membership/contracts.py",
                                    "_validate_assignment_offer",
                                    "assignment",
                                    "expires_at",
                                    "_number(message['expires_at'])",
                                    "validate_membership_message",
                                ) | (
                                    "mycelium_membership/contracts.py",
                                    "_validate_drain_ack",
                                    "assignment",
                                    "expires",
                                    "_number(message['expires_at'])",
                                    "validate_membership_message",
                                )
                            )
                    }
                    // The sole call passes a parsed argparse float positionally; the required
                    // parameter feeds exactly one monotonic deadline in the callee.
                    "reviewed-source-sole-caller-cli-clock" => {
                        source.path == target.path
                            && source.path == "scripts/run_a4_physical_idle_staleness_gate.py"
                            && f[19]
                                == "_observe_stall_progression:observation_window=args.observation_window"
                            && source.kind == "assignment"
                            && source.scope == "_observe_stall_progression"
                            && source.symbol == "deadline"
                            && source.expression == "time.monotonic() + observation_window"
                            && target.kind == "cli-default"
                            && target.symbol == "--observation-window"
                    }
                    // A module-default injected callable receives the reviewed forwarded value
                    // (exact pairs; tests substitute fakes, production never does).
                    "reviewed-source-default-callable-forward" => {
                        source.path == "scripts/run_a10_concurrency_probe.py"
                            && target.path == source.path
                            && source.kind == "keyword"
                            && target.kind == "keyword"
                            && source.symbol == "timeout"
                            && target.symbol == "timeout_seconds"
                            && source.expression == "timeout_seconds"
                            && target.expression == "timeout_seconds"
                            && matches!(
                                (source.scope.as_str(), target.scope.as_str(), f[19]),
                                ("_post_json", "fire_concurrent_requests._one", "_post_json")
                                    | ("_get_json", "fetch_observation_evidence", "_get_json")
                            )
                    }
                    // Every production call of the sharder passes the same reviewed constant
                    // keyword; the callee leaves its None default untouched.
                    "reviewed-source-cross-module-callers-forward" => {
                        source.path == "mycelium_qualification/safetensors_sharding.py"
                            && target.path == "scripts/build_qwen_live_route.py"
                            && f[19] == "253,1639,1666"
                            && source.kind == "keyword"
                            && source.symbol == "max_file_bytes"
                            && source.expression == "max_file_bytes"
                            && target.kind == "keyword"
                            && target.symbol == "max_file_bytes"
                            && target.expression == "_MAX_STAGED_MODEL_FILE_BYTES"
                    }
                    // The only construction passes the reviewed dataclass-field keyword; the
                    // constructor stores the validated count unchanged.
                    "reviewed-source-sole-constructor-field-forward" => {
                        source.path == "mycelium_internet/bootstrap.py"
                            && target.path == source.path
                            && f[19] == "InviteAttemptTracker"
                            && source.kind == "assignment"
                            && source.scope == "InviteAttemptTracker.__init__"
                            && source.symbol == "self._max_attempts"
                            && source.expression == "max_attempts"
                            && target.kind == "keyword"
                            && target.symbol == "max_attempts"
                            && target.expression == "self.max_join_attempts_per_invite"
                    }
                    // The breaker cell is written only by the 0 sentinel and this max(); the
                    // module constant is scaled seconds-to-milliseconds exactly once.
                    "reviewed-source-scaled-max-deadline" => {
                        source.path == "mycelium_a6_recovery.py"
                            && target.path == source.path
                            && f[19] == "SUCCESSOR_BREAKER_SECONDS*1000"
                            && source.kind == "assignment"
                            && source.scope == "RecoveryRuntime.record_successor_failure"
                            && source.symbol == "self._breaker_open_until_unix_ms"
                            && source.expression
                                == "max(self._breaker_open_until_unix_ms, now + SUCCESSOR_BREAKER_SECONDS * 1000)"
                            && target.kind == "assignment"
                            && target.scope == "<module>"
                            && target.symbol == "SUCCESSOR_BREAKER_SECONDS"
                    }
                    // The module-default wait helper is only reached through the seven
                    // `self.wait(..., timeout=...)` call sites (witnessed in the review text).
                    "reviewed-source-default-wait-callers" => {
                        source.path == "scripts/run_a12_mobile_physical_gate.py"
                            && target.path == source.path
                            && f[19] == "279,292,370,376,378,393,404"
                            && source.kind == "assignment"
                            && source.scope == "wait_until"
                            && source.symbol == "deadline"
                            && source.expression == "clock() + timeout"
                            && target.kind == "keyword"
                            && target.symbol == "timeout"
                            && target.expression == "self.timeout"
                    }
                    // A property read on the annotated fault target returns the single-writer
                    // constructor field, and the returned closure has exactly one call.
                    "reviewed-source-annotated-property-read" => {
                        source.path == "mycelium_a6_product_fault_seams.py"
                            && target.path == source.path
                            && f[19] == "wait_seconds"
                            && source.kind == "positional-call"
                            && source.symbol == "join"
                            && source.expression == "target.wait_seconds"
                            && source.scope == "ProductCaseSeam._apply_cancellation_during_prefill"
                            && target.kind == "assignment"
                            && target.scope == "LiveProductFaultTarget.__init__"
                            && target.symbol == "self._wait_seconds"
                    }
                    "reviewed-source-closure-sole-call" => {
                        source.path == "mycelium_a6_product_fault_seams.py"
                            && target.path == source.path
                            && f[19] == "join"
                            && source.kind == "keyword"
                            && source.symbol == "timeout"
                            && source.expression == "seconds"
                            && source.scope == "LiveProductFaultTarget.start_cancel.join"
                            && target.kind == "positional-call"
                            && target.symbol == "join"
                            && target.expression == "target.wait_seconds"
                    }
                    // select() waits the remaining part of the deadline passed by the executor's
                    // two _reply calls (witnessed); both carry the same stored timeout.
                    "reviewed-source-select-remaining-deadline" => {
                        source.path == "scripts/qwen_phone_builder.py"
                            && target.path == source.path
                            && f[19] == "_reply:366,402"
                            && source.kind == "positional-call"
                            && source.symbol == "selector.select"
                            && source.expression == "remaining"
                            && source.scope == "RustStageExecutor._reply"
                            && target.kind == "assignment"
                            && target.scope == "RustStageExecutor.execute"
                            && target.symbol == "deadline"
                            && target.expression == "time.monotonic() + self.timeout"
                    }
                    // One exact sleep that waits a fixed fraction of a typed settings interval.
                    "reviewed-source-interval-submultiple" => {
                        source.path == "mycelium_a7_physical_measurement.py"
                            && target.path == source.path
                            && f[19] == "poll_s/5"
                            && source.kind == "positional-call"
                            && source.symbol == "time.sleep"
                            && source.scope == "_await_unfrozen_settle"
                            && source.expression == "settings.poll_s / 5"
                            && target.kind == "assignment"
                            && target.scope == "MeasurementSettings"
                            && target.symbol == "poll_s"
                    }
                    // The callee rejects the required argument above a named module ceiling before
                    // any use; the CLI forwards the parsed float unchanged.
                    "reviewed-source-callee-validator-ceiling" => {
                        source.path == "scripts/manage_seed_membership.py"
                            && target.path == "mycelium_seed/operator.py"
                            && f[19] == "begin_seed_key_rotation"
                            && source.kind == "keyword"
                            && source.symbol == "overlap_seconds"
                            && source.expression == "args.overlap_seconds"
                            && target.kind == "assignment"
                            && target.scope == "<module>"
                            && target.symbol == "_MAX_ROTATION_OVERLAP_SECONDS"
                    }
                    // Exactly two shipped calls inside one function annotated with the settings
                    // type pass the field (git-grep evidence in the witness); nominal default only.
                    "reviewed-source-settings-callers" => {
                        source.path == target.path
                            && source.path == "mycelium_a7_physical_measurement.py"
                            && f[19] == "955,1188"
                            && source.kind == "keyword"
                            && source.symbol == "timeout"
                            && source.expression == "timeout_s"
                            && source.scope == "_wait_done"
                            && target.kind == "assignment"
                            && target.scope == "MeasurementSettings"
                            && target.symbol == "terminal_timeout_s"
                    }
                    "reviewed-source-cli-guarded-clock" => {
                        source.path == target.path
                            && source.kind == "keyword"
                            && f[19].starts_with("args.")
                            && source.expression.ends_with(&format!(" + {} * 1000", f[19]))
                            && target.kind == "comparison"
                            && target.scope == source.scope
                            && target.line < source.line
                            && target.expression.contains(f[19])
                    }
                    "reviewed-source-expiry-alias" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && source.expression == target.symbol
                            && target.symbol == f[19]
                            && target.line < source.line
                    }
                    "reviewed-source-tightening-minimum" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && target.symbol == f[19]
                            && target.line < source.line
                            && target.expression.starts_with("min(now + ")
                            && (source.expression.starts_with(&format!("min({}, ", f[19]))
                                || source.expression.ends_with(&format!(", {})", f[19])))
                    }
                    "reviewed-source-qualified-clock" => {
                        target.scope == "<module>"
                            && source.expression
                                == format!("time.monotonic() + {}.{}", f[19], target.symbol)
                            && target.path == format!("{}.py", f[19])
                    }
                    "reviewed-source-class-optional" => {
                        source.path == target.path
                            && source.scope.starts_with(&format!("{}.", target.scope))
                            && source.expression
                                == format!(
                                    "self.{} if {} is None else {}",
                                    target.symbol, f[19], f[19]
                                )
                    }
                    "reviewed-source-tuple-default" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && target.kind == "parameter"
                            && source.symbol == "(self.opener, self.timeout)"
                            && f[19] == "1"
                            && source.expression == format!("(opener, {})", target.symbol)
                    }
                    "reviewed-source-tuple-field" => {
                        source.path == target.path
                            && target.kind == "parameter"
                            && target.scope.ends_with(".__init__")
                            && source.expression == format!("self.{}", target.symbol)
                            && source.expression == f[19]
                            && source.scope.rsplit_once('.').map(|x| x.0)
                                == target.scope.rsplit_once('.').map(|x| x.0)
                    }
                    "reviewed-source-lexical-closure" => {
                        source.path == target.path
                            && target.kind == "parameter"
                            && source.scope.starts_with(&format!("{}.", target.scope))
                            && source.expression == target.symbol
                            && f[19] == target.symbol
                    }
                    "reviewed-source-local-alias" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.kind == "positional-call"
                            && source.expression == f[19]
                    }
                    "reviewed-source-optional-alias" => {
                        source.path == target.path
                            && target.scope.ends_with(".__init__")
                            && source.scope.rsplit_once('.').map(|x| x.0)
                                == target.scope.rsplit_once('.').map(|x| x.0)
                            && source.kind == "positional-call"
                            && source.expression == f[19]
                    }
                    "reviewed-source-validated-default" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && target.kind == "parameter"
                            && source.kind == "assignment"
                            && source.expression
                                == format!("{}('{}', {})", f[19], target.symbol, target.symbol)
                    }
                    "reviewed-source-positional-upper" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.kind == "assignment"
                            && source.expression.starts_with(&format!("{}(", f[19]))
                            && source
                                .expression
                                .ends_with(&format!(", 1, {})", target.symbol))
                    }
                    "reviewed-source-integer-clock" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && source.expression == format!("{} + {}", f[19], target.symbol)
                    }
                    "reviewed-source-tightened-expiry" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && target.symbol == f[19]
                            && target.line < source.line
                            && (source.expression == f[19]
                                || source.expression
                                    == format!("min({}, float(row['expires_at']))", f[19]))
                    }
                    "reviewed-source-walrus-remaining" => {
                        source.path == target.path
                            && source.scope == target.scope
                            && source.expression == f[19]
                            && target.symbol == "deadline"
                            && source.symbol == "connection.settimeout"
                            && target.expression.starts_with("time.monotonic() + ")
                    }
                    // Frame readers receive one extra byte so an over-limit frame is detected
                    // (payload plus newline terminator): `len(x) > self.F + 1` still references
                    // the constructor field `F` exactly; the offset stays in the pinned expression.
                    "reviewed-source-comparison-sentinel" => {
                        source.path == target.path
                            && source.kind == "comparison"
                            && target.kind == "assignment"
                            && target.symbol.starts_with("self.")
                            && target
                                .scope
                                .strip_suffix(".__init__")
                                .is_some_and(|class| source.scope.starts_with(&format!("{class}.")))
                            && source.expression
                                == format!("len({}) > {} + 1", f[19], target.symbol)
                    }
                    // A bare local `remaining` slice (single writer `deadline - time.monotonic()`,
                    // positive guard before use) consumed by a wait call, measured against one
                    // reviewed monotonic deadline in the same or an enclosing function. The
                    // slice carries the deadline's allowance; it is not an OS/IO completion bound.
                    "reviewed-source-local-remaining" => {
                        source.path == target.path
                            && matches!(source.kind.as_str(), "positional-call" | "keyword")
                            && source.expression == f[19]
                            && f[19] == "remaining"
                            && target.kind == "assignment"
                            && target.symbol == "deadline"
                            && target.expression.starts_with("time.monotonic() + ")
                            && target.line < source.line
                            && (source.scope == target.scope
                                || source.scope.starts_with(&format!("{}.", target.scope)))
                    }
                    "reviewed-source-helper-default" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.expression == format!("{}()", f[19])
                    }
                    "reviewed-source-helper-default-clock" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.expression == format!("started + {}()", f[19])
                    }
                    "reviewed-source-conditional-clock" => {
                        source.path == target.path
                            && target.scope == "<module>"
                            && source.expression
                                == format!(
                                    "time.monotonic() + ({} if not {} else 0.0)",
                                    target.symbol, f[19]
                                )
                    }
                    "reviewed-source-seed-return-ceiling" => {
                        source.path == "mycelium_seed/coordinator.py"
                            && target.path == "mycelium_membership/contracts.py"
                            && target.scope == "<module>"
                            && target.symbol == "MAX_MESSAGE_TTL_SECONDS"
                            && match f[19] {
                                "call" => source.expression.starts_with(
                                    "self._bounded_deadline(issued_at=now, ttl_seconds=self.",
                                ),
                                "minimum" => source.expression.starts_with(
                                    "min(self._bounded_deadline(issued_at=now, ttl_seconds=self.",
                                ),
                                "renewal-alias" => source.expression == "renewed_until",
                                "guarded-advance" => {
                                    source.expression == "advanced"
                                        && source.scope == "SeedCoordinator._bounded_deadline"
                                }
                                // min() and the one-ulp headroom are bounded by construction
                                // by `ceiling`; the max() with the previous deadline (:451)
                                // is an unvalidated intermediate and stays pending.
                                "min-ceiling" | "headroom-ceiling" => {
                                    source.kind == "assignment"
                                        && source.symbol == "deadline"
                                        && source.scope == "SeedCoordinator._bounded_deadline"
                                        && source.expression
                                            == match f[19] {
                                                "min-ceiling" => "min(requested, ceiling)",
                                                _ => "math.nextafter(ceiling, -math.inf)",
                                            }
                                }
                                _ => false,
                            }
                    }
                    _ => false,
                }
            })
}

// Full pinned source/target witnesses from the canonical A review.
// These describe exact defaults/caps, not runtime mutation authority.
fn reviewed_partition_a_round2(rule: &str, source: &Candidate, target: &Candidate) -> bool {
    source.key != target.key
        && crate::inventory::keyed_lines(
            include_str!("../../data/python-partition-a-round2-edges.tsv"),
            &source.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            if f.len() != 21
                || !exact_candidate(&f[..9], source)
                || !exact_candidate(&f[9..18], target)
                || f[19] != rule
                || f[20].is_empty()
            {
                return false;
            }
            match (f[18], rule) {
                ("a-clipped-poll", "reviewed-composed-ceiling") => {
                    target.path == "mycelium_deadlines.py"
                        && target.symbol == "CLEANUP_RECEIPT_POLL_SECONDS"
                        && source.expression
                            == "max(0.0, min(CLEANUP_RECEIPT_POLL_SECONDS, deadline - clock()))"
                }
                ("a-configured-grace", "typed-config-field") => {
                    source.path == target.path
                        && target.kind == "dict-field"
                        && target.symbol == "member_restart_grace"
                        && source.expression == "self.budgets['member_restart_grace']"
                }
                ("a-inherited-timeout", "resolved-optional-value") => {
                    source.path == "mycelium_a13_macos/local_contributor.py"
                        && target.path == "mycelium_a13_macos/peer.py"
                        && target.scope == "RemoteMacMember.__init__"
                        && target.symbol == "self._timeout"
                        && source.expression == target.symbol
                }
                ("a-transport-helper", "reviewed-helper-return") => {
                    source.path == target.path
                        && target.scope == "ManagedControlClient.__init__"
                        && target.symbol == "self.timeout"
                        && source.expression == "self._transport_timeout()"
                }
                ("a-renewal-margin", "reviewed-composed-ceiling") => {
                    source.path == target.path
                        && source.kind == "comparison"
                        && target.scope == "<module>"
                        && target.symbol == "RENEWAL_MARGIN_SECONDS"
                        && source.expression
                            == "left <= min(RENEWAL_MARGIN_SECONDS, self._window / 3)"
                }
                ("reviewed-source-lexical-closure", "resolved-optional-value") => {
                    source.path == target.path
                        && target.kind == "parameter"
                        && source.scope.starts_with(&format!("{}.", target.scope))
                        && source.expression == target.symbol
                }
                ("a-imported-milliseconds", "resolved-optional-value") => {
                    source.path == "mycelium_a13_macos/token_policy.py"
                        && target.path == "mycelium_a13_macos/managed_control.py"
                        && target.symbol == "MAX_CLOCK_SKEW_SECONDS"
                        && source.expression == "int(MAX_CLOCK_SKEW_SECONDS * 1000)"
                }
                ("a-integral-bound-default", "resolved-optional-value") => {
                    source.path == target.path
                        && source.scope == target.scope
                        && target.kind == "parameter"
                        && target.expression == "_STDERR_RING_BYTES"
                        && source.expression == format!("int({})", target.symbol)
                }
                ("a-class-clock", "reviewed-clock-offset") => {
                    source.path == target.path
                        && target.scope == "NativeSidecarProcess"
                        && target.symbol == "socket_conflict_grace_seconds"
                        && source.expression
                            == "time.monotonic() + max(0.0, self.socket_conflict_grace_seconds)"
                }
                ("a-poll-success-cap", "reviewed-helper-return") => {
                    source.path == target.path
                        && source.scope == "SwarmCoordinator.poll_work"
                        && target.scope == "<module>"
                        && target.symbol == "_MAX_LONG_POLL_SECONDS"
                        && matches!(source.expression.as_str(), "now + timeout" | "remaining")
                }
                ("a-timing-expiry", "reviewed-clock-offset") => {
                    source.path == target.path
                        && target.kind == "dict-field"
                        && target.symbol == "stale_after_s"
                        && source.expression == "issued + self._timings['stale_after_s']"
                }
                ("a-timing-helper", "resolved-optional-value") => {
                    source.path == target.path
                        && target.kind == "dict-field"
                        && target.symbol == "sample_interval_s"
                        && source.expression == "resolve_timings(policy)['sample_interval_s']"
                }
                _ => false,
            }
        })
}

pub fn accepts(rule: &str, source: &Candidate, target: &Candidate) -> bool {
    if reviewed_partition_a_round2(rule, source, target) {
        return true;
    }
    if rule == "reviewed-composed-ceiling" {
        return source.key != target.key
            && source.path == target.path
            && source.scope == target.scope
            && source.kind == "assignment"
            && target.kind == "ceiling-operand"
            && super::categories::scaled_decimal(&target.expression, 1000).is_some_and(|n| n > 0)
            && crate::inventory::keyed_lines(include_str!("../../data/python-composed-ceilings.tsv"), &source.key).any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 20 && exact_candidate(&f[..9], source)
                        && exact_candidate(&f[9..18], target) && !f[19].is_empty()
                        && match f[18] {
                            "min-first" => super::categories::call_pair(&source.expression, "min")
                                .is_some_and(|(left, _)| left == target.expression),
                            "deadline-inner-min" => source.expression == format!(
                                "min(deadline, time.monotonic() + min({}, remaining / 2))", target.expression),
                            "deadline-min-offset" => source.expression == format!(
                                "min(deadline, time.monotonic() + {})", target.expression),
                            "stop-min-three" => source.expression == format!(
                                "min(self.response_timeout_seconds, {}, max(0.0, deadline - time.monotonic()))", target.expression),
                            "conditional-backoff-offset" => source.expression == format!(
                                "self._clock() + (min({}, self.rollover_backoff_seconds * 2 ** (failures - 1)) if failures else 0.0)", target.expression),
                            _ => false,
                        }
                });
    }
    if reviewed_source_flow(rule, source, target) {
        return true;
    }
    if rule == "typed-config-field" && configured_float_field(source, target) {
        return true;
    }
    if rule == "reviewed-clock-offset" && configured_clock_offset(source, target) {
        return true;
    }
    if rule == "reviewed-helper-return" {
        return source.path == target.path
            && source.scope == target.scope
            && source.key != target.key
            && target.kind == "keyword"
            && target.symbol == "maximum"
            && target.line >= source.line
            && matches!(source.kind.as_str(), "assignment" | "keyword")
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-helper-return-edges.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 22
                    && exact_candidate(&f[..9], source)
                    && exact_candidate(&f[9..18], target)
                    && matches!(
                        f[18],
                        "mycelium_platform_neutral/primitives.py"
                            | "mycelium_gateway/event_adapter.py"
                            | "mycelium_interactive/server.py"
                            | "mycelium_swarm_artifacts.py"
                    )
                    && f[19].parse::<usize>().is_ok_and(|n| n > 0)
                    && !f[20].is_empty()
                    && !f[21].is_empty()
                    && source
                        .expression
                        .contains(&format!("maximum={}", target.expression))
            });
    }
    if rule == "gossip-successful-validation" {
        return source.path == "mycelium_gossip/schema.py"
            && target.path == source.path
            && target.line == 453
            && target.column == 7
            && target.scope == "_validate_envelope_fields"
            && target.kind == "comparison"
            && target.symbol == "ttl"
            && target.expression == "ttl > 7 * 24 * 60 * 60 * 1000"
            && match (
                source.line,
                source.column,
                source.scope.as_str(),
                source.kind.as_str(),
                source.symbol.as_str(),
            ) {
                (452, 4, "_validate_envelope_fields", "assignment", "ttl") => {
                    source.expression == "_require_int(ttl_ms, 'ttl_ms', 1)"
                }
                (481, 8, "build_record", "keyword", "ttl_ms") => source.expression == "ttl_ms",
                (494, 8, "build_record", "keyword", "ttl_ms") => source.expression == "ttl",
                (526, 8, "record_from_dict", "keyword", "ttl_ms") => {
                    source.expression == "value['ttl_ms']"
                }
                _ => false,
            };
    }
    if rule == "reviewed-retry-state" {
        return source.key != target.key
            && source.path == target.path
            && crate::inventory::keyed_lines(include_str!("../../data/python-retry-state-edges.tsv"), &source.key)
                .any(|line| {
                    let f: Vec<_> = line.split('\t').collect();
                    f.len() == 20
                        && exact_candidate(&f[..9], source)
                        && exact_candidate(&f[9..18], target)
                        && !f[19].is_empty()
                        && match f[18] {
                            "phase-backoff-wait" => source.kind == "positional-call"
                                && source.scope == target.scope
                                && target.line < source.line
                                && source.expression == "delay"
                                && target.symbol == "self._outbound_retry_backoff",
                            "http-backoff-wait" => source.kind == "positional-call"
                                && source.scope == "ManagedControlClient._request"
                                && source.expression == "wait"
                                && matches!(source.symbol.as_str(), "time.sleep" | "wake.wait")
                                && target.scope == "<module>"
                                && target.symbol == "HTTP_RETRY_MAX_SECONDS",
                            "deadline-tighten" => source.kind == "assignment"
                                && source.scope == target.scope
                                && target.line < source.line
                                && target.expression == "time.monotonic() + HTTP_RETRY_WINDOW_SECONDS"
                                && source.expression == format!("min({}, parent)", target.symbol),
                            "attempt-limit" => source.kind == "comparison"
                                && source.scope == "ManagedControlClient._request"
                                && target.scope == "<module>"
                                && source.expression == format!("attempt + 1 == {}", target.symbol),
                            "conditional-retry-cap" => source.kind == "assignment"
                                && target.scope == "<module>"
                                && source.expression == format!(
                                    "min({}, self._retry_delay * 2) if self._retry_delay else _RETRY_FIRST_SECONDS", target.symbol),
                            "retry-clock-offset" => source.kind == "assignment"
                                && source.scope == target.scope
                                && target.line < source.line
                                && source.expression == format!("now + {}", target.symbol),
                            _ => false,
                        }
                });
    }
    if rule == "reviewed-helper-field" {
        return source.key != target.key
            && source.path == target.path
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-helper-edges.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 19
                    && exact_candidate(&f[..9], source)
                    && exact_candidate(&f[9..18], target)
                    && !f[18].is_empty()
                    && (source.kind == "validated-field" || target.kind == "validated-field")
            });
    }
    if rule == "reviewed-clock-offset" {
        return source.key != target.key
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-clock-offsets.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 21
                    && exact_candidate(&f[..9], source)
                    && exact_candidate(&f[9..18], target)
                    && !f[18].is_empty()
                    && matches!(
                        f[19],
                        "monotonic-seconds" | "wall-seconds" | "wall-milliseconds"
                    )
                    && !f[20].is_empty()
                    && source.expression == format!("{} + {}", f[18], target.symbol)
            });
    }
    if rule == "directory-offer-age" {
        return source.path == "mycelium_swarm_directory/directory.py"
            && target.path == source.path
            && target.line == 47
            && target.column == 4
            && target.scope == "Limits"
            && target.symbol == "offer_ttl"
            && target.kind == "assignment"
            && target.expression == "3600"
            && source.kind == "comparison"
            && match (source.line, source.column, source.scope.as_str()) {
                (327, 21, "Directory._owner_inbox") => {
                    source.symbol == "now - v['received']"
                        && source.expression == "now - v['received'] <= self.limits.offer_ttl"
                }
                (303, 15, "Directory._entry_offer") => {
                    source.symbol == "now - value['received']"
                        && source.expression == "now - value['received'] > self.limits.offer_ttl"
                }
                _ => false,
            };
    }
    if rule == "schedule-maximum" {
        return crate::inventory::keyed_lines(
            include_str!("../../data/python-schedule-edges.tsv"),
            &source.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            if f.len() != 22
                || !exact_candidate(&f[..9], source)
                || !exact_candidate(&f[9..18], target)
                || f[20] != rule
                || f[21].is_empty()
            {
                return false;
            }
            let Ok(all) = super::candidates_ref() else {
                return false;
            };
            let Some(schedule) = all.iter().find(|c| c.key == f[18]) else {
                return false;
            };
            if schedule.path != source.path
                || schedule.path != target.path
                || schedule.scope != "<module>"
                || schedule.kind != "assignment"
                || schedule.expression != f[19]
                || target.kind != "tuple-element"
                || target.scope != schedule.scope
                || target.line != schedule.line
            {
                return false;
            }
            let Some(inner) = schedule
                .expression
                .strip_prefix('(')
                .and_then(|v| v.strip_suffix(')'))
            else {
                return false;
            };
            let members: Vec<_> = inner.split(',').map(str::trim).collect();
            let Some(values) = members
                .iter()
                .map(|v| super::categories::scaled_decimal(v, 1000))
                .collect::<Option<Vec<_>>>()
            else {
                return false;
            };
            let Some(index) = target
                .symbol
                .strip_prefix(&format!("{}[", schedule.symbol))
                .and_then(|v| v.strip_suffix(']'))
                .and_then(|v| v.parse::<usize>().ok())
            else {
                return false;
            };
            members.get(index) == Some(&target.expression.as_str())
                && values.get(index) == values.iter().max()
                && source.expression
                    == format!(
                        "{}[min(attempt, len({}) - 1)]",
                        schedule.symbol, schedule.symbol
                    )
        });
    }

    if rule == "validated-required-ceiling" {
        return source.path == target.path
            && source.scope == target.scope
            && source.kind == "assignment"
            && target.kind == "comparison"
            && target.line < source.line
            && source.expression == target.symbol
            && target
                .expression
                .starts_with(&format!("{} > ", target.symbol))
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-required-ceiling-edges.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 20
                    && exact_candidate(&f[..9], source)
                    && exact_candidate(&f[9..18], target)
                    && f[18] == rule
                    && !f[19].is_empty()
            });
    }
    if rule == "comparison-imported-constant" {
        return source.kind == "comparison"
            && [" <= ", " < ", " >= ", " > "].iter().any(|operator| {
                source
                    .expression
                    .rsplit_once(operator)
                    .is_some_and(|(left, right)| {
                        if left.is_empty() || right.is_empty() {
                            return false;
                        }
                        let mut reference = source.clone();
                        reference.expression = right.into();
                        accepts("imported-constant", &reference, target)
                    })
            });
    }
    if matches!(
        rule,
        "terminal-composer-policy"
            | "terminal-retention-window"
            | "retained-kv-expiry"
            | "terminal-capacity-enforcement"
            | "terminal-positive-policy"
    ) {
        let witnessed = crate::inventory::keyed_lines(
            include_str!("../../data/python-terminal-policy-edges.tsv"),
            &source.key,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 20
                && exact_candidate(&f[..9], source)
                && exact_candidate(&f[9..18], target)
                && f[18] == rule
                && !f[19].is_empty()
        });
        return witnessed
            && source.key != target.key
            && match rule {
                "terminal-composer-policy" => {
                    target.path == "mycelium_router/terminal_kv_retention.py"
                        && target.scope == "<module>"
                        && matches!(
                            target.symbol.as_str(),
                            "DEFAULT_TERMINAL_KV_RETENTION_SECONDS"
                                | "DEFAULT_TERMINAL_KV_RETENTION_ENTRIES"
                        )
                        && matches!(source.kind.as_str(), "keyword" | "assignment")
                }
                "terminal-retention-window" => {
                    source.path == target.path
                        && source.expression
                            == "min(float(now) + self.seconds, kv_lease_deadline(state))"
                        && target.symbol == "self.seconds"
                        && target.scope == "TerminalKVRetention.enable"
                }
                "retained-kv-expiry" => {
                    source.path == target.path
                        && source.scope == "TerminalKVRetention.expire"
                        && source.kind == "comparison"
                        && source.expression == "float(now) >= deadline"
                        && target.scope == "TerminalKVRetention.hold"
                        && target.symbol == "deadline"
                        && target.expression
                            == "min(float(now) + self.seconds, kv_lease_deadline(state))"
                }
                "terminal-capacity-enforcement" => {
                    source.path == target.path
                        && source.expression == "len(self._held) > self.max_entries"
                        && target.symbol == "self.max_entries"
                        && target.scope == "TerminalKVRetention.enable"
                }
                "terminal-positive-policy" => {
                    source.path == target.path
                        && source.kind == "comparison"
                        && matches!(
                            source.expression.as_str(),
                            "seconds > 0" | "self.seconds > 0"
                        )
                        && target.symbol == "self.seconds"
                        && target.scope == "TerminalKVRetention.enable"
                }
                _ => false,
            };
    }
    if matches!(rule, "reviewed-config-clamp" | "reviewed-local-comparison") {
        return source.key != target.key
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-reviewed-policy-edges.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 18
                    && f[0] == source.key
                    && f[1] == source.path
                    && f[2].parse::<usize>() == Ok(source.line)
                    && f[3].parse::<usize>() == Ok(source.column)
                    && f[4] == source.scope
                    && f[5] == source.symbol
                    && f[6] == source.expression
                    && f[7] == source.kind
                    && f[8] == source.source
                    && f[9] == target.key
                    && f[10] == target.path
                    && f[11] == target.scope
                    && f[12] == target.symbol
                    && f[13] == rule
                    && !f[17].is_empty()
                    && target.kind == "assignment"
                    && if rule == "reviewed-config-clamp" {
                        source.kind == "assignment"
                            && f[15] == "max"
                            && f[16] == "0.0"
                            && f[14].ends_with(&format!(".{}", target.symbol))
                            && source.expression == format!("max(0.0, {})", f[14])
                    } else {
                        source.kind == "comparison"
                            && source.path == target.path
                            && source.scope == target.scope
                            && target.line < source.line
                            && f[14] == target.symbol
                            && f[15] == ">"
                            && f[16].is_empty()
                            && source.expression == format!("{} > {}", source.symbol, target.symbol)
                    }
            });
    }
    if matches!(
        rule,
        "upper-cap-reference"
            | "lower-floor-reference"
            | "imported-upper-cap-reference"
            | "monotonic-upper-cap-reference"
    ) {
        let operation = if rule == "lower-floor-reference" {
            "max"
        } else {
            "min"
        };
        let expression = if rule == "monotonic-upper-cap-reference" {
            let Some(inner) = source.expression.strip_prefix("time.monotonic() + ") else {
                return false;
            };
            inner
        } else {
            source.expression.as_str()
        };
        let Some((left, right)) = super::categories::call_pair(expression, operation) else {
            return false;
        };
        return source.key != target.key
            && (rule == "imported-upper-cap-reference" || source.path == target.path)
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-clamp-bindings.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 16
                    && f[0] == source.key
                    && f[1] == source.path
                    && f[2].parse::<usize>() == Ok(source.line)
                    && f[3].parse::<usize>() == Ok(source.column)
                    && f[4] == source.scope
                    && f[5] == source.symbol
                    && f[6] == source.expression
                    && f[7] == source.kind
                    && f[8] == source.source
                    && f[9] == target.key
                    && f[10] == target.symbol
                    && f[11] == operation
                    && !f[15].is_empty()
                    && ((f[13] == "0" && f[14] == left) || (f[13] == "1" && f[14] == right))
                    && match f[12] {
                        "value" => rule != "imported-upper-cap-reference" && f[14] == target.symbol,
                        "imported-value" if rule == "imported-upper-cap-reference" => {
                            let mut reference = source.clone();
                            reference.expression = f[14].into();
                            accepts("imported-constant", &reference, target)
                        }
                        "clock-offset" => {
                            operation == "min"
                                && f[14] == format!("time.monotonic() + {}", target.symbol)
                        }
                        _ => false,
                    }
            });
    }
    if matches!(
        rule,
        "validated-parameter-default"
            | "optional-parameter-default"
            | "resolved-optional-value"
            | "resolved-poll-default"
    ) {
        if source.path != target.path || source.key == target.key {
            return false;
        }
        let table = if rule == "validated-parameter-default" {
            include_str!("../../data/python-validated-bindings.tsv")
        } else if rule == "optional-parameter-default" {
            include_str!("../../data/python-optional-bindings.tsv")
        } else {
            include_str!("../../data/python-resolved-default-bindings.tsv")
        };
        return crate::inventory::keyed_lines(table, &source.key).any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 15
                && f[0] == source.key
                && f[1] == source.path
                && f[2].parse::<usize>() == Ok(source.line)
                && f[3].parse::<usize>() == Ok(source.column)
                && f[4] == source.scope
                && f[5] == source.symbol
                && f[6] == source.expression
                && f[7] == source.kind
                && f[8] == source.source
                && f[9] == target.key
                && f[10] == target.symbol
                && f[12].parse::<usize>().is_ok_and(|n| n > 0)
                && !f[13].is_empty()
                && !f[14].is_empty()
                && if rule == "validated-parameter-default" {
                    source.kind == "assignment"
                        && target.kind == "parameter"
                        && source.scope == target.scope
                        && target.expression != "None"
                        && (source
                            .expression
                            .starts_with(&format!("{}({},", f[11], target.symbol))
                            || source.expression == format!("{}({})", f[11], target.symbol))
                } else if rule == "resolved-poll-default" {
                    source.kind == "assignment"
                        && target.kind == "parameter"
                        && target.expression == "None"
                        && source.scope == target.scope
                        && source.expression == f[13]
                        && source.expression
                            == format!(
                                "{} if single_poll or {} is None else {}",
                                f[11], target.symbol, target.symbol
                            )
                } else if rule == "resolved-optional-value" {
                    matches!(
                        source.kind.as_str(),
                        "assignment" | "keyword" | "dict-field"
                    ) && target.kind == "parameter"
                        && target.expression == "None"
                        && source.scope == target.scope
                        && source
                            .expression
                            .contains(&format!("{} if {} is None else ", f[11], target.symbol))
                        && f[13].contains(&format!("{} is None", target.symbol))
                } else {
                    source.kind == "parameter"
                        && source.expression == "None"
                        && target.kind == "assignment"
                        && f[11] == target.symbol
                        && f[13].contains(&format!("{} is None", source.symbol))
                        && (target.scope == "<module>"
                            || target.scope.strip_suffix(".__init__").is_some_and(|class| {
                                source
                                    .scope
                                    .rsplit_once('.')
                                    .is_some_and(|(owner, _)| owner == class)
                            }))
                }
        });
    }
    if rule == "argparse-option" {
        return argparse_option_lines(
            crate::inventory::keyed_lines(
                include_str!("../../data/python-argparse-bindings.tsv"),
                &source.key,
            ),
            source,
            target,
        );
    }
    if rule == "typed-config-field" {
        return source.key != target.key
            && target.kind == "assignment"
            && crate::inventory::keyed_lines(
                include_str!("../../data/python-config-bindings.tsv"),
                &source.key,
            )
            .any(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f.len() == 9
                    && f[0] == source.key
                    && f[1] == source.path
                    && f[2] == source.scope
                    && f[4] == target.key
                    && f[5] == target.path
                    && f[6] == target.scope
                    && f[7] == target.symbol
                    && !f[8].is_empty()
                    && source.expression == format!("{}.{}", f[3], f[7])
            });
    }
    if rule == "imported-constant" || rule == "imported-float-constant" {
        if source.key == target.key || target.scope != "<module>" || target.kind != "assignment" {
            return false;
        }
        let reference = if rule == "imported-float-constant" {
            let Some(inner) = source
                .expression
                .strip_prefix("float(")
                .and_then(|s| s.strip_suffix(')'))
            else {
                return false;
            };
            if !target
                .expression
                .parse::<f64>()
                .is_ok_and(|n| n.is_finite() && n >= 0.0 && n <= (1_u64 << 53) as f64)
            {
                return false;
            }
            inner
        } else {
            source.expression.as_str()
        };
        return crate::inventory::keyed_lines(
            include_str!("../../data/python-import-bindings.tsv"),
            &source.path,
        )
        .any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 5
                && f[0] == source.path
                && f[1] == reference
                && f[2] == target.key
                && f[3] == target.symbol
                && f[2].split(':').next() == Some(target.path.as_str())
                && f[4].parse::<usize>().is_ok_and(|n| n > 0)
        });
    }
    if source.key == target.key || source.path != target.path {
        return false;
    }
    if rule == "class-field" {
        return target.kind == "assignment"
            && target.scope != "<module>"
            && source
                .scope
                .rsplit_once('.')
                .is_some_and(|(class, _)| class == target.scope)
            && source.expression == format!("self.{}", target.symbol)
            && !target.symbol.contains('.');
    }
    if rule == "parameter-bound-float-normalization" {
        return target.kind == "parameter"
            && target.scope == source.scope
            && source.expression == format!("float({})", target.symbol)
            && !target.expression.is_empty()
            && target
                .expression
                .bytes()
                .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_');
    }
    if rule == "remaining-monotonic-local" {
        return target.kind == "assignment"
            && target.scope == source.scope
            && target.line < source.line
            && target.expression.starts_with("time.monotonic() + ")
            && [
                format!("max(0.0, {} - time.monotonic())", target.symbol),
                format!("max(0, {} - time.monotonic())", target.symbol),
            ]
            .contains(&source.expression);
    }
    if let Some(binding) = rule.strip_prefix("comparison-reference-") {
        if source.kind != "comparison"
            || !matches!(
                binding,
                "same-scope-parameter" | "module-constant" | "constructor-field"
            )
        {
            return false;
        }
        let matched = [" <= ", " < ", " >= ", " > "].iter().any(|operator| {
            source
                .expression
                .rsplit_once(operator)
                .is_some_and(|(left, right)| {
                    !left.is_empty()
                        && right == target.symbol
                        && !left.contains(" == ")
                        && !left.contains(" != ")
                })
        });
        let mut reference = source.clone();
        reference.expression = target.symbol.clone();
        return matched && accepts(binding, &reference, target);
    }
    if rule == "zero-validation-same-scope-parameter" {
        if source.kind != "comparison" {
            return false;
        }
        let matched = [" < 0", " <= 0", " > 0", " < 0.0", " <= 0.0", " > 0.0"]
            .iter()
            .any(|suffix| source.expression.strip_suffix(suffix) == Some(target.symbol.as_str()));
        let mut reference = source.clone();
        reference.expression = target.symbol.clone();
        return matched && accepts("same-scope-parameter", &reference, target);
    }
    if let Some(binding) = rule.strip_prefix("monotonic-offset-") {
        if !matches!(
            binding,
            "same-scope-parameter" | "same-scope-binding" | "module-constant" | "constructor-field"
        ) {
            return false;
        }
        let Some(offset) = source.expression.strip_prefix("time.monotonic() + ") else {
            return false;
        };
        let mut relative = source.clone();
        relative.expression = offset.into();
        return accepts(binding, &relative, target);
    }
    if matches!(
        rule,
        "parameter-float-normalization" | "parameter-integral-normalization"
    ) {
        if target.kind != "parameter" || source.scope != target.scope {
            return false;
        }
        let expression = target.expression.as_str();
        if expression.is_empty() || !expression.bytes().all(|b| b.is_ascii_digit() || b == b'.') {
            return false;
        }
        return if rule == "parameter-float-normalization" {
            let finite = expression
                .parse::<f64>()
                .is_ok_and(|value| value.is_finite());
            let exact_integer = expression.contains('.')
                || expression
                    .parse::<u64>()
                    .is_ok_and(|value| value <= (1_u64 << 53));
            finite && exact_integer && source.expression == format!("float({})", target.symbol)
        } else {
            let (whole, fraction) = expression.split_once('.').unwrap_or((expression, ""));
            whole.parse::<u64>().is_ok()
                && fraction.bytes().all(|b| b == b'0')
                && source.expression == format!("int({})", target.symbol)
        };
    }
    if source.expression != target.symbol {
        return false;
    }
    match rule {
        "same-scope-parameter" => target.kind == "parameter" && source.scope == target.scope,
        "same-scope-binding" => {
            target.kind == "assignment" && source.scope == target.scope && target.line < source.line
        }
        "module-constant" => target.kind == "assignment" && target.scope == "<module>",
        "constructor-field" => {
            target.kind == "assignment"
                && target.symbol.starts_with("self.")
                && target.scope.strip_suffix(".__init__").is_some_and(|class| {
                    source
                        .scope
                        .rsplit_once('.')
                        .is_some_and(|(scope, _)| scope == class)
                })
        }
        _ => false,
    }
}

pub fn validate(row: &Bound, rows: &[Bound], candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with_lookup(
        row,
        &|key| rows.iter().find(|bound| bound.key == key),
        candidates,
    )
}

/// Same validation with the reviewed-row lookup supplied by the caller (an index).
pub fn validate_with_lookup<'a>(
    row: &Bound,
    find: &dyn Fn(&str) -> Option<&'a Bound>,
    candidates: &BTreeMap<String, Candidate>,
) -> bool {
    let Some(claim) = row.review.strip_prefix("binding-category:") else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    let mut matching = crate::inventory::keyed_lines(
        include_str!("../../data/python-binding-edges.tsv"),
        &row.key,
    )
    .map(|line| line.split('\t').collect::<Vec<_>>())
    .filter(|fields| fields.first().is_some_and(|key| *key == row.key));
    let Some(edge) = matching.next() else {
        return false;
    };
    if edge.len() != 4 || matching.next().is_some() || edge[2] != rule || edge[3] != row.expression
    {
        return false;
    }
    let Some(source) = candidates.get(&row.key) else {
        return false;
    };
    let Some(target) = candidates.get(edge[1]) else {
        return false;
    };
    let Some(bound) = find(&target.key) else {
        return false;
    };
    let budget = if bound.budget.is_empty() && bound.disposition == "derived" {
        &bound.relation
    } else {
        &bound.budget
    };
    accepts(rule, source, target)
        && !budget.is_empty()
        && (!(rule.starts_with("monotonic-offset-")
            || rule == "monotonic-upper-cap-reference"
            || rule == "schedule-maximum"
            || rule == "reviewed-clock-offset"
            || rule == "remaining-monotonic-local")
            || row.unit == "milliseconds")
        && (rule != "parameter-bound-float-normalization"
            || (bound.disposition == "derived"
                && matches!(bound.unit.as_str(), "milliseconds" | "bytes" | "entries")
                && budgets::BudgetId::from_name(budget).is_some_and(|id| match id.entry().value {
                    budgets::table::Value::Duration(n) => n.millis() <= (1_u64 << 53),
                    budgets::table::Value::Storage(n) => n.get() <= (1_u64 << 53),
                    budgets::table::Value::Count(n) => n.get() <= (1_u64 << 53),
                    _ => false,
                })))
        && (!(rule == "typed-config-field" && source.expression.starts_with("float("))
            || (bound.unit == "milliseconds"
                && budgets::BudgetId::from_name(budget).is_some_and(|id| {
                    matches!(id.entry().value, budgets::table::Value::Duration(n) if n.millis() <= (1_u64 << 53))
                })))
        && *budget == row.relation
        && row.unit == bound.unit
        && row.disposition == "derived"
        && row.value.is_none()
        && source.expression == row.expression
        && source.path == row.path
        && source.line == row.line
        && source.column == row.column
        && source.scope == row.scope
        && source.symbol == row.symbol
}

/// The `argparse-option` edge against a pinned table text. The receiver is `args`/`arguments`; a one-letter
/// namespace `a` is admitted only for a row whose pinned reason starts with `namespace-alias:`.
pub fn argparse_option_with(table: &str, source: &Candidate, target: &Candidate) -> bool {
    argparse_option_lines(
        table.lines().skip(1).filter(|l| l.starts_with(&source.key)),
        source,
        target,
    )
}

fn argparse_option_lines<'a>(
    mut lines: impl Iterator<Item = &'a str>,
    source: &Candidate,
    target: &Candidate,
) -> bool {
    source.key != target.key
        && source.path == target.path
        && target.kind == "cli-default"
        && lines.any(|line| {
            let f: Vec<_> = line.split('\t').collect();
            f.len() == 9
                && f[0] == source.key
                && f[1] == source.path
                && f[2] == source.scope
                && f[3] == source.expression
                && f[4] == target.key
                && f[5] == target.scope
                && f[6] == target.symbol
                && !f[8].is_empty()
                && f[7]
                    .parse::<usize>()
                    .is_ok_and(|n| n > 0 && n <= target.line)
                && source
                    .expression
                    .rsplit_once('.')
                    .is_some_and(|(receiver, field)| {
                        (matches!(receiver, "args" | "arguments")
                            || (receiver == "a" && f[8].starts_with("namespace-alias:")))
                            && target
                                .symbol
                                .strip_prefix("--")
                                .is_some_and(|flag| flag.replace('-', "_") == field)
                    })
        })
}
