// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Span};
use desktop::{channel::*, lifecycle::*, platform::*};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

// C-1: setup alone is not an AppleEvent registration/run-loop readiness receipt.
#[test]
fn c1_readiness_requires_native_ready_and_a_later_event_barrier() {
    let mut r = Readiness::default();
    assert!(!r.is_ready());
    r.event_barrier();
    assert!(!r.is_ready());
    r.native_ready();
    assert!(!r.is_ready());
    r.event_barrier();
    assert!(r.is_ready());
}
struct Fake {
    count: Arc<AtomicUsize>,
    answer: Poll,
}
impl Cleanup for Fake {
    fn begin(&mut self, _deadline: Span) -> Result<(), CleanupError> {
        self.count.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Ok(self.answer)
    }
}
fn shutdown(answer: Poll) -> (Shutdown<Fake>, Arc<AtomicUsize>, Span) {
    let count = Arc::new(AtomicUsize::new(0));
    // Harness budget only; product deadline is supplied by L6/L1, never this value.
    let budget = BudgetId::RouterClose.span(budgets::Entries::ZERO).unwrap();
    (
        Shutdown::new(
            Fake {
                count: count.clone(),
                answer,
            },
            budget,
        )
        .unwrap(),
        count,
        budget,
    )
}
#[test]
fn f16_all_quit_sources_share_one_cleanup_even_before_ready() {
    let (mut s, count, _) = shutdown(Poll::Verified);
    for source in [
        QuitSource::Menu,
        QuitSource::CommandQ,
        QuitSource::AppleEvent,
        QuitSource::WebUi,
    ] {
        s.request(source, Span::ZERO);
    }
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert_eq!(s.tick(Span::ZERO), State::Verified);
}
#[test]
fn f16_hung_cleanup_expires_without_permission_to_exit_or_retry() {
    let (mut s, count, budget) = shutdown(Poll::Pending);
    s.request(QuitSource::AppleEvent, Span::ZERO);
    assert_eq!(s.tick(Span::ZERO), State::Stopping);
    assert_eq!(s.tick(budget), State::Failed(CleanupError::Deadline));
    s.request(QuitSource::Menu, budget);
    assert_eq!(count.load(Ordering::SeqCst), 1);
    assert!(!s.can_exit());
}
/// v0.1.4 `__main__.py:219-223 @ f4e366e9`: a Quit that leaves a recorded cleanup error (a close
/// step raised) returns 3 (`launcher_cleanup_failed` / `cleanup_incomplete`); otherwise 0.
/// Executed: `SLICE-1-evidence/r8-oracle/launcher-{proven,unproven}.log`. While the cleanup is
/// running there is no status yet.
///
/// RU-032 / SE-SLICE1-L11-QUIT-JOIN-60 corrects the round-10 reading of the allowance running out:
/// `demo_app.py:202-203 @ f4e366e9` is `run.stop(); run.join(60)` with NO check of the join's
/// result, so a teardown that outlasts the join is NOT a cleanup error and the launcher exits 0
/// (executed `SLICE-1-evidence/r10-oracle/` q4: `exit_code` 0, `quit_to_exit_s` 60.01,
/// `run_state_after` "stopping", stderr empty). The round-10 assertion `Failed(Deadline)` ->
/// `Some(3)` moved here as `Some(0)`; the exit-3 assertions it stood for are kept below for the
/// failures that really are a close step raising (begin/poll errors).
#[test]
fn quit_exit_status_is_the_v014_launcher_status() {
    use desktop::lifecycle::{CLEANUP_INCOMPLETE_EVENT, EXIT_CLEANUP_INCOMPLETE};
    let (mut s, _, _) = shutdown(Poll::Verified);
    assert_eq!(s.exit_status(), None, "running");
    s.request(QuitSource::Menu, Span::ZERO);
    assert_eq!(s.exit_status(), None, "stopping");
    assert_eq!(s.tick(Span::ZERO), State::Verified);
    assert_eq!(s.exit_status(), Some(0));
    let (mut s, _, budget_hung) = shutdown(Poll::Pending);
    s.request(QuitSource::AppleEvent, Span::ZERO);
    assert_eq!(s.tick(Span::ZERO), State::Stopping);
    assert_eq!(s.exit_status(), None, "pending is not a result");
    assert_eq!(s.tick(budget_hung), State::Failed(CleanupError::Deadline));
    assert_eq!(
        s.exit_status(),
        Some(0),
        "q4: a teardown still running at the join allowance exits 0 as v0.1.4 does"
    );
    assert!(
        !s.can_exit(),
        "F16: a failed cleanup is still not a verified exit"
    );
    assert_eq!(EXIT_CLEANUP_INCOMPLETE, 3);
    assert_eq!(
        CLEANUP_INCOMPLETE_EVENT,
        r#"{"event": "launcher_cleanup_failed", "code": "cleanup_incomplete", "state_retained": true}"#
    );
}
/// The exit 3 stays where v0.1.4 has it: a close step raised. Begin refused / receipt poll failed.
struct Raising {
    begin: Option<CleanupError>,
    poll: Option<CleanupError>,
}
impl Cleanup for Raising {
    fn begin(&mut self, _deadline: Span) -> Result<(), CleanupError> {
        self.begin.map_or(Ok(()), Err)
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        self.poll.map_or(Ok(Poll::Pending), Err)
    }
}
#[test]
fn a_close_step_that_raises_still_exits_3() {
    let budget = BudgetId::RouterClose.span(budgets::Entries::ZERO).unwrap();
    let mut begin_failed = Shutdown::new(
        Raising {
            begin: Some(CleanupError::Begin),
            poll: None,
        },
        budget,
    )
    .unwrap();
    begin_failed.request(QuitSource::Menu, Span::ZERO);
    assert_eq!(begin_failed.state(), State::Failed(CleanupError::Begin));
    assert_eq!(begin_failed.exit_status(), Some(3));
    let mut poll_failed = Shutdown::new(
        Raising {
            begin: None,
            poll: Some(CleanupError::Poll),
        },
        budget,
    )
    .unwrap();
    poll_failed.request(QuitSource::CommandQ, Span::ZERO);
    assert_eq!(
        poll_failed.tick(Span::ZERO),
        State::Failed(CleanupError::Poll)
    );
    assert_eq!(poll_failed.exit_status(), Some(3));
}
/// RU-032: the packaged and development shells give Quit the L1 id
/// `PyMyceliumA13MacosDemoAppCloseRunJoinL203` (`run.join(60)`), not the stage-cleanup or router
/// budgets they used before. Pins every production call site (source text: the packaged
/// executable cannot be launched off-bundle) to the one `quit_allowance()` and refuses a
/// re-introduced stage/router budget in those files.
#[test]
fn every_production_shell_entry_uses_the_one_quit_allowance() {
    let manifest = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    for relative in [
        "src/bin/mycelium.rs",
        "src/bin/dev.rs",
        "../node-service/src/bin/mycelium.rs",
        "../node-service/src/desktop_host.rs",
    ] {
        let source = std::fs::read_to_string(manifest.join(relative)).unwrap();
        assert!(
            source.contains("quit_allowance()"),
            "{relative} must take its Quit allowance from lifecycle::quit_allowance()"
        );
        for other in ["BudgetId::RouterClose", "BudgetId::StageCleanup"] {
            assert!(
                !source.contains(other),
                "{relative} must not give Quit the {other} budget (RU-032)"
            );
        }
    }
}
#[test]
fn platform_preserves_oracle_exact_legacy_exception() {
    for version in ["15.6.1", "26.0", "26.5.1", "27.0"] {
        assert_eq!(admit("macos", "aarch64", version), Ok(()), "{version}");
    }
    for version in ["15.6", "15.6.2", "25.9", "26", "26.x", "26.0.0.0", "1000.0"] {
        assert_eq!(
            admit("macos", "aarch64", version),
            Err(PlatformError::UnsupportedOs)
        );
    }
    assert_eq!(
        admit("linux", "aarch64", "26.0"),
        Err(PlatformError::UnsupportedPlatform)
    );
    assert_eq!(
        admit("macos", "x86_64", "26.0"),
        Err(PlatformError::UnsupportedArchitecture)
    );
}
fn request() -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: "mycelium://localhost/api/quit".into(),
        headers: vec![
            ("origin".into(), ORIGIN.into()),
            ("x-mycelium-action".into(), "1".into()),
            ("content-type".into(), "application/json".into()),
        ],
        body: b"{}".to_vec(),
    }
}
#[test]
fn channel_allows_trusted_window_but_refuses_browser_assumptions() {
    let p = Channel::default();
    assert_eq!(p.authorize(&request()), Ok(()));
    for origin in [
        "null",
        "http://127.0.0.1:9000",
        "tauri://localhost",
        "https://evil.invalid",
        "mycelium://localhost.evil",
    ] {
        let mut r = request();
        r.headers[0].1 = origin.into();
        assert_eq!(p.authorize(&r), Err(ChannelError::Origin));
    }
    let mut r = request();
    r.headers.remove(0);
    assert_eq!(p.authorize(&r), Err(ChannelError::Origin));
    let mut r = request();
    r.window = "foreign".into();
    assert_eq!(p.authorize(&r), Err(ChannelError::Window));
}
#[test]
fn channel_rejects_duplicate_headers_paths_and_replay_after_close() {
    let mut p = Channel::default();
    let mut r = request();
    r.headers.push(("Origin".into(), ORIGIN.into()));
    assert_eq!(p.authorize(&r), Err(ChannelError::Origin));
    for uri in [
        "mycelium://localhost/api/../quit",
        "mycelium://localhost/api/%71uit",
        "mycelium://localhost/api/quit?token=x",
        "mycelium://user@localhost/api/quit",
        "http://localhost/api/quit",
    ] {
        let mut r = request();
        r.uri = uri.into();
        assert_eq!(p.authorize(&r), Err(ChannelError::Path));
    }
    p.revoke();
    assert_eq!(p.authorize(&request()), Err(ChannelError::Closed));
}
#[test]
fn native_missing_origin_requires_verified_document_not_a_browser_exception() {
    let p = Channel::default();
    let mut r = request();
    r.headers.remove(0);
    assert_eq!(p.authorize(&r), Err(ChannelError::Origin));
    assert_eq!(
        p.authorize_native_document(&r, "mycelium://localhost/"),
        Ok(())
    );
    for source in [
        "https://evil.invalid/",
        "about:blank",
        "file:///index.html",
        "mycelium://localhost.evil/",
        "mycelium://localhost/api/quit",
    ] {
        assert_eq!(
            p.authorize_native_document(&r, source),
            Err(ChannelError::Origin)
        );
    }
    r.headers.push(("origin".into(), "null".into()));
    assert_eq!(
        p.authorize_native_document(&r, "mycelium://localhost/"),
        Err(ChannelError::Origin)
    );
}

#[test]
fn navigation_never_opens_remote_file_or_bootstrap_endpoints() {
    for uri in ["mycelium://localhost/", "mycelium://localhost/chat"] {
        assert!(navigation_allowed(uri));
    }
    for uri in [
        "https://example.org/",
        "file:///etc/passwd",
        "mycelium://localhost/session",
        "mycelium://localhost/native-session",
        "mycelium://localhost/api/quit",
    ] {
        assert!(!navigation_allowed(uri));
    }
}
