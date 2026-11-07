// SPDX-License-Identifier: AGPL-3.0-only
use crate::lint::{Diagnostic, RULES, Source};
use serde::Deserialize;
use std::{
    collections::BTreeSet,
    path::{Component, Path},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Date {
    year: u32,
    month: u32,
    day: u32,
}
impl Date {
    /// Parse a strict Gregorian date for owned policy expiry.
    ///
    /// ```
    /// use xtask::policy::Date;
    /// assert!(Date::parse("2028-02-29").is_ok());
    /// assert!(Date::parse("2027-02-29").is_err());
    /// ```
    pub fn parse(value: &str) -> Result<Self, String> {
        let bytes = value.as_bytes();
        if bytes.len() != 10
            || bytes[4] != b'-'
            || bytes[7] != b'-'
            || !bytes
                .iter()
                .enumerate()
                .all(|(i, b)| i == 4 || i == 7 || b.is_ascii_digit())
        {
            return Err("date must be YYYY-MM-DD".into());
        }
        let year = value[..4].parse::<u32>().map_err(|e| e.to_string())?;
        let month = value[5..7].parse::<u32>().map_err(|e| e.to_string())?;
        let day = value[8..].parse::<u32>().map_err(|e| e.to_string())?;
        if year == 0 || !(1..=12).contains(&month) || day == 0 || day > month_days(year, month) {
            return Err("invalid calendar date".into());
        }
        Ok(Self { year, month, day })
    }
    pub fn today() -> Result<Self, String> {
        let seconds = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| format!("CLOCK: {e}"))?
            .as_secs();
        // Epoch-to-calendar conversion, not an operational timeout.
        Self::parse(&Self::from_epoch_days(seconds / 86_400)?)
    }
    pub fn from_epoch_days(mut days: u64) -> Result<String, String> {
        let mut year = 1970;
        loop {
            let length = if leap(year) { 366 } else { 365 };
            if days < length {
                break;
            }
            days -= length;
            year += 1;
            if year > 9999 {
                return Err("CLOCK: date outside supported calendar".into());
            }
        }
        let mut month = 1;
        while days >= u64::from(month_days(year, month)) {
            days -= u64::from(month_days(year, month));
            month += 1;
        }
        Ok(format!("{year:04}-{month:02}-{:02}", days + 1))
    }
}
fn leap(year: u32) -> bool {
    year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400))
}
fn month_days(year: u32, month: u32) -> u32 {
    match month {
        2 if leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

pub(crate) fn nonempty(value: &str, field: &str) -> Result<(), String> {
    if value.trim().is_empty() || value.chars().any(char::is_control) {
        Err(format!("POLICY: {field} must be nonempty single-line text"))
    } else {
        Ok(())
    }
}
pub(crate) fn relative_path(value: &str) -> Result<(), String> {
    nonempty(value, "path")?;
    if value.contains(['*', '?', '\\'])
        || !Path::new(value)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
    {
        return Err("POLICY: path must be a literal workspace-relative path".into());
    }
    Ok(())
}
pub(crate) fn expiry(value: &str, today: Date) -> Result<(), String> {
    if Date::parse(value)? <= today {
        Err(format!("POLICY: expired on {value} (UTC)"))
    } else {
        Ok(())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Exception {
    path: String,
    line: usize,
    rule: String,
    source_line: String,
    owner: String,
    reason: String,
    expiry: String,
    replacement_plan: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Allowlist {
    version: u32,
    entries: Vec<Exception>,
}
impl Allowlist {
    pub fn parse(json: &str, today: Date) -> Result<Self, String> {
        let policy: Self = serde_json::from_str(json).map_err(|e| format!("ALLOWLIST: {e}"))?;
        if policy.version != 1 {
            return Err("ALLOWLIST: unsupported version".into());
        }
        let mut keys = BTreeSet::new();
        for entry in &policy.entries {
            relative_path(&entry.path)?;
            if !entry.path.ends_with(".rs")
                || entry.line == 0
                || !RULES.contains(&entry.rule.as_str())
            {
                return Err("ALLOWLIST: invalid path, line or rule".into());
            }
            for (field, value) in [
                ("owner", &entry.owner),
                ("reason", &entry.reason),
                ("replacement_plan", &entry.replacement_plan),
                ("source_line", &entry.source_line),
            ] {
                nonempty(value, field)?;
            }
            expiry(&entry.expiry, today)?;
            if !keys.insert((&entry.path, entry.line, &entry.rule)) {
                return Err("ALLOWLIST: duplicate entry".into());
            }
        }
        Ok(policy)
    }
    pub fn apply(
        &self,
        diagnostics: Vec<Diagnostic>,
        sources: &[Source],
    ) -> Result<Vec<Diagnostic>, String> {
        let mut suppressed = BTreeSet::new();
        for entry in &self.entries {
            let exact = sources
                .iter()
                .find(|source| source.path == entry.path)
                .and_then(|source| source.text.lines().nth(entry.line - 1));
            if exact != Some(entry.source_line.as_str()) {
                return Err(format!(
                    "ALLOWLIST: stale source {}:{}",
                    entry.path, entry.line
                ));
            }
            let key = Diagnostic {
                path: entry.path.clone(),
                line: entry.line,
                code: entry.rule.clone(),
            };
            if !diagnostics.contains(&key) {
                return Err(format!(
                    "ALLOWLIST: unused exception {}:{} {}",
                    entry.path, entry.line, entry.rule
                ));
            }
            suppressed.insert(key);
        }
        Ok(diagnostics
            .into_iter()
            .filter(|d| !suppressed.contains(d))
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn leap_centuries_and_epoch_boundaries() {
        assert!(!leap(1900));
        assert!(leap(2000));
        assert_eq!(Date::from_epoch_days(365).unwrap(), "1971-01-01");
        assert_eq!(Date::from_epoch_days(789).unwrap(), "1972-02-29");
        assert_eq!(Date::from_epoch_days(790).unwrap(), "1972-03-01");
        assert!(Date::from_epoch_days(u64::MAX).is_err());
    }
}
