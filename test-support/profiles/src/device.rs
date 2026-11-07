// SPDX-License-Identifier: AGPL-3.0-only
//! V2 read-only recorded-device importer. Unknown fields are ignored by serde,
//! never copied to the output. No live app, recursive discovery or network I/O.
use crate::ImportResult;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Write,
    path::Path,
    process::{Command, Stdio},
};

#[derive(Default, Deserialize)]
struct Host {
    load: Option<Vec<f64>>,
    os_version: Option<String>,
}
#[derive(Default, Deserialize)]
struct Hosts {
    owner: Option<Host>,
    laptop: Option<Host>,
}
#[derive(Default, Deserialize)]
struct Run {
    #[serde(default)]
    state: String,
    step: Option<String>,
    run_id: Option<String>,
    build_seconds: Option<f64>,
    cleanup_unproven: Option<serde::de::IgnoredAny>,
}
#[derive(Default, Deserialize)]
struct Model {
    selected: Option<String>,
}
#[derive(Default, Deserialize)]
struct Member {
    #[serde(default)]
    run_model: Model,
    status_latency_s: Option<f64>,
}
#[derive(Default, Deserialize)]
struct Rollover {
    start_s: Option<f64>,
    end_s: Option<f64>,
    gap_s: Option<f64>,
    gap_upper_s: Option<f64>,
}
#[derive(Default, Deserialize)]
struct Chat {
    ttft_s: Option<f64>,
    total_s: Option<f64>,
    decode_tok_s: Option<f64>,
    done: Option<bool>,
}
#[derive(Default, Deserialize)]
struct Record {
    #[serde(default)]
    event: String,
    elapsed_s: Option<f64>,
    #[serde(alias = "at")]
    at_epoch: Option<f64>,
    host: Option<String>,
    #[serde(default)]
    summary: Host,
    #[serde(default)]
    hosts: Hosts,
    source_sha: Option<String>,
    exe_sha256: Option<String>,
    #[serde(default)]
    member_join: Member,
    #[serde(default)]
    member: Member,
    #[serde(default)]
    owner_run: Run,
    ttft_s: Option<f64>,
    total_s: Option<f64>,
    decode_tok_s: Option<f64>,
    done: Option<bool>,
    latency_s: Option<f64>,
    recovery_seconds: Option<f64>,
    recovered: Option<bool>,
    #[serde(default)]
    chats: Vec<Chat>,
    #[serde(default)]
    rollovers: Vec<Rollover>,
    seconds_to_exit: Option<f64>,
    quit_clean: Option<bool>,
    processes_left: Option<u64>,
    state: Option<String>,
    #[serde(alias = "seconds_since_stop")]
    seconds_after_stop: Option<f64>,
    #[serde(default)]
    status: Run,
    // Only presence is retained; unknown nested error/secret fields are discarded.
    cleanup_unproven: Option<serde::de::IgnoredAny>,
}
fn parse(s: &[u8]) -> ImportResult<Record> {
    serde_json::from_slice(s).map_err(|_| "device_evidence_json_invalid".into())
}
fn parse_at(s: &[u8], source: &str) -> ImportResult<Record> {
    parse(s).map_err(|_| format!("device_evidence_json_invalid:{source}").into())
}
#[derive(Debug, Clone, Serialize)]
pub struct Source {
    pub path: String,
    pub sha256: Option<String>,
    pub bytes: Option<u64>,
    pub disposition: String,
}
#[derive(Debug, Clone, Serialize)]
pub struct Sample {
    pub value: f64,
    pub upper_value: Option<f64>,
    pub source: String,
    pub method: String,
    pub outcome: String,
    pub at_epoch: Option<f64>,
    pub load: BTreeMap<String, Vec<f64>>,
}
#[derive(Debug, Clone, Serialize)]
pub struct Statistics {
    pub count: usize,
    pub p50: Option<f64>,
    pub p99: Option<f64>,
    pub status: String,
    pub method: String,
    pub blocked_reasons: Vec<String>,
}
#[derive(Debug, Clone, Serialize)]
pub struct Profile {
    pub id: String,
    pub attempt: String,
    pub phase: String,
    pub machine: String,
    pub hardware: Option<String>,
    pub macos: Option<String>,
    pub source_sha: Option<String>,
    pub executable_sha256: Option<String>,
    pub model: Option<String>,
    pub origin: String,
    pub multiplier: u32,
    pub unit: String,
    pub exact_metric: bool,
    pub samples: Vec<Sample>,
    pub statistics: Statistics,
}
#[derive(Debug, Serialize)]
pub struct Bundle {
    pub schema_version: u32,
    pub method_version: String,
    pub files: Vec<Source>,
    pub profiles: Vec<Profile>,
    pub host_observations: Vec<HostObservation>,
}
#[derive(Debug, Serialize)]
pub struct HostObservation {
    pub attempt: String,
    pub machine: String,
    pub source: String,
    pub macos: Option<String>,
    pub load: Option<Vec<f64>>,
    pub at_epoch: Option<f64>,
}
fn summary(p: &Profile) -> Statistics {
    let values: Vec<_> = p.samples.iter().map(|s| s.value).collect();
    // Existing nearest-rank descriptive policy. These observational runs do not
    // establish independent comparable trials, even when status polling has n>=100.
    let s = crate::statistics(&values, p.exact_metric, false);
    let mut reasons = vec!["independence_not_established".into()];
    if s.count < 100 {
        reasons.push("fewer_than_100_samples".into());
    }
    if !p.exact_metric {
        reasons.push("interval_censored_or_composite".into());
    }
    if p.samples.iter().any(|s| s.load.is_empty()) {
        reasons.push("sample_host_load_missing".into());
    }
    if p.hardware.is_none() {
        reasons.push("hardware_unrecorded".into());
    }
    if p.macos.is_none() {
        reasons.push("macos_unrecorded".into());
    }
    if p.source_sha.is_none() {
        reasons.push("source_sha_unrecorded".into());
    }
    if p.model.is_none() {
        reasons.push("model_unrecorded".into());
    }
    if p.samples.is_empty() {
        reasons.push("no_phase_samples".into());
    }
    Statistics {
        count: s.count,
        p50: s.p50_ms,
        p99: None,
        status: if p.origin == "SYNTHETIC" {
            "SYNTHETIC_NOT_MEASURED"
        } else {
            "BLOCKED"
        }
        .into(),
        method: "nearest_rank_descriptive; p99 requires independent comparable exact loaded trials"
            .into(),
        blocked_reasons: reasons,
    }
}
fn hash(bytes: &[u8]) -> ImportResult<String> {
    // System data hashing only; no executable evidence or application is invoked.
    let mut child = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let write = child
        .stdin
        .take()
        .ok_or("hash_stdin_missing")?
        .write_all(bytes);
    let output = child.wait_with_output()?;
    write?;
    if !output.status.success() {
        return Err("source_hash_failed".into());
    }
    let text = String::from_utf8(output.stdout).map_err(|_| "source_hash_invalid")?;
    let digest = text
        .split_whitespace()
        .next()
        .ok_or("source_hash_missing")?;
    validate_digest(digest, 64)?;
    Ok(digest.into())
}
/// SHA-256 of `bytes` via the system hasher (data only; nothing is executed from evidence).
pub fn sha256_hex(bytes: &[u8]) -> ImportResult<String> {
    hash(bytes)
}
pub(crate) fn validate_digest(s: &str, width: usize) -> ImportResult<()> {
    if s.len() != width || !s.bytes().all(|x| x.is_ascii_hexdigit()) {
        return Err("invalid_identity_digest".into());
    }
    Ok(())
}
pub(crate) fn read(
    root: &Path,
    relative: &str,
    files: &mut Vec<Source>,
    disposition: &str,
) -> ImportResult<Option<Vec<u8>>> {
    let path = root.join(relative);
    let metadata = match fs::symlink_metadata(&path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            files.push(Source {
                path: relative.into(),
                sha256: None,
                bytes: None,
                disposition: "MISSING".into(),
            });
            return Ok(None);
        }
        Err(e) => return Err(e.into()),
    };
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err("non_regular_evidence_file".into());
    }
    let bytes = fs::read(&path)?;
    files.push(Source {
        path: relative.into(),
        sha256: Some(hash(&bytes)?),
        bytes: Some(bytes.len() as u64),
        disposition: disposition.into(),
    });
    Ok(Some(bytes))
}
pub(crate) fn finite(v: f64) -> ImportResult<f64> {
    if !v.is_finite() || v < 0.0 {
        Err("invalid_device_measurement".into())
    } else {
        Ok(v)
    }
}
fn loads(hosts: &Hosts) -> ImportResult<BTreeMap<String, Vec<f64>>> {
    let mut out = BTreeMap::new();
    for (name, h) in [("owner", &hosts.owner), ("laptop", &hosts.laptop)] {
        if let Some(h) = h
            && let Some(v) = &h.load
        {
            for x in v {
                finite(*x)?;
            }
            if v.is_empty() {
                return Err("empty_load_observation".into());
            }
            out.insert(name.into(), v.clone());
        }
    }
    Ok(out)
}
fn push(
    profiles: &mut [Profile],
    (phase, machine): (&str, &str),
    value: f64,
    source: &str,
    method: &str,
    r: &Record,
    outcome: &str,
) -> ImportResult<()> {
    let p = profiles
        .iter_mut()
        .find(|p| p.phase == phase && p.machine == machine)
        .ok_or("unknown_device_phase")?;
    p.samples.push(Sample {
        value: finite(value)?,
        upper_value: None,
        source: source.into(),
        method: method.into(),
        outcome: outcome.into(),
        at_epoch: r.at_epoch,
        load: loads(&r.hosts)?,
    });
    Ok(())
}
fn chat(ps: &mut [Profile], c: &Chat, r: &Record, source: &str) -> ImportResult<()> {
    let outcome = if c.done == Some(true) {
        "completed"
    } else {
        "incomplete_or_failed"
    };
    for (phase, value, scale, method) in [
        (
            "ttft",
            c.ttft_s,
            1000.0,
            "client token-chunk polling TTFT; not native prefill",
        ),
        (
            "chat_total",
            c.total_s,
            1000.0,
            "client request elapsed; failures retained",
        ),
        (
            "decode",
            c.decode_tok_s,
            1.0,
            "recorded client decode tokens/s; not per-token native latency",
        ),
    ] {
        if let Some(v) = value {
            push(ps, (phase, "route"), v * scale, source, method, r, outcome)?;
        }
    }
    Ok(())
}
fn rollovers(
    ps: &mut [Profile],
    r: &Record,
    source: &str,
    seen: &mut BTreeMap<(u64, u64), (u64, Option<u64>)>,
) -> ImportResult<()> {
    for (i, g) in r.rollovers.iter().enumerate() {
        if let (Some(start), Some(end), Some(gap)) = (g.start_s, g.end_s, g.gap_s) {
            finite(start)?;
            finite(end - start)?;
            finite(gap)?;
            let fingerprint = (gap.to_bits(), g.gap_upper_s.map(f64::to_bits));
            if let Some(prior) = seen.insert((start.to_bits(), end.to_bits()), fingerprint) {
                if prior != fingerprint {
                    return Err("conflicting_rollover_measurement".into());
                }
                continue;
            }
            let at = format!("{source}#/rollovers/{i}");
            push(
                ps,
                ("rollover", "route"),
                gap * 1000.0,
                &at,
                "first observed unavailable to first observed serving; interval censored",
                r,
                "observed_serving",
            )?;
            if let Some(upper) = g.gap_upper_s {
                if finite(upper)? < gap {
                    return Err("rollover_upper_below_observation".into());
                }
                let p = ps
                    .iter_mut()
                    .find(|p| p.phase == "rollover")
                    .ok_or("rollover_profile_missing")?;
                p.samples
                    .last_mut()
                    .ok_or("rollover_sample_missing")?
                    .upper_value = Some(upper * 1000.0);
            }
        }
    }
    Ok(())
}
fn empty(attempt: &str, result: &Record, phase: &str, machine: &str, exact: bool) -> Profile {
    Profile {
        id: format!("v014-{attempt}-{machine}-{phase}-v2"),
        attempt: attempt.into(),
        phase: phase.into(),
        machine: machine.into(),
        hardware: None,
        macos: None,
        source_sha: result.source_sha.clone(),
        executable_sha256: result.exe_sha256.clone(),
        model: result.member_join.run_model.selected.clone(),
        origin: "MEASURED".into(),
        multiplier: 1,
        unit: if phase == "decode" { "tokens/s" } else { "ms" }.into(),
        exact_metric: exact,
        samples: Vec::new(),
        statistics: Statistics {
            count: 0,
            p50: None,
            p99: None,
            status: "BLOCKED".into(),
            method: String::new(),
            blocked_reasons: Vec::new(),
        },
    }
}
pub(crate) fn attempt_name(name: &str) -> bool {
    let Some(s) = name.strip_prefix("attempt") else {
        return false;
    };
    let number = s.bytes().take_while(u8::is_ascii_digit).count();
    number > 0 && s[number..].bytes().all(|b| b.is_ascii_lowercase())
}
/// Import immediate attempt directories only. Logs are alternate encodings of
/// events, not independent measurements. Result summaries are per-family fallback.
pub fn import_device(root: &Path) -> ImportResult<Bundle> {
    if fs::symlink_metadata(root)?.file_type().is_symlink() {
        return Err("symlink_evidence_root".into());
    }
    let mut attempts = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| "non_utf8_attempt")?;
        if attempt_name(&name) {
            if !entry.file_type()?.is_dir() {
                return Err("non_directory_attempt".into());
            }
            attempts.push(name);
        }
    }
    attempts.sort();
    if attempts.is_empty() {
        return Err("no_device_attempts".into());
    }
    let mut b = Bundle {
        schema_version: 2,
        method_version: "recorded-device-v2.2".into(),
        files: Vec::new(),
        profiles: Vec::new(),
        host_observations: Vec::new(),
    };
    for attempt in attempts {
        let result_path = format!("{attempt}/RESULT.json");
        let result_bytes = read(root, &result_path, &mut b.files, "PROVENANCE_AND_FALLBACK")?;
        let result = match result_bytes {
            Some(ref bytes) => parse_at(bytes, &result_path)?,
            None => Record::default(),
        };
        if let Some(s) = &result.source_sha {
            validate_digest(s, 40)?;
        }
        if let Some(s) = &result.exe_sha256 {
            validate_digest(s, 64)?;
        }
        let mut ps = Vec::new();
        for (phase, machine, exact) in [
            ("compile", "owner", true),
            ("publish", "owner", false),
            ("start", "owner", false),
            ("stop", "owner", false),
            ("close", "owner", false),
            ("relay_rtt", "route", true),
            ("ttft", "route", true),
            ("decode", "route", true),
            ("chat_total", "route", true),
            ("owner_status", "owner", true),
            ("member_status", "laptop", true),
            ("rollover", "route", false),
            ("restart", "owner", false),
            ("quit", "owner", false),
            ("quit", "laptop", false),
        ] {
            ps.push(empty(&attempt, &result, phase, machine, exact));
        }
        let mut os = BTreeMap::new();
        let mut seen_builds = BTreeSet::new();
        for (event_file, log_file, summary_file) in [
            ("events.jsonl", "stdout.log", "RESULT.json"),
            ("soak-events.jsonl", "soak-stdout.log", "SOAK-RESULT.json"),
        ] {
            let source = format!("{attempt}/{event_file}");
            let bytes = read(root, &source, &mut b.files, "PRIMARY_EVENTS")?;
            let log_source = format!("{attempt}/{log_file}");
            let log = read(
                root,
                &log_source,
                &mut b.files,
                if bytes.is_some() {
                    "DUPLICATE_STREAM_NOT_COUNTED"
                } else {
                    "FALLBACK_LOG"
                },
            )?;
            let (chosen, data) = if let Some(bytes) = bytes {
                (source, Some(bytes))
            } else {
                (log_source, log)
            };
            let mut chats_seen = false;
            let mut seen_rollovers = BTreeMap::new();
            let mut start = None;
            let mut phase_start: Option<(String, f64, usize)> = None;
            if let Some(bytes) = data {
                for (line, s) in bytes.split(|c| *c == b'\n').enumerate() {
                    if s.iter().all(u8::is_ascii_whitespace) {
                        continue;
                    }
                    // stdout may contain non-JSON diagnostic text. Never emit it.
                    if chosen.ends_with(".log") && s.first() != Some(&b'{') {
                        continue;
                    }
                    let at = format!("{chosen}:{}", line + 1);
                    let r = parse_at(s, &at)?;
                    if let Some(t) = r.elapsed_s {
                        finite(t)?;
                    }
                    if let Some(v) = r.owner_run.build_seconds {
                        finite(v)?;
                    }
                    if r.event == "pre_ready_device_bytes" {
                        for (observed, expected) in [
                            (&r.source_sha, &result.source_sha),
                            (&r.exe_sha256, &result.exe_sha256),
                        ] {
                            if let (Some(a), Some(b)) = (observed, expected)
                                && a != b
                            {
                                return Err("conflicting_recorded_build_identity".into());
                            }
                        }
                    }
                    if (r.event == "installed"
                        || r.event == "host_summary"
                        || r.event == "host_admission")
                        && let Some(machine) = r
                            .host
                            .as_deref()
                            .filter(|s| ["owner", "laptop"].contains(s))
                    {
                        if let Some(v) = &r.summary.os_version {
                            os.insert(machine.to_owned(), v.clone());
                        }
                        if let Some(v) = &r.summary.load {
                            for x in v {
                                finite(*x)?;
                            }
                        }
                        b.host_observations.push(HostObservation {
                            attempt: attempt.clone(),
                            machine: machine.into(),
                            source: at.clone(),
                            macos: r.summary.os_version.clone(),
                            load: r.summary.load.clone(),
                            at_epoch: r.at_epoch,
                        });
                    }
                    if r.event == "chat" {
                        chats_seen = true;
                        chat(
                            &mut ps,
                            &Chat {
                                ttft_s: r.ttft_s,
                                total_s: r.total_s,
                                decode_tok_s: r.decode_tok_s,
                                done: r.done,
                            },
                            &r,
                            &at,
                        )?;
                    }
                    if r.event == "owner_status"
                        && let Some(v) = r.latency_s
                    {
                        push(
                            &mut ps,
                            ("owner_status", "owner"),
                            v * 1000.0,
                            &at,
                            "HTTP elapsed; not relay RTT",
                            &r,
                            "observed",
                        )?;
                    }
                    if r.event == "member_status"
                        && let Some(v) = r.member.status_latency_s
                    {
                        push(
                            &mut ps,
                            ("member_status", "laptop"),
                            v * 1000.0,
                            &at,
                            "member HTTP elapsed; not SSH or relay RTT",
                            &r,
                            "observed",
                        )?;
                    }
                    if ["owner_status", "run_progress", "owner_restart_result"]
                        .contains(&r.event.as_str())
                        && let (Some(id), Some(v)) =
                            (&r.owner_run.run_id, r.owner_run.build_seconds)
                        && v > 0.0
                        && seen_builds.insert(id.clone())
                    {
                        push(
                            &mut ps,
                            ("compile", "owner"),
                            v * 1000.0,
                            &at,
                            "owner_run.build_seconds once per run; reported compiler duration",
                            &r,
                            "reported",
                        )?;
                    }
                    if r.event == "run_progress" {
                        let t = r.elapsed_s.ok_or("phase_time_missing")?;
                        if start.is_none() && r.owner_run.state != "serving" {
                            start = Some((t, line + 1));
                        }
                        let step = r.owner_run.step.clone().unwrap_or_default();
                        if let Some((prior, begin, first)) = &phase_start
                            && prior != &step
                            && prior == "publish"
                        {
                            push(
                                &mut ps,
                                ("publish", "owner"),
                                (t - begin) * 1000.0,
                                &format!("{chosen}:{first}-{}", line + 1),
                                "poll transition delta; interval censored",
                                &r,
                                "observed",
                            )?;
                        }
                        if r.owner_run.state == "serving"
                            && let Some((begin, first)) = start.take()
                        {
                            push(
                                &mut ps,
                                ("start", "owner"),
                                (t - begin) * 1000.0,
                                &format!("{chosen}:{first}-{}", line + 1),
                                "first run progress to confirmed serving; composite polling",
                                &r,
                                "observed_serving",
                            )?;
                        }
                        if phase_start.as_ref().is_none_or(|(s, _, _)| s != &step) {
                            phase_start = Some((step, t, line + 1));
                        }
                    }
                    if r.event == "rollover_observation" {
                        rollovers(&mut ps, &r, &at, &mut seen_rollovers)?;
                    }
                    if r.event == "owner_restart_result"
                        && let Some(v) = r.recovery_seconds
                    {
                        push(
                            &mut ps,
                            ("restart", "owner"),
                            v * 1000.0,
                            &at,
                            "reported owner restart recovery composite",
                            &r,
                            if r.recovered == Some(true) {
                                "recovered"
                            } else {
                                "not_recovered"
                            },
                        )?;
                    }
                }
            }
            let summary_source = format!("{attempt}/{summary_file}");
            let summary = if summary_file == "RESULT.json" {
                None
            } else {
                read(root, &summary_source, &mut b.files, "SUMMARY_DEDUP_EVENTS")?
            };
            let sr = match summary.as_ref() {
                Some(bytes) => parse_at(bytes, &summary_source)?,
                None => Record::default(),
            };
            let sr = if summary_file == "RESULT.json" {
                &result
            } else {
                &sr
            };
            if !chats_seen {
                for (i, c) in sr.chats.iter().enumerate() {
                    chat(&mut ps, c, sr, &format!("{summary_source}#/chats/{i}"))?;
                }
            }
            rollovers(&mut ps, sr, &summary_source, &mut seen_rollovers)?;
        }
        for machine in ["owner", "laptop"] {
            let source = format!("{attempt}/quit-{machine}.json");
            if let Some(bytes) = read(root, &source, &mut b.files, "QUIT_MEASUREMENT")? {
                let r = match parse_at(&bytes, &source) {
                    Ok(r) => r,
                    Err(_error) => {
                        b.files.last_mut().ok_or("quit_source_missing")?.disposition =
                            "BLOCKED_INVALID_QUIT_RECEIPT".into();
                        continue;
                    }
                };
                if let Some(v) = r.seconds_to_exit {
                    push(
                        &mut ps,
                        ("quit", machine),
                        v * 1000.0,
                        &source,
                        "AppleEvent to observed process exit; polling resolution unrecorded",
                        &r,
                        if r.quit_clean == Some(true) && r.processes_left == Some(0) {
                            "clean"
                        } else {
                            "unproven"
                        },
                    )?;
                }
            }
        }
        for file in ["probe-stop-inflight.log", "probe-stop-duration-1.log"] {
            let source = format!("{attempt}/{file}");
            if let Some(bytes) = read(root, &source, &mut b.files, "STOP_PROBE")? {
                let mut pending = false;
                for (line, s) in bytes.split(|c| *c == b'\n').enumerate() {
                    if s.iter().all(u8::is_ascii_whitespace) {
                        continue;
                    }
                    let r = parse_at(s, &format!("{source}:{}", line + 1))?;
                    if r.event == "stop_sent" {
                        pending = true;
                    }
                    if pending
                        && ["owner_state", "state"].contains(&r.event.as_str())
                        && (r.state.as_deref() == Some("stopped") || r.status.state == "stopped")
                        && let Some(v) = r.seconds_after_stop
                    {
                        push(
                            &mut ps,
                            ("stop", "owner"),
                            v * 1000.0,
                            &format!("{source}:{}", line + 1),
                            "stop request to observed stopped; not proof of native close",
                            &r,
                            if r.cleanup_unproven.is_some() || r.status.cleanup_unproven.is_some() {
                                "cleanup_unproven"
                            } else {
                                "observed_stopped"
                            },
                        )?;
                        pending = false;
                    }
                }
            }
        }
        for mut p in ps {
            p.macos = os.get(&p.machine).cloned();
            p.statistics = summary(&p);
            b.profiles.push(p.clone());
            if p.unit == "ms" {
                for factor in [2, 3] {
                    let mut synthetic = p.clone();
                    synthetic.origin = "SYNTHETIC".into();
                    synthetic.multiplier = factor;
                    synthetic.id = format!("{}-synthetic-{factor}x", p.id);
                    for sample in &mut synthetic.samples {
                        sample.value = finite(sample.value * f64::from(factor))?;
                        sample.upper_value = sample.upper_value.map(|v| v * f64::from(factor));
                    }
                    synthetic.statistics = summary(&synthetic);
                    b.profiles.push(synthetic);
                }
            }
        }
    }
    Ok(b)
}
