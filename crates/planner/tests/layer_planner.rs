// SPDX-License-Identifier: AGPL-3.0-only
//! Round 6: the v0.1.4 snapshot planner, topology replanner and replan simulator against
//! actual oracle outputs/exceptions (`fixtures/layer-planner.json`, provenance beside it).
use planner::layer_planner::contracts::{format_g12, py_sum};
use planner::layer_planner::{
    TopologyEvent, plan_snapshot, replan_for_event, simulate_bundle_file,
};
use planner::py::{Kind, PyResult, fsum};
use serde_json::Value;
use std::path::PathBuf;

fn fixtures() -> &'static Value {
    static F: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    F.get_or_init(|| serde_json::from_str(include_str!("fixtures/layer-planner.json")).unwrap())
}

fn bytes(v: &Value) -> String {
    planner::canonical(v, false).unwrap()
}

/// Oracle success → byte-identical canonical JSON. Oracle `ValueError` → same text.
/// Any other oracle exception class → a refusal.
fn check(label: &str, oracle: &Value, got: PyResult<Value>) {
    if oracle["ok"] == true {
        let got = got.unwrap_or_else(|e| panic!("{label}: oracle accepted, Rust refused: {e}"));
        let (want, have) = (bytes(&oracle["value"]), bytes(&got));
        if want != have {
            let at = want
                .bytes()
                .zip(have.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(want.len().min(have.len()));
            let window = |s: &str| {
                s.chars()
                    .skip(at.saturating_sub(120))
                    .take(260)
                    .collect::<String>()
            };
            panic!(
                "{label}: first difference at byte {at}\n oracle: {}\n rust:   {}",
                window(&want),
                window(&have)
            );
        }
        return;
    }
    let err = match got {
        Ok(v) => panic!(
            "{label}: oracle raised {} {:?}; Rust produced {}",
            oracle["class"],
            oracle["message"],
            bytes(&v).chars().take(200).collect::<String>()
        ),
        Err(e) => e,
    };
    if oracle["class"] == "ValueError" {
        assert_eq!(err.kind, Kind::Value, "{label}: {err}");
        assert_eq!(oracle["message"].as_str().unwrap(), err.message, "{label}");
    }
}

#[test]
fn provenance_pins_the_exact_fixture_counts_and_scenarios() {
    use sha2::{Digest, Sha256};
    let p: Value =
        serde_json::from_str(include_str!("fixtures/layer-planner-provenance.json")).unwrap();
    let raw = include_str!("fixtures/layer-planner.json");
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let f = fixtures();
    assert_eq!(p["counts"]["plans"], f["plans"].as_array().unwrap().len());
    assert_eq!(
        p["counts"]["replans"],
        f["replans"].as_array().unwrap().len()
    );
    assert_eq!(
        p["counts"]["simulations"],
        f["simulations"].as_array().unwrap().len()
    );
    assert!(p["scenario_sha256"]["product-v1-replanning.json"].is_string());
}

#[test]
fn plan_snapshot_matches_the_oracle_for_scenarios_generated_fleets_and_mutations() {
    let f = fixtures();
    let (mut ok, mut refused) = (0, 0);
    let mut modes = std::collections::BTreeSet::new();
    let mut replicated = 0;
    for case in f["plans"].as_array().unwrap() {
        let got = plan_snapshot(&case["snapshot"]).and_then(|p| p.to_value());
        if case["oracle"]["ok"] == true {
            ok += 1;
            let v = &case["oracle"]["value"];
            modes.insert(v["provenance"]["mode"].as_str().unwrap().to_owned());
            if !v["diagnostics"]["accepted_replica_nodes"]
                .as_array()
                .unwrap()
                .is_empty()
            {
                replicated += 1;
            }
        } else {
            refused += 1;
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    // Every cycle-search family and replication are exercised, not just the easy paths.
    for mode in [
        "exact_topology_enumeration",
        "held_karp",
        "multi_start_insertion",
    ] {
        assert!(
            modes.contains(mode),
            "missing search mode {mode}: {modes:?}"
        );
    }
    assert!(replicated >= 5, "{replicated} replicated plans");
    assert!(
        ok >= 100 && refused >= 50,
        "{ok} accepted / {refused} refused"
    );
}

/// Round 7: the replica-selection threshold at EXACT equality. Each case carries three
/// actual oracle plans whose policy fraction is `f`, `nextafter(f, -inf)` and
/// `nextafter(f, +inf)` where `current_admitted * f == robust_gain` in IEEE double, so a
/// flipped `>`/`>=` or `<=`/`<` in the accept test or the reason label changes the output.
#[test]
fn replica_threshold_boundary_matches_the_oracle_at_exact_equality() {
    use sha2::{Digest, Sha256};
    let raw = include_str!("fixtures/replica-boundary.json");
    let p: Value =
        serde_json::from_str(include_str!("fixtures/replica-boundary-provenance.json")).unwrap();
    assert_eq!(
        p["output_sha256"].as_str().unwrap(),
        format!("{:x}", Sha256::digest(raw.as_bytes()))
    );
    assert_eq!(
        p["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let data: Value = serde_json::from_str(raw).unwrap();
    let cases = data["cases"].as_array().unwrap();
    assert_eq!(p["counts"]["cases"], cases.len());
    let (mut accept, mut reason) = (0, 0);
    for case in cases {
        match case["kind"].as_str().unwrap() {
            "accept" => accept += 1,
            _ => reason += 1,
        }
        let pid = case["placement_id"].as_str().unwrap();
        for name in ["below", "equal", "above"] {
            let variant = &case["variants"][name];
            let mut snapshot = case["snapshot"].clone();
            snapshot["policy"] = variant["snapshot_policy"].clone();
            let label = format!("{}:{name}", case["label"].as_str().unwrap());
            let got = plan_snapshot(&snapshot).and_then(|p| p.to_value());
            // The oracle data itself must show the flip: recompute the decision from Rust's output.
            let decisions =
                got.as_ref().unwrap()["diagnostics"]["replication"]["candidate_decisions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|d| d["iteration"] == 0 && d["placement_id"] == pid)
                    .unwrap_or_else(|| panic!("{label}: no iteration-0 decision for {pid}"))
                    .clone();
            for key in [
                "accepted",
                "reason",
                "robust_gain_rps",
                "minimum_required_gain_rps",
            ] {
                assert_eq!(
                    bytes(&decisions[key]),
                    bytes(&variant["candidate"][key]),
                    "{label}: decision field {key}"
                );
            }
            check(&label, &variant["oracle"], got);
        }
        let (lo, eq, hi) = (
            &case["variants"]["below"]["candidate"],
            &case["variants"]["equal"]["candidate"],
            &case["variants"]["above"]["candidate"],
        );
        if case["kind"] == "accept" {
            assert!(lo["accepted"] == true && eq["accepted"] == false && hi["accepted"] == false);
        } else {
            assert!(
                lo["reason"] == "lower_ranked_candidate" && eq["reason"] == "gain_below_threshold"
            );
        }
    }
    assert!(
        accept >= 5 && reason >= 5,
        "{accept} accept / {reason} reason"
    );
}

fn plan_for(case_index: usize) -> (planner::layer_planner::RoutePlan, Value) {
    let snapshot = fixtures()["plans"][case_index]["snapshot"].clone();
    (plan_snapshot(&snapshot).unwrap(), snapshot)
}

#[test]
fn topology_replanning_matches_the_oracle_for_every_event_family() {
    let f = fixtures();
    let (mut ok, mut refused) = (0, 0);
    let mut recommendations = std::collections::BTreeSet::new();
    for case in f["replans"].as_array().unwrap() {
        let (prev, snapshot) = plan_for(case["plan_case"].as_u64().unwrap() as usize);
        let got = TopologyEvent::from_value(&case["event"]).and_then(|event| {
            let threshold = if case["threshold"].is_null() {
                0.05
            } else {
                planner::layer_planner::contracts::py_float(&case["threshold"])?
            };
            replan_for_event(&prev, &snapshot, &event, threshold)?.to_value()
        });
        if case["oracle"]["ok"] == true {
            ok += 1;
            recommendations.insert(
                case["oracle"]["value"]["recommendation"]
                    .as_str()
                    .unwrap()
                    .to_owned(),
            );
        } else {
            refused += 1;
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    for r in [
        "retain_current_plan",
        "router_builder_validate_existing_track",
        "provision_replanned_intent",
    ] {
        assert!(
            recommendations.contains(r),
            "no vector reaches {r}: {recommendations:?}"
        );
    }
    assert!(
        ok >= 100 && refused >= 20,
        "{ok} accepted / {refused} refused"
    );
}

#[test]
fn simulate_bundle_matches_the_oracle_including_the_shipped_replanning_scenario() {
    let f = fixtures();
    let root =
        PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("l7-sim-{}", std::process::id()));
    let mut shipped = false;
    for (i, case) in f["simulations"].as_array().unwrap().iter().enumerate() {
        let dir = root.join(i.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        let name = case["bundle"]["base_snapshot"]
            .as_str()
            .filter(|n| !n.contains('/') && !n.is_empty())
            .unwrap_or("base.json");
        std::fs::write(
            dir.join(name),
            serde_json::to_vec(&case["base_snapshot"]).unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.join("bundle.json"),
            serde_json::to_vec(&case["bundle"]).unwrap(),
        )
        .unwrap();
        let got = simulate_bundle_file(&dir.join("bundle.json"));
        if case["label"] == "scenario:product-v1-replanning" {
            shipped = true;
            assert_eq!(case["oracle"]["ok"], true);
        }
        check(case["label"].as_str().unwrap(), &case["oracle"], got);
    }
    std::fs::remove_dir_all(&root).unwrap();
    assert!(shipped);
}

#[test]
fn python_sum_and_fsum_and_g12_formatting_are_bit_exact() {
    let f = fixtures();
    for case in f["sums"].as_array().unwrap() {
        let values: Vec<f64> = case["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        assert_eq!(
            py_sum(values.iter().copied()).to_bits(),
            case["sum"].as_f64().unwrap().to_bits(),
            "{values:?}"
        );
        assert_eq!(
            fsum(values.iter().copied()).to_bits(),
            case["fsum"].as_f64().unwrap().to_bits(),
            "{values:?}"
        );
    }
    for case in f["g12"].as_array().unwrap() {
        let x = case["value"].as_f64().unwrap();
        assert_eq!(format_g12(x), case["g12"].as_str().unwrap(), "{x:e}");
    }
}

#[test]
fn cpython_small_int_set_iteration_order_is_reproduced_exactly() {
    let orders = fixtures()["set_orders"].as_array().unwrap();
    assert!(orders.len() > 5000);
    for case in orders {
        let input: Vec<usize> = case[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as usize)
            .collect();
        let want: Vec<usize> = case[1]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as usize)
            .collect();
        assert_eq!(
            planner::layer_planner::search::small_int_set_order(&input),
            want,
            "{input:?}"
        );
    }
}

/// The two L5 compatibility fixtures that were canonical-only are now produced by the
/// real planner code from their sources (pinned scenario / bound tranche inputs).
#[test]
fn l5_replan_report_fixture_is_reproduced_by_the_real_replanner() {
    let l5: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/contracts/layer-replan-simulation-report-v1.json"
    ))
    .unwrap();
    let case = fixtures()["simulations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "scenario:product-v1-replanning")
        .unwrap();
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("l7-l5report-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("product-v1-replicated.json"),
        serde_json::to_vec(&case["base_snapshot"]).unwrap(),
    )
    .unwrap();
    std::fs::write(
        dir.join("product-v1-replanning.json"),
        serde_json::to_vec(&case["bundle"]).unwrap(),
    )
    .unwrap();
    let report = simulate_bundle_file(&dir.join("product-v1-replanning.json")).unwrap();
    std::fs::remove_dir_all(&dir).unwrap();
    assert_eq!(bytes(&l5), bytes(&report));
}

#[test]
fn l5_control_plane_tranche_fixture_validates_through_the_real_bound_compiler() {
    use planner::bound::validate_control_plane_tranche;
    let tranche: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/contracts/control-plane-tranche-v1.json"
    ))
    .unwrap();
    let manifest = &fixtures_bound()["manifests"]["gpt2"];
    let limits = protocol::gossip::Limits {
        max_record_ttl_ms: 604_800_000,
    };
    validate_control_plane_tranche(&tranche, manifest, limits).unwrap();
    // The fixture's route plan is itself the planner's output for its bound snapshot.
    let plan = plan_snapshot(&tranche["planner_snapshot"])
        .unwrap()
        .to_value()
        .unwrap();
    assert_eq!(bytes(&tranche["route_plan"]), bytes(&plan));
}

fn fixtures_bound() -> &'static Value {
    static F: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    F.get_or_init(|| serde_json::from_str(include_str!("fixtures/bound-compiler.json")).unwrap())
}
