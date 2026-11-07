// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L10-REPLAY-BOUND: v0.1.4 bounds the replay-id window of both membership endpoints at 4096 ids
//! (`_MAX_REPLAY_IDS = 4096`; reaching it refuses with `*_replay_window_full`). The constant has no
//! quantity hint in its name, so the L0 census does not record it: the ids are non-`Py` registry entries
//! with exact oracle provenance `path:line @ f4e366e9 sha256:<file>; <symbol>`, listed in
//! `legacy-budget-ids.txt`. The oracle defines the constant twice (the member side and the seed
//! coordinator); both are registered because L10 needs the member one and L5/L6 the seed one
//! (SE-L10-SEED-COORD). Expected values are the oracle source text, not the table.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
use std::collections::BTreeSet;

struct Row {
    id: &'static str,
    file: &'static str,
    sha: &'static str,
    line: u32,
    /// Verbatim oracle line (trimmed) at `file:line`.
    text: &'static str,
    /// Where the bound is enforced (verbatim oracle line, trimmed) and its line.
    enforced_line: u32,
    enforced: &'static str,
    symbol: &'static str,
}

const ROWS: [Row; 2] = [
    Row {
        id: "NodeMembershipReplayIds",
        file: "mycelium_node/membership.py",
        sha: "2bc7531d9f83227358bf255e7f204e3012e679b07dc5c3eb17f3a8eedac824b2",
        line: 46,
        text: "_MAX_REPLAY_IDS = 4096",
        enforced_line: 320,
        enforced: ">= _MAX_REPLAY_IDS",
        symbol: "_MAX_REPLAY_IDS (membership_replay_window_full at :318-321)",
    },
    Row {
        id: "SeedReplayIds",
        file: "mycelium_seed/coordinator.py",
        sha: "2612a51acce2d43626ec69cd7dc19db080e595915c82030d3ec8d5f7a4456f15",
        line: 72,
        text: "_MAX_REPLAY_IDS = 4096",
        enforced_line: 1196,
        enforced: "if len(member.seen_ids) >= _MAX_REPLAY_IDS:",
        symbol: "_MAX_REPLAY_IDS (seed_replay_window_full at :1196; capacity at :1187,:1438)",
    },
];

const VALUE: u64 = 4096;
const ORACLE_SHA: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

#[test]
fn replay_id_bounds_pin_value_class_purpose_and_exact_oracle_source() {
    for row in &ROWS {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        assert_eq!(
            entry.value,
            Value::Count(budgets::Entries::new(VALUE)),
            "{}",
            row.id
        );
        assert!(matches!(entry.purpose, Purpose::Resource), "{}", row.id);
        let Provenance::Source(source) = entry.provenance else {
            panic!("{}: source constant masquerading as measurement", row.id)
        };
        assert_eq!(
            source,
            format!(
                "{}:{} @ f4e366e9 sha256:{}; {}",
                row.file, row.line, row.sha, row.symbol
            ),
            "{}",
            row.id
        );
        // The pinned texts are the declaration and the enforcement the value was read from.
        assert!(row.text.ends_with(&VALUE.to_string()), "{}", row.id);
        assert!(row.enforced.contains("_MAX_REPLAY_IDS"), "{}", row.id);
    }
}

#[test]
fn replay_id_bounds_are_non_py_listed_once_and_distinct() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 2);
    assert!(names.iter().all(|name| !name.starts_with("Py")));
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
    // Two oracle files, two sites; the registry keeps them separate so each consumer cites its own line.
    assert_ne!(ROWS[0].file, ROWS[1].file);
}

#[test]
fn the_pinned_lines_and_file_hashes_match_the_oracle_checkout() {
    // Host check against the read-only oracle (fails when the checkout is absent).
    let oracle = oracle_blobs::oracle_dir_or_panic();
    for row in &ROWS {
        let show = std::process::Command::new("git")
            .arg("-C")
            .arg(&oracle)
            .args(["show", &format!("{ORACLE_SHA}:{}", row.file)])
            .output()
            .unwrap();
        assert!(show.status.success(), "{}", row.file);
        let text = String::from_utf8(show.stdout).unwrap();
        let at = |n: u32| text.split('\n').nth(n as usize - 1).unwrap().trim();
        assert_eq!(at(row.line), row.text, "{} declaration", row.id);
        assert!(
            at(row.enforced_line).contains(row.enforced),
            "{} enforcement line {}: {}",
            row.id,
            row.enforced_line,
            at(row.enforced_line)
        );
        use std::io::Write;
        let mut child = std::process::Command::new("shasum")
            .args(["-a", "256"])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(text.as_bytes())
            .unwrap();
        let out = String::from_utf8(child.wait_with_output().unwrap().stdout).unwrap();
        assert_eq!(
            out.split_whitespace().next().unwrap(),
            row.sha,
            "{}",
            row.id
        );
    }
}
