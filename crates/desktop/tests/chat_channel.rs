// SPDX-License-Identifier: AGPL-3.0-only
//! Header contract: mycelium_a13_macos/chat.py:562-567 @ f4e366e98a1f705dfaf182897044500d290dbd7f.
//! Only the three exact chat POST paths require Chat instead of Action.
use desktop::channel::{Channel, ChannelError, ORIGIN, Request};

const PATHS: [&str; 3] = ["/chat/api/state", "/chat/api/send", "/chat/api/events"];
fn chat(path: &str) -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: format!("{ORIGIN}{path}"),
        headers: vec![
            ("Content-Type".into(), "application/json".into()),
            ("X-Mycelium-Chat".into(), "1".into()),
        ],
        body: b"{}".to_vec(),
    }
}
#[test]
fn exact_chat_posts_need_chat_not_action_in_each_pinned_document() {
    let guard = Channel::default();
    for path in PATHS {
        let mut request = chat(path);
        for document in ["mycelium://localhost/", "mycelium://localhost/chat"] {
            assert_eq!(
                guard.authorize_native_document(&request, document),
                Ok(()),
                "{path}"
            );
        }
        assert_eq!(guard.authorize(&request), Err(ChannelError::Origin));
        request.headers.push(("Origin".into(), ORIGIN.into()));
        assert_eq!(guard.authorize(&request), Ok(()));
        // The oracle requires Chat; Action alone cannot substitute for it.
        request.headers[1].0 = "X-Mycelium-Action".into();
        assert_eq!(guard.authorize(&request), Err(ChannelError::Action));
    }
}
#[test]
fn chat_header_never_authorizes_non_chat_or_action_paths() {
    for path in [
        "/api/quit",
        "/api/serving/stop",
        "/chat/api/cancel",
        "/chat/api/state/",
        "/chat/api/send/more",
        "/CHAT/api/events",
        "/chat/api/events-extra",
    ] {
        assert_eq!(
            Channel::default().authorize_native_document(&chat(path), "mycelium://localhost/"),
            Err(ChannelError::Action),
            "{path}"
        );
    }
    let mut request = chat("/api/quit");
    request.headers.remove(1);
    assert_eq!(
        Channel::default().authorize_native_document(&request, "mycelium://localhost/"),
        Err(ChannelError::Action)
    );
}
#[test]
fn chat_keeps_foreign_window_and_native_document_refusals() {
    let guard = Channel::default();
    for path in PATHS {
        for window in ["frame", "popup", "foreign", "", "MAIN"] {
            let mut request = chat(path);
            request.window = window.into();
            assert_eq!(
                guard.authorize_native_document(&request, "mycelium://localhost/chat"),
                Err(ChannelError::Window)
            );
        }
        for document in [
            "about:srcdoc",
            "about:blank",
            "data:text/html,chat",
            "blob:mycelium://localhost/id",
            "https://example.invalid/chat",
            "mycelium://localhost/frame",
            "mycelium://localhost/chat/",
        ] {
            assert_eq!(
                guard.authorize_native_document(&chat(path), document),
                Err(ChannelError::Origin)
            );
        }
    }
}
#[test]
fn chat_keeps_supplied_origin_content_type_and_duplicate_header_refusals() {
    let guard = Channel::default();
    for path in PATHS {
        for origins in [
            vec!["null"],
            vec![""],
            vec!["https://example.invalid"],
            vec![ORIGIN, ORIGIN],
            vec![ORIGIN, "null"],
        ] {
            let mut request = chat(path);
            request
                .headers
                .extend(origins.into_iter().map(|v| ("Origin".into(), v.into())));
            assert_eq!(
                guard.authorize_native_document(&request, "mycelium://localhost/chat"),
                Err(ChannelError::Origin)
            );
        }
        for value in ["", "0", "1, 1", "true"] {
            let mut request = chat(path);
            request.headers[1].1 = value.into();
            assert_eq!(
                guard.authorize_native_document(&request, "mycelium://localhost/chat"),
                Err(ChannelError::Action)
            );
        }
        let mut request = chat(path);
        request.headers.push(("x-mycelium-chat".into(), "1".into()));
        assert_eq!(
            guard.authorize_native_document(&request, "mycelium://localhost/chat"),
            Err(ChannelError::Action)
        );
        let mut request = chat(path);
        request.headers[0].1 = "text/plain".into();
        assert_eq!(
            guard.authorize_native_document(&request, "mycelium://localhost/chat"),
            Err(ChannelError::ContentType)
        );
    }
}
#[test]
fn chat_keeps_path_method_and_revocation_checks() {
    let mut guard = Channel::default();
    for path in [
        "/chat/api/%73end",
        "/chat/api/../api/send",
        "/chat/api/send?x=1",
        "/chat/api/events#x",
        "//chat/api/send",
    ] {
        assert_eq!(
            guard.authorize_native_document(&chat(path), "mycelium://localhost/chat"),
            Err(ChannelError::Path)
        );
    }
    let mut request = chat(PATHS[0]);
    request.method = "OPTIONS".into();
    assert_eq!(
        guard.authorize_native_document(&request, "mycelium://localhost/chat"),
        Err(ChannelError::Method)
    );
    guard.revoke();
    assert_eq!(
        guard.authorize_native_document(&chat(PATHS[0]), "mycelium://localhost/chat"),
        Err(ChannelError::Closed)
    );
}
