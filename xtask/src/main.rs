// SPDX-License-Identifier: AGPL-3.0-only
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
    process::ExitCode,
};
use xtask::{
    gate, groups, lint,
    policy::{Allowlist, Date},
    preflight::Preflight,
    provenance, quick, release, retention, retire, workspace,
};

fn read(root: &Path, name: &str) -> Result<String, String> {
    fs::read_to_string(root.join(name)).map_err(|error| format!("{name}:1: IO: {:?}", error.kind()))
}
fn run() -> Result<bool, String> {
    let mut args = env::args().skip(1);
    let command = args.next().ok_or(
        "USAGE: cargo xtask <lint|preflight|gate|release|retire|retention|behaviours|quick> [--root PATH] [--group ID]",
    )?;
    if command != "lint" && command != "preflight" {
        return Err(format!("USAGE: unknown command {command}"));
    }
    let mut root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("WORKSPACE: no workspace parent")?
        .to_path_buf();
    let mut groups = Vec::new();
    let mut explicit_root = false;
    let mut strict_provenance = false;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--strict-provenance" if command == "lint" && !strict_provenance => {
                strict_provenance = true;
            }
            "--root" if !explicit_root => {
                root = PathBuf::from(args.next().ok_or("USAGE: --root requires a path")?);
                explicit_root = true;
            }
            "--group" if command == "preflight" => {
                groups.push(args.next().ok_or("USAGE: --group requires an id")?)
            }
            _ => return Err(format!("USAGE: unknown or repeated option {arg}")),
        }
    }
    let today = Date::today()?;
    match command.as_str() {
        "lint" => {
            let allowlist = Allowlist::parse(&read(&root, "xtask/allowlist.json")?, today)?;
            let sources = workspace::sources(&root)?;
            let diagnostics = allowlist.apply(lint::scan_sources(&sources)?, &sources)?;
            for diagnostic in &diagnostics {
                println!(
                    "{}:{}: {}: {}",
                    diagnostic.path,
                    diagnostic.line,
                    diagnostic.code,
                    lint::explanation(&diagnostic.code)
                );
            }
            let fixtures = provenance::scan(&root)?;
            for finding in &fixtures.findings {
                let detail = match finding.code {
                    "FP001" | "FP006" => String::new(),
                    _ => format!(" ({})", finding.detail),
                };
                println!(
                    "{}:{}: {}{}: {}{detail}",
                    finding.path,
                    finding.line,
                    if strict_provenance { "" } else { "WARN " },
                    finding.code,
                    provenance::explanation(finding.code)
                );
            }
            let violations = diagnostics.len()
                + if strict_provenance {
                    fixtures.findings.len()
                } else {
                    0
                };
            println!(
                "lint: {} source files, {violations} violations",
                sources.len()
            );
            let by_crate = fixtures
                .lacking_by_crate()
                .iter()
                .map(|(krate, count)| format!("{krate}={count}"))
                .collect::<Vec<_>>()
                .join(", ");
            println!(
                "provenance: {} fixture files, {} with rows, {} lacking ({}); {} other findings; lacking by crate: {}",
                fixtures.fixtures,
                fixtures.covered,
                fixtures.lacking(),
                if strict_provenance { "FAIL" } else { "WARN" },
                fixtures.findings.len() - fixtures.lacking(),
                if by_crate.is_empty() {
                    "none"
                } else {
                    &by_crate
                }
            );
            Ok(violations == 0)
        }
        "preflight" => {
            let manifest = read(&root, "xtask/prerequisites.json")?;
            let quarantine = read(&root, "xtask/quarantine.json")?;
            let preflight = Preflight::parse(&manifest, &quarantine, today)?;
            let environment = env::vars_os()
                .filter_map(|(key, value)| key.into_string().map(|key| (key, value)).ok())
                .collect::<BTreeMap<_, _>>();
            let path = env::var_os("PATH").unwrap_or_default();
            let statuses = preflight.evaluate(&root, &path, &environment, &groups)?;
            for status in &statuses {
                println!("{}", status.render());
            }
            println!(
                "preflight: readiness only; RUN admits selection, it is not a test PASS; no tools or jobs executed"
            );
            Ok(!statuses
                .iter()
                .any(|status| status.mandatory && status.reason.is_some()))
        }
        _ => Err("USAGE: unsupported command".into()),
    }
}
fn main() -> ExitCode {
    let arguments: Vec<String> = env::args().skip(1).collect();
    if arguments.first().map(String::as_str) == Some("gate") {
        return match gate::cli(&arguments[1..]) {
            Ok(code) => ExitCode::from(code),
            Err(error) => {
                eprintln!("{error}");
                ExitCode::from(2)
            }
        };
    }
    for (word, handler) in [
        (
            "release",
            release::cli as fn(&[String]) -> Result<u8, String>,
        ),
        ("retire", retire::cli),
        ("retention", retention::cli),
        ("behaviours", groups::behaviours_main),
        ("quick", quick::cli),
    ] {
        if arguments.first().map(String::as_str) == Some(word) {
            return match handler(&arguments[1..]) {
                Ok(code) => ExitCode::from(code),
                Err(error) => {
                    eprintln!("{error}");
                    ExitCode::from(2)
                }
            };
        }
    }
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::from(2)
        }
    }
}
