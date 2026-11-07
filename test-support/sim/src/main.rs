// SPDX-License-Identifier: AGPL-3.0-only
use sim::{
    model::{Config, Incident, Mode, violations},
    trace::Trace,
};
use std::{
    fs::{File, OpenOptions},
    io::BufReader,
};
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let a: Vec<_> = std::env::args().collect();
    match a.get(1).map(String::as_str) {
        Some("migrate") if a.len() == 7 => {
            let raw: serde_json::Value = serde_json::from_reader(BufReader::new(File::open(&a[2])?))?;
            let baseline: serde_json::Value = serde_json::from_reader(BufReader::new(File::open(&a[3])?))?;
            let t = Trace::migrate_legacy(raw, a[4].clone(), a[5].clone())?;
            let historical = t.verify_recorded()?;
            let corrected = t.corrected().verify_recorded()?;
            if serde_json::to_value(&historical)? != baseline["historical"]
                || serde_json::to_value(&corrected)? != baseline["corrected"] {
                return Err("baseline_outcome_mismatch".into());
            }
            let file = OpenOptions::new().write(true).create_new(true).open(&a[6])?;
            serde_json::to_writer_pretty(file, &t)?;
            println!("MIGRATED {:?}: historical and corrected complete outcomes identical; reads={}/{}", t.config.incident, t.budget_snapshot.len(), t.corrected_budget_snapshot.len());
        }
        Some("record") if a.len() == 6 => {
            let incident = match a[2].as_str() {
                "F1" => Incident::F1, "F2" => Incident::F2, "F3" => Incident::F3,
                "F4" => Incident::F4, "F5" => Incident::F5, "F13" => Incident::F13,
                "F14" => Incident::F14, "F15" => Incident::F15, "F15b" => Incident::F15b,
                "F16" => Incident::F16, _=>return Err("unknown_incident".into()) };
            let c = Config::standard(incident, Mode::Historical, 1)?;
            let t = Trace::generate(a[3].clone(), a[4].clone(), 17, c)?.minimize()?;
            let file = OpenOptions::new().write(true).create_new(true).open(&a[5])?;
            serde_json::to_writer_pretty(file, &t)?;
            println!("MODEL {incident:?} concrete_commands={} violations={:?}",t.commands.len(),t.expected_failure);
        }
        Some("replay") if a.len() == 7 => {
            let mut t: Trace = serde_json::from_reader(BufReader::new(File::open(&a[2])?))?;
            t.require_identity(&a[3], &a[4])?;
            let b: latency_profiles::Bundle = serde_json::from_reader(BufReader::new(File::open(&a[5])?))?;
            t.validate_profile(&b)?;
            let recorded = t.verify_recorded()?;
            let o = if a[6] == "corrected" {
                t = t.corrected();
                t.verify_recorded()?
            } else if a[6] == "historical" {
                recorded
            } else {
                return Err("unknown_mode".into());
            };
            let v = violations(&t.config,&o);
            println!("MODEL {:?} {:?}: served={} rollovers={} cleanup={} chats={} interrupted={} violations={v:?}",t.config.incident,t.config.mode,o.served,o.rollovers,o.cleanup_proofs,o.successful_chats,o.interrupted_chats);
            if !o.metrics.is_empty() { println!("metrics={:?}", o.metrics); }
            assert!(v.is_empty(), "section-7 model invariant violation: {v:?}");
        }
        _ => return Err("usage: sim record INCIDENT BUILD LOCK_SHA OUTPUT; sim replay TRACE BUILD LOCK_SHA PROFILES historical|corrected".into()),
    }
    Ok(())
}
