// SPDX-License-Identifier: AGPL-3.0-only
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() == 4 && args[1] == "--device-v2" {
        let bundle = latency_profiles::device::import_device(std::path::Path::new(&args[2]))?;
        let file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&args[3])?;
        serde_json::to_writer_pretty(file, &bundle)?;
        println!(
            "schema=2 profiles={} files={} measured_p99={}",
            bundle.profiles.len(),
            bundle.files.len(),
            bundle
                .profiles
                .iter()
                .filter(|p| p.statistics.p99.is_some())
                .count()
        );
        return Ok(());
    }
    if (args.len() == 6 || args.len() == 7) && args[1] == "--device-qualify" {
        // ROOT DECLARATION DECLARATION_SHA256 OUTPUT [--event-level-only]
        let policy = latency_profiles::qualify::Policy {
            event_level_only: match args.get(6).map(String::as_str) {
                None => false,
                Some("--event-level-only") => true,
                Some(_) => return Err("unknown_option".into()),
            },
        };
        let q = latency_profiles::qualify::qualify_device(
            std::path::Path::new(&args[2]),
            std::path::Path::new(&args[3]),
            &args[4],
            policy,
        )?;
        let file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&args[5])?;
        serde_json::to_writer_pretty(file, &q)?;
        println!(
            "schema=3 cells={} empirical_p99={} pooled={} unpooled={}",
            q.cells.len(),
            q.cells.iter().filter(|c| c.p99.is_some()).count(),
            q.attempts_pooled.len(),
            q.attempts_unpooled.len()
        );
        return Ok(());
    }
    if args.len() != 3 {
        return Err("usage: latency-profiles [--device-v2 | --device-qualify ROOT DECLARATION SHA256] SAVED_DEVICE_ROOT OUTPUT_JSON".into());
    }
    let b = latency_profiles::import_saved(std::path::Path::new(&args[1]))?;
    let file = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&args[2])?;
    serde_json::to_writer_pretty(file, &b)?;
    println!(
        "profiles={} source_receipts={} measured_p99={}",
        b.profiles.len(),
        b.files.len(),
        b.profiles
            .iter()
            .filter(|p| p.qualified_p99().is_some())
            .count()
    );
    Ok(())
}
