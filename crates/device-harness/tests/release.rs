// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::release_github as gh;
use device_harness::{Fault, FaultKind, Result, release::*};
use std::collections::BTreeMap;

#[derive(Default)]
struct GithubFake {
    pages: BTreeMap<u32, (Vec<gh::Release>, Option<u32>)>,
    published: Option<gh::Release>,
    calls: Vec<String>,
    bounds: Vec<u64>,
    now: u64,
    latency: u64,
    failure: bool,
}
impl gh::Github for GithubFake {
    fn now_ms(&self) -> u64 {
        self.now
    }
    fn list_authenticated(&mut self, repo: &str, page: u32, bound: Span) -> Result<gh::Page> {
        assert_eq!(repo, "fixture/mycelium");
        self.calls.push(format!("authenticated-list:{page}"));
        self.bounds.push(bound.millis());
        self.now += self.latency;
        if self.failure {
            return Err(Fault::new("github_auth_failed", FaultKind::Observation));
        }
        let (releases, next_page) = self.pages.get(&page).cloned().expect("unexpected page");
        Ok(gh::Page {
            releases,
            next_page,
        })
    }
    fn by_tag(&mut self, repo: &str, tag: &str, bound: Span) -> Result<gh::Release> {
        assert_eq!((repo, tag), ("fixture/mycelium", "v0.2.0"));
        self.calls.push("tags-endpoint".into());
        self.bounds.push(bound.millis());
        self.now += self.latency;
        self.published.clone().ok_or(Fault::new(
            "github_release_not_found",
            FaultKind::Observation,
        ))
    }
}
fn github_expected() -> gh::Expected {
    gh::Expected {
        repository: "fixture/mycelium".into(),
        tag: "v0.2.0".into(),
        asset: "mycelium-0.2.0.zip".into(),
        sidecar_sha256: "e".repeat(64),
        identity: identity(),
        visibility: gh::Visibility::Draft,
    }
}
fn github_release() -> gh::Release {
    gh::Release {
        id: 42,
        tag_name: "v0.2.0".into(),
        draft: true,
        prerelease: false,
        assets: vec![
            gh::Asset {
                name: "mycelium-0.2.0.zip".into(),
                digest: format!("sha256:{}", "d".repeat(64)),
                state: "uploaded".into(),
            },
            gh::Asset {
                name: "mycelium-0.2.0.zip.sha256".into(),
                digest: format!("sha256:{}", "e".repeat(64)),
                state: "uploaded".into(),
            },
        ],
    }
}
fn github_limits() -> gh::Limits {
    gh::Limits {
        total: Span::from_millis(100),
        max_pages: 4,
    }
}
fn github_fake() -> GithubFake {
    let mut other = github_release();
    other.tag_name = "v0.2.0-other".into();
    other.id = 41;
    GithubFake {
        pages: [
            (1, (vec![other], Some(2))),
            (2, (vec![github_release()], None)),
        ]
        .into(),
        ..GithubFake::default()
    }
}
#[test]
fn draft_hidden_from_tags_completes_release_state_only_after_authenticated_listing() {
    let id = identity();
    let mut inner = Fake::default();
    for stage in [
        Stage::Preflight,
        Stage::Package,
        Stage::Cert,
        Stage::Device,
        Stage::Soak,
        Stage::Proof,
    ] {
        advance(&mut inner, &id, stage).unwrap();
    }
    let mut io = gh::ReadbackIo {
        inner,
        github: github_fake(),
        expected: github_expected(),
        limits: github_limits(),
    };
    assert!(io.github.published.is_none()); // fake GitHub tags endpoint is a 404 for this draft
    let result = advance(&mut io, &id, Stage::Publish).unwrap();
    assert!(!result.release_qualified);
    assert_eq!(
        io.github.calls,
        ["authenticated-list:1", "authenticated-list:2"]
    );
    assert!(
        io.inner
            .state
            .as_ref()
            .unwrap()
            .completed
            .contains(&Stage::Publish)
    );
    advance(&mut io, &id, Stage::Publish).unwrap();
    assert_eq!(
        io.inner
            .calls
            .iter()
            .filter(|s| **s == Stage::Publish)
            .count(),
        1
    );
    io.github
        .pages
        .get_mut(&2)
        .unwrap()
        .0
        .push(github_release());
    assert_eq!(
        advance(&mut io, &id, Stage::Publish).unwrap_err().code,
        "github_release_match_not_unique"
    );
}
#[test]
fn draft_lookup_requires_exact_one_across_all_pages_and_never_falls_back() {
    for case in 0..5 {
        let mut f = github_fake();
        let expected = match case {
            0 => {
                f.pages.get_mut(&2).unwrap().0.clear();
                "github_release_match_not_unique"
            }
            1 => {
                f.pages.get_mut(&1).unwrap().0.push(github_release());
                "github_release_match_not_unique"
            }
            2 => {
                let mut r = github_release();
                r.draft = false;
                f.pages.get_mut(&1).unwrap().0.push(r);
                "github_release_match_not_unique"
            }
            3 => {
                f.failure = true;
                "github_auth_failed"
            }
            _ => {
                f.pages.get_mut(&2).unwrap().1 = Some(1);
                "github_pagination_invalid"
            }
        };
        assert_eq!(
            gh::readback(&mut f, &github_expected(), github_limits())
                .unwrap_err()
                .code,
            expected
        );
        assert!(!f.calls.iter().any(|s| s == "tags-endpoint"));
    }
}
#[test]
fn release_readback_preserves_draft_asset_and_published_endpoint_guards() {
    for case in 0..8 {
        let mut f = github_fake();
        let r = &mut f.pages.get_mut(&2).unwrap().0[0];
        match case {
            0 => r.draft = false,
            1 => r.prerelease = true,
            2 => r.id = 0,
            3 => r.assets[0].digest = format!("sha256:{}", "f".repeat(64)),
            4 => r.assets[1].digest = format!("sha256:{}", "f".repeat(64)),
            5 => r.assets[0].state = "new".into(),
            6 => r.assets.push(r.assets[0].clone()),
            _ => r.assets[1].name = r.assets[0].name.clone(),
        }
        assert!(
            gh::readback(&mut f, &github_expected(), github_limits()).is_err(),
            "{case}"
        );
    }
    let mut e = github_expected();
    e.visibility = gh::Visibility::Published;
    let mut r = github_release();
    r.draft = false;
    let mut f = GithubFake {
        published: Some(r.clone()),
        ..GithubFake::default()
    };
    assert_eq!(gh::readback(&mut f, &e, github_limits()).unwrap(), r);
    assert_eq!(f.calls, ["tags-endpoint"]);
    f.published.as_mut().unwrap().tag_name = "v0.2.0-near".into();
    assert!(gh::readback(&mut f, &e, github_limits()).is_err());
}
#[test]
fn draft_pagination_has_one_total_budget_and_incomplete_listing_never_passes() {
    let mut f = github_fake();
    f.latency = 30;
    gh::readback(&mut f, &github_expected(), github_limits()).unwrap();
    assert_eq!(f.bounds, [100, 70]);
    let mut f = github_fake();
    f.latency = 50;
    assert_eq!(
        gh::readback(&mut f, &github_expected(), github_limits())
            .unwrap_err()
            .code,
        "github_readback_deadline"
    );
    let mut f = github_fake();
    let limits = gh::Limits {
        max_pages: 1,
        ..github_limits()
    };
    assert_eq!(
        gh::readback(&mut f, &github_expected(), limits)
            .unwrap_err()
            .code,
        "github_listing_incomplete"
    );
    let mut e = github_expected();
    e.repository = "fixture/../mycelium".into();
    let mut f = github_fake();
    assert_eq!(
        gh::readback(&mut f, &e, github_limits()).unwrap_err().code,
        "github_readback_input_invalid"
    );
    assert!(f.calls.is_empty());
}
#[test]
fn failed_remote_readback_leaves_intent_and_cannot_repeat_publication() {
    let id = identity();
    let mut inner = Fake::default();
    for s in [
        Stage::Preflight,
        Stage::Package,
        Stage::Cert,
        Stage::Device,
        Stage::Soak,
        Stage::Proof,
    ] {
        advance(&mut inner, &id, s).unwrap();
    }
    let mut io = gh::ReadbackIo {
        inner,
        github: github_fake(),
        expected: github_expected(),
        limits: github_limits(),
    };
    io.github.pages.get_mut(&2).unwrap().0.clear();
    assert_eq!(
        advance(&mut io, &id, Stage::Publish).unwrap_err().code,
        "github_release_match_not_unique"
    );
    assert!(
        io.inner
            .state
            .as_ref()
            .unwrap()
            .issued
            .contains(&Stage::Publish)
    );
    assert!(
        !io.inner
            .state
            .as_ref()
            .unwrap()
            .completed
            .contains(&Stage::Publish)
    );
    io.github
        .pages
        .get_mut(&2)
        .unwrap()
        .0
        .push(github_release());
    advance(&mut io, &id, Stage::Publish).unwrap();
    assert_eq!(
        io.inner
            .calls
            .iter()
            .filter(|s| **s == Stage::Publish)
            .count(),
        1
    );
}
fn audit_inventory() -> device_harness::audit::Inventory {
    use device_harness::audit::{Inventory, Test};
    Inventory {
        source: "a".repeat(40),
        source_files: BTreeMap::from([("src/lib.rs".into(), "f".repeat(64))]),
        required_obligations: ["R7".into()].into(),
        previous_tests: BTreeMap::new(),
        tests: BTreeMap::from([(
            "fixture::mandatory".into(),
            Test {
                job: "fixture-mandatory".into(),
                obligations: ["R7".into()].into(),
                incidents: MANDATORY.iter().map(|s| (*s).into()).collect(),
                passed: true,
            },
        )]),
        selected_jobs: ["fixture-mandatory".into()].into(),
        removals: BTreeMap::new(),
    }
}
fn identity() -> Identity {
    Identity {
        source: "a".repeat(40),
        lock: "b".repeat(64),
        executable: "c".repeat(64),
        archive: "d".repeat(64),
        selection: device_harness::package::digest(
            &serde_json::to_vec(&audit_inventory()).unwrap(),
        ),
    }
}
#[derive(Default)]
struct Fake {
    state: Option<State>,
    receipts: BTreeMap<Stage, Receipt>,
    calls: Vec<Stage>,
    fail: Option<Stage>,
    drop_readback: bool,
    conflict: bool,
    denied: bool,
    missing_audit: bool,
}
impl ReleaseIo for Fake {
    fn read_audit(&mut self) -> Result<Option<device_harness::audit::Inventory>> {
        Ok((!self.missing_audit).then(audit_inventory))
    }
    fn load(&mut self) -> Result<Option<State>> {
        Ok(self.state.clone())
    }
    fn save(&mut self, expected: Option<u64>, next: &State) -> Result<()> {
        if self.conflict || self.state.as_ref().map(|s| s.revision) != expected {
            return Err(Fault::new(
                "release_concurrent_writer",
                FaultKind::Observation,
            ));
        }
        self.state = Some(next.clone());
        Ok(())
    }
    fn authorized(&mut self, _stage: Stage, _id: &Identity) -> Result<bool> {
        Ok(!self.denied)
    }
    fn read_receipt(&mut self, stage: Stage) -> Result<Option<Receipt>> {
        Ok(if self.drop_readback && stage == Stage::Package {
            None
        } else {
            self.receipts.get(&stage).cloned()
        })
    }
    fn execute(&mut self, stage: Stage, id: &Identity) -> Result<()> {
        self.calls.push(stage);
        if self.fail == Some(stage) {
            return Err(Fault::new("fixture_failure", FaultKind::Observation));
        }
        self.receipts.insert(
            stage,
            Receipt {
                identity: id.clone(),
                stage,
                passed: true,
                physical: false,
                incidents: MANDATORY.iter().map(|s| s.to_string()).collect(),
                removed_tests: vec![],
                approved_removals: vec![],
            },
        );
        Ok(())
    }
}
#[test]
fn preflight_then_package_then_independent_cert_device_branches() {
    let id = identity();
    let mut f = Fake::default();
    assert_eq!(
        advance(&mut f, &id, Stage::Cert).unwrap_err().code,
        "release_prerequisite_missing"
    );
    assert!(f.calls.is_empty());
    advance(&mut f, &id, Stage::Preflight).unwrap();
    advance(&mut f, &id, Stage::Package).unwrap();
    advance(&mut f, &id, Stage::Device).unwrap();
    advance(&mut f, &id, Stage::Cert).unwrap();
    assert_eq!(
        f.calls,
        [Stage::Preflight, Stage::Package, Stage::Device, Stage::Cert]
    );
    let restored =
        serde_json::from_str::<State>(&serde_json::to_string(f.state.as_ref().unwrap()).unwrap())
            .unwrap();
    f.state = Some(restored);
    advance(&mut f, &id, Stage::Package).unwrap();
    assert_eq!(f.calls.len(), 4);
}
#[test]
fn no_rebuild_or_replay_after_success_or_ambiguous_effect() {
    let id = identity();
    let mut f = Fake::default();
    advance(&mut f, &id, Stage::Preflight).unwrap();
    f.drop_readback = true;
    assert!(advance(&mut f, &id, Stage::Package).is_err());
    assert_eq!(f.calls, [Stage::Preflight, Stage::Package]);
    assert_eq!(
        advance(&mut f, &id, Stage::Package).unwrap_err().code,
        "release_effect_unproven"
    );
    assert_eq!(f.calls.len(), 2);
    f.drop_readback = false;
    advance(&mut f, &id, Stage::Package).unwrap();
    assert_eq!(f.calls.len(), 2);
}
#[test]
fn changed_identity_mismatched_receipt_and_concurrent_writer_fail_closed() {
    let id = identity();
    let mut f = Fake::default();
    advance(&mut f, &id, Stage::Preflight).unwrap();
    let mut other = id.clone();
    other.archive = "f".repeat(64);
    assert_eq!(
        advance(&mut f, &other, Stage::Package).unwrap_err().code,
        "release_identity_changed"
    );
    f.receipts.get_mut(&Stage::Preflight).unwrap().identity = other;
    assert_eq!(
        advance(&mut f, &id, Stage::Preflight).unwrap_err().code,
        "release_receipt_invalid"
    );
    let mut f = Fake {
        conflict: true,
        ..Fake::default()
    };
    assert!(advance(&mut f, &id, Stage::Preflight).is_err());
    assert!(f.calls.is_empty());
}
#[test]
fn failed_branch_is_preserved_not_retried_as_flake() {
    let id = identity();
    let mut f = Fake::default();
    advance(&mut f, &id, Stage::Preflight).unwrap();
    advance(&mut f, &id, Stage::Package).unwrap();
    f.fail = Some(Stage::Cert);
    assert_eq!(
        advance(&mut f, &id, Stage::Cert).unwrap_err().code,
        "fixture_failure"
    );
    f.fail = None;
    assert_eq!(
        advance(&mut f, &id, Stage::Cert).unwrap_err().code,
        "release_effect_unproven"
    );
    assert_eq!(f.calls.iter().filter(|s| **s == Stage::Cert).count(), 1);
    let saved = serde_json::to_value(f.state.as_ref().unwrap()).unwrap();
    assert_eq!(saved["failures"]["Cert"]["code"], "fixture_failure");
    assert_eq!(saved["failures"]["Cert"]["kind"], "observation");
    advance(&mut f, &id, Stage::Device).unwrap();
    assert!(advance(&mut f, &id, Stage::Soak).is_err());
}
#[test]
fn promotion_requires_all_gates_exact_ids_and_removed_test_approvals() {
    let id = identity();
    let mut f = Fake::default();
    for s in [
        Stage::Preflight,
        Stage::Package,
        Stage::Cert,
        Stage::Device,
        Stage::Soak,
        Stage::Proof,
        Stage::Publish,
        Stage::Download,
    ] {
        advance(&mut f, &id, s).unwrap();
    }
    let mut cert = f.receipts[&Stage::Cert].clone();
    cert.incidents.pop();
    f.receipts.insert(Stage::Cert, cert.clone());
    assert!(advance(&mut f, &id, Stage::Promote).is_err());
    assert!(!f.calls.contains(&Stage::Promote));
    cert.incidents = MANDATORY.iter().map(|s| s.to_string()).collect();
    cert.removed_tests = vec!["old::unique".into()];
    f.receipts.insert(Stage::Cert, cert.clone());
    assert!(advance(&mut f, &id, Stage::Promote).is_err());
    cert.approved_removals = vec!["old::unique".into()];
    f.receipts.insert(Stage::Cert, cert);
    let r = advance(&mut f, &id, Stage::Promote).unwrap();
    assert!(!r.release_qualified);
    assert_eq!(r.boundary, "fake_only");
    let promotion = promotion_receipt(&mut f, &id).unwrap();
    let saved = serde_json::to_vec(&promotion).unwrap();
    let loaded: PromotionReceipt = serde_json::from_slice(&saved).unwrap();
    assert_eq!(loaded.identity, id);
    assert_eq!(loaded.stages.len(), 9);
    assert!(!loaded.release_qualified);
    assert_eq!(loaded.boundary, "fake_only");
    assert!(loaded.stages.contains(&Stage::Promote));
    f.receipts
        .get_mut(&Stage::Download)
        .unwrap()
        .identity
        .archive = "f".repeat(64);
    assert!(promotion_receipt(&mut f, &id).is_err());
    assert_eq!(f.calls.iter().filter(|s| **s == Stage::Promote).count(), 1);
}
#[test]
fn denied_publication_and_promotion_never_emit_effects_or_receipts() {
    let id = identity();
    let mut f = Fake::default();
    assert!(promotion_receipt(&mut f, &id).is_err());
    assert!(f.calls.is_empty());
    for s in [
        Stage::Preflight,
        Stage::Package,
        Stage::Cert,
        Stage::Device,
        Stage::Soak,
        Stage::Proof,
    ] {
        advance(&mut f, &id, s).unwrap();
    }
    f.denied = true;
    assert_eq!(
        advance(&mut f, &id, Stage::Publish).unwrap_err().code,
        "release_owner_approval_required"
    );
    assert!(!f.calls.contains(&Stage::Publish));
    f.denied = false;
    advance(&mut f, &id, Stage::Publish).unwrap();
    advance(&mut f, &id, Stage::Download).unwrap();
    f.denied = true;
    assert_eq!(
        advance(&mut f, &id, Stage::Promote).unwrap_err().code,
        "release_owner_approval_required"
    );
    assert!(!f.calls.contains(&Stage::Promote));
    assert!(promotion_receipt(&mut f, &id).is_err());
}
#[test]
fn forged_completed_state_without_intent_or_dependency_refuses_before_effects() {
    for orphan in [Stage::Preflight, Stage::Package] {
        let id = identity();
        let mut f = Fake::default();
        advance(&mut f, &id, Stage::Preflight).unwrap();
        if orphan == Stage::Package {
            advance(&mut f, &id, Stage::Package).unwrap();
            f.state
                .as_mut()
                .unwrap()
                .completed
                .remove(&Stage::Preflight);
        } else {
            f.state.as_mut().unwrap().issued.clear();
        }
        let calls = f.calls.clone();
        assert_eq!(
            advance(&mut f, &id, orphan).unwrap_err().code,
            "release_state_invalid"
        );
        assert_eq!(f.calls, calls);
    }
}

