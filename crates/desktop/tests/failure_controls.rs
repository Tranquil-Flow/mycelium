// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Entries, Span};
use desktop::{channel::*, lifecycle::*};
struct Failing {
    begin_fails: bool,
}
impl Cleanup for Failing {
    fn begin(&mut self, _: Span) -> Result<(), CleanupError> {
        if self.begin_fails {
            Err(CleanupError::Begin)
        } else {
            Ok(())
        }
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        Err(CleanupError::Poll)
    }
}
#[test]
fn f16_cleanup_errors_remain_terminal_and_observable() {
    let budget = BudgetId::RouterClose.span(Entries::ZERO).unwrap();
    for (begin_fails, error) in [(true, CleanupError::Begin), (false, CleanupError::Poll)] {
        let mut state = Shutdown::new(Failing { begin_fails }, budget).unwrap();
        state.request(QuitSource::Menu, Span::ZERO);
        assert_eq!(state.tick(Span::ZERO), State::Failed(error));
        state.request(QuitSource::AppleEvent, Span::ZERO);
        assert_eq!(state.tick(budget), State::Failed(error));
        assert!(!state.can_exit());
    }
}
#[test]
fn clock_overflow_and_zero_budget_refuse_before_cleanup() {
    assert!(matches!(
        Shutdown::new(Failing { begin_fails: true }, Span::ZERO),
        Err(CleanupError::InvalidBudget)
    ));
    let budget = BudgetId::RouterClose.span(Entries::ZERO).unwrap();
    let mut state = Shutdown::new(Failing { begin_fails: true }, budget).unwrap();
    state.request(QuitSource::CommandQ, Span::from_millis(u64::MAX));
    assert_eq!(state.state(), State::Failed(CleanupError::Clock));
}
#[cfg(feature = "tauri-shell")]
#[test]
fn production_entry_rejects_zero_budget_before_calling_native_factory() {
    let result = desktop::shell::run_packaged::<Failing, _>(
        &desktop::package::Manifest {
            schema: 1,
            entries: Vec::new(),
        },
        |_| panic!("native factory must not run before admission"),
        Span::ZERO,
    );
    assert_eq!(result, Err("cleanup:InvalidBudget".into()));
}

#[cfg(all(feature = "tauri-shell", target_os = "macos"))]
#[test]
fn production_entry_refuses_unbundled_executable_before_native_factory() {
    let result = desktop::shell::run_packaged::<Failing, _>(
        &desktop::package::Manifest {
            schema: 1,
            entries: Vec::new(),
        },
        |_| panic!("native factory must not run before payload admission"),
        BudgetId::RouterClose.span(Entries::ZERO).unwrap(),
    );
    assert_eq!(result, Err("payload:ExecutableLayout".into()));
}

fn post() -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: "mycelium://localhost/api/action".into(),
        headers: vec![
            ("Origin".into(), ORIGIN.into()),
            ("X-Mycelium-Action".into(), "1".into()),
            ("Content-Type".into(), "application/json".into()),
        ],
        body: b"{}".to_vec(),
    }
}
#[test]
fn channel_content_method_size_and_duplicate_action_refusals() {
    let channel = Channel::default();
    let mut r = post();
    r.headers[1].1 = "0".into();
    assert_eq!(channel.authorize(&r), Err(ChannelError::Action));
    let mut r = post();
    r.headers.push(("x-mycelium-action".into(), "1".into()));
    assert_eq!(channel.authorize(&r), Err(ChannelError::Action));
    let mut r = post();
    r.headers[2].1 = "text/plain".into();
    assert_eq!(channel.authorize(&r), Err(ChannelError::ContentType));
    let mut r = post();
    r.method = "OPTIONS".into();
    assert_eq!(channel.authorize(&r), Err(ChannelError::Method));
    let mut r = post();
    r.body.resize(65_537, b'x');
    assert_eq!(channel.authorize(&r), Err(ChannelError::Body));
    for endpoint in ["session", "native-session"] {
        let mut r = post();
        r.uri = format!("{ORIGIN}/{endpoint}");
        assert_eq!(
            channel.authorize_native_document(&r, "mycelium://localhost/"),
            Err(ChannelError::Path)
        );
    }
}
