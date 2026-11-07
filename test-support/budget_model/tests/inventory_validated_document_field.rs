// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_swarm_artifacts.py; O:path:mycelium_qualification/qualifier.py.
//! L1b round 12, family `r2-validated-document-field` (34 rows): an allowance read from a validated document
//! field is `source-unbounded` (RU-014, owner ruling 2026-10-28 option a) only when v0.1.4 ships no maximum for
//! that field, and that is a NEGATIVE proof. Round 11 found that the round-6 census scan (a comparison naming
//! the quoted field) misses aliases (`reserve = policy['reserve_bytes']` ... `reserve <= _MAX_BYTES`) and loop
//! variables, so this family pins the proof: every occurrence of the key literal in the scoped oracle tree and
//! every ordering comparison of the local the value is read into carries a pinned verdict
//! (`python-field-occurrences.tsv`), and no verdict is a ceiling. Rows whose field IS bounded stay pending
//! (neighbour test below, each with the oracle line that bounds it).
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{Bound, candidate_keys, checked_in, unbounded, validate};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::{BTreeMap, BTreeSet};

const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
const OCC: &str = include_str!("../data/python-field-occurrences.tsv");
const SCOPE: &str = include_str!("../data/python-scope.tsv");

/// (key, family, field, registry id)
const ROWS: &[(&str, &str, &str, &str)] = &[
    (
        "mycelium_a13_windows/service_lifecycle.py:232:8:dict-field:restart_delay_seconds",
        "o4",
        "restart_delay_seconds",
        "PySourceUnboundedMyceliumA13WindowsServiceLifecycleL232C8",
    ),
    (
        "mycelium_a13_windows/service_lifecycle.py:233:8:dict-field:stop_timeout_seconds",
        "o4",
        "stop_timeout_seconds",
        "PySourceUnboundedMyceliumA13WindowsServiceLifecycleL233C8",
    ),
    (
        "mycelium_m19_recovery.py:336:12:keyword:maximum_recovery_attempts",
        "o4",
        "maximum_recovery_attempts",
        "PySourceUnboundedMyceliumM19RecoveryL336C12",
    ),
    (
        "mycelium_qualification/authority.py:231:12:assignment:max_age_ms",
        "o4",
        "max_load_proof_age_ms",
        "PySourceUnboundedMyceliumQualificationAuthorityL231C12",
    ),
    (
        "mycelium_qualification/qualifier.py:273:4:assignment:max_age",
        "o4",
        "max_load_proof_age_ms",
        "PySourceUnboundedMyceliumQualificationQualifierL273C4",
    ),
    (
        "mycelium_qualification/qualifier.py:660:8:assignment:max_age",
        "o4",
        "max_load_proof_age_ms",
        "PySourceUnboundedMyceliumQualificationQualifierL660C8",
    ),
    (
        "mycelium_qualification/qualifier.py:2065:4:assignment:max_age",
        "o4",
        "max_load_proof_age_ms",
        "PySourceUnboundedMyceliumQualificationQualifierL2065C4",
    ),
    (
        "mycelium_swarm_artifacts.py:1069:4:assignment:reserve",
        "o4",
        "disk_reserve_bytes",
        "PySourceUnboundedMyceliumSwarmArtifactsL1069C4",
    ),
    (
        "mycelium_swarm_artifacts.py:830:4:assignment:maximum_bytes",
        "o4",
        "maximum_total_bytes",
        "PySourceUnboundedMyceliumSwarmArtifactsL830C4",
    ),
    (
        "mycelium_live/local_preparer.py:1571:20:dict-field:maximum_total_bytes",
        "o4",
        "total_size_bytes",
        "PySourceUnboundedMyceliumLiveLocalPreparerL1571C20",
    ),
    (
        "mycelium_live/member_transport.py:1041:20:dict-field:maximum_total_bytes",
        "o4",
        "total_size_bytes",
        "PySourceUnboundedMyceliumLiveMemberTransportL1041C20",
    ),
];

