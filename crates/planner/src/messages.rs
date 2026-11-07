// SPDX-License-Identifier: AGPL-3.0-only
//! Human refusal texts of the A13 resource check (`model_resources.py:449-510`, `_MESSAGES`,
//! `refusal_message`, `cap_refusal_message`, `_gb`), byte for byte, so an owner reading the refusal sees the
//! same Mac, layers and numbers v0.1.4 shows. Executed-oracle replay: `tests/chain_compile.rs`.
use crate::{Error, placement::Member};
use serde_json::Value;

const GENERIC: &str = "A Mac cannot hold its share of this model.";
const MEMORY_INSUFFICIENT: &str = "{who} has {memory} of memory. Its share of {model} (layers {first}-{last}) needs about {need} at its load peak, more than the {ceiling} this app lets a Mac with that memory use. Add a Mac with more memory, or choose a smaller model.";
const DISK_INSUFFICIENT: &str = "{who} has {free} of free disk. Its share of {model} (layers {first}-{last}) needs about {need} on disk for this run. Free some disk space on that Mac, or choose a smaller model.";
const MEMORY_CAP: &str = "{who} agreed to at most {cap} of memory per run, but its share of {model} (layers {first}-{last}) needs {need}. Raise that Mac's memory limit (rejoin with a larger limit), add a Mac, or choose a smaller model.";
const STORAGE_CAP: &str = "{who} agreed to at most {cap} of storage, but its share of {model} (layers {first}-{last}) needs {need}. Raise that Mac's storage limit, add a Mac, or choose a smaller model.";
const CANNOT_HOLD: &str = "{who} cannot hold even the smallest share of {model} within half of its memory. Add a Mac with more memory, or choose a smaller model.";

/// `_gb`: `"?"` for a missing value, else `value / 1e9` to one decimal.
fn gb(v: Option<u64>) -> String {
    match v {
        None => "?".into(),
        Some(v) => format!("{:.1} GB", v as f64 / 1e9),
    }
}

fn who(node: Option<&str>) -> String {
    match node {
        Some(n) => format!("The Mac {}", n.chars().take(12).collect::<String>()),
        None => "A Mac".into(),
    }
}

struct Fill {
    who: String,
    model: String,
    first: String,
    last: String,
    memory: String,
    ceiling: String,
    cap: String,
    free: String,
    need: String,
}

fn render(template: &str, f: &Fill) -> String {
    template
        .replace("{who}", &f.who)
        .replace("{model}", &f.model)
        .replace("{first}", &f.first)
        .replace("{last}", &f.last)
        .replace("{memory}", &f.memory)
        .replace("{ceiling}", &f.ceiling)
        .replace("{cap}", &f.cap)
        .replace("{free}", &f.free)
        .replace("{need}", &f.need)
}

/// Message of a refusal that has no resource row (`SplitRefused` raised by the split itself): only
/// `member_cannot_hold_share` has a template, every other code reads the generic sentence.
pub(crate) fn split_refusal(code: &str, node: Option<&str>, label: &str) -> String {
    if code != "member_cannot_hold_share" {
        return GENERIC.into();
    }
    render(
        CANNOT_HOLD,
        &Fill {
            who: who(node),
            model: label.into(),
            first: "1".into(),
            last: "?".into(),
            memory: gb(None),
            ceiling: gb(None),
            cap: gb(None),
            free: gb(None),
            need: gb(None),
        },
    )
}

/// Attach the oracle's text to a stage refusal (`check_members` / `cap_refusal_message`).
pub(crate) fn annotate(mut e: Error, label: &str, row: &Value, m: &Member) -> Error {
    let u = |k: &str| row[k].as_u64();
    let caps = m.caps.as_ref();
    let (template, cap, free, need) = match e.code {
        "member_memory_cap_below_share" => (
            MEMORY_CAP,
            caps.and_then(|c| c.max_memory_bytes),
            None,
            u("max_rss_bytes"),
        ),
        "member_storage_cap_below_share" => (
            STORAGE_CAP,
            caps.and_then(|c| c.storage_bytes),
            None,
            u("max_disk_bytes"),
        ),
        "member_memory_insufficient_for_share" => {
            (MEMORY_INSUFFICIENT, None, None, u("max_rss_bytes"))
        }
        "member_disk_insufficient_for_share" => (
            DISK_INSUFFICIENT,
            None,
            m.available_storage_bytes,
            u("disk_footprint_bytes"),
        ),
        _ => return e,
    };
    let fill = Fill {
        who: who(Some(&m.node_id)),
        model: label.into(),
        first: (u("start").unwrap_or(0) + 1).to_string(),
        last: u("end").map_or("?".into(), |v| v.to_string()),
        memory: gb(u("memory_bytes")),
        ceiling: gb(u("memory_ceiling_bytes")),
        cap: gb(cap),
        free: gb(free),
        need: gb(need),
    };
    e.detail = Some(render(template, &fill).into());
    e
}
