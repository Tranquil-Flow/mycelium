// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-FAIL-06 (partial: serving-read loss is reported, not worker death; blocked, dead and unadmitted stay fatal).
//!
//! L17 classification. An observation is never successful serving or cleanup.
use runtime::discovery::*;
fn context() -> ServingContext<'static> {
    ServingContext {
        serving: true,
        run: "run",
        deployment: "deployment",
        attached: Some(("run", "deployment")),
    }
}
fn failure() -> ReadFailure<'static> {
    ReadFailure {
        managed: true,
        code: "managed_control_unreachable",
        status: None,
        cause: None,
        path: "/managed-control/staging/read",
    }
}
fn healthy() -> Result<NativeStatus<'static>, &'static str> {
    Ok(NativeStatus {
        state: "running",
        worker_running: true,
        work_admitted: true,
        model_load_authorized: false,
        error_code: None,
    })
}
#[test]
fn serving_read_loss_is_reported_not_worker_death() {
    for path in [
        "/managed-control/staging/read",
        "/managed-control/staging/cleanup/read",
    ] {
        for code in [
            "managed_control_unreachable",
            "managed_control_response_timeout",
        ] {
            for cause in [
                None,
                Some("dns_resolution_failed"),
                Some("connect_timeout"),
                Some("relay_connect_timeout"),
                Some("relay_connect_unavailable"),
            ] {
                assert_eq!(
                    classify(
                        &context(),
                        ReadFailure {
                            path,
                            code,
                            cause,
                            ..failure()
                        },
                        healthy
                    ),
                    Classification::Reported
                );
            }
        }
    }
}
#[test]
fn refusals_writes_unknown_causes_and_nonserving_never_soften() {
    let cases = [
        ReadFailure {
            managed: false,
            ..failure()
        },
        ReadFailure {
            status: Some(400),
            ..failure()
        },
        ReadFailure {
            cause: Some("policy_guard"),
            ..failure()
        },
        ReadFailure {
            path: "/managed-control/command",
            ..failure()
        },
        ReadFailure {
            code: "permission_denied",
            ..failure()
        },
    ];
    for f in cases {
        assert_eq!(
            classify(&context(), f, || panic!("should not probe")),
            Classification::Fatal
        );
    }
    for c in [
        ServingContext {
            serving: false,
            ..context()
        },
        ServingContext {
            attached: None,
            ..context()
        },
        ServingContext {
            attached: Some(("stale", "deployment")),
            ..context()
        },
        ServingContext {
            attached: Some(("run", "stale")),
            ..context()
        },
    ] {
        assert_eq!(
            classify(&c, failure(), || panic!("wrong context")),
            Classification::Fatal
        );
    }
}
#[test]
fn native_status_failure_blocked_dead_and_unadmitted_stay_fatal() {
    assert_eq!(
        classify(&context(), failure(), || Err("native_status_unavailable")),
        Classification::NativeFailure("native_status_unavailable")
    );
    let mut n = healthy().unwrap();
    n.state = "blocked";
    n.error_code = Some("pump_failed");
    assert_eq!(
        classify(&context(), failure(), || Ok(n)),
        Classification::NativeFailure("pump_failed")
    );
    for field in [0, 1, 2] {
        let mut n = healthy().unwrap();
        match field {
            0 => n.state = "stopped",
            1 => n.worker_running = false,
            _ => n.work_admitted = false,
        };
        assert_eq!(
            classify(&context(), failure(), || Ok(n)),
            Classification::Fatal
        );
    }
    let mut n = healthy().unwrap();
    n.work_admitted = false;
    n.model_load_authorized = true;
    assert_eq!(
        classify(&context(), failure(), || Ok(n)),
        Classification::Reported
    );
}
