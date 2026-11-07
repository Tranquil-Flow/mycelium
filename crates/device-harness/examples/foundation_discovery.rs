// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only discovery; no execution or opening of referenced native/oracle assets.
use device_harness::evidence_inputs::{Content, Inputs, Pins};
fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let pins = || -> Option<Pins> {
        if args.len() != 4 {
            return None;
        }
        Some(Pins {
            native: Content {
                size_bytes: args[0].parse().ok()?,
                sha256: args[1].clone(),
            },
            oracle: Content {
                size_bytes: args[2].parse().ok()?,
                sha256: args[3].clone(),
            },
        })
    };
    let Some(pins) = pins() else {
        eprintln!(
            "usage: foundation_discovery NATIVE_BYTES NATIVE_SHA256 ORACLE_BYTES ORACLE_SHA256; paths from MYCELIUM_NATIVE_INPUT_MANIFEST and MYCELIUM_ORACLE_MANIFEST"
        );
        return std::process::ExitCode::from(2);
    };
    match Inputs::read_env(pins) {
        Ok(inputs) => {
            println!(
                "{}",
                serde_json::json!({"schema":"mycelium.harness.foundation-discovery.v1","native_manifest":inputs.native_identity(),"oracle_manifest":inputs.oracle_identity(),"rust_snapshot_source":inputs.rust_source(),"oracle_source":inputs.oracle_source(),"native_resources":inputs.resources().iter().map(|r|serde_json::json!({"destination":r.destination,"content":r.source.content})).collect::<Vec<_>>(),"referenced_assets_opened":false,"native_execution":false,"release_qualified":false})
            );
            std::process::ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("{}", error.code);
            std::process::ExitCode::from(1)
        }
    }
}
