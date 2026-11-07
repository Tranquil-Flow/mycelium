// SPDX-License-Identifier: AGPL-3.0-only
use latency_profiles::Bundle;
#[test]
fn saved_device_snapshot_is_sparse_not_a_p99_qualification() {
    let b: Bundle = serde_json::from_str(include_str!("../data/v014-v1.json")).unwrap();
    assert_eq!(b.schema_version, 1);
    assert_eq!(b.profiles.len(), 106);
    assert!(b.profiles.iter().all(|p| p.qualified_p99().is_none()));
    for (id, count) in [
        ("v014-attempt6-ttft-v1", 5),
        ("v014-attempt7-ttft-v1", 0),
        ("v014-attempt8-ttft-v1", 4),
        ("v014-attempt6-owner_status-v1", 132),
    ] {
        let p = b.profiles.iter().find(|p| p.id == id).unwrap();
        assert_eq!(p.samples.len(), count);
        assert_eq!(p.statistics.count, count);
        assert!(p.statistics.p99_ms.is_none());
    }
    for phase in ["stop", "close", "relay_rtt"] {
        for a in [6, 7, 8] {
            let id = format!("v014-attempt{a}-{phase}-v1");
            assert!(
                b.profiles
                    .iter()
                    .find(|p| p.id == id)
                    .unwrap()
                    .samples
                    .is_empty()
            );
        }
    }
    assert!(
        b.profiles
            .iter()
            .all(|p| p.origin != "SYNTHETIC" || p.statistics.status == "SYNTHETIC_NOT_MEASURED")
    );
    let ids: std::collections::BTreeSet<_> = b.profiles.iter().map(|p| &p.id).collect();
    assert_eq!(ids.len(), b.profiles.len());
}
