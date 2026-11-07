// SPDX-License-Identifier: AGPL-3.0-only
//! RU-046 static launch graph. Scope = code the shipped app can execute = the frozen PYZ closure plus every
//! scoped file reachable from it by a reference (import, dotted name, path, basename, `-m` package string or a
//! bare name in a parenthesised import). A file no executed file references is `unreached`: shipped as runtime
//! source but never executed by the app, so its pending rows resolve as `not-executed`. The reference rules
//! are deliberately broad (docstrings and string literals count; only whole-line `#` comments do not) so that
//! the answer errs towards "in scope". `python-launch-graph.tsv` is re-derived here from the oracle blobs and
//! the package census; it is a fixpoint, so the order of discovery does not matter.
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budget_model::inventory::adjudication as adj;
use std::collections::{BTreeMap, BTreeSet};

const CENSUS: &str = include_str!("../../obligations/data/package-census.json");
const SCOPE: &str = include_str!("../data/python-scope.tsv");

fn ident(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// Occurrences of `needle` not preceded or followed by an identifier character.
fn bounded(hay: &str, needle: &str) -> Vec<usize> {
    let bytes = hay.as_bytes();
    hay.match_indices(needle)
        .map(|(i, _)| i)
        .filter(|&i| {
            (i == 0 || !ident(bytes[i - 1]))
                && bytes.get(i + needle.len()).is_none_or(|b| !ident(*b))
        })
        .collect()
}

fn dotted(path: &str) -> String {
    let m = path.trim_end_matches(".py").replace('/', ".");
    m.strip_suffix(".__init__")
        .map_or(m.clone(), str::to_string)
}

/// The single PyInstaller entry script (`Analysis([APP/__main__.py])`; carchive member `Demo!/__main__`).
const ENTRY: &str = "mycelium_a13_macos/__main__.py";

fn pyz_frozen(path: &str) -> bool {
    CENSUS.contains(&format!("PYZ.pyz!/{}\"", dotted(path)))
}

/// The rule (if any) by which one source line references file `target`.
fn rule_of(line: &str, target: &str) -> Option<&'static str> {
    if line.trim_start().starts_with('#') {
        return None;
    }
    let parts: Vec<&str> = target.trim_end_matches(".py").split('/').collect();
    let stem = parts[parts.len() - 1];
    let pkg = parts[..parts.len() - 1].join(".");
    if !bounded(line, &dotted(target)).is_empty() {
        return Some("dotted");
    }
    if line.contains(target) {
        return Some("path");
    }
    if !bounded(line, &format!("{stem}.py")).is_empty() {
        return Some("basename");
    }
    if stem == "__main__" && !pkg.is_empty() {
        for open in ['\'', '"'] {
            for close in ['\'', '"'] {
                if line.contains(&format!("{open}{pkg}{close}")) {
                    return Some("main-pkg");
                }
            }
        }
    }
    if stem != "__main__" && stem != "__init__" {
        let t = line.trim_start_matches([' ', '\t']);
        let rest = ["from", "import"].iter().find_map(|kw| {
            t.strip_prefix(kw)
                .filter(|r| r.starts_with([' ', '\t']))
                .map(|r| &r[1..])
        });
        if let Some(rest) = rest
            && let Some(&at) = bounded(rest, stem).last()
        {
            let upto = &line[..line.len() - rest.len() + at + stem.len()];
            let from_dot = upto.trim_start().strip_prefix("from").is_some_and(|r| {
                r.starts_with(char::is_whitespace) && r.trim_start().starts_with('.')
            });
            if pkg.is_empty() || upto.contains(&pkg) || from_dot {
                return Some("import-line");
            }
        }
        let t = line.trim().trim_end_matches(',').trim_end();
        if t == stem || t.strip_prefix(stem).is_some_and(|r| r.starts_with(" as ")) {
            return Some("bare-name");
        }
    }
    None
}

/// Cheap necessary condition for any rule to fire in `text` (every rule needs one of these substrings).
fn may_reference(text: &str, target: &str) -> bool {
    let parts: Vec<&str> = target.trim_end_matches(".py").split('/').collect();
    let stem = parts[parts.len() - 1];
    let pkg = parts[..parts.len() - 1].join(".");
    match stem {
        "__init__" => text.contains(&dotted(target)) || text.contains("__init__.py"),
        "__main__" => pkg.is_empty() || text.contains(&pkg) || text.contains("__main__"),
        _ => text.contains(stem),
    }
}

struct Graph {
    status: BTreeMap<String, String>,
}

fn paths() -> Vec<String> {
    SCOPE
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap().to_string())
        .collect()
}

fn table() -> BTreeMap<String, (String, String, usize, String)> {
    adj::parse(&adj::TABLES).unwrap().launch
}

fn recompute(texts: &BTreeMap<String, String>) -> Graph {
    let all = paths();
    let mut status: BTreeMap<String, String> = all
        .iter()
        .map(|p| {
            let s = if pyz_frozen(p) {
                "frozen"
            } else if p == ENTRY {
                "entry"
            } else {
                "unreached"
            };
            (p.clone(), s.to_string())
        })
        .collect();
    loop {
        let executed: Vec<&String> = all.iter().filter(|p| status[*p] != "unreached").collect();
        let mut added = Vec::new();
        for target in all.iter().filter(|p| status[*p] == "unreached") {
            let hit = executed.iter().any(|from| {
                *from != target
                    && may_reference(&texts[*from], target)
                    && texts[*from]
                        .split('\n')
                        .any(|l| rule_of(l, target).is_some())
            });
            if hit {
                added.push(target.clone());
            }
        }
        if added.is_empty() {
            break;
        }
        for p in added {
            status.insert(p, "reached".into());
        }
    }
    Graph { status }
}

