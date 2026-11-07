// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{Result, site::*};
use serde_json::{Value, json};
use std::collections::BTreeMap;
#[derive(Default)]
struct Fake {
    requests: Vec<Request>,
}
impl Transport for Fake {
    fn get(&mut self, r: Request, _: Span) -> Result<(u16, Value)> {
        self.requests.push(r);
        Ok((200, json!({"healthy":true})))
    }
}
#[test]
fn site_verifier_never_sends_client_cf_headers_case_insensitively() {
    for name in ["CF-Connecting-IP", "cf-ray", "Cf-Visitor", "cF-Anything"] {
        let mut f = Fake::default();
        let result = verify(
            &mut f,
            "https://fixture.invalid/status",
            [(name.into(), "fixture".into())].into(),
            Span::from_millis(20_000),
        );
        assert_eq!(result.unwrap_err().code, "site_header_forbidden");
        assert!(f.requests.is_empty());
    }
    let mut f = Fake::default();
    assert_eq!(
        verify(
            &mut f,
            "https://fixture.invalid/status",
            BTreeMap::new(),
            Span::from_millis(20_000)
        )
        .unwrap(),
        json!({"healthy":true})
    );
    assert!(f.requests[0].headers.is_empty());
    assert!(!f.requests[0].follow_redirects);
}
#[test]
fn site_verifier_refuses_credential_forwarding_injection_and_unsafe_origins() {
    for (k, v) in [
        ("Cookie", "fixture"),
        ("Authorization", "fixture"),
        ("X-Native-Authorization", "fixture"),
        ("Accept\r\nCF-Ray", "fixture"),
        ("Accept", "text/plain\r\nCF-Ray: forged"),
    ] {
        let mut f = Fake::default();
        assert!(
            verify(
                &mut f,
                "https://fixture.invalid/status",
                [(k.into(), v.into())].into(),
                Span::from_millis(20_000)
            )
            .is_err()
        );
        assert!(f.requests.is_empty());
    }
    for url in [
        "http://fixture.invalid/status",
        "https://user:password@fixture.invalid/status",
        "https://fixture.invalid/status\r\nCF-Ray: forged",
    ] {
        let mut f = Fake::default();
        assert!(verify(&mut f, url, BTreeMap::new(), Span::from_millis(20_000)).is_err());
        assert!(f.requests.is_empty());
    }
    let mut f = Fake::default();
    assert!(
        verify(
            &mut f,
            "https://fixture.invalid/status",
            BTreeMap::new(),
            Span::ZERO
        )
        .is_err()
    );
    assert!(f.requests.is_empty());
}