#[test]
fn proof_publication_download_and_promotion_require_the_pinned_id_audit() {
    for target in [
        Stage::Proof,
        Stage::Publish,
        Stage::Download,
        Stage::Promote,
    ] {
        let id = identity();
        let mut f = Fake::default();
        for stage in [
            Stage::Preflight,
            Stage::Package,
            Stage::Cert,
            Stage::Device,
            Stage::Soak,
            Stage::Proof,
            Stage::Publish,
            Stage::Download,
        ] {
            if stage == target {
                break;
            }
            advance(&mut f, &id, stage).unwrap();
        }
        let before = f.calls.clone();
        f.missing_audit = true;
        assert_eq!(
            advance(&mut f, &id, target).unwrap_err().code,
            "release_audit_missing"
        );
        assert_eq!(f.calls, before);
    }
}
#[derive(Clone, Default)]
struct Aside {
    snapshot: Option<CandidateSlots>,
    moves: Vec<Slot>,
    race: bool,
    fired: bool,
    residue: bool,
}
fn slots() -> CandidateSlots {
    CandidateSlots {
        source: "a".repeat(40),
        generation: 7,
        root: Some("root".into()),
        parent: Some("parent".into()),
        branch: Some("branch".into()),
        log: Some("log".into()),
        unfired: true,
    }
}
impl AsideIo for Aside {
    fn inspect(&mut self) -> Result<CandidateSlots> {
        let mut s = self.snapshot.clone().unwrap();
        if self.fired {
            s.unfired = false;
        }
        if self.race {
            s.generation += 1;
        }
        Ok(s)
    }
    fn set_aside_exact(&mut self, expected: &CandidateSlots, slot: Slot) -> Result<()> {
        let s = self.snapshot.as_mut().unwrap();
        assert_eq!(s.generation, expected.generation);
        self.moves.push(slot);
        if !self.residue {
            match slot {
                Slot::Root => s.root = None,
                Slot::Parent => s.parent = None,
                Slot::Branch => s.branch = None,
                Slot::Log => s.log = None,
            }
        }
        Ok(())
    }
}
#[test]
fn same_sha_retry_clears_root_parent_branch_log_only_after_unfired_proof() {
    let original = slots();
    let mut f = Aside {
        snapshot: Some(original.clone()),
        ..Aside::default()
    };
    let receipt = set_aside(&mut f, &original).unwrap();
    assert_eq!(f.moves, [Slot::Root, Slot::Parent, Slot::Branch, Slot::Log]);
    let after = f.inspect().unwrap();
    assert!(
        after.root.is_none()
            && after.parent.is_none()
            && after.branch.is_none()
            && after.log.is_none()
    );
    assert_eq!(receipt.source, original.source);
    assert!(receipt.same_sha_retry_ready);
}
#[test]
fn fired_unknown_racing_and_residual_candidates_never_become_retry_ready() {
    for case in 0..4 {
        let original = slots();
        let mut f = Aside {
            snapshot: Some(original.clone()),
            fired: case == 0,
            race: case == 1,
            residue: case == 2,
            ..Aside::default()
        };
        if case == 3 {
            f.snapshot.as_mut().unwrap().unfired = false;
        }
        assert!(set_aside(&mut f, &original).is_err());
        if case != 2 {
            assert!(f.moves.is_empty());
        }
    }
}
