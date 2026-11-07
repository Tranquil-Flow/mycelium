// SPDX-License-Identifier: AGPL-3.0-only
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Statistics {
    pub count: usize,
    pub p50_ms: Option<f64>,
    pub p99_ms: Option<f64>,
    pub status: String,
}
/// Nearest rank; 100 independent, comparable exact samples is a minimum,
/// not a confidence bound. Missing load and interval-censored phases block p99.
pub fn statistics(samples: &[f64], exact: bool, load_known: bool) -> Statistics {
    let valid = samples.iter().all(|x| x.is_finite() && *x >= 0.0);
    let mut sorted = samples.to_vec();
    sorted.sort_by(f64::total_cmp);
    let n = sorted.len();
    let p50 = (valid && n >= 2 && exact).then(|| sorted[n.div_ceil(2) - 1]);
    let p99 =
        (valid && n >= 100 && exact && load_known).then(|| sorted[(n * 99).div_ceil(100) - 1]);
    Statistics {
        count: n,
        p50_ms: p50,
        p99_ms: p99,
        status: if p99.is_some() {
            "EMPIRICAL_P99"
        } else {
            "BLOCKED"
        }
        .into(),
    }
}

pub mod device;
mod importer;
pub mod qualify;
pub use importer::*;
