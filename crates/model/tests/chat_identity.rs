// SPDX-License-Identifier: AGPL-3.0-only
//! The Rust chat renderer equals the recorded Qwen3.6 template output byte for byte
//! (FX-L8-qwen36-tokenizer-identity: `capture_tokenizer.py` sha256 1ca0e11c..., fixture sha256
//! 41ce71fa..., oracle f4e366e9, transformers 4.57.6 / jinja2 3.1.6) and refuses what it does not port.
use model::chat::{Message, Role, py_strip, render};
use serde_json::Value;
use stage_contract::ErrorCode;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/qwen36-tokenizer-identity.json")).unwrap()
}
fn role(name: &str) -> Role {
    match name {
        "system" => Role::System,
        "user" => Role::User,
        "assistant" => Role::Assistant,
        other => panic!("fixture role {other}"),
    }
}
#[test]
fn every_recorded_case_renders_identically_with_and_without_thinking() {
    let fx = fixture();
    let cases = fx["cases"].as_object().unwrap();
    assert!(cases.len() >= 9, "fixture lost cases");
    let mut checks = 0;
    for (name, case) in cases {
        let messages: Vec<Message> = case["messages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| Message {
                role: role(m["role"].as_str().unwrap()),
                content: m["content"].as_str().unwrap(),
            })
            .collect();
        for (thinking, key) in [(true, "thinking"), (false, "no_thinking")] {
            assert_eq!(
                render(&messages, thinking).unwrap(),
                case[key]["prompt"].as_str().unwrap(),
                "{name} {key}"
            );
            checks += 1;
        }
    }
    assert_eq!(checks, cases.len() * 2);
}
#[test]
fn python_strip_differs_from_rust_trim_exactly_on_the_separator_controls() {
    assert_eq!(py_strip("\u{1c}\u{1f} x \u{2003}\u{a0}\u{85}"), "x");
    assert_eq!(
        "\u{1c}x".trim(),
        "\u{1c}x",
        "control: Rust trim keeps U+001C"
    );
}
#[test]
fn unsupported_template_features_are_refused_not_approximated() {
    let user = |content| Message {
        role: Role::User,
        content,
    };
    let refuse = |m: &[Message]| render(m, true).unwrap_err().code;
    assert_eq!(refuse(&[]), ErrorCode::Unsupported);
    assert_eq!(
        refuse(&[Message {
            role: Role::Assistant,
            content: "no user"
        }]),
        ErrorCode::Unsupported
    );
    assert_eq!(
        refuse(&[
            user("a"),
            Message {
                role: Role::System,
                content: "late"
            }
        ]),
        ErrorCode::Unsupported
    );
    assert_eq!(
        refuse(&[user("<tool_response>x</tool_response>")]),
        ErrorCode::Unsupported
    );
    assert_eq!(
        refuse(&[
            user("a"),
            Message {
                role: Role::Assistant,
                content: "<think>r</think>b"
            }
        ]),
        ErrorCode::Unsupported
    );
}