/// Rows of the 34 that look like "no maximum" by name but ARE bounded in v0.1.4 (or are not a document
/// allowance at all): they stay pending. (key, path, line, exact text that bounds it)
const BOUNDED_NEIGHBOURS: &[(&str, &str, usize, &str)] = &[
    (
        "mycelium_gossip/signed_bundle.py:253:8:assignment:ttl_ms",
        "mycelium_gossip/schema.py",
        453,
        "if ttl > 7 * 24 * 60 * 60 * 1000:",
    ),
    (
        "mycelium_internet/physical.py:2599:8:assignment:await_seconds",
        "mycelium_internet/physical.py",
        2605,
        "or not 0.0 < float(await_seconds) <= 3600.0",
    ),
    (
        "mycelium_a13_macos/app.py:832:20:keyword:ttl_seconds",
        "mycelium_a13_macos/pairing.py",
        666,
        "or not _WINDOW_TTL_MIN <= ttl_seconds <= _WINDOW_TTL_MAX):",
    ),
    (
        "mycelium_swarm_directory/entry.py:506:66:keyword:max_requests",
        "mycelium_swarm_directory/docs.py",
        239,
        "integer(quota['max_requests'], low=1, high=1000, code='open_quota_invalid')",
    ),
    (
        "mycelium_platform_neutral/windows_provider.py:497:16:dict-field:workspace_bounded_bytes",
        "mycelium_platform_neutral/acceleration_profile.py",
        210,
        "workspace_bounded_bytes=bounded_int(",
    ),
    (
        "mycelium_a13_macos/adaptive_host.py:767:20:keyword:reserve_bytes",
        "mycelium_a13_macos/adaptive_host.py",
        118,
        "if type(reserve) is not int or not 0 <= reserve <= _MAX_BYTES:",
    ),
    (
        "mycelium_live/artifact_provisioner.py:1042:20:keyword:maximum_retries",
        "mycelium_swarm_artifacts.py",
        1058,
        "value.get(\"maximum_retries_per_chunk\"), \"artifact_policy_invalid\", maximum=64",
    ),
    (
        "mycelium_a13_macos/model_resources.py:567:12:dict-field:max_rss_bytes",
        "mycelium_a13_macos/model_resources.py",
        521,
        "if ceiling is not None and row[\"max_rss_bytes\"] > ceiling:",
    ),
    (
        "scripts/a13_operator/round14_bootstrap_compile.py:229:48:dict-field:storage_limit_bytes",
        "mycelium_a13_macos/app.py",
        251,
        "100_000_000 <= c['storage_bytes'] <= storage_consent_ceiling()",
    ),
    (
        "mycelium_router/a10_qualification.py:412:9:keyword:maximum_bytes",
        "mycelium_router/a10_qualification.py",
        253,
        "if record[\"candidate_bytes\"] > SCAFFOLD_MAXIMUM_VERIFICATION_BYTES:",
    ),
];

/// Independent floor-only witnesses: (path, first line, last line, exact substring that must occur in the span).
const FLOOR_WITNESSES: &[(&str, usize, usize, &str)] = &[
    (
        "mycelium_a13_windows/service_lifecycle.py",
        177,
        184,
        "if isinstance(item, bool) or not isinstance(item, int) or item < minimum:",
    ),
    (
        "mycelium_m19_recovery.py",
        495,
        495,
        "\"maximum_recovery_attempts\", minimum=1)",
    ),
    (
        "mycelium_qualification/authority.py",
        248,
        248,
        "if type(max_age_ms) is not int or max_age_ms < 1:",
    ),
    (
        "mycelium_swarm_artifacts.py",
        1069,
        1069,
        "reserve = _integer(value.get(\"disk_reserve_bytes\"), \"artifact_policy_invalid\")",
    ),
    (
        "mycelium_swarm_artifacts.py",
        830,
        832,
        "value.get(\"maximum_total_bytes\"), \"artifact_grant_invalid\", minimum=1",
    ),
    (
        "mycelium_swarm_artifacts.py",
        511,
        511,
        "value.get(\"total_size_bytes\"), \"stage_pack_manifest_invalid\", minimum=1",
    ),
];