#[test]
fn table_covers_all_scoped_files_and_frozen_equals_the_pyz_census() {
    let all = paths();
    assert_eq!(all.len(), 725);
    let t = table();
    assert_eq!(t.keys().cloned().collect::<Vec<_>>(), {
        let mut s = all.clone();
        s.sort();
        s
    });
    let frozen: BTreeSet<&String> = all.iter().filter(|p| pyz_frozen(p)).collect();
    assert_eq!(frozen.len(), 435, "frozen into the app PYZ");
    for (path, (status, ..)) in &t {
        assert_eq!(status == "frozen", frozen.contains(path), "{path}");
    }
    assert!(CENSUS.contains("\"source_sha\": \"f4e366e98a1f705dfaf182897044500d290dbd7f\""));
    // the app's single entry script is a carchive member, not a PYZ module, and is an execution root
    assert!(CENSUS.contains("Mycelium A13 Demo!/__main__\""));
    assert!(!pyz_frozen(ENTRY));
    assert_eq!(t[ENTRY].0, "entry");
    assert_eq!(t.values().filter(|v| v.0 == "entry").count(), 1);
}

#[test]
fn launch_graph_is_the_fixpoint_of_the_reference_rules_over_the_oracle() {
    oracle_blobs::oracle_dir_or_panic();
    let all = paths();
    let texts = oracle_blobs::blobs(&all);
    let graph = recompute(&texts);
    let t = table();
    let counts = |f: &dyn Fn(&str) -> bool| graph.status.values().filter(|s| f(s)).count();
    assert_eq!(
        (
            counts(&|s| s == "frozen"),
            counts(&|s| s == "reached"),
            counts(&|s| s == "unreached")
        ),
        (
            t.values().filter(|v| v.0 == "frozen").count(),
            t.values().filter(|v| v.0 == "reached").count(),
            t.values().filter(|v| v.0 == "unreached").count()
        )
    );
    for (path, status) in &graph.status {
        assert_eq!(&t[path].0, status, "{path}");
    }
    // every witness is a real reference by the named rule from an executed file
    for (path, (status, via, line, rule)) in &t {
        if status != "reached" {
            continue;
        }
        assert_ne!(
            t[via].0, "unreached",
            "{path}: witness {via} is not executed"
        );
        let text = oracle_blobs::line_of(&texts[via], *line).unwrap();
        assert_eq!(
            rule_of(text, path),
            Some(rule.as_str()),
            "{path}: {via}:{line}"
        );
    }
}

#[test]
fn no_executed_file_builds_a_module_or_script_name_dynamically() {
    oracle_blobs::oracle_dir_or_panic();
    let all = paths();
    let t = table();
    let texts = oracle_blobs::blobs(&all);
    for (path, (status, ..)) in &t {
        if status == "unreached" {
            continue;
        }
        for (i, line) in texts[path].split('\n').enumerate() {
            if line.trim_start().starts_with('#') {
                continue;
            }
            for call in [
                "import_module(f",
                "__import__(f",
                "run_module(f",
                "run_path(f",
                "import_module(\"mycelium_\" +",
                "import_module('mycelium_' +",
            ] {
                assert!(
                    !line.contains(call),
                    "{path}:{}: dynamic name {call}",
                    i + 1
                );
            }
        }
    }
}

#[test]
fn reference_rules_have_positive_and_negative_examples() {
    let t = "mycelium_x/sub/tool.py";
    assert_eq!(
        rule_of("from mycelium_x.sub import tool", t),
        Some("import-line")
    );
    assert_eq!(
        rule_of("import mycelium_x.sub.tool as t", t),
        Some("dotted")
    );
    assert_eq!(rule_of("from .tool import go", t), Some("import-line"));
    assert_eq!(
        rule_of("    run('mycelium_x/sub/tool.py')", t),
        Some("path")
    );
    assert_eq!(rule_of("name = 'tool.py'", t), Some("basename"));
    assert_eq!(rule_of("    tool,", t), Some("bare-name"));
    assert_eq!(rule_of("    tool as t,", t), Some("bare-name"));
    assert_eq!(
        rule_of("python -m mycelium_x.sub", "mycelium_x/sub/__main__.py"),
        None
    );
    assert_eq!(
        rule_of("HOST = \"mycelium_x.sub\"", "mycelium_x/sub/__main__.py"),
        Some("main-pkg")
    );
    // a whole-line comment never references; an identifier that merely contains the name does not either
    assert_eq!(rule_of("# from mycelium_x.sub import tool", t), None);
    assert_eq!(rule_of("tooling = 1", t), None);
    assert_eq!(rule_of("from other import tools", t), None);
    assert_eq!(
        rule_of("from other import tool", t),
        None,
        "another package's `tool`"
    );
    assert_eq!(rule_of("mytool.py", t), None);
}

#[test]
fn the_not_executed_set_of_pending_rows_is_what_the_table_says() {
    use budget_model::inventory::{candidates, checked_in};
    let t = table();
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|b| b.key).collect();
    let unreached_pending = candidates()
        .unwrap()
        .into_iter()
        .filter(|c| !reviewed.contains(&c.key) && t[&c.path].0 == "unreached")
        .count();
    // 128 pending rows sit outside the frozen PYZ (round 12); 82 of them are in files no executed file references.
    // RU-051 adds ten keyword operands in unreached files. They remain discovery
    // candidates; the launch graph does not silently adjudicate this new queue.
    assert_eq!(unreached_pending, 82 + 10);
}
