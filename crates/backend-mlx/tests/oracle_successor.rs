// SPDX-License-Identifier: AGPL-3.0-only
//! Historical captures stay immutable; successor equivalence is a separate seal.
#[test]
fn accepted_oracle_retains_every_recorded_compute_source() {
    let pin: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/oracle-successor.json")).unwrap();
    assert_eq!(
        pin["accepted_oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(pin["compute_sources_unchanged"], true);
    for fixture in [
        include_str!("fixtures/qwen2-packed.json"),
        include_str!("fixtures/qwen3-f32.json"),
        include_str!("fixtures/qwen3-packed.json"),
        include_str!("fixtures/qwen35-f32.json"),
        include_str!("fixtures/qwen35-packed.json"),
    ] {
        let recorded: serde_json::Value = serde_json::from_str(fixture).unwrap();
        assert_eq!(recorded["oracle_commit"], pin["recorded_oracle"]);
        assert_eq!(recorded["oracle_sources"], pin["sources"]);
    }
    for fixture in [
        include_str!("fixtures/moe-component-f32.json"),
        include_str!("fixtures/moe-component-packed.json"),
    ] {
        let recorded: serde_json::Value = serde_json::from_str(fixture).unwrap();
        assert_eq!(recorded["oracle_commit"], pin["accepted_oracle"]);
        assert_eq!(recorded["sources"], pin["sources"]);
        assert_eq!(
            recorded["scope"],
            "first-layer-moe-block-only-not-stage-or-model-acceptance"
        );
    }
}
