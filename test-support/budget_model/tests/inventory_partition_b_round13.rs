// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_host_platform/__init__.py; O:path:mycelium_a13_linux/package_manifest.py.
use budget_model::inventory::{Bound, candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

fn at<'a>(rows: &'a [Bound], path: &str, line: usize) -> &'a Bound {
    rows.iter()
        .find(|r| r.path == path && r.line == line && r.review.starts_with("partition-b-family:"))
        .unwrap_or_else(|| panic!("missing B family row {path}:{line}"))
}

#[test]
fn b_sole_external_caller_bounds_flow_through_public_wrappers_without_new_authority() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (path, line, unit, relation, wrong) in [
        (
            "mycelium_host_platform/__init__.py",
            105,
            "bytes",
            "PyMyceliumNodeMainMaxJoinBundleBytesL133",
            "PyMyceliumUiGatewayConfigGatewayconfigMaxSseFrameBytesL62",
        ),
        (
            "mycelium_host_platform/__init__.py",
            108,
            "bytes",
            "PyMyceliumNodeMainMaxJoinBundleBytesL133",
            "PyMyceliumUiGatewayConfigGatewayconfigMaxSseFrameBytesL62",
        ),
        (
            "mycelium_a13_linux/package_manifest.py",
            69,
            "milliseconds",
            "PyExactScriptsBuildA13LinuxL232C41",
            "CancelAndCleanup",
        ),
    ] {
        let r = at(&rows, path, line);
        assert_eq!(r.disposition, "derived");
        assert_eq!(r.unit, unit);
        assert_eq!(r.relation, relation, "{path}:{line}");
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(proof::validate(r, &rows, &all), "{path}:{line}");
        let mut bad = r.clone();
        bad.relation = wrong.into();
        assert!(
            !proof::validate(&bad, &rows, &all),
            "wrong ID {path}:{line}"
        );
        bad = r.clone();
        bad.unit = if unit == "bytes" {
            "milliseconds"
        } else {
            "bytes"
        }
        .into();
        assert!(!proof::validate(&bad, &rows, &all), "unit {path}:{line}");
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}

#[test]
fn b_fence_store_two_caller_reader_stays_pending_without_one_value() {
    let rows = checked_in().unwrap();
    // _win32_read_owner_file has two callers with different byte limits (1 MiB literal and
    // LAST_CUTOVER_MAX_BYTES), so no single ID can describe its parameter.
    assert!(
        !rows
            .iter()
            .any(|r| r.path == "mycelium_a7_fence_store.py" && r.line == 154)
    );
}
