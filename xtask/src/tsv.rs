// SPDX-License-Identifier: AGPL-3.0-only
//! Minimal strict TSV reader for the registry files (the TSVs stay the source of truth; no JSON copy).
//!
//! One header line, then one row per non-blank line; cells are split on TAB only (no quoting).
//! A row whose cell count differs from the header is not read and is returned in `ragged` so the
//! caller can report it; nothing is padded or truncated silently.

#[derive(Debug, Clone, Default)]
pub struct Table {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<String>>,
    /// 1-based line number of each entry of `rows`.
    pub lines: Vec<usize>,
    /// `(line, cells found)` of every row whose width differs from the header.
    pub ragged: Vec<(usize, usize)>,
}

impl Table {
    /// `exact`: the header must equal `required`; otherwise it must contain every name in `required`
    /// (extra columns are allowed, e.g. a trailing notes column).
    pub fn parse(text: &str, required: &[&str], exact: bool) -> Result<Self, String> {
        let mut lines = text.lines().enumerate();
        let Some((_, header)) = lines.next() else {
            return Err("empty file: no header line".into());
        };
        let columns: Vec<String> = header
            .trim_end_matches('\r')
            .split('\t')
            .map(str::to_string)
            .collect();
        let missing: Vec<&&str> = required
            .iter()
            .filter(|name| !columns.iter().any(|c| c == **name))
            .collect();
        if !missing.is_empty() {
            return Err(format!("header lacks column(s) {missing:?}"));
        }
        if exact
            && columns
                .iter()
                .map(String::as_str)
                .ne(required.iter().copied())
        {
            return Err(format!(
                "header is {columns:?}, expected exactly {required:?}"
            ));
        }
        let mut table = Self {
            columns,
            ..Self::default()
        };
        for (index, line) in lines {
            let line = line.trim_end_matches('\r');
            if line.trim().is_empty() {
                continue;
            }
            let cells: Vec<String> = line.split('\t').map(str::to_string).collect();
            if cells.len() == table.columns.len() {
                table.rows.push(cells);
                table.lines.push(index + 1);
            } else {
                table.ragged.push((index + 1, cells.len()));
            }
        }
        Ok(table)
    }
    /// The trimmed cell of `row` in column `name` ("" for an unknown column).
    pub fn get(&self, row: usize, name: &str) -> &str {
        self.columns
            .iter()
            .position(|c| c == name)
            .and_then(|column| self.rows.get(row).and_then(|r| r.get(column)))
            .map_or("", |cell| cell.trim())
    }
}

/// Splits an id list written with `;`, `,` or white space between items.
pub fn id_list(cell: &str) -> Vec<String> {
    cell.split([';', ',', ' ', '\t'])
        .map(str::trim)
        .filter(|item| !item.is_empty())
        .map(str::to_string)
        .collect()
}
