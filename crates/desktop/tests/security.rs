// SPDX-License-Identifier: AGPL-3.0-only
use desktop::channel::{CSP, Channel, ChannelError, ORIGIN, Request, navigation_allowed};

fn post() -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: "mycelium://localhost/api/security-probe".into(),
        headers: vec![
            ("content-type".into(), "application/json".into()),
            ("x-mycelium-action".into(), "1".into()),
        ],
        body: b"{}".to_vec(),
    }
}
#[test]
fn pinned_document_csp_has_explicit_child_and_active_content_restrictions() {
    let directives: Vec<_> = CSP.split(';').map(str::trim).collect();
    for expected in [
        "default-src 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "worker-src 'none'",
        "frame-ancestors 'none'",
        "img-src 'self'",
    ] {
        assert!(directives.contains(&expected), "missing {expected}");
    }
    let config: serde_json::Value =
        serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
    assert_eq!(config["app"]["security"]["csp"], CSP);
}
#[test]
fn native_document_adversarial_url_and_window_matrix() {
    let guard = Channel::default();
    for pinned in ["mycelium://localhost/", "mycelium://localhost/chat"] {
        assert_eq!(guard.authorize_native_document(&post(), pinned), Ok(()));
        for window in ["popup", "frame", "", "MAIN"] {
            let mut request = post();
            request.window = window.into();
            assert_eq!(
                guard.authorize_native_document(&request, pinned),
                Err(ChannelError::Window)
            );
        }
    }
    // URL supplied ONLY by the native adapter; these cannot grant the capability.
    for url in [
        "about:blank",
        "about:srcdoc",
        "data:text/html,attack",
        "blob:mycelium://localhost/id",
        "blob:https://example.org/id",
        "https://example.org/redirect",
        "mycelium://localhost/redirect",
        "mycelium://localhost/frame",
        "mycelium://localhost/chat/",
        "mycelium://localhost/?next=/",
        "mycelium://localhost/#frame",
        "mycelium://localhost/%2e",
        "mycelium://localhost/./",
        "mycelium://localhost.evil/",
        "mycelium://evil@localhost/",
        "file:///index.html",
    ] {
        assert!(!navigation_allowed(url), "{url}");
        assert_eq!(
            guard.authorize_native_document(&post(), url),
            Err(ChannelError::Origin),
            "{url}"
        );
    }
}
#[test]
fn supplied_origin_always_preserves_strict_guard_regressions() {
    let guard = Channel::default();
    assert_eq!(guard.authorize(&post()), Err(ChannelError::Origin));
    for (values, expected) in [
        (vec![ORIGIN], Ok(())),
        (vec!["null"], Err(ChannelError::Origin)),
        (vec![""], Err(ChannelError::Origin)),
        (vec!["https://example.org"], Err(ChannelError::Origin)),
        (vec![ORIGIN, ORIGIN], Err(ChannelError::Origin)),
        (vec![ORIGIN, "null"], Err(ChannelError::Origin)),
    ] {
        let mut request = post();
        for value in values {
            request.headers.push(("Origin".into(), value.into()));
        }
        assert_eq!(
            guard.authorize_native_document(&request, "mycelium://localhost/"),
            expected
        );
        assert_eq!(guard.authorize(&request), expected);
    }
}
