// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_iroh_sidecar/client.py; O:path:mycelium_router/transports/iroh.py.
use budget_model::inventory::checked_in;
#[path = "common/partition_b.rs"]
mod proof;
use proof::validate_rows as validate;

#[test]
fn b_protocol_compositions_and_configured_defaults_have_distinct_source_values() {
    let rows = checked_in().unwrap();
    // Independent expected values: >BBQ16sI has 30 bytes, not native alignment;
    // wire payload = 16 MiB + endpoint 32 + two generation fields of 8 bytes.
    for (path, line, value, unit) in [
        ("mycelium_iroh_sidecar/client.py", 37, 16_777_264, "bytes"),
        ("mycelium_iroh_sidecar/client.py", 52, 16_777_326, "bytes"),
        ("mycelium_live/route.py", 110, 1_400, "milliseconds"),
        ("mycelium_router/transports/iroh.py", 916, 4, "entries"),
        ("mycelium_router/transports/iroh.py", 925, 129, "entries"),
        ("mycelium_router/transports/iroh.py", 929, 128, "entries"),
        (
            "mycelium_router/transports/iroh.py",
            4970,
            1_000,
            "milliseconds",
        ),
        (
            "mycelium_router/transports/iroh.py",
            5022,
            250,
            "milliseconds",
        ),
        (
            "mycelium_iroh_sidecar/client.py",
            725,
            5_500,
            "milliseconds",
        ),
        (
            "mycelium_iroh_sidecar/client.py",
            774,
            5_500,
            "milliseconds",
        ),
    ] {
        let index = rows
            .iter()
            .position(|r| r.path == path && r.line == line)
            .expect("reviewed exact B quantity");
        let row = &rows[index];
        assert_eq!(row.value, Some(value), "{}", row.key);
        assert_eq!(row.unit, unit);
        assert!(row.review.starts_with("partition-b-quantity:"));
        assert!(row.disposition.is_empty());
        for mutation in 0..4 {
            let mut bad = rows.clone();
            match mutation {
                0 => bad[index].expression.push_str(" + 1"),
                1 => bad[index].review = "partition-b-quantity:unreviewed; reject".into(),
                2 => bad[index].symbol.push('!'),
                3 => bad[index].scope.push('!'),
                _ => unreachable!(),
            }
            assert!(validate(&bad).is_err(), "{} mutation {mutation}", row.key);
        }
    }
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn b_chunk_rate_limiter_consumes_bytes_not_a_positional_timeout() {
    let rows = checked_in().unwrap();
    let index = rows
        .iter()
        .position(|r| r.path == "mycelium_live/artifact_transport.py" && r.line == 471)
        .expect("reviewed byte-count operand");
    assert_eq!(rows[index].unit, "bytes");
    assert_eq!(rows[index].expression, "len(block)");
    assert_eq!(rows[index].disposition, "derived");
    let mut bad = rows.clone();
    bad[index].relation = "PyMyceliumIrohSidecarClientOperationalMaxFrameBytesL23".into();
    assert_ne!(bad[index].relation, rows[index].relation);
    assert!(validate(&bad).is_err());
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn b_queue_aliases_and_trace_byte_remainders_keep_their_own_units() {
    let rows = checked_in().unwrap();
    for (line, unit, wrong_id) in [
        (
            930,
            "entries",
            "PyMyceliumRouterTransportsIrohCancellationClientCapacityL916",
        ),
        (
            946,
            "entries",
            "PyMyceliumRouterTransportsIrohCancellationClientCapacityL916",
        ),
        (
            4551,
            "bytes",
            "PyMyceliumIrohSidecarClientOperationalMaxFrameBytesL23",
        ),
        (
            4691,
            "bytes",
            "PyMyceliumIrohSidecarClientOperationalMaxFrameBytesL23",
        ),
        (
            4697,
            "bytes",
            "PyMyceliumIrohSidecarClientOperationalMaxFrameBytesL23",
        ),
    ] {
        let index = rows
            .iter()
            .position(|r| r.path == "mycelium_router/transports/iroh.py" && r.line == line)
            .expect("reviewed source-bound resource operand");
        assert_eq!(rows[index].unit, unit);
        assert!(rows[index].value.is_none());
        assert_eq!(rows[index].disposition, "derived");
        let mut bad = rows.clone();
        bad[index].relation = wrong_id.into();
        assert_ne!(bad[index].relation, rows[index].relation);
        assert!(validate(&bad).is_err(), "same-unit wrong ID at {line}");
    }
    assert_eq!(validate(&rows), Ok(()));
}
