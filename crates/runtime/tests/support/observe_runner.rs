// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-RUN-25. Runner + OwnerChain with the production ObserveAdapter. Model, member
//! reports, signed-session IO and mailbox are controlled collaborators; compile is intentionally
//! absent and must refuse, never claim serving. No native/GPU process is launched.
use super::*;
use owner_run::machine::Identity;
use owner_run::owner_chain::{ChainPorts, OwnerChain, Ports};
use owner_run::runner::{Clocks, Runner, Timing};
use owner_run::steps::contributor::ContributorPort;
use owner_run::steps::model::{ModelPort, PreparedModel, PreparedSource, Reverify};
use std::time::Instant;

struct Model;
impl ModelPort for Model {
    fn owner_and_running(&self) -> bool {
        true
    }
    fn swarm_created(&self) -> bool {
        true
    }
    fn has_closing_thread(&self) -> bool {
        false
    }
    fn closing_thread_alive(&mut self) -> bool {
        false
    }
    fn acquisition_state(&mut self) -> String {
        "prepared".into()
    }
    fn reverify_available(&self) -> bool {
        false
    }
    fn reverify(&mut self) -> Reverify {
        Reverify::Done
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        Ok(PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen2.5-0.5B-Instruct".into(),
                resolved_commit: "revision".into(),
                architecture: "qwen2".into(),
                label: "Qwen2.5".into(),
                choice: None,
                shape_registered: false,
                owner_storage_need: 0,
            },
            manifest_digest: DEFAULT_DIGEST.into(),
            source_bytes: json!(0),
        })
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        Ok(None)
    }
    fn check_rollover_members(&mut self, _: &Value) -> Result<(), StepError> {
        panic!("not a rollover");
    }
}
struct Contributor;
impl ContributorPort for Contributor {
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        Ok("n1".into())
    }
    fn exchange_available(&self) -> bool {
        true
    }
    fn attach_agent(&mut self, n: &str) -> Result<(), StepError> {
        assert_eq!(n, "n1");
        Ok(())
    }
}
struct Exchange {
    nodes: Vec<String>,
    announcements: Arc<Mutex<Vec<Announcement>>>,
    now: f64,
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.announcements.lock().unwrap().push(a.clone());
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(self
            .nodes
            .iter()
            .map(|n| {
                (
                    n.clone(),
                    json!({"run_id":RUN,"deployment_id":DEP,
            "received_at":self.now,"state":"bootstrap_running",
            "descriptor":{"statement":{"target_node_id":n,"expires_at":self.now + 3600.0}}}),
                )
            })
            .collect())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.nodes
            .iter()
            .map(|n| MemberRow {
                node_id: n.clone(),
                lifecycle_state: Some("ACTIVE".into()),
                lease_expires_at: Some(self.now + 3600.0),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        Some("owner-authority".into())
    }
    fn member_identity(&mut self, n: &str) -> Value {
        let m = member(n);
        json!([
            m["generation"],
            m["incarnation"],
            m["verification_key_digest"],
            m["endpoint_id"]
        ])
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {}
}
struct Clock {
    wall: f64,
    mono: Mutex<f64>,
}
impl Clocks for Clock {
    fn mono(&self) -> f64 {
        *self.mono.lock().unwrap()
    }
    fn wall(&self) -> f64 {
        self.wall + self.mono()
    }
    fn simulated(&self) -> bool {
        true
    }
    fn advance(&self, seconds: f64) {
        *self.mono.lock().unwrap() += seconds;
    }
}
struct Fleet<O> {
    model: Model,
    contributor: Contributor,
    reports: Exchange,
    observe: O,
    teardown: Arc<Mutex<Vec<bool>>>,
}
impl<O: ObservePort + Send> ChainPorts for Fleet<O> {
    fn ports(&mut self) -> Ports<'_> {
        Ports {
            model: &mut self.model,
            contributor: Some(&mut self.contributor),
            reports: Some(&mut self.reports),
            observe: Some(&mut self.observe),
            compile: None,
        }
    }
    fn teardown(&mut self, failed: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        self.teardown.lock().unwrap().push(failed);
        Ok(())
    }
}

#[test]
fn runner_owner_chain_reaches_compile_only_after_real_context_collection_for_two_and_three_members()
{
    for nodes in [vec!["n1", "n2"], vec!["n1", "n2", "n3"]] {
        let log = Log::default();
        let observe = adapter(&json!({"known":nodes}), &log);
        let announcements = Arc::new(Mutex::new(Vec::new()));
        let teardown = Arc::new(Mutex::new(Vec::new()));
        let now = fixture()["provenance"]["run_start_unix"].as_f64().unwrap();
        let host = Fleet {
            model: Model,
            contributor: Contributor,
            reports: Exchange {
                nodes: nodes.iter().map(|s| (*s).into()).collect(),
                announcements: announcements.clone(),
                now,
            },
            observe,
            teardown: teardown.clone(),
        };
        let sink = Arc::new(Mutex::new(RunState::new("", "", Reason::Start)));
        let chain = OwnerChain::new(host).with_state_sink(sink.clone());
        let runner = Runner::spawn(
            Box::new(chain),
            Box::new(|| Identity {
                run_id: RUN.into(),
                deployment_id: DEP.into(),
            }),
            Arc::new(Clock {
                wall: now,
                mono: Mutex::new(0.0),
            }),
            Timing::registered(),
        )
        .unwrap();
        runner.start("start", None).unwrap();
        let limit = std::time::Duration::from_millis(
            budgets::BudgetId::HarnessOwnerStatusCheck
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        );
        let start = Instant::now();
        while runner.thread_alive() || !runner.quiescent() {
            assert!(
                start.elapsed() < limit,
                "owner chain did not settle: {}",
                runner.snapshot()
            );
            std::thread::yield_now();
        }
        let snapshot = runner.snapshot();
        runner.close();
        let steps = snapshot["steps"].as_array().unwrap();
        assert_eq!(
            steps.iter().find(|s| s["name"] == "observe").unwrap()["state"],
            "done"
        );
        let compile = steps.iter().find(|s| s["name"] == "compile").unwrap();
        assert_eq!(compile["state"], "failed");
        assert!(
            snapshot.to_string().contains("NP-SLICE1-compile"),
            "{snapshot}"
        );
        assert_eq!(*teardown.lock().unwrap(), vec![true]);
        assert_eq!(sink.lock().unwrap().participants, nodes);
        let calls = log.lock().unwrap();
        for node in nodes {
            assert_eq!(
                calls
                    .iter()
                    .filter(|c| c.starts_with(&format!("send:{node}:hello:")))
                    .count(),
                1
            );
            assert_eq!(
                calls
                    .iter()
                    .filter(|c| *c == &format!("close:{node}"))
                    .count(),
                1
            );
            assert!(calls.iter().any(|c| c == &format!("current_member:{node}")));
        }
        let announced = announcements.lock().unwrap();
        assert_eq!(
            announced
                .iter()
                .map(|a| a.phase.as_str())
                .collect::<Vec<_>>(),
            vec!["collect", "observe"]
        );
        assert_eq!(
            calls
                .iter()
                .filter(|c| c.starts_with("retire_offer:"))
                .count(),
            announced[1].per_node.as_ref().unwrap().len()
        );
    }
}
