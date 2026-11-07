// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::process::require_normal_memory_pressure;
#[test]
fn oracle_memory_admission_is_fail_closed_without_spawning() {
    assert!(require_normal_memory_pressure("1\n").is_ok());
    for raw in ["2\n", "4\n", "", "unknown", "1\n2", "0"] {
        assert_eq!(
            require_normal_memory_pressure(raw),
            Err("BLOCKED:memory_pressure_not_normal".into())
        );
    }
}
