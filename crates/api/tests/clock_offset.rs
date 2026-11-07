// SPDX-License-Identifier: AGPL-3.0-only
use api::clock_offset::PeerClockOffsets;
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
#[test]
fn peer_clock_offsets_match_executed_oracle_including_staleness_and_rounding() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_clock_offset.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let now = Arc::new(AtomicU64::new(1000_f64.to_bits()));
    let clock = now.clone();
    let offsets = PeerClockOffsets::new(Box::new(move || {
        f64::from_bits(clock.load(Ordering::SeqCst))
    }))
    .unwrap();
    for (index, row) in fixture["rows"].as_array().unwrap().iter().enumerate() {
        let op = &row["input"];
        if let Some(n) = op["now"].as_f64() {
            now.store(n.to_bits(), Ordering::SeqCst);
        }
        if op["op"] == "observe" {
            offsets.observe(
                op["node"].as_str().unwrap(),
                &op["signed_at"],
                op.get("received_at"),
                op.get("rtt_seconds"),
            );
        }
        assert_eq!(
            offsets.snapshot(),
            row["snapshot"],
            "snapshot op{index}: {op}"
        );
        assert_eq!(
            json!({"one":offsets.offset("one"),"two":offsets.offset("two"),"missing":offsets.offset("missing")}),
            row["offsets"],
            "offset op{index}"
        );
        let object = |rows: Vec<(String, f64)>| {
            Value::Object(rows.into_iter().map(|(k, v)| (k, json!(v))).collect())
        };
        assert_eq!(
            object(offsets.exceeded(None)),
            row["exceeded"],
            "exceeded op{index}"
        );
        assert_eq!(
            json!(
                offsets
                    .exceeded(None)
                    .into_iter()
                    .map(|(node, _)| node)
                    .collect::<Vec<_>>()
            ),
            row["exceeded_order"],
            "insertion order op{index}"
        );
        assert_eq!(
            object(offsets.exceeded(Some(&["one".into()]))),
            row["only_one"],
            "filtered op{index}"
        );
        assert_eq!(offsets.view("missing"), row["missing"], "missing op{index}");
    }
}