const VERDICTS: &[&str] = &[
    "read",
    "declare",
    "write",
    "floor",
    "type-guard",
    "relative",
    "other-document",
];

/// The four rows whose only validator ceiling is the int64 default of the swarm_artifacts `_integer` helper.
const TYPE_GUARD_IDS: &[&str] = &[
    "PySourceUnboundedMyceliumSwarmArtifactsL1069C4",
    "PySourceUnboundedMyceliumSwarmArtifactsL830C4",
    "PySourceUnboundedMyceliumLiveLocalPreparerL1571C20",
    "PySourceUnboundedMyceliumLiveMemberTransportL1041C20",
];

struct Occ {
    field: String,
    path: String,
    line: usize,
    verdict: String,
    witness: String,
    alias: String,
}

fn occurrences() -> Vec<Occ> {
    OCC.lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            assert_eq!(f.len(), 6, "{l}");
            Occ {
                field: f[0].into(),
                path: f[1].into(),
                line: f[2].parse().unwrap(),
                verdict: f[3].into(),
                witness: f[4].into(),
                alias: f[5].into(),
            }
        })
        .collect()
}

fn oracle_text(path: &str) -> String {
    let show = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle_blobs::oracle_dir_or_panic())
        .args(["show", &format!("{ORACLE}:{path}")])
        .output()
        .unwrap();
    assert!(show.status.success(), "{path}");
    String::from_utf8(show.stdout).unwrap()
}

fn scoped_paths() -> BTreeSet<String> {
    SCOPE
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap().to_string())
        .collect()
}

fn rows() -> Vec<Bound> {
    checked_in().unwrap()
}

fn fields() -> BTreeSet<String> {
    occurrences().into_iter().map(|o| o.field).collect()
}

#[test]
fn the_11_validated_document_rows_are_source_unbounded_o4() {
    let all = rows();
    assert_eq!(ROWS.len(), 11);
    for (key, family, field, id) in ROWS {
        assert_eq!(*family, "o4");
        let row = all
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key} must be reviewed"));
        assert_eq!(row.disposition, "source-unbounded", "{key}");
        assert!(row.value.is_none() && row.budget.is_empty(), "{key}");
        assert_eq!(row.unit, "not-applicable", "{key}");
        assert_eq!(row.relation, *id, "{key}");
        assert!(row.review.starts_with("source-unbounded:v03;"), "{key}");
        assert!(
            row.review.contains("python-field-occurrences.tsv"),
            "{key}: the review names its pinned negative proof"
        );
        let entry = BudgetId::from_name(id).unwrap().entry();
        assert_eq!(entry.value, Value::Blocked, "{id}");
        let Provenance::Blocked(text) = entry.provenance else {
            panic!("{id} provenance must be Blocked")
        };
        assert!(
            text.starts_with(&format!(
                "v0.1.4 ships no ceiling at {}:{} @ ef9ae61d; field {field};",
                row.path, row.line
            )),
            "{id}: {text}"
        );
        assert!(text.contains("V03"), "{id}");
    }
    let ids: BTreeSet<_> = ROWS.iter().map(|r| r.3).collect();
    assert_eq!(ids.len(), 11, "one id per site, no sharing");
    validate(&all).unwrap();
}

#[test]
fn every_pinned_verdict_is_a_known_non_ceiling_verdict() {
    let occ = occurrences();
    assert!(!occ.is_empty());
    for o in &occ {
        assert!(
            VERDICTS.contains(&o.verdict.as_str()),
            "{}:{} {}: verdict {} is not a pinned non-ceiling verdict",
            o.path,
            o.line,
            o.field,
            o.verdict
        );
    }
    // The row fields (and the destination key `maximum_total_bytes` of the two manifest copies) are all pinned.
    let pinned = fields();
    for (_, _, field, _) in ROWS {
        assert!(pinned.contains(*field), "{field}");
    }
    assert!(pinned.contains("maximum_total_bytes"));
    // Each row's own read is pinned as `read` or `floor` at (or just after) its line.
    let all = rows();
    for (key, _, field, _) in ROWS {
        let row = all.iter().find(|r| r.key == *key).unwrap();
        assert!(
            occ.iter().any(|o| o.field == *field
                && o.path == row.path
                && (row.line..=row.line + 2).contains(&o.line)
                && matches!(o.verdict.as_str(), "read" | "floor" | "type-guard")),
            "{key}: own read not pinned"
        );
    }
}

