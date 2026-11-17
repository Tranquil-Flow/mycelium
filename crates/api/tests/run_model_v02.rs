// SPDX-License-Identifier: AGPL-3.0-only
//! B13 (ORCHESTRATOR DECISION 17 Nov, reversible; ledger row 73, ruling 28): a fresh install's run
//! model is `Qwen/Qwen3-0.6B`, the only model proven across two Macs (the hermetic ten-row gate and
//! the rv40 physical run), and the picker stops claiming that Qwen2.5-0.5B is "tested on two Macs".
//! v0.1.4's default (`run_model.py`, `demo_default` = `a9-route`, Qwen2.5-0.5B-Instruct, "tested
//! starter") is a recorded v0.2 divergence (`contracts/divergences.json` `run_model_default`).
use api::preferences::run_model;
use std::{fs, path::PathBuf};

fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("b13-run-model-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn a_fresh_install_runs_qwen3_0_6b_the_one_model_proven_on_two_macs() {
    let dir = root("fresh");
    let model = run_model::selected_model(&dir, None).unwrap();
    assert_eq!(
        (model.model_id, model.resolved_commit),
        (
            "Qwen/Qwen3-0.6B",
            "c1899de289a04d12100db370d81485cdf75e47ca"
        )
    );
    let document = run_model::document(&dir, None, None).unwrap();
    assert_eq!(document["selected"], "qwen3-0.6b");
    assert_eq!(document["source"], "demo_default");
    assert_eq!(document["demo_default"], "qwen3-0.6b");
    assert_eq!(document["model_id"], "Qwen/Qwen3-0.6B");
    // Nothing was written: the default is not a saved choice.
    assert!(!dir.join(run_model::FILE).exists());
    // A saved choice and the environment override still win over the default.
    run_model::save_choice(&dir, "a9-route").unwrap();
    assert_eq!(
        run_model::selected_model(&dir, None).unwrap().model_id,
        "Qwen/Qwen2.5-0.5B-Instruct"
    );
    let fresh = root("env");
    assert_eq!(
        run_model::selected_model(&fresh, Some("a9-route"))
            .unwrap()
            .model_id,
        "Qwen/Qwen2.5-0.5B-Instruct"
    );
    fs::remove_dir_all(dir).unwrap();
    fs::remove_dir_all(fresh).unwrap();
}

#[test]
fn the_picker_says_which_model_was_tested_on_two_macs() {
    let dir = root("labels");
    let document = run_model::document(&dir, None, None).unwrap();
    let choices = document["choices"].as_array().unwrap();
    let label = |name: &str| -> String {
        choices
            .iter()
            .find(|c| c["name"] == name)
            .and_then(|c| c["label"].as_str())
            .unwrap_or_else(|| panic!("no picker entry {name}"))
            .to_owned()
    };
    let tested = label("qwen3-0.6b");
    assert!(
        tested.starts_with("Qwen3-0.6B")
            && tested.contains("tested on two Macs")
            && !tested.contains("not yet"),
        "{tested}"
    );
    for name in ["a9-route", "qwen3-8b", "qwen3-14b"] {
        let text = label(name);
        assert!(
            text.contains("not yet tested on two Macs"),
            "{name}: {text}"
        );
        assert!(!text.contains("tested starter"), "{name}: {text}");
    }
    assert_eq!(choices.len(), 4, "{choices:?}");
    fs::remove_dir_all(dir).unwrap();
}
