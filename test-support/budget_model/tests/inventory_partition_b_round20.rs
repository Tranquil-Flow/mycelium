// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/member_transport.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_live/member_transport.py";

#[test]
fn b_embedded_remote_install_script_registers_only_its_streaming_block_size() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = rows
        .iter()
        .find(|r| r.path == P && r.line == 164)
        .expect("embedded install helper streaming block");
    assert_eq!(r.unit, "bytes");
    assert_eq!(r.value, Some(1_048_576));
    assert_eq!(
        r.budget,
        "PyMyceliumLiveMemberTransportRemoteInstallBlockBytesL164"
    );
    assert!(proof::validate(r, &rows, &all));
    // The helper has no wait, deadline or retry; a time or count claim for it is wrong.
    for (unit, value) in [("milliseconds", 1_048_576), ("entries", 1_048_576)] {
        let mut bad = r.clone();
        bad.unit = unit.into();
        bad.value = Some(value);
        assert!(!proof::validate(&bad, &rows, &all));
    }
    let mut bad = r.clone();
    bad.budget = "PyMyceliumNodeMainMaxJoinBundleBytesL133".into();
    bad.value = Some(1_048_576);
    assert!(
        !proof::validate(&bad, &rows, &all),
        "same-unit, same-value join-bundle limit must not satisfy provenance"
    );
    // The candidate expression is the whole script, so the block size count is pinned.
    let c = all.get(&r.key).unwrap();
    assert_eq!(c.expression.matches("1_048_576").count(), 2);
    for word in ["timeout", "sleep", "wait", "deadline"] {
        assert!(!c.expression.contains(word), "{word}");
    }
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