#[test]
fn pinned_witnesses_equal_the_oracle_lines() {
    oracle_blobs::oracle_dir_or_panic();
    let mut cache: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for o in occurrences() {
        let lines = cache
            .entry(o.path.clone())
            .or_insert_with(|| oracle_text(&o.path).lines().map(String::from).collect());
        assert_eq!(
            lines[o.line - 1].trim(),
            o.witness,
            "{}:{} ({})",
            o.path,
            o.line,
            o.field
        );
    }
}

/// Completeness of the literal-key occurrences: the oracle contains EXACTLY the pinned lines naming the key
/// literal (either quote style) in the scoped files.
#[test]
fn literal_key_occurrences_are_exactly_the_oracle_grep() {
    oracle_blobs::oracle_dir_or_panic();
    let scope = scoped_paths();
    let occ = occurrences();
    for field in fields() {
        let (single, double) = (format!("'{field}'"), format!("\"{field}\""));
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(oracle_blobs::oracle_dir_or_panic())
            .args(["grep", "-n", "-F", "-e", &single, "-e", &double, ORACLE])
            .args(["--", "*.py"])
            .output()
            .unwrap();
        let mut grep: BTreeSet<(String, usize)> = BTreeSet::new();
        for l in String::from_utf8(out.stdout).unwrap().lines() {
            let rest = l.strip_prefix(&format!("{ORACLE}:")).unwrap();
            let (path, rest) = rest.split_once(':').unwrap();
            let (line, _) = rest.split_once(':').unwrap();
            if scope.contains(path) {
                grep.insert((path.to_string(), line.parse().unwrap()));
            }
        }
        let pinned: BTreeSet<(String, usize)> = occ
            .iter()
            .filter(|o| {
                o.field == field && (o.witness.contains(&single) || o.witness.contains(&double))
            })
            .map(|o| (o.path.clone(), o.line))
            .collect();
        assert!(!grep.is_empty(), "{field}");
        assert_eq!(grep, pinned, "{field}: occurrences changed or unpinned");
    }
}

/// Alias completeness: for every pinned read `NAME = ...` the function span is pinned (`NAME@a-b`); the lines of
/// that span (other than the read) that name NAME and contain an ordering operator are exactly the pinned alias
/// lines of the field in the file. Textual, conservative: a generic or string `<` only adds a pinned line.
#[test]
fn alias_ordering_comparisons_are_exactly_the_textual_scan() {
    oracle_blobs::oracle_dir_or_panic();
    let occ = occurrences();
    let ordering = |line: &str| {
        let t = line
            .replace("->", "")
            .replace("<<", "")
            .replace(">>", "")
            .replace("=>", "");
        t.contains('<') || t.contains('>')
    };
    let word = |line: &str, name: &str| {
        line.match_indices(name).any(|(i, _)| {
            let b = line.as_bytes();
            let before = i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_');
            let j = i + name.len();
            let after = j >= b.len() || !(b[j].is_ascii_alphanumeric() || b[j] == b'_');
            before && after
        })
    };
    let mut aliases = 0;
    for o in occ.iter().filter(|o| o.alias != "-") {
        aliases += 1;
        let (name, span) = o.alias.split_once('@').unwrap();
        let (a, b) = span.split_once('-').unwrap();
        let (a, b): (usize, usize) = (a.parse().unwrap(), b.parse().unwrap());
        assert!(a <= o.line && o.line <= b, "{}", o.alias);
        let text = oracle_text(&o.path);
        let lines: Vec<&str> = text.lines().collect();
        let found: BTreeSet<usize> = (a..=b)
            .filter(|&n| n != o.line && word(lines[n - 1], name) && ordering(lines[n - 1]))
            .collect();
        let pinned: BTreeSet<usize> = occ
            .iter()
            .filter(|p| {
                p.field == o.field
                    && p.path == o.path
                    && p.alias == "-"
                    && (a..=b).contains(&p.line)
                    && p.line != o.line
                    && word(&p.witness, name)
                    && ordering(&p.witness)
            })
            .map(|p| p.line)
            .collect();
        assert_eq!(
            found, pinned,
            "{}:{} alias {name}: unpinned or stale ordering comparison",
            o.path, o.line
        );
    }
    assert!(aliases >= 8, "the alias reads are pinned: {aliases}");
}

