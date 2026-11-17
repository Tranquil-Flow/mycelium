// SPDX-License-Identifier: AGPL-3.0-only
//! The invite card renders `<img src="/api/pair/window/qr/{window_id}.svg">` (run.js `renderQr`), and
//! nothing served that path: `PairingWindows::handle` answers only `request`/`poll`, `actions.rs` has
//! no QR action, and the one source hit for the route was the auth allowlist. So every invite QR in
//! the app was a broken image -- which is exactly what the owner reported ("make sure the QR codes
//! properly work as I think they still broken").
use api::pairing_windows::PairingWindows;
use serde_json::json;

fn open_window(windows: &PairingWindows) -> serde_json::Value {
    windows
        .open(
            &json!("https://demo.mycelium.now"),
            &json!("sha256:cfaeb52e233a7837ba2166a0bafcacab9ce8b09feda8bde80d262811e30f5b0f"),
            &json!(600),
        )
        .expect("open")
        .to_value()
}

#[test]
fn an_open_window_publishes_its_join_link_as_an_svg() {
    let windows = PairingWindows::system();
    let opened = open_window(&windows);
    let id = opened["window_id"].as_str().expect("id").to_owned();
    let svg = windows
        .link_svg(&json!(id))
        .expect("svg for an open window");
    let text = String::from_utf8(svg).expect("utf8");
    assert!(
        text.contains("<svg"),
        "not an svg: {}",
        &text[..40.min(text.len())]
    );
}

#[test]
fn an_unknown_or_closed_window_has_no_qr() {
    let windows = PairingWindows::system();
    assert!(
        windows.link_svg(&json!("ZZZZ")).is_none(),
        "unknown id must not render"
    );
    let opened = open_window(&windows);
    let id = opened["window_id"].as_str().expect("id").to_owned();
    windows.revoke(&json!(id)).expect("revoke");
    assert!(
        windows.link_svg(&json!(id)).is_none(),
        "a revoked window has no join link"
    );
}
