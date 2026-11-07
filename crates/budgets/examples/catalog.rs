// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{
    BudgetId, Entries,
    table::{BUDGET_TABLE, Value},
};
fn main() {
    println!("| BudgetId | Value (ms / bytes / count) | Purpose | Provenance |");
    println!("|---|---|---|---|");
    for row in BUDGET_TABLE {
        let resolved = match row.value {
            Value::Derived(_) => format!(
                "{:?}; two peers: {:?}",
                row.value,
                row.id.span(Entries::new(2))
            ),
            _ => format!("{:?}", row.value),
        };
        println!(
            "| {:?} | {} | {:?} | {:?} |",
            row.id, resolved, row.purpose, row.provenance
        );
    }
    println!(
        "\nDeclared entries: {}. Missing p99 is BLOCKED, not a zero or a default.",
        BUDGET_TABLE.len()
    );
    println!(
        "\nCorrected restart: {:?}",
        BudgetId::CorrectedRestart.span(Entries::new(2))
    );
}
