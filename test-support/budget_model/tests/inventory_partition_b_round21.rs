// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/a10_batch_admission.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_router/a10_batch_admission.py";

#[test]
fn b_frozen_batch_collection_bytes_follow_its_single_constructor_only() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let r = rows
        .iter()
        .find(|r| r.path == P && r.line == 331 && r.review.starts_with("partition-b-family:"))
        .expect("batch collection byte cap forward");
    assert_eq!(r.disposition, "derived");
    assert_eq!(r.unit, "bytes");
    assert_eq!(r.relation, "PyHelperMyceliumRouterA10BatchAdmissionL178C9");
    assert!(proof::validate(r, &rows, &all));
    let mut bad = r.clone();
    bad.relation = "PyMyceliumNodeMainMaxJoinBundleBytesL133".into();
    assert!(!proof::validate(&bad, &rows, &all), "same-unit wrong ID");
    // The sibling deadline field has a dynamic member-derived constructor operand.
    assert!(
        !rows
            .iter()
            .any(|x| x.path == P && x.line == 333 && x.review.starts_with("partition-b-family:"))
    );
    // Without the constructor's reviewed policy field there is no source.
    let without: Vec<_> = rows
        .iter()
        .filter(|x| !(x.path == P && x.line == 913))
        .cloned()
        .collect();
    assert!(!proof::validate(r, &without, &all));
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
