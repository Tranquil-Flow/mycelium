// SPDX-License-Identifier: AGPL-3.0-only
//! RED stub of the M16 coordinator status projection (mycelium_m16_runtime.py:601-652,
//! :988-1018, :1043-1160 @ f4e366e9): a fixed minimal status, a validator that accepts
//! everything and an attach that stores the raw document. The port replaces it.
use super::M16RuntimeCoordinator;
use crate::m16_ledger::LedgerError;
use crate::m16_router_contracts::{PyJson, PyStr, RouterContractError};

/// :37 `PROTOCOL`.
pub const STATUS_PROTOCOL: &str = "mycelium.concurrent_request_runtime.v1";
/// mycelium_performance_budget.py:14 `PROTOCOL_V3`.
pub const PERFORMANCE_BUDGET_V3_PROTOCOL: &str = "mycelium.performance_budget.v3";
/// `float(request.admitted_at)` raised OverflowError "int too large to convert to float"
/// (:1012).
pub const STATUS_INT_TOO_LARGE_FOR_FLOAT: &str = "m16_status_int_too_large_for_float";
/// A JSON value with no Python counterpart (a parsed graph's number always has one, so this
/// never fires).
pub const STATUS_UNREPRESENTABLE_VALUE: &str = "m16_status_unrepresentable_value";
/// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
const POLICY_UNAVAILABLE: RouterContractError =
    RouterContractError("m16_coordinator_policy_unavailable");

/// One `validate_m16_runtime_status` refusal (:1065-1160), in its check order.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StatusValidationError {
    /// :1066-1067 not a mapping, or its keys are not `_TOP_LEVEL_FIELDS`.
    Shape,
    /// :1068-1069 `document.get("protocol") != PROTOCOL`.
    Protocol,
    /// :1070-1080 `queue` is not a mapping of exactly its eight keys.
    QueueShape,
    /// :1125-1146 placements (a non-empty list of exact mappings), requests (at most
    /// `_MAX_STATUS_REQUESTS`), incidents (at most 256), batch_state, performance_budgets (a
    /// list of at most 16).
    NestedShape,
    /// :1147-1153 a budget's PerformanceBudgetError or TypeError.
    BudgetShape,
    /// :1043-1051 a mapping with a `_PRIVATE_FIELDS` key, at any depth.
    PrivateContent,
    /// :1054-1062 a NaN or infinite float, at any depth.
    NonFinite,
    /// :1156-1159 `json.dumps` refused the document (an int past 4,300 digits).
    NotJsonSafe,
    /// A budget's `float(int)` OverflowError, which :1152 does not catch.
    BudgetIntTooLargeForFloat,
    /// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
    PolicyUnavailable,
}

impl StatusValidationError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Shape => "m16_runtime_status_shape_invalid",
            Self::Protocol => "m16_runtime_status_protocol_invalid",
            Self::QueueShape => "m16_runtime_status_queue_shape_invalid",
            Self::NestedShape => "m16_runtime_status_nested_shape_invalid",
            Self::BudgetShape => "m16_runtime_status_budget_shape_invalid",
            Self::PrivateContent => "m16_runtime_status_private_content",
            Self::NonFinite => "m16_runtime_status_non_finite_value",
            Self::NotJsonSafe => "m16_runtime_status_not_json_safe",
            Self::BudgetIntTooLargeForFloat => "m16_runtime_status_budget_int_too_large_for_float",
            Self::PolicyUnavailable => POLICY_UNAVAILABLE.0,
        }
    }

    /// The Python exception kind: `value` (ValueError), `overflow` (OverflowError) or `config`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::BudgetIntTooLargeForFloat => "overflow",
            Self::PolicyUnavailable => "config",
            _ => "value",
        }
    }

    /// The exception's `str()`.
    pub fn message(&self) -> &'static str {
        match self {
            Self::Shape => "M16 runtime status shape is invalid",
            Self::Protocol => "M16 runtime status protocol is invalid",
            Self::QueueShape => "M16 runtime status queue shape is invalid",
            Self::NestedShape => "M16 runtime status nested shape is invalid",
            Self::BudgetShape => "M16 runtime status budget shape is invalid",
            Self::PrivateContent => "M16 runtime status contains private content",
            Self::NonFinite => "M16 runtime status contains non-finite value",
            Self::NotJsonSafe => "M16 runtime status is not JSON-safe",
            Self::BudgetIntTooLargeForFloat => "int too large to convert to float",
            Self::PolicyUnavailable => POLICY_UNAVAILABLE.0,
        }
    }
}

