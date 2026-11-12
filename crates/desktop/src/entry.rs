// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable's entry routing, ported from `mycelium_a13_macos/__main__.py::main`
//! (f4e366e9): a bare launch (or Finder's `-psn_*`) outside `programs/<version>` refuses and says
//! so; `--install`, `--uninstall`, `--rollback`, `--install-status` run the per-user installer
//! (`Install Mycelium.command` starts the app's own executable with `--install --bundle <dir>`);
//! a bare launch from the install starts the app; anything else is refused. Pure and testable
//! without a window.
use crate::{
    install::{self, Roots, Seams},
    launch_location::{self, Refusal},
};
use serde_json::{Map, Value, json};
use std::path::{Path, PathBuf};

pub const INSTALLER_ACTIONS: [&str; 4] =
    ["--install", "--uninstall", "--rollback", "--install-status"];
/// Setting this to `off` suppresses the refusal alert (scripted and test launches only).
pub const ALERT_ENV: &str = "MYCELIUM_LAUNCH_ALERT";
const ALERT_TITLE: &str = "Mycelium cannot run on this Mac";

#[derive(Debug)]
pub enum Route {
    /// A bare launch outside the install: print the event, alert, exit 2, start nothing.
    Refuse(Refusal),
    /// An installer action: `run_installer`.
    Installer,
    /// A bare launch from `programs/<version>`: start the app.
    Run,
    /// An argument the shell does not accept.
    Reject,
}

pub fn route(argv: &[String], executable: &Path, home: &Path) -> Route {
    let roots_programs = launch_location::programs_root(home);
    let entry = home.join("Applications").join(install::APPLICATIONS_ENTRY);
    let bare = argv.iter().skip(1).all(|a| a.starts_with("-psn_"));
    if bare {
        return launch_location::not_installed_refusal(
            argv,
            executable,
            &roots_programs,
            Some(&entry),
        )
        .map_or(Route::Run, Route::Refuse);
    }
    if argv
        .get(1)
        .is_some_and(|a| INSTALLER_ACTIONS.contains(&a.as_str()))
    {
        Route::Installer
    } else {
        Route::Reject
    }
}

/// The one `launcher_refused` event line (no path appears in it).
pub fn refusal_event(refusal: &Refusal) -> String {
    json!({
        "event": "launcher_refused", "code": refusal.code,
        "location": match refusal.location {
            launch_location::Location::Installed => "installed",
            launch_location::Location::Translocated => "translocated",
            launch_location::Location::Elsewhere => "elsewhere",
        },
        "message": refusal.message,
    })
    .to_string()
}

/// The AppleScript a refusal alert runs (`_show_refusal_dialog`, `__main__.py:93-99` @ f4e366e9).
pub fn alert_script(message: &str) -> String {
    format!(
        "display alert \"{ALERT_TITLE}\" message {} as critical buttons {{\"OK\"}} default button \"OK\"",
        py_json_str(message)
    )
}

/// A refused launch on an unsupported Mac (`preflight`, `app.py:122-133` @ f4e366e9).
#[derive(Debug, Eq, PartialEq)]
pub struct PlatformRefusal {
    /// The `launcher_refused` stderr line without its newline, as `json.dumps` writes it.
    pub event: String,
    /// What the Finder-launch alert says.
    pub message: String,
    pub exit_code: u8,
}

