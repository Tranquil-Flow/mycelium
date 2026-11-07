// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:model_adapters.py; O:path:mycelium_a13_macos/managed_runtime.py.
use budget_model::inventory::{candidates, categories::accepts};

#[test]
fn reviewed_categories_match_real_rows_and_reject_unrelated_waits() {
    let rows = candidates().unwrap();
    let examples = [
        (
            "timestamp-context",
            "mycelium_live/command_controller.py",
            "observed_at_ms",
            "result.observed_at_ms",
        ),
        (
            "timestamp-context",
            "mycelium_a13_macos/compiler_inputs.py",
            "now_unix_ms",
            "0",
        ),
        (
            "latency-observation",
            "mycelium_a11_draft_runtime.py",
            "draft_queue_ms",
            "0.0",
        ),
        (
            "planner-cost",
            "mycelium_layer_planner/network_cost.py",
            "propagation_km_per_ms",
            "200.0",
        ),
        (
            "fixture-data",
            "mycelium_replica_contracts.py",
            "issued_at_unix_ms",
            "10000",
        ),
        (
            "window-identity",
            "mycelium_host_platform/_win32_api.py",
            "CREATE_NO_WINDOW",
            "134217728",
        ),
        (
            "windows-path-grammar",
            "layer_assignment.py",
            "windows_path",
            "PureWindowsPath(value)",
        ),
        (
            "http-status-code",
            "mycelium_a13_macos/pairing.py",
            "pairing_window_unknown",
            "404",
        ),
        (
            "string-join",
            "model_adapters.py",
            "', '.join",
            "self.layer_count_fields",
        ),
        (
            "path-join",
            "scripts/a13_operator/test_pyz_ledger_coverage.py",
            "os.path.join",
            "'/Users/mycelium-dev/Documents/mycelium-core-program/runs'",
        ),
        (
            "boolean-state",
            "mycelium_a13_macos/demo_chain.py",
            "released",
            "False",
        ),
        (
            "install-release-data",
            "mycelium_a13_linux/install_source.py",
            "install_modes",
            "INSTALL_MODES",
        ),
        (
            "clock-provider",
            "mycelium_a11_evidence_control.py",
            "clock_ms",
            "None",
        ),
        (
            "clock-observation",
            "mycelium_a13_macos/managed_runtime.py",
            "recorded_at_utc_ms",
            "int(time.time() * 1000)",
        ),
        (
            "absent-measurement",
            "mycelium_a13_macos/clock_offset.py",
            "rtt_seconds",
            "None",
        ),
        (
            "protocol-label",
            "mycelium_a13_macos/managed_control.py",
            "TARGET_POLL_PROTOCOL",
            "'mycelium.managed_control.target_poll.v1'",
        ),
        (
            "schema-unit-label",
            "mycelium_product_spine/contracts.py",
            "command_timeout_ms",
            "'count'",
        ),
        (
            "packaged-unit-test",
            "scripts/a13_operator/test_pyz_ledger_coverage.py",
            "os.path.join",
            "'/Users/mycelium-dev/Documents/mycelium-core-program/runs'",
        ),
    ];
    let wait = rows
        .iter()
        .find(|r| r.path == "mycelium_a13_macos/managed_runtime.py" && r.symbol == "timeout")
        .unwrap();
    for (category, path, symbol, expression) in examples {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.symbol == symbol && r.expression == expression)
            .unwrap_or_else(|| panic!("missing real example: {category} {path} {symbol}"));
        assert!(accepts(category, row), "{category}: {}", row.key);
        assert!(
            !accepts(category, wait),
            "category waived an actual wait: {category}"
        );
    }
}

#[test]
fn numeric_categories_keep_attempts_bytes_and_duration_units_distinct() {
    use budget_model::inventory::categories::numeric_literal;
    let rows = candidates().unwrap();
    for (path, symbol, expression, expected) in [
        (
            "mycelium_internet/enrollment.py",
            "max_attempts",
            "3",
            ("entries", 3),
        ),
        (
            "mycelium_mobile/android_sidecar.py",
            "poll_attempts",
            "10",
            ("entries", 10),
        ),
        (
            "mycelium_a13_macos/app.py",
            "max_bytes",
            "4096",
            ("bytes", 4096),
        ),
        (
            "mycelium_a13_macos/demo_target.py",
            "report_interval",
            "10.0",
            ("milliseconds", 10000),
        ),
        (
            "mycelium_a13_macos/managed_control.py",
            "ttl_seconds",
            "60.0",
            ("milliseconds", 60000),
        ),
        (
            "mycelium_a13_macos/pairing.py",
            "_REQUEST_TTL_MAX",
            "600",
            ("milliseconds", 600000),
        ),
    ] {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.symbol == symbol && r.expression == expression)
            .unwrap();
        assert_eq!(numeric_literal(row), Some(expected), "{}", row.key);
    }
    let speed = rows
        .iter()
        .find(|r| r.symbol == "propagation_km_per_ms")
        .unwrap();
    assert_eq!(numeric_literal(speed), None, "speed is not a duration");
    let mut row = rows
        .iter()
        .find(|r| r.symbol == "ttl_seconds" && r.expression == "60.0")
        .unwrap()
        .clone();
    for expression in [
        "True",
        "None",
        "-1",
        "0.0001",
        "1 / 3",
        "float('inf')",
        "18446744073709551616",
        "1e30",
    ] {
        row.expression = expression.into();
        assert_eq!(numeric_literal(&row), None, "{expression}");
    }
    row.expression = "1.5".into();
    assert_eq!(numeric_literal(&row), Some(("milliseconds", 1500)));
    row.symbol = "max_attempts".into();
    assert_eq!(numeric_literal(&row), None);
}

