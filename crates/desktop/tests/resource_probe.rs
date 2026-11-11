// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged resource probe (`Contents/MacOS/native/resource_probe`, lane REL item 4): its
//! answers are v0.1.4's `mycelium_a13_macos/native/resource_probe.m` answers (@ f4e366e9) for the
//! same OS facts, and its output is exactly what `api::resources` admits.
use api::resources::{Observation, Observer, ProbeObserver, parse_probe_output};
use desktop::resource_probe::{line, network, power, thermal};

#[test]
fn power_is_the_pmset_power_source_like_v014() {
    let ac = b"Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n";
    let battery = b"Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t68%; discharging; 1:34 remaining present: true\n";
    assert_eq!(power(Some(ac)), "external");
    assert_eq!(power(Some(battery)), "battery");
    // resource_probe.m:38-43 checks AC first.
    assert_eq!(power(Some(b"Now drawing from 'AC Power' Now drawing from 'Battery Power'")), "external");
    assert_eq!(power(Some(b"Now drawing from 'UPS Power'\n")), "unknown");
    assert_eq!(power(Some(b"")), "unknown");
    // pmset did not run or exited non-zero.
    assert_eq!(power(None), "unknown");
    // Not UTF-8: NSString is nil and containsString: answers NO.
    assert_eq!(power(Some(b"Now drawing from 'AC Power'\xff")), "unknown");
}

#[test]
fn thermal_is_the_four_process_info_states_and_unknown_otherwise() {
    assert_eq!(
        [-1, 0, 1, 2, 3, 4].map(thermal),
        ["unknown", "nominal", "fair", "serious", "critical", "unknown"]
    );
}

#[test]
fn network_is_the_first_path_update_like_v014() {
    assert_eq!(network(Some((1, false, false))), "unmetered");
    assert_eq!(network(Some((1, true, false))), "metered");
    assert_eq!(network(Some((1, false, true))), "metered");
    assert_eq!(network(Some((2, false, false))), "unavailable");
    assert_eq!(network(Some((3, true, true))), "unavailable");
    assert_eq!(network(Some((0, false, false))), "unknown");
    // No update inside the wait.
    assert_eq!(network(None), "unknown");
}

#[test]
fn the_line_is_the_v014_helper_output_and_the_api_admits_it() {
    // Recorded from the v0.1.4 helper on this Mac (rel-evidence probe-parity.txt).
    let text = line("battery", "nominal", "unmetered");
    assert_eq!(
        text,
        "{\"power\":\"battery\",\"thermal\":\"nominal\",\"network\":\"unmetered\"}\n"
    );
    assert_eq!(
        parse_probe_output(text.as_bytes()),
        Observation {
            power: "battery".into(),
            thermal: "nominal".into(),
            network: "unmetered".into()
        }
    );
    assert_eq!(
        parse_probe_output(line("unknown", "unknown", "unknown").as_bytes()),
        Observation::unknown()
    );
}

/// The built helper, run the way the packaged app runs it (`ProbeObserver`, bounded), answers
/// one admitted observation of this Mac; thermal and power are always known on a Mac.
#[cfg(target_os = "macos")]
#[test]
fn the_built_helper_answers_an_admitted_observation_of_this_mac() {
    let exe = std::path::PathBuf::from(env!("CARGO_BIN_EXE_resource_probe"));
    let direct = std::process::Command::new(&exe).output().unwrap();
    assert!(direct.status.success(), "{direct:?}");
    let parsed = parse_probe_output(&direct.stdout);
    assert_eq!(
        String::from_utf8(direct.stdout).unwrap(),
        line(&parsed.power, &parsed.thermal, &parsed.network),
        "one compact JSON line in the v0.1.4 key order"
    );
    assert_ne!(parsed.thermal, "unknown", "{parsed:?}");
    assert_ne!(parsed.power, "unknown", "{parsed:?}");
    let observed = ProbeObserver {
        candidates: vec![exe],
    }
    .observe();
    assert_ne!(observed, Observation::unknown(), "{observed:?}");
}

/// Parity with the v0.1.4 helper binary on this Mac's current state (explicit gate:
/// `MYCELIUM_V014_RESOURCE_PROBE=<v0.1.4 checkout>/mycelium_a13_macos/native/resource_probe`).
#[cfg(target_os = "macos")]
#[test]
#[ignore = "needs the v0.1.4 helper binary path in MYCELIUM_V014_RESOURCE_PROBE"]
fn the_helper_answers_what_the_v014_helper_answers_on_this_mac() {
    let v014 = std::env::var_os("MYCELIUM_V014_RESOURCE_PROBE").expect("MYCELIUM_V014_RESOURCE_PROBE");
    let run = |exe: &std::ffi::OsStr| {
        let out = std::process::Command::new(exe).output().unwrap();
        assert!(out.status.success(), "{out:?}");
        String::from_utf8(out.stdout).unwrap()
    };
    for _ in 0..3 {
        let old = run(&v014);
        let new = run(env!("CARGO_BIN_EXE_resource_probe").as_ref());
        println!("v0.1.4 {old}v0.2   {new}");
        assert_eq!(parse_probe_output(new.as_bytes()), parse_probe_output(old.as_bytes()));
        assert_eq!(new, old, "byte-identical line");
    }
}
