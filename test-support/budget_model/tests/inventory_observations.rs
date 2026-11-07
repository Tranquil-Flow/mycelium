// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/worker.py; O:path:mycelium_router/rtt_measurement.py.
use budget_model::inventory::{candidates, categories, checked_in};

#[test]
fn reviewed_measurements_and_clock_origins_are_not_policy_or_consumption() {
    let all = candidates().unwrap();
    for (path, line, symbol) in [
        ("mycelium_a13_macos/worker.py", 1129, "headroom_ms"),
        ("mycelium_live/router_port.py", 1691, "attempt_started_ms"),
        ("mycelium_openai_shim/ollama.py", 53, "total_duration"),
        ("mycelium_router/rtt_measurement.py", 390, "age_seconds"),
        (
            "scripts/run_a4_product_gate.py",
            257,
            "cancellation_total_ms",
        ),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line && c.symbol == symbol)
            .unwrap();
        assert!(
            categories::accepts("reviewed-observation-slot", c),
            "{}",
            c.key
        );
    }
    for (path, line) in [
        ("mycelium_a13_macos/worker.py", 790),
        ("mycelium_live/router_port.py", 1730),
        ("mycelium_router/transports/iroh.py", 1667),
        ("mycelium_speculative_decode_execution.py", 1361),
        ("scripts/run_a11_route_benchmark.py", 505),
    ] {
        let rows: Vec<_> = all
            .iter()
            .filter(|c| c.path == path && c.line == line)
            .collect();
        assert!(!rows.is_empty());
        for c in rows {
            assert!(
                !categories::accepts("reviewed-observation-slot", c),
                "{}",
                c.key
            );
        }
    }
}

#[test]
fn observation_review_is_bound_to_every_source_identity_field() {
    let all = candidates().unwrap();
    let reviewed = checked_in().unwrap();
    let rows: Vec<_> = reviewed
        .iter()
        .filter(|r| r.review.starts_with("category:reviewed-observation-slot;"))
        .collect();
    assert!(!rows.is_empty());
    for row in rows {
        let c = all.iter().find(|c| c.key == row.key).unwrap();
        assert!(categories::accepts("reviewed-observation-slot", c));
        for field in [
            "key",
            "path",
            "line",
            "column",
            "scope",
            "symbol",
            "expression",
            "kind",
            "source",
        ] {
            let mut bad = c.clone();
            match field {
                "key" => bad.key.push_str("-other"),
                "path" => bad.path = "other.py".into(),
                "line" => bad.line += 1,
                "column" => bad.column += 1,
                "scope" => bad.scope = "other".into(),
                "symbol" => bad.symbol = "request_deadline".into(),
                "expression" => bad.expression = "request_deadline_ms".into(),
                "kind" => bad.kind = "unreviewed".into(),
                "source" => bad.source.push_str(" # drift"),
                _ => unreachable!(),
            }
            assert!(
                !categories::accepts("reviewed-observation-slot", &bad),
                "{} {field}",
                c.key
            );
        }
    }
}

#[test]
fn observed_maximum_frame_size_is_not_the_transport_capacity_or_spent_deadline() {
    let rows = candidates().unwrap();
    for line in [824, 2383, 2545] {
        let c = rows
            .iter()
            .find(|r| {
                r.path == "mycelium_router/transports/iroh.py"
                    && r.line == line
                    && r.symbol.contains("max_frame_bytes")
            })
            .unwrap();
        assert!(
            categories::accepts("reviewed-observation-slot", c),
            "{}",
            c.key
        );
        let mut bad = c.clone();
        bad.expression = "MAX_TRANSPORT_FRAME_BYTES".into();
        assert!(!categories::accepts("reviewed-observation-slot", &bad));
    }
    for (path, line) in [
        ("mycelium_platform_neutral/peer_profile.py", 172),
        ("mycelium_speculative_decode_execution.py", 1361),
        ("mycelium_live/route.py", 4382),
    ] {
        let c = rows
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert!(
            !categories::accepts("reviewed-observation-slot", c),
            "{}",
            c.key
        );
    }
}
