// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L5-TLS-BUDGETS: the three certificate validity durations of v0.1.4's loopback TLS material
//! (`mycelium_a13_macos/tls.py`, `_generate_material`): both certificates are back-dated 5 minutes
//! (`not_valid_before(now - timedelta(minutes=5))`, :169 CA and :206 server), the CA is valid for
//! 3650 days (:170) and the server certificate for 825 days (:207). The names carry no quantity
//! hint the census could use, so the ids are non-`Py` registry entries with exact oracle provenance
//! `path:line @ f4e366e9 sha256:<file>; <symbol>` (see `inventory_adaptive_bounds.rs`). Expected
//! values are the oracle source text and the arithmetic of its `timedelta` arguments, not the table.
//! `tls.py` is byte-identical at ef9ae61d and f4e366e9.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
use std::collections::BTreeSet;

const FILE: &str = "mycelium_a13_macos/tls.py";
const SHA: &str = "5208ab05c54b780fcdb726186f201e314a4295e3fa9fc4260a09e2ee2e5d6705";
const ORACLE_SHA: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

struct Row {
    id: &'static str,
    /// `timedelta(<unit>=<amount>)` as written in the oracle.
    unit: &'static str,
    amount: u64,
    millis: u64,
    line: u32,
    /// Verbatim oracle line (trimmed) at `FILE:line`.
    text: &'static str,
    /// Other oracle line with the same literal (verbatim, trimmed), if any.
    twin: Option<(u32, &'static str)>,
    symbol: &'static str,
}

const ROWS: [Row; 3] = [
    Row {
        id: "TlsCertificateBackdate",
        unit: "minutes",
        amount: 5,
        millis: 300_000,
        line: 169,
        text: ".not_valid_before(now - timedelta(minutes=5))",
        twin: Some((206, ".not_valid_before(now - timedelta(minutes=5))")),
        symbol: "CA not_valid_before back-date (server certificate :206 uses the same literal)",
    },
    Row {
        id: "TlsCaValidity",
        unit: "days",
        amount: 3650,
        millis: 315_360_000_000,
        line: 170,
        text: ".not_valid_after(now + timedelta(days=3650))",
        twin: None,
        symbol: "CA not_valid_after",
    },
    Row {
        id: "TlsServerCertificateValidity",
        unit: "days",
        amount: 825,
        millis: 71_280_000_000,
        line: 207,
        text: ".not_valid_after(now + timedelta(days=825))",
        twin: None,
        symbol: "server not_valid_after",
    },
];

#[test]
fn tls_validity_ids_pin_value_class_purpose_and_exact_oracle_source() {
    for row in &ROWS {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        let Value::Duration(span) = entry.value else {
            panic!("{}: not a duration: {:?}", row.id, entry.value)
        };
        assert_eq!(span.millis(), row.millis, "{}", row.id);
        assert!(matches!(entry.purpose, Purpose::Policy), "{}", row.id);
        let Provenance::Source(source) = entry.provenance else {
            panic!("{}: source constant masquerading as measurement", row.id)
        };
        assert_eq!(
            source,
            format!(
                "{FILE}:{} @ f4e366e9 sha256:{SHA}; {}",
                row.line, row.symbol
            ),
            "{}",
            row.id
        );
        // The pinned text is the declaration the value was read from, and the arithmetic holds.
        assert!(
            row.text.contains(&format!("{}={}", row.unit, row.amount)),
            "{}",
            row.id
        );
        let unit_ms: u64 = match row.unit {
            "minutes" => 60_000,
            "days" => 86_400_000,
            other => panic!("unit {other}"),
        };
        assert_eq!(row.amount * unit_ms, row.millis, "{}", row.id);
    }
}

#[test]
fn tls_validity_ids_are_non_py_listed_once_distinct_and_ordered() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 3);
    assert!(names.iter().all(|name| !name.starts_with("Py")));
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 3);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
    // v0.1.4 invariants the TLS builder relies on: the server leaf never outlives the CA, and the
    // back-date is far smaller than either validity.
    let ms = |id: &str| match BudgetId::from_name(id).unwrap().entry().value {
        Value::Duration(span) => span.millis(),
        other => panic!("{id}: {other:?}"),
    };
    assert!(ms("TlsCertificateBackdate") < ms("TlsServerCertificateValidity"));
    assert!(ms("TlsServerCertificateValidity") < ms("TlsCaValidity"));
}

#[test]
fn the_pinned_lines_and_file_hash_match_the_oracle_checkout() {
    // Host check against the read-only oracle (fails when the checkout is absent).
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let show = std::process::Command::new("git")
        .arg("-C")
        .arg(&oracle)
        .args(["show", &format!("{ORACLE_SHA}:{FILE}")])
        .output()
        .unwrap();
    assert!(show.status.success());
    let text = String::from_utf8(show.stdout).unwrap();
    let at = |n: u32| text.split('\n').nth(n as usize - 1).unwrap().trim();
    for row in &ROWS {
        assert_eq!(at(row.line), row.text, "{} declaration", row.id);
        if let Some((line, twin)) = row.twin {
            assert_eq!(at(line), twin, "{} twin", row.id);
        }
    }
    // These three literals are the only `timedelta` uses in the file: no fourth duration is missed.
    let uses = text.matches("timedelta(").count();
    assert_eq!(uses, 4, "timedelta( occurrences");
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
    assert_eq!(out.split_whitespace().next().unwrap(), SHA);
}
