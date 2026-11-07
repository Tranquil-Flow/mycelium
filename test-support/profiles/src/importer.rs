// SPDX-License-Identifier: AGPL-3.0-only
//! Saved-evidence reader. Unknown JSON fields use serde IgnoredAny: they are never
//! materialized in a Value, logged, retained or reserialized. No directory recursion.
use crate::{Statistics, statistics};
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::{BufRead, BufReader},
    path::Path,
};

pub type ImportResult<T> = Result<T, Box<dyn std::error::Error>>;
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sample {
    pub value: f64,
    pub source: String,
    pub method: String,
    pub host_load: Option<f64>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub id: String,
    pub phase: String,
    pub unit: String,
    pub origin: String,
    pub multiplier: u32,
    pub build: String,
    pub model: String,
    pub device: String,
    pub exact: bool,
    pub samples: Vec<Sample>,
    pub statistics: Statistics,
}
impl Profile {
    pub fn refresh(&mut self) {
        self.statistics = statistics(
            &self.samples.iter().map(|s| s.value).collect::<Vec<_>>(),
            self.exact,
            self.samples.iter().all(|s| s.host_load.is_some()),
        );
        // A synthetic percentile is never offered as a measured one.
        if self.origin == "SYNTHETIC" {
            self.statistics.status = "SYNTHETIC_NOT_MEASURED".into();
        }
    }
    pub fn loaded(&self, factor: u32) -> ImportResult<Self> {
        if ![2, 3].contains(&factor) || self.unit != "ms" {
            return Err("invalid_loaded_variant".into());
        }
        let mut p = self.clone();
        p.id = format!("{}-synthetic-{factor}x", self.id);
        p.origin = "SYNTHETIC".into();
        p.multiplier = factor;
        for s in &mut p.samples {
            s.value *= f64::from(factor);
        }
        p.refresh();
        Ok(p)
    }
    pub fn qualified_p99(&self) -> Option<f64> {
        if self.origin != "MEASURED" || self.statistics.status != "EMPIRICAL_P99" {
            return None;
        }
        // Do not trust persisted summary fields; recompute from raw data.
        statistics(
            &self.samples.iter().map(|s| s.value).collect::<Vec<_>>(),
            self.exact,
            self.samples.iter().all(|s| s.host_load.is_some()),
        )
        .p99_ms
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bundle {
    pub schema_version: u32,
    pub files: Vec<FileReceipt>,
    pub profiles: Vec<Profile>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FileReceipt {
    pub source: String,
    pub status: String,
}

// This is the complete raw-input key allowlist. No flatten/Value/maps.
#[derive(Default, Deserialize)]
pub struct Evidence {
    #[serde(default)]
    event: String,
    elapsed_s: Option<f64>,
    latency_s: Option<f64>,
    ttft_s: Option<f64>,
    total_s: Option<f64>,
    #[serde(default)]
    owner_run: Run,
    #[serde(default)]
    member: Member,
    #[serde(default)]
    member_join: Member,
    #[serde(default)]
    acquisition: Acquisition,
    #[serde(default)]
    summary: Summary,
    exe_sha256: Option<String>,
    #[serde(default)]
    chats: Vec<Chat>,
}
fn nullable_string<'de, D: serde::Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    Ok(Option::<String>::deserialize(d)?.unwrap_or_default())
}
#[derive(Default, Deserialize)]
struct Run {
    #[serde(default, deserialize_with = "nullable_string")]
    step: String,
    #[serde(default)]
    state: String,
}
#[derive(Default, Deserialize)]
struct Member {
    status_latency_s: Option<f64>,
    #[serde(default)]
    run_model: Model,
}
#[derive(Default, Deserialize)]
struct Model {
    selected: Option<String>,
}
#[derive(Default, Deserialize)]
struct Acquisition {
    downloaded_bytes: Option<u64>,
    total_bytes: Option<u64>,
}
#[derive(Default, Deserialize)]
struct Summary {
    mailbox_bytes: Option<u64>,
}
#[derive(Default, Deserialize)]
struct Chat {
    ttft_s: Option<f64>,
    total_s: Option<f64>,
}

pub fn parse_evidence(input: &str) -> ImportResult<Evidence> {
    // Errors are a fixed code, never serde's potentially secret-bearing value message.
    serde_json::from_str(input).map_err(|_| "evidence_json_invalid".into())
}
fn empty(attempt: u32, phase: &str, unit: &str, exact: bool, build: &str, model: &str) -> Profile {
    Profile {
        id: format!("v014-attempt{attempt}-{phase}-v1"),
        phase: phase.into(),
        unit: unit.into(),
        origin: "MEASURED".into(),
        multiplier: 1,
        build: build.into(),
        model: model.into(),
        device: if phase == "member_status" {
            "member-device-hardware-unrecorded"
        } else {
            "owner-device-hardware-unrecorded"
        }
        .into(),
        exact,
        samples: Vec::new(),
        statistics: statistics(&[], exact, false),
    }
}
fn push(
    profiles: &mut [Profile],
    phase: &str,
    value: f64,
    source: String,
    method: &str,
) -> ImportResult<()> {
    if !value.is_finite() || value < 0.0 {
        return Err("invalid_timing_or_size".into());
    }
    let p = profiles
        .iter_mut()
        .find(|p| p.phase == phase)
        .ok_or("unknown_phase")?;
    p.samples.push(Sample {
        value,
        source,
        method: method.into(),
        host_load: None,
    });
    Ok(())
}
/// Consume only named result and event files. RESULT chats are fallback only: the
/// same event repeated in a result must not inflate sample count. Never reads logs,
/// window.json/native-control.json, joins, credentials, unpack trees or live state.
pub fn import_saved(root: &Path) -> ImportResult<Bundle> {
    let mut bundle = Bundle {
        schema_version: 1,
        files: Vec::new(),
        profiles: Vec::new(),
    };
    for attempt in [6, 7, 8] {
        let result_name = format!("attempt{attempt}/RESULT.json");
        let result: Evidence = if root.join(&result_name).is_file() {
            serde_json::from_reader(BufReader::new(File::open(root.join(&result_name))?))
                .map_err(|_| "evidence_json_invalid")?
        } else {
            return Err("mandatory_result_missing".into());
        };
        let build = result
            .exe_sha256
            .as_deref()
            .filter(|s| s.len() == 64 && s.bytes().all(|c| c.is_ascii_hexdigit()))
            .ok_or("invalid_build_digest")?;
        let model = result
            .member_join
            .run_model
            .selected
            .as_deref()
            .filter(|s| *s == "a9-route")
            .unwrap_or("UNKNOWN");
        let mut ps = Vec::new();
        for phase in [
            "compile",
            "publish",
            "start",
            "stop",
            "close",
            "relay_rtt",
            "ttft",
            "chat_total",
            "owner_status",
            "member_status",
        ] {
            ps.push(empty(
                attempt,
                phase,
                "ms",
                !["compile", "publish", "start", "stop", "close"].contains(&phase),
                build,
                model,
            ));
        }
        for phase in ["acquisition_downloaded", "acquisition_total", "mailbox"] {
            ps.push(empty(attempt, phase, "bytes", false, build, model));
        }
        for (events, result_file) in [
            ("events.jsonl", "RESULT.json"),
            ("soak-events.jsonl", "SOAK-RESULT.json"),
        ] {
            let source = format!("attempt{attempt}/{events}");
            let path = root.join(&source);
            let exists = path.is_file();
            bundle.files.push(FileReceipt {
                source: source.clone(),
                status: if exists {
                    "IMPORTED_ALLOWLIST"
                } else {
                    "MISSING"
                }
                .into(),
            });
            let mut chat_count = 0;
            let mut phase_start: Option<(String, f64, usize)> = None;
            let mut run_start = None;
            if exists {
                for (line, text) in BufReader::new(File::open(path)?).lines().enumerate() {
                    let e = parse_evidence(&text?)?;
                    let at = format!("{source}:{}", line + 1);
                    if e.event == "chat" {
                        if let Some(v) = e.ttft_s {
                            push(
                                &mut ps,
                                "ttft",
                                v * 1000.0,
                                at.clone(),
                                "client token-chunk polling; 250ms resolution; not native TTFT",
                            )?;
                            chat_count += 1;
                        }
                        if let Some(v) = e.total_s {
                            push(
                                &mut ps,
                                "chat_total",
                                v * 1000.0,
                                at.clone(),
                                "client request elapsed; failed requests retained",
                            )?;
                        }
                    }
                    if e.event == "owner_status"
                        && let Some(v) = e.latency_s
                    {
                        push(
                            &mut ps,
                            "owner_status",
                            v * 1000.0,
                            at.clone(),
                            "owner status HTTP elapsed; not relay RTT",
                        )?;
                    }
                    if e.event == "member_status"
                        && let Some(v) = e.member.status_latency_s
                    {
                        push(
                            &mut ps,
                            "member_status",
                            v * 1000.0,
                            at.clone(),
                            "member status HTTP elapsed; excludes remote harness transport",
                        )?;
                    }
                    if e.event == "run_progress" {
                        let t = e.elapsed_s.ok_or("phase_timestamp_missing")?;
                        if e.owner_run.step == "model" {
                            run_start = Some((t, line + 1));
                        }
                        if let Some((step, begin, first)) = &phase_start
                            && step != &e.owner_run.step
                            && ["compile", "publish"].contains(&step.as_str())
                        {
                            push(
                                &mut ps,
                                step,
                                (t - begin) * 1000.0,
                                format!("{source}:{first}-{}", line + 1),
                                "poll transition delta; interval-censored, not exact phase duration",
                            )?;
                        }
                        if e.owner_run.state == "serving"
                            && let Some((begin, first)) = run_start.take()
                        {
                            push(
                                &mut ps,
                                "start",
                                (t - begin) * 1000.0,
                                format!("{source}:{first}-{}", line + 1),
                                "first observed model step to confirmed serving; composite, interval-censored",
                            )?;
                        }
                        if phase_start
                            .as_ref()
                            .is_none_or(|(s, _, _)| *s != e.owner_run.step)
                        {
                            phase_start = Some((e.owner_run.step, t, line + 1));
                        }
                    }
                    for (phase, v) in [
                        ("acquisition_downloaded", e.acquisition.downloaded_bytes),
                        ("acquisition_total", e.acquisition.total_bytes),
                        ("mailbox", e.summary.mailbox_bytes),
                    ] {
                        if let Some(v) = v {
                            push(
                                &mut ps,
                                phase,
                                v as f64,
                                at.clone(),
                                "raw byte counter snapshot; repeated snapshots are not independent allocations",
                            )?;
                        }
                    }
                }
            }
            let name = format!("attempt{attempt}/{result_file}");
            let rp = root.join(&name);
            bundle.files.push(FileReceipt {
                source: name.clone(),
                status: if rp.is_file() {
                    "READ_ALLOWLIST_DEDUP_EVENTS"
                } else {
                    "MISSING"
                }
                .into(),
            });
            if rp.is_file() {
                let r: Evidence = serde_json::from_reader(BufReader::new(File::open(rp)?))
                    .map_err(|_| "evidence_json_invalid")?;
                if chat_count == 0 {
                    for (i, c) in r.chats.iter().enumerate() {
                        if let Some(v) = c.ttft_s {
                            push(
                                &mut ps,
                                "ttft",
                                v * 1000.0,
                                format!("{name}#/chats/{i}/ttft_s"),
                                "result-only client polling TTFT",
                            )?;
                        }
                        if let Some(v) = c.total_s {
                            push(
                                &mut ps,
                                "chat_total",
                                v * 1000.0,
                                format!("{name}#/chats/{i}/total_s"),
                                "result-only client elapsed",
                            )?;
                        }
                    }
                }
            }
        }
        for mut p in ps {
            p.refresh();
            bundle.profiles.push(p);
        }
    }
    // Reports are curated numeric observations, not measurements by this importer.
    // L13 source numbers are also declared by L1, avoiding a second budget table.
    for (phase, id, load) in [
        ("stop", budgets::BudgetId::ObservedStop, Some(17.0)),
        ("start", budgets::BudgetId::ObservedStart, None),
    ] {
        let mut p = empty(
            0,
            phase,
            "ms",
            false,
            "L13-report-source-f5bfb709",
            "a9-route",
        );
        p.id = format!("v014-L13-reported-{phase}-v1");
        p.origin = "REPORTED_APPROXIMATE".into();
        p.device = "two-Mac-report-hardware-unrecorded".into();
        p.samples.push(Sample { value: id.span(budgets::Entries::new(2)).map_err(|_| "budget_unavailable")?.millis() as f64,
            source: "L13-ROLLSTOP.md:61-64".into(), method: "owner-reported single approximate observation; NOT p99; start boundary differs from confirmed serving".into(), host_load: load });
        p.refresh();
        bundle.profiles.push(p);
    }
    let mut size = empty(0, "planner_share", "bytes", false, "L10-report", "a9-route");
    size.id = "v014-L10-planner-share-v1".into();
    size.origin = "PLANNER_ESTIMATE_NOT_DEVICE".into();
    size.samples.push(Sample {
        value: budgets::BudgetId::CorrectedOwnerShare
            .bytes()
            .map_err(|_| "budget_unavailable")?
            .get() as f64,
        source: "L10-SPLIT.md:31-33".into(),
        method: "planner output, not physical bytes".into(),
        host_load: None,
    });
    size.refresh();
    bundle.profiles.push(size);
    bundle.files.push(FileReceipt {
        source: "L12-WALL.md:22-29".into(),
        status: "POLICY_ONLY_NO_LATENCY_SAMPLES".into(),
    });
    let variants: Vec<_> = bundle
        .profiles
        .iter()
        .filter(|p| p.unit == "ms")
        .flat_map(|p| [p.loaded(2), p.loaded(3)])
        .collect();
    for p in variants {
        bundle.profiles.push(p?);
    }
    Ok(bundle)
}
