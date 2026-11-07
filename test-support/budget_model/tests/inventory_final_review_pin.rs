// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:physical_inference_qualification.py.
use budget_model::inventory::{CENSUS_SOURCE_SHA, SOURCE_SHA, candidates, checked_in, validate};
use std::collections::{BTreeMap, BTreeSet};
#[test]
fn final_review_manifest_is_an_exact_source_key_crosswalk() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    let mut seen = BTreeSet::new();
    let mut lines = include_str!("../data/python-final-review-sites.tsv").lines();
    assert_eq!(lines.next(), Some("key\tsource_sha\tsource"));
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 3);
        assert!(seen.insert(f[0]));
        assert_eq!(f[1], CENSUS_SOURCE_SHA);
        assert_eq!(all.get(f[0]).unwrap().source, f[2]);
        assert!(
            rows.iter()
                .any(|r| r.key == f[0] && r.source_sha == CENSUS_SOURCE_SHA)
        );
    }
    let reviewed: BTreeSet<_> = rows
        .iter()
        .filter(|r| r.source_sha == CENSUS_SOURCE_SHA)
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(seen, reviewed);
}
#[test]
fn final_source_reviews_cannot_be_backdated_or_relabel_historical_rows() {
    let rows = checked_in().unwrap();
    let index = rows
        .iter()
        .position(|r| r.path == "physical_inference_qualification.py" && r.line == 795)
        .expect("new source review");
    assert_eq!(rows[index].source_sha, CENSUS_SOURCE_SHA);
    assert_eq!(validate(&rows), Ok(()));
    let mut bad = rows.clone();
    bad[index].source_sha = SOURCE_SHA.into();
    assert!(validate(&bad).is_err());
    let historical = rows
        .iter()
        .position(|r| r.source_sha == SOURCE_SHA)
        .unwrap();
    let mut bad = rows.clone();
    bad[historical].source_sha = CENSUS_SOURCE_SHA.into();
    assert!(validate(&bad).is_err());
}
