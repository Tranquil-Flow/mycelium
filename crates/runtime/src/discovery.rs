// SPDX-License-Identifier: AGPL-3.0-only
//! Exact serving discovery exception classification, demo_target.py:254–291.
#[derive(Clone, Copy)]
pub struct ReadFailure<'a> {
    pub managed: bool,
    pub code: &'a str,
    pub status: Option<u16>,
    pub cause: Option<&'a str>,
    pub path: &'a str,
}
#[derive(Clone, Copy)]
pub struct NativeStatus<'a> {
    pub state: &'a str,
    pub worker_running: bool,
    pub work_admitted: bool,
    pub model_load_authorized: bool,
    pub error_code: Option<&'static str>,
}
pub struct ServingContext<'a> {
    pub serving: bool,
    pub run: &'a str,
    pub deployment: &'a str,
    pub attached: Option<(&'a str, &'a str)>,
}
#[derive(Debug, PartialEq, Eq)]
pub enum Classification {
    Reported,
    Fatal,
    NativeFailure(&'static str),
}
pub fn classify(
    context: &ServingContext<'_>,
    failure: ReadFailure<'_>,
    native: impl FnOnce() -> Result<NativeStatus<'static>, &'static str>,
) -> Classification {
    if !context.serving
        || !failure.managed
        || failure.status.is_some()
        || !matches!(
            failure.code,
            "managed_control_unreachable" | "managed_control_response_timeout"
        )
        || !matches!(
            failure.cause,
            None | Some(
                "dns_resolution_failed"
                    | "connect_timeout"
                    | "relay_connect_timeout"
                    | "relay_connect_unavailable"
            )
        )
        || !matches!(
            failure.path,
            "/managed-control/staging/read" | "/managed-control/staging/cleanup/read"
        )
        || context.attached != Some((context.run, context.deployment))
    {
        return Classification::Fatal;
    }
    let status = match native() {
        Ok(s) => s,
        Err(code) => return Classification::NativeFailure(code),
    };
    if status.state == "blocked" {
        return Classification::NativeFailure(
            status
                .error_code
                .filter(|s| !s.is_empty())
                .unwrap_or("native_worker_blocked"),
        );
    }
    if status.state == "running"
        && status.worker_running
        && (status.work_admitted || status.model_load_authorized)
    {
        Classification::Reported
    } else {
        Classification::Fatal
    }
}
