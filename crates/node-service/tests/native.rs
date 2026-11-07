// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "metal")]
#[test]
fn two_process_native_chat_stop_and_rollover() {
    let checkpoint =
        std::env::var("MYCELIUM_SLICE_CHECKPOINT").expect("explicit checkpoint required");
    let tokenizer = std::env::var("MYCELIUM_SLICE_TOKENIZER").expect("explicit tokenizer required");
    let evidence =
        std::env::var("MYCELIUM_SLICE_EVIDENCE").expect("fresh evidence directory required");
    let status = std::process::Command::new(env!("CARGO_BIN_EXE_mycelium-node"))
        .args(["qualify", &checkpoint, &tokenizer, &evidence])
        .status()
        .unwrap();
    assert!(status.success(), "native slice incomplete");
    let summary: serde_json::Value = serde_json::from_slice(
        &std::fs::read(std::path::Path::new(&evidence).join("summary.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        summary["transport"],
        "Iroh-signed-NodeControl/ArtifactChunk/Router"
    );
    assert_eq!(summary["control_transport"], "signed-node-control");
    assert_eq!(summary["loopback_tcp_control"], false);
    assert_eq!(summary["persisted_iroh_endpoint_identity"], true);
    assert_eq!(summary["persisted_identity_and_consent"], true);
    assert_eq!(summary["signed_enrollment_and_fenced_stop"], true);
    assert_eq!(summary["wall_timed_rollover"], true);
    assert_eq!(
        summary["runs"][0]["lifecycle_boundary"],
        "wall_timed_rollover"
    );
    assert_eq!(summary["runs"][1]["started_by"], "l6_continue_rollover");
    let runs = summary["runs"].as_array().unwrap();
    assert_eq!(
        runs.iter()
            .map(|r| r["lifecycle_run"].as_u64().unwrap())
            .collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    assert!(runs.iter().all(|r| r["lifecycle_epoch"] == 1));
}
