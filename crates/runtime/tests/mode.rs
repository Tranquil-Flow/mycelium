// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-CHAT-12 (normal/fast selection: baseline default, saved preference and explicit override; invalid input never silently falls back; CL-L9-04).
//!
use runtime::mode::*;
use serde_json::json;
#[test]
fn baseline_default_saved_preference_and_explicit_override() {
    assert_eq!(
        select(None, None).unwrap(),
        Selection {
            mode: Mode::Normal,
            source: ModeSource::Default,
            locked: false
        }
    );
    for enabled in [false, true] {
        let saved = json!({"protocol":"mycelium.a13_fast_mode.v1","enabled":enabled});
        assert_eq!(
            select(None, Some(&saved)).unwrap(),
            Selection {
                mode: if enabled { Mode::Fast } else { Mode::Normal },
                source: ModeSource::Saved,
                locked: false
            }
        );
        for (value, want) in [("on", Mode::Fast), ("off", Mode::Normal)] {
            assert_eq!(
                select(Some(value), Some(&saved)).unwrap(),
                Selection {
                    mode: want,
                    source: ModeSource::Environment,
                    locked: true
                }
            );
        }
    }
}
#[test]
fn invalid_override_and_state_never_silently_fallback() {
    assert_eq!(
        select(Some("ON"), None).unwrap_err().0,
        "token_path_activation_invalid"
    );
    for saved in [
        json!({}),
        json!({"protocol":"wrong","enabled":false}),
        json!({"protocol":"mycelium.a13_fast_mode.v1","enabled":1}),
        json!({"protocol":"mycelium.a13_fast_mode.v1","enabled":true,"extra":0}),
    ] {
        assert_eq!(
            select(None, Some(&saved)).unwrap_err().0,
            "fast_mode_state_invalid"
        );
        assert_eq!(select(Some("on"), Some(&saved)).unwrap().mode, Mode::Fast);
    }
}
