// SPDX-License-Identifier: AGPL-3.0-only
//! SE-S25 (L12 -> L1b), RU-014: three native-harness byte caps that v0.1.4 does not bound. They are
//! registered source-unbounded (`Value::Blocked`, provenance `v0.1.4 ships no ceiling ... V03: ...`),
//! exactly as L12 specified in `L12-evidence/S25-registry-ids.md` ("L1b half"), so the harness reads
//! them the way v0.1.4 reads (`read_text()`, no cap) and V03-tags the ceiling. No number is invented.
//! The provenance strings below are the L12 packet text, copied verbatim; the oracle cross-check at the
//! end pins the source lines they cite (the cited files are byte-identical at ef9ae61d and f4e366e9).
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
use std::collections::BTreeSet;

struct Row {
    id: &'static str,
    provenance: &'static str,
}

const ROWS: [Row; 3] = [
    Row {
        id: "HarnessNativeLaunchFileMaxBytes",
        provenance: "v0.1.4 ships no ceiling: open-demo.html is written (mycelium_a13_macos/app.py:870-880 @ ef9ae61d) and never read back; V03: ceiling to be designed in v0.3 (V03-L12-1; RU-014)",
    },
    Row {
        id: "HarnessNativeSessionRecordMaxBytes",
        provenance: "v0.1.4 ships no ceiling: harness-native-session.json is a harness file, not a v0.1.4 file; V03: ceiling to be designed in v0.3 (V03-L12-1; RU-014)",
    },
    Row {
        id: "HarnessLoopbackResponseMaxBytes",
        provenance: "v0.1.4 ships no ceiling on the native loopback reply read (mycelium_a13_macos/app.py reopen_native/status polling @ ef9ae61d); V03: ceiling to be designed in v0.3 (V03-L12-1; RU-014)",
    },
];

#[test]
fn s25_ids_are_source_unbounded_resource_rows_with_the_l12_provenance() {
    for row in &ROWS {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        assert_eq!(entry.value, Value::Blocked, "{}", row.id);
        assert!(matches!(entry.purpose, Purpose::Resource), "{}", row.id);
        assert_eq!(
            entry.provenance,
            Provenance::Blocked(row.provenance),
            "{}",
            row.id
        );
        // The shape L12's `Registry::ceiling` accepts as SourceUnbounded.
        assert!(row.provenance.starts_with("v0.1.4 ships no ceiling"));
        assert!(row.provenance.contains("V03"));
        assert!(row.provenance.contains("RU-014"));
    }
}

#[test]
fn s25_ids_are_non_py_listed_once_and_distinct() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 3);
    assert!(names.iter().all(|name| !name.starts_with("Py")));
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 3);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
}

#[test]
fn the_cited_oracle_lines_say_what_the_provenance_claims() {
    // Host check against the read-only oracle (fails when the checkout is absent).
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let show = |commit: &str| {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(&oracle)
            .args(["show", &format!("{commit}:mycelium_a13_macos/app.py")])
            .output()
            .unwrap();
        assert!(out.status.success(), "{commit}");
        String::from_utf8(out.stdout).unwrap()
    };
    let cited = show("ef9ae61d");
    assert_eq!(cited, show("f4e366e98a1f705dfaf182897044500d290dbd7f"));
    let lines: Vec<&str> = cited.split('\n').collect();
    let at = |n: usize| lines[n - 1].trim();
    // Launch file: written at :870-880, never read back.
    assert_eq!(at(870), "def make_launch_file(self):");
    assert_eq!(at(880), "private_write(self.launch_file, raw)");
    for (index, line) in lines.iter().enumerate() {
        if line.contains("launch_file") {
            for reader in ["read_text", "read_bytes", "open(", ".read("] {
                assert!(
                    !line.contains(reader),
                    "app.py:{} reads the launch file: {line}",
                    index + 1
                );
            }
        }
    }
    // The only capped native read is native-control.json (4096, :306), not these three.
    assert_eq!(
        at(306),
        "raw = _win32_private_file(root / 'native-control.json', max_bytes=4096)"
    );
    assert_eq!(at(302), "def reopen_native(root):");
}
