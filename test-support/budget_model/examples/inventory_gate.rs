// SPDX-License-Identifier: AGPL-3.0-only
//! Print the inventory report: candidate, reviewed, mapped and disposition counts, then the closure
//! status (PASS, or the violation count and the first few violations).
//!
//! Ruling RU-053: this is a report, not a gate. It exits 0 whenever the report was produced, even when
//! closure violations remain. It still exits non-zero for real errors, i.e. inputs that cannot be read
//! or parsed. The name is historical.
use budget_model::inventory::report;
fn main() -> Result<(), String> {
    for line in report()?.render() {
        println!("{line}");
    }
    Ok(())
}