#[test]
fn decimal_conversion_is_exact_and_rejects_unrepresentable_values() {
    use budget_model::inventory::categories::numeric_literal;
    let mut row = candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.symbol == "ttl_seconds" && r.expression == "60.0")
        .unwrap();
    for (expression, expected) in [
        ("0.001", Some(1)),
        ("0.1", Some(100)),
        ("1.234", Some(1234)),
        ("0.0001", None),
        ("NaN", None),
        ("inf", None),
        ("-1", None),
        ("0", None),
        ("1.2.3", None),
        ("18446744073709551615", None),
    ] {
        row.expression = expression.into();
        assert_eq!(
            numeric_literal(&row),
            expected.map(|n| ("milliseconds", n)),
            "{expression}"
        );
    }
}

#[test]
fn binary_byte_capacities_are_exact_and_do_not_accept_rates_as_storage() {
    use budget_model::inventory::categories::numeric_literal;
    let row = candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == "mycelium_mobile/artifact_grant.py" && r.line == 42)
        .unwrap();
    assert_eq!(numeric_literal(&row), Some(("bytes", 4194304)));
    for expression in [
        "1 << 64",
        "2 << 63",
        "-1 << 20",
        "1 >> 20",
        "1 << bits",
        "1 << 2 << 3",
    ] {
        let mut bad = row.clone();
        bad.expression = expression.into();
        assert_eq!(numeric_literal(&bad), None, "{expression}");
    }
    let rate = candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == "mycelium_mobile/artifact_grant.py" && r.line == 210)
        .unwrap();
    assert_eq!(numeric_literal(&rate), Some(("bytes-per-second", 33554432)));
}

#[test]
fn integer_product_categories_preserve_literal_byte_and_time_arithmetic() {
    use budget_model::inventory::categories::numeric_literal;
    let rows = candidates().unwrap();
    for (path, symbol, expected) in [
        (
            "mycelium_live/member_artifact_provisioner.py",
            "maximum_bytes",
            ("bytes", 16777216),
        ),
        (
            "scripts/a13_operator/single_mac_ui_rehearsal.py",
            "max_memory_bytes",
            ("bytes", 8589934592),
        ),
        (
            "scripts/build_a7_operator_plan.py",
            "--ttl-ms",
            ("milliseconds", 43200000),
        ),
    ] {
        let row = rows
            .iter()
            .find(|r| r.path == path && r.symbol == symbol)
            .unwrap();
        assert_eq!(numeric_literal(row), Some(expected));
        let mut invalid = row.clone();
        for expression in [
            "2 * supplied_value",
            "2 ** 64",
            "1 + dynamic",
            "2 * 0.0001",
            "1 / 0",
        ] {
            invalid.expression = expression.into();
            assert_eq!(numeric_literal(&invalid), None);
        }
    }
}

#[test]
fn per_token_byte_ratio_is_not_a_byte_capacity() {
    use budget_model::inventory::categories::numeric_literal;
    let mut row = candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.symbol == "max_checkpoint_bytes_per_committed_token")
        .unwrap();
    assert_eq!(numeric_literal(&row), None);
    row.expression = "8".into();
    assert_eq!(numeric_literal(&row), None);
}

#[test]
fn similar_spellings_do_not_waive_duration_values_or_thread_join() {
    let rows = candidates().unwrap();
    let mut row = rows
        .iter()
        .find(|r| r.symbol == "', '.join")
        .unwrap()
        .clone();
    row.symbol = "thread.join".into();
    assert!(!accepts("string-join", &row));
    row.symbol = "'x'.join + other.join".into();
    assert!(!accepts("string-join", &row));
    row.kind = "assignment".into();
    row.symbol = "release_timeout".into();
    assert!(!accepts("install-release-data", &row));
    row.symbol = "deadline_ms".into();
    row.expression = "None".into();
    assert!(!accepts("absent-measurement", &row));
    row.expression = "'5s'".into();
    assert!(!accepts("protocol-label", &row));
    row.path = "scripts/build_a13_macos.py".into();
    assert!(!accepts("packaged-unit-test", &row));
    assert!(!accepts("unknown-category", &row));
}