#[test]
fn floors_are_the_only_validation_the_oracle_ships_for_the_rows() {
    oracle_blobs::oracle_dir_or_panic();
    for (path, a, b, needle) in FLOOR_WITNESSES {
        let text = oracle_text(path);
        let span: Vec<&str> = text.lines().skip(a - 1).take(b - a + 1).collect();
        assert!(
            span.iter().any(|l| l.trim().contains(needle)),
            "{path}:{a}-{b}: {needle}"
        );
    }
}

#[test]
fn bounded_neighbours_stay_pending_with_their_bounding_line_named() {
    let reviewed: BTreeSet<_> = rows().into_iter().map(|r| r.key).collect();
    let pending = candidate_keys().unwrap();
    for (key, path, line, text) in BOUNDED_NEIGHBOURS {
        assert!(pending.contains(*key), "{key} must be a candidate");
        assert!(
            !reviewed.contains(*key),
            "{key} is bounded in v0.1.4 ({path}:{line}); it must stay pending"
        );
        let body = oracle_text(path);
        let got = body.lines().nth(line - 1).unwrap().trim();
        assert!(got.contains(text), "{path}:{line}: {got}");
    }
}

/// Rewrite the occurrence table: `edit` maps a split line to its replacement (`None` drops the line).
fn edited(edit: impl Fn(&[&str]) -> Option<String>) -> String {
    OCC.lines()
        .filter_map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            if f.len() == 6 {
                edit(&f)
            } else {
                Some(l.to_string())
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n"
}

fn with_verdict(field: &str, from: &str, to: &str) -> String {
    edited(|f| {
        Some(if f[0] == field && f[3] == from {
            [f[0], f[1], f[2], to, f[4], f[5]].join("\t")
        } else {
            f.join("\t")
        })
    })
}

#[test]
fn mutated_proofs_are_refused() {
    use budget_model::inventory::candidates;
    let by_key: BTreeMap<String, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let base = rows();
    let table = unbounded_table();
    for (key, _, field, _) in ROWS {
        let row = base.iter().find(|r| r.key == *key).unwrap();
        assert!(
            unbounded::validate_with(row, &by_key, table, OCC),
            "{key}: the checked-in tables accept the row"
        );
        // A ceiling verdict anywhere on the field refuses (every field has a floor or relative line).
        for from in ["floor", "relative", "read", "type-guard"] {
            let m = with_verdict(field, from, "ceiling");
            if m != OCC {
                assert!(
                    !unbounded::validate_with(row, &by_key, table, &m),
                    "{key}: a ceiling verdict (was {from}) must refuse"
                );
            }
        }
        // An unreviewed verdict refuses.
        let m = with_verdict(field, "declare", "unreviewed");
        if m != OCC {
            assert!(
                !unbounded::validate_with(row, &by_key, table, &m),
                "{key}: unknown verdict"
            );
        }
        // A field with no pinned occurrence refuses.
        let m = edited(|f| (f[0] != *field).then(|| f.join("\t")));
        assert!(
            !unbounded::validate_with(row, &by_key, table, &m),
            "{key}: unpinned field"
        );
        // The row's own site must be pinned (read/floor at its line or the next two).
        let m = edited(|f| {
            let own = f[0] == *field
                && f[1] == row.path
                && matches!(f[3], "read" | "floor" | "type-guard")
                && f[2]
                    .parse::<usize>()
                    .is_ok_and(|n| (row.line..=row.line + 2).contains(&n));
            (!own).then(|| f.join("\t"))
        });
        assert!(
            !unbounded::validate_with(row, &by_key, table, &m),
            "{key}: own read unpinned"
        );
        // Demoting the row's own read to a non-read verdict leaves it unpinned: refused.
        for to in ["declare", "relative", "write", "other-document"] {
            let m = edited(|f| {
                let own = f[0] == *field
                    && f[1] == row.path
                    && matches!(f[3], "read" | "floor" | "type-guard")
                    && f[2]
                        .parse::<usize>()
                        .is_ok_and(|n| (row.line..=row.line + 2).contains(&n));
                Some(if own {
                    [f[0], f[1], f[2], to, f[4], f[5]].join("\t")
                } else {
                    f.join("\t")
                })
            });
            assert!(
                !unbounded::validate_with(row, &by_key, table, &m),
                "{key}: own read demoted to {to}"
            );
        }
        // A row with a seventh column is malformed too.
        let m = format!("{OCC}a\tb\tc\td\te\tf\tg\n");
        assert!(
            !unbounded::validate_with(row, &by_key, table, &m),
            "{key}: wide row"
        );
        // A corrupted header or a malformed row refuses (the table is closed).
        let m = OCC.replacen("field\tpath", "field\tpathx", 1);
        assert!(
            !unbounded::validate_with(row, &by_key, table, &m),
            "{key}: header"
        );
        let m = format!("{OCC}stray\trow\n");
        assert!(
            !unbounded::validate_with(row, &by_key, table, &m),
            "{key}: malformed row"
        );
        // The family tag must name a family that carries a negative proof.
        let m = table.replace(&format!("\t{field}\t"), &format!("\t{field}x\t"));
        assert!(
            !unbounded::validate_with(row, &by_key, &m, OCC),
            "{key}: edited field"
        );
    }
}

fn unbounded_table() -> &'static str {
    include_str!("../data/python-source-unbounded.tsv")
}