/// Python's `json.dumps` string (default `ensure_ascii`), so the stderr line is v0.1.4's bytes.
fn py_json_str(text: &str) -> String {
    let mut out = String::from("\"");
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `preflight` + `unsupported_mac_message` (`app.py:84-133` @ f4e366e9) plus the launcher's
/// refusal (`__main__.py:196-203`): `None` when this Mac is admitted; otherwise the stderr event,
/// the dialog text and exit code 2. Arguments are Python's `platform.system()/machine()/mac_ver()`
/// values (`Darwin`, `arm64`, `15.6.1`). The decision itself is `platform::admit`.
pub fn platform_refusal(system: &str, machine: &str, os_version: &str) -> Option<PlatformRefusal> {
    let os = if system == "Darwin" { "macos" } else { "other" };
    let arch = if machine == "arm64" {
        "aarch64"
    } else {
        "other"
    };
    crate::platform::admit(os, arch, os_version).err()?;
    let shown = if os_version.chars().count() <= 16 {
        os_version
    } else {
        "unknown"
    };
    let message = if system == "Darwin" && machine != "arm64" {
        format!(
            "This Mac has an Intel processor (macOS {shown}). Mycelium needs an Apple Silicon Mac (M1 or later) with macOS 26.0 or later, because the model runs on the Apple GPU with MLX. You can still use the shared chat page from this Mac's browser."
        )
    } else if system == "Darwin" {
        format!(
            "This Mac runs macOS {shown}. Mycelium needs macOS 26.0 or later. Update macOS in System Settings > General > Software Update, then open the app again. Until then you can still use the shared chat page from this Mac's browser."
        )
    } else {
        "This build of Mycelium runs on Apple Silicon Macs with macOS 26.0 or later.".to_string()
    };
    let event = format!(
        "{{\"event\": \"launcher_refused\", \"code\": \"unsupported_platform\", \"observed_platform\": {{\"system\": {}, \"machine\": {}, \"os_version\": {}}}, \"message\": {}}}",
        py_json_str(system),
        py_json_str(machine),
        py_json_str(os_version),
        py_json_str(&message)
    );
    Some(PlatformRefusal {
        event,
        message,
        exit_code: 2,
    })
}

/// The same decision for the machine this runs on. `Err` only when the OS version cannot be read
/// (that stays the generic launch failure: no claim about the Mac is made).
pub fn host_platform_refusal() -> Result<Option<PlatformRefusal>, crate::platform::PlatformError> {
    let version = crate::platform::host_version()?;
    let system = if std::env::consts::OS == "macos" {
        "Darwin"
    } else {
        std::env::consts::OS
    };
    let machine = if std::env::consts::ARCH == "aarch64" {
        "arm64"
    } else {
        std::env::consts::ARCH
    };
    Ok(platform_refusal(system, machine, &version))
}

/// A Finder launch has no terminal: show the refusal as a native alert (as v0.1.4 does).
pub fn show_alert(message: &str) {
    if std::env::var(ALERT_ENV).is_ok_and(|v| v == "off") {
        return;
    }
    let script = alert_script(message);
    // A failed alert is only reported: the refusal line on stderr and the exit code stand alone.
    if let Err(error) = std::process::Command::new("/usr/bin/osascript")
        .args(["-e", &script])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
    {
        eprintln!("alert_unavailable:{:?}", error.kind());
    }
}

#[derive(Debug, Eq, PartialEq)]
pub struct Outcome {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Plain-language text for the refusals a person can act on (`installer_core.MESSAGES`).
fn message(code: &str) -> Option<&'static str> {
    Some(match code {
        "install_already_running" => {
            "Another Mycelium install, rollback or uninstall is running right now. Wait for it to finish (or close its Terminal window), then try again."
        }
        "install_io_error" => {
            "The installer could not read or write a file. Check free disk space and that your user folder is writable, then run the installer again; nothing was half-installed."
        }
        "installer_failed" => {
            "The installer stopped on an unexpected problem and changed nothing it had not verified. Run the installer again; if it repeats, report the code."
        }
        "previous_version_missing" => {
            "The previous version is no longer on disk, so there is nothing to roll back to."
        }
        "installation_record_invalid" => {
            "The record of installed versions is damaged. Uninstall, then install again."
        }
        _ => return None,
    })
}
fn refusal(code: &str) -> String {
    let mut line = Map::new();
    line.insert("protocol".into(), install::PROTOCOL.into());
    line.insert("refused".into(), code.into());
    if let Some(text) = message(code) {
        line.insert("message".into(), text.into());
    }
    Value::Object(line).to_string() + "\n"
}

/// `cli_parser` + `emit`: exactly one action, `--bundle PATH`, `--delete-identity-and-state`.
/// Success: the report as JSON on stdout, code 0. Failure: one structured `refused` line on
/// stderr, code 2, never a panic.
pub fn run_installer(args: &[String], executable: &Path, roots: &Roots, seams: &Seams) -> Outcome {
    let usage = |text: &str| Outcome {
        code: 2,
        stdout: String::new(),
        stderr: format!(
            "usage: mycelium (--install | --uninstall | --rollback | --install-status) [--bundle BUNDLE] [--delete-identity-and-state]\nerror: {text}\n"
        ),
    };
    let mut action: Option<&str> = None;
    let mut bundle: Option<PathBuf> = None;
    let mut delete = false;
    let mut rest = args.iter().skip(1);
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            a if INSTALLER_ACTIONS.contains(&a) => {
                if action.is_some() {
                    return usage("only one action may be given");
                }
                action = Some(a);
            }
            "--bundle" => match rest.next() {
                Some(path) => bundle = Some(PathBuf::from(path)),
                None => return usage("--bundle needs a path"),
            },
            "--delete-identity-and-state" => delete = true,
            other => return usage(&format!("unrecognized argument {other}")),
        }
    }
    let Some(action) = action else {
        return usage("an action is required");
    };
    let result = match action {
        "--install" => {
            // <bundle>/Mycelium.app/Contents/MacOS/<exe> -> <bundle>
            let default = executable.ancestors().nth(4).map(Path::to_path_buf);
            match bundle.or(default) {
                Some(dir) => install::install(&dir, roots, seams),
                None => Err(install::Refusal("bundle_incomplete")),
            }
        }
        "--uninstall" => install::uninstall(roots, seams, delete),
        "--rollback" => install::rollback(roots, seams),
        _ => install::status(roots),
    };
    match result {
        Ok(report) => Outcome {
            code: 0,
            stdout: serde_json::to_string_pretty(&report).unwrap_or_default() + "\n",
            stderr: String::new(),
        },
        Err(install::Refusal(code)) => Outcome {
            code: 2,
            stdout: String::new(),
            stderr: refusal(code),
        },
    }
}
