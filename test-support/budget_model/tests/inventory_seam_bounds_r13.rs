// SPDX-License-Identifier: AGPL-3.0-only
//! Seams SE-L9-3, SE-L11-CHAT-LIMITS and SE-L10-ACQ-BOUNDS (L1b round 13): registry ids for v0.1.4 literals
//! that the Rust crates currently pin only by oracle tests. Every value below is an independent literal read off
//! the oracle line it cites (f4e366e9) and is re-derived from the oracle blob; the registry entry must equal it
//! and its provenance must name that exact line. The constants are source-configured policy/resource values, not
//! measured: SidecarReadyAddressWindow's 3 s was also observed by L9's executed probe (3046/3049 ms).
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budgets::{
    BudgetId,
    table::{Provenance, Value},
};

/// (id, canonical unit, value, oracle path, line, text the oracle line must contain)
type Seam = (
    &'static str,
    &'static str,
    u64,
    &'static str,
    usize,
    &'static str,
);

const SEAMS: &[Seam] = &[
    (
        "SidecarReadyAddressWindow",
        "milliseconds",
        3_000,
        "native/iroh_transport/src/ready_address.rs",
        45,
        "Duration::from_secs(3)",
    ),
    (
        "ArtifactMaxFence",
        "entries",
        65_536,
        "native/mobile_core/src/artifact_net.rs",
        94,
        "MAX_FENCE: usize = 65_536",
    ),
    (
        "ChatMaxPromptChars",
        "entries",
        2_000,
        "mycelium_a13_macos/chat.py",
        39,
        "MAX_PROMPT_CHARS = 2000",
    ),
    (
        "ChatMaxNewTokensCap",
        "entries",
        256,
        "mycelium_a13_macos/chat.py",
        40,
        "MAX_NEW_TOKENS_CAP = 256",
    ),
    (
        "ChatDefaultNewTokens",
        "entries",
        128,
        "mycelium_a13_macos/chat.py",
        41,
        "DEFAULT_NEW_TOKENS = 128",
    ),
    (
        "ChatPerClientPerWindow",
        "entries",
        4,
        "mycelium_a13_macos/chat.py",
        42,
        "PER_CLIENT_PER_MINUTE = 4",
    ),
    (
        "ChatGlobalPerWindow",
        "entries",
        30,
        "mycelium_a13_macos/chat.py",
        43,
        "GLOBAL_PER_MINUTE = 30",
    ),
    (
        "ChatRateWindow",
        "milliseconds",
        60_000,
        "mycelium_a13_macos/chat.py",
        289,
        "now - queue[0] >= 60",
    ),
    (
        "AcquisitionMaxRepositoryFiles",
        "entries",
        256,
        "mycelium_a13_macos/acquisition.py",
        82,
        "_MAX_REPOSITORY_FILES = 256",
    ),
    (
        "AcquisitionMaxPathParts",
        "entries",
        16,
        "mycelium_a13_macos/acquisition.py",
        84,
        "_MAX_PATH_PARTS = 16",
    ),
    (
        "AcquisitionStreamChunkBytes",
        "bytes",
        65_536,
        "mycelium_a13_macos/acquisition.py",
        86,
        "_STREAM_CHUNK_BYTES = 64 * 1024",
    ),
    (
        "AcquisitionCloseJoinSlack",
        "milliseconds",
        2_000,
        "mycelium_a13_macos/acquisition.py",
        1541,
        "float(timeout) + 2.0",
    ),
    (
        "PhysicalDeploymentRelativePathMaxChars",
        "entries",
        1_024,
        "mycelium_qualification/physical_deployment.py",
        685,
        "len(value) > 1024",
    ),
    (
        "PhysicalDeploymentRelativePathMaxParts",
        "entries",
        16,
        "mycelium_qualification/physical_deployment.py",
        692,
        "len(path.parts) > 16",
    ),
];

/// Ids L10 asked for that already exist under their census names (no new id is added for them).
const ALREADY_REGISTERED: &[(&str, &str, u64)] = &[
    (
        "PyMyceliumA13MacosResourcesMaxProbeOutputBytesL21",
        "bytes",
        4_096,
    ),
    (
        "PyMyceliumA13MacosResourcesProbeTimeoutSecondsL22",
        "milliseconds",
        2_500,
    ),
    (
        "PyMyceliumQualificationPhysicalDeploymentMaxSafetensorsHeaderBytesL53",
        "bytes",
        104_857_600,
    ),
];

fn view(name: &str) -> (&'static str, u64) {
    match BudgetId::from_name(name)
        .unwrap_or_else(|| panic!("{name} is not registered"))
        .entry()
        .value
    {
        Value::Duration(s) => ("milliseconds", s.millis()),
        Value::Storage(b) => ("bytes", b.get()),
        Value::Count(n) => ("entries", n.get()),
        other => panic!("{name}: {other:?}"),
    }
}

#[test]
fn each_seam_id_has_the_oracle_value_and_names_its_exact_line() {
    for (id, unit, value, path, line, _) in SEAMS {
        assert_eq!(view(id), (*unit, *value), "{id}");
        let Provenance::Source(text) = BudgetId::from_name(id).unwrap().entry().provenance else {
            panic!("{id}: provenance must be Source")
        };
        assert!(
            text.starts_with(&format!("{path}:{line} @ f4e366e9")),
            "{id}: {text}"
        );
    }
}

#[test]
fn the_seam_values_equal_the_oracle_lines_they_cite() {
    oracle_blobs::oracle_dir_or_panic();
    let paths: Vec<String> = SEAMS
        .iter()
        .map(|s| s.3.to_string())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    let blobs = oracle_blobs::blobs(&paths);
    for (id, _, _, path, line, needle) in SEAMS {
        let text = oracle_blobs::line_of(&blobs[*path], *line).unwrap();
        assert!(text.contains(needle), "{id}: {path}:{line} is `{text}`");
    }
    // the two native rows also pin the file digest the request quoted
    let provenance = |id: &str| match BudgetId::from_name(id).unwrap().entry().provenance {
        Provenance::Source(t) => t,
        other => panic!("{other:?}"),
    };
    for (id, path) in [
        (
            "SidecarReadyAddressWindow",
            "native/iroh_transport/src/ready_address.rs",
        ),
        ("ArtifactMaxFence", "native/mobile_core/src/artifact_net.rs"),
    ] {
        let digest = oracle_blobs::sha256_hex(blobs[path].as_bytes());
        assert!(provenance(id).contains(&format!("sha256:{digest}")), "{id}");
    }
}

#[test]
fn ids_that_already_existed_keep_their_values() {
    for (id, unit, value) in ALREADY_REGISTERED {
        assert_eq!(view(id), (*unit, *value), "{id}");
    }
}

#[test]
fn the_chat_rate_limits_are_per_sixty_second_window_not_per_calendar_minute() {
    // chat.py:15 documents "per-client 4 requests / 60 s and swarm-wide 30 / 60 s"; the window literal is :289
    assert_eq!(view("ChatRateWindow"), ("milliseconds", 60_000));
    assert_eq!(view("ChatPerClientPerWindow"), ("entries", 4));
    assert_eq!(view("ChatGlobalPerWindow"), ("entries", 30));
}