/// A `status()` refusal, by the kind of exception the oracle raises.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StatusError {
    /// `validate_m16_runtime_status`'s refusal (:652).
    Validation(StatusValidationError),
    /// The ledger's `placement_status()` exception, unchanged (:621; e.g. TypeError for
    /// placement ids `sorted` cannot order).
    Ledger(LedgerError),
    /// A Python exception of the projection ([`STATUS_INT_TOO_LARGE_FOR_FLOAT`],
    /// [`STATUS_UNREPRESENTABLE_VALUE`]).
    Python(&'static str),
    /// The `_MAX_STATUS_REQUESTS` budget row is unavailable (the real table has it).
    Config(RouterContractError),
}

impl StatusError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Validation(error) => error.code(),
            Self::Ledger(error) => error.0,
            Self::Python(code) => code,
            Self::Config(error) => error.0,
        }
    }

    /// The exception kind: `validation`, `ledger`, `python` or `config`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Validation(_) => "validation",
            Self::Ledger(_) => "ledger",
            Self::Python(_) => "python",
            Self::Config(_) => "config",
        }
    }
}

/// A `validate_performance_budget_v3` refusal (mycelium_performance_budget.py:180-239).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BudgetError {
    /// PerformanceBudgetError("performance_budget_invalid").
    Invalid,
    /// TypeError: a `dimension` (:208, the set of names) or `state` (:211, set membership)
    /// that is a `list` or `dict` (the type name).
    Unhashable(&'static str),
    /// OverflowError: `float(int)` of a `bound` or `observed` past the double range (:48).
    IntTooLargeForFloat,
}

impl BudgetError {
    /// The specific code.
    pub fn code(&self) -> &'static str {
        match self {
            Self::Invalid => "performance_budget_invalid",
            Self::Unhashable(_) => "performance_budget_unhashable",
            Self::IntTooLargeForFloat => "performance_budget_int_too_large_for_float",
        }
    }

    /// The Python exception kind: `performance_budget`, `type` or `overflow`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Invalid => "performance_budget",
            Self::Unhashable(_) => "type",
            Self::IntTooLargeForFloat => "overflow",
        }
    }

    /// The exception's `str()` (CPython 3.14).
    pub fn message(&self) -> String {
        match self {
            Self::Invalid => "performance_budget_invalid".to_owned(),
            Self::Unhashable(name) => {
                format!("cannot use '{name}' as a set element (unhashable type: '{name}')")
            }
            Self::IntTooLargeForFloat => "int too large to convert to float".to_owned(),
        }
    }
}

impl M16RuntimeCoordinator {
    /// RED stub: a fixed minimal document, no clock read, no projection.
    pub fn status(&self) -> Result<PyJson, StatusError> {
        Ok(PyJson::Dict(vec![(
            PyStr::new("protocol"),
            PyJson::str(STATUS_PROTOCOL),
        )]))
    }

    /// RED stub: stores the raw document.
    pub fn attach_performance_budget(&self, document: &PyJson) -> Result<(), BudgetError> {
        self.lock().performance_budgets = vec![document.clone()];
        Ok(())
    }

    /// `self._performance_budgets` (read-only).
    pub fn performance_budgets(&self) -> Vec<PyJson> {
        self.lock().performance_budgets.clone()
    }
}

/// RED stub: accepts everything.
pub fn validate_m16_runtime_status(document: &PyJson) -> Result<PyJson, StatusValidationError> {
    Ok(document.clone())
}

/// RED stub: accepts everything.
pub fn validate_performance_budget_v3(document: &PyJson) -> Result<PyJson, BudgetError> {
    Ok(document.clone())
}
