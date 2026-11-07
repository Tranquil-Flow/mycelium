// SPDX-License-Identifier: AGPL-3.0-only
use mycelium_mobile_core::live_status;
#[test]
fn shared_live_status_has_no_mobile_specific_policy_default() {
    let limits = live_status::Limits {
        fatal_code_chars: 128,
        cooperative_work_ms: 2000.0,
        cleanup_ms: {
            let raw = include_bytes!("../../protocol/fixtures/round4-live-provenance.json");
            mycelium_mobile_core::strict::object(raw, raw.len()).unwrap()["cleanup_max_ms"]
                .as_u64()
                .unwrap()
        },
        maximum_concurrent_requests: 4,
        a10_intervals: 256,
        a10_observations: 64,
    };
    let raw = concat!(
        "{\"rows\":",
        include_str!("../../protocol/fixtures/round4-live-vectors.json"),
        "}"
    )
    .as_bytes();
    let rows = mycelium_mobile_core::strict::object(raw, raw.len()).unwrap();
    for row in rows["rows"].as_array().unwrap() {
        let out = live_status::project(&row["value"], &limits).unwrap();
        assert_eq!(
            mycelium_mobile_core::canonical::to_bytes(&out).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
    }
}
