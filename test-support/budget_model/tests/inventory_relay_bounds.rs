// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L10-RELAY-BOUNDS: the four relay-proxy bounds v0.1.4 enforces in
//! `mycelium_a13_macos/public_relay.py` that no registry id carried yet: the 1 MiB frame ceiling
//! (`_MAX_FRAME=1024*1024`), the 16 concurrent connection slots (`BoundedSemaphore(16)`), the chunk
//! size line ceiling (`_CHUNK_LINE_MAX=10` bytes) and the chunk-size hex width (`{1,8}`). The names
//! carry no quantity hint the census could use, so the ids are non-`Py` registry entries with exact
//! oracle provenance `path:line @ f4e366e9 sha256:<file>; <symbol>`. Expected values are the oracle
//! source text, not the table. The file is byte-identical at ef9ae61d and f4e366e9.
//!
//! Line numbers: L10's seam text cites `:22/:36/:63`; at f4e366e9 the declarations are at `:27`,
//! `:38`/`:39` and `:63` (checked below); the values agree.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
use std::collections::BTreeSet;

const FILE: &str = "mycelium_a13_macos/public_relay.py";
const SHA: &str = "03397d4407e5c4c7a1483d678bf7c36bb5ff9e465fc7057737256a2f150e8012";
const ORACLE_SHA: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

#[derive(Debug, PartialEq, Eq)]
enum Q {
    Bytes(u64),
    Count(u64),
}

struct Row {
    id: &'static str,
    quantity: Q,
    line: u32,
    /// Verbatim oracle line (trimmed) at `FILE:line`.
    text: &'static str,
    /// Substring of `text` the value was read from.
    needle: &'static str,
    symbol: &'static str,
    /// Lines (and verbatim trimmed text) that enforce the bound.
    enforced: &'static [(u32, &'static str)],
}

const ROWS: [Row; 4] = [
    Row {
        id: "RelayFrameMaxBytes",
        quantity: Q::Bytes(1_048_576),
        line: 27,
        text: "_MAX_FRAME=1024*1024",
        needle: "1024*1024",
        symbol: "_MAX_FRAME (request body, response and chunked body ceiling)",
        enforced: &[
            (
                109,
                "if not 0<=length<=_MAX_FRAME or (self.command=='GET' and length):raise ValueError()",
            ),
            (
                118,
                "response=connection.getresponse();raw=response.read(_MAX_FRAME+1)",
            ),
            (119, "if len(raw)>_MAX_FRAME:raise ValueError()"),
            (138, "if len(body)+size>_MAX_FRAME:raise ValueError()"),
        ],
    },
    Row {
        id: "RelayConnectionSlots",
        quantity: Q::Count(16),
        line: 63,
        text: "self.slots=threading.BoundedSemaphore(16)",
        needle: "BoundedSemaphore(16)",
        symbol: "slots BoundedSemaphore (concurrent connections; the next is closed unanswered)",
        enforced: &[(67, "if not self.slots.acquire(blocking=False):")],
    },
    Row {
        id: "RelayChunkLineMaxBytes",
        quantity: Q::Bytes(10),
        line: 38,
        text: "_CHUNK_LINE_MAX=10",
        needle: "=10",
        symbol: "_CHUNK_LINE_MAX (chunk-size line, readline(_CHUNK_LINE_MAX+1))",
        enforced: &[(134, "line=self.rfile.readline(_CHUNK_LINE_MAX+1)")],
    },
    Row {
        id: "RelayChunkSizeHexDigitsMax",
        quantity: Q::Count(8),
        line: 39,
        text: "_CHUNK_SIZE_RE=re.compile(rb'[0-9A-Fa-f]{1,8}\\r\\n')",
        needle: "{1,8}",
        symbol: "_CHUNK_SIZE_RE hex width (chunk size at most 8 hex digits)",
        enforced: &[(
            135,
            "if not _CHUNK_SIZE_RE.fullmatch(line):raise ValueError()",
        )],
    },
];

fn quantity(value: Value) -> Q {
    match value {
        Value::Storage(bytes) => Q::Bytes(bytes.get()),
        Value::Count(entries) => Q::Count(entries.get()),
        other => panic!("unexpected value class {other:?}"),
    }
}

#[test]
fn relay_bound_ids_pin_value_class_purpose_and_exact_oracle_source() {
    for row in &ROWS {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        assert_eq!(quantity(entry.value), row.quantity, "{}", row.id);
        assert!(matches!(entry.purpose, Purpose::Resource), "{}", row.id);
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
        assert!(row.text.contains(row.needle), "{}", row.id);
    }
}

#[test]
fn relay_bound_ids_are_non_py_listed_once_and_distinct() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 4);
    assert!(names.iter().all(|name| !name.starts_with("Py")));
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 4);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
}

#[test]
fn the_chunk_line_ceiling_is_the_chunk_size_width_plus_crlf() {
    // v0.1.4: `readline(10 + 1)` against `[0-9A-Fa-f]{1,8}\r\n` -> 8 digits + CR LF = 10 bytes.
    let value = |id: &str| quantity(BudgetId::from_name(id).unwrap().entry().value);
    let (Q::Bytes(line), Q::Count(digits)) = (
        value("RelayChunkLineMaxBytes"),
        value("RelayChunkSizeHexDigitsMax"),
    ) else {
        panic!("classes")
    };
    assert_eq!(line, digits + 2);
    // An 8-digit chunk size cannot exceed the frame ceiling by representation alone: the frame
    // ceiling is what bounds the body (:138), not the digit width.
    let Q::Bytes(frame) = value("RelayFrameMaxBytes") else {
        panic!("class")
    };
    assert!(frame < 16u64.pow(digits as u32));
}

#[test]
fn the_pinned_lines_and_file_hash_match_the_oracle_checkout() {
    // Host check against the read-only oracle (fails when the checkout is absent).
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let show = |commit: &str| {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(&oracle)
            .args(["show", &format!("{commit}:{FILE}")])
            .output()
            .unwrap();
        assert!(out.status.success(), "{commit}");
        String::from_utf8(out.stdout).unwrap()
    };
    let text = show(ORACLE_SHA);
    assert_eq!(text, show("ef9ae61d"));
    let at = |n: u32| text.split('\n').nth(n as usize - 1).unwrap().trim();
    for row in &ROWS {
        assert_eq!(at(row.line), row.text, "{} declaration", row.id);
        for (line, expected) in row.enforced {
            assert_eq!(at(*line), *expected, "{} enforcement :{line}", row.id);
        }
    }
    // No further frame/slot declaration exists: one `_MAX_FRAME=` and one `BoundedSemaphore(`.
    assert_eq!(text.matches("_MAX_FRAME=").count(), 1);
    assert_eq!(text.matches("BoundedSemaphore(").count(), 1);
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
