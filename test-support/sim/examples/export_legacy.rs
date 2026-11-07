// SPDX-License-Identifier: AGPL-3.0-only
//! Run at a984d632 (plus this exporter only) before the v2 migration.
use sim::model::{self, Config, Input, Mode, Outcome};
use std::{fs::File, path::Path};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let out = std::env::args().nth(1).ok_or("output directory required")?;
    std::fs::create_dir_all(&out)?;
    for id in [
        "F1", "F2", "F3", "F4", "F5", "F13", "F14", "F15", "F15b", "F16",
    ] {
        let path = format!("test-support/sim/traces/{id}-v1.json");
        let raw: serde_json::Value = serde_json::from_reader(File::open(path)?)?;
        let mut config: Config = serde_json::from_value(raw["config"].clone())?;
        let commands: Vec<Input> = serde_json::from_value(raw["commands"].clone())?;
        let recorded: Outcome = serde_json::from_value(raw["expected_outcome"].clone())?;
        let historical = model::run(&config, &commands)?;
        assert_eq!(historical, recorded, "legacy historical outcome {id}");
        assert_eq!(
            serde_json::to_value(model::violations(&config, &historical))?,
            raw["expected_failure"]
        );
        config.mode = Mode::Corrected;
        let corrected = model::run(&config, &commands)?;
        assert!(
            model::violations(&config, &corrected).is_empty(),
            "legacy corrected {id}"
        );
        let file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(Path::new(&out).join(format!("{id}-outcomes.json")))?;
        serde_json::to_writer_pretty(
            file,
            &serde_json::json!({"source_commit": "a984d632d4733fdaa95d21bfb0a5af98e18a2582", "historical": historical, "corrected": corrected}),
        )?;
        println!(
            "{id}: original recorded historical equality; corrected contract GREEN; both complete outcomes exported"
        );
    }
    Ok(())
}