/// JUDGEMENT (for the parent): the swarm_artifacts `_integer` helper ships `maximum: int = 2**63 - 1`. That is an
/// int64 type guard, not a policy ceiling, so the four rows read through it are still `source-unbounded`; each
/// Blocked provenance says so. Reverting the judgement moves these four rows back to pending.
#[test]
fn type_guard_rows_name_the_int64_helper_default() {
    for id in TYPE_GUARD_IDS {
        let Provenance::Blocked(text) = BudgetId::from_name(id).unwrap().entry().provenance else {
            panic!("{id}")
        };
        assert!(
            text.contains("only the int64 type guard of _integer (maximum=2**63-1) bounds it"),
            "{id}: {text}"
        );
        assert!(ROWS.iter().any(|r| r.3 == *id), "{id}");
    }
    // No other row's provenance claims a type guard.
    for (_, _, _, id) in ROWS.iter().filter(|r| !TYPE_GUARD_IDS.contains(&r.3)) {
        let Provenance::Blocked(text) = BudgetId::from_name(id).unwrap().entry().provenance else {
            panic!("{id}")
        };
        assert!(!text.contains("type guard"), "{id}");
    }
    let text = oracle_text("mycelium_swarm_artifacts.py");
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(
        lines[311].trim(),
        "value: object, code: str, *, minimum: int = 0, maximum: int = 2**63 - 1"
    );
    assert!(lines[313].contains("not minimum <= value <= maximum"));
}

#[test]
fn total_pending_after_the_validated_document_rows() {
    let reviewed: BTreeSet<_> = rows().into_iter().map(|r| r.key).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 707 - 11 validated-document-field rows - 4 round-12 RU-033 propagated rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
