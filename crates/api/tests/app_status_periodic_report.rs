// SPDX-License-Identifier: AGPL-3.0-only
//! When a joined Mac's next periodic capability report is due, as the SHIPPED v0.1.4 app keeps it
//! (lane PH3b S4c-a item 2). v0.1.4 @ f4e366e9, reached from `DemoApp` (app.py:505):
//! `DemoApp.start` starts `_standing_loop` (:642) -> every 2 s `_standing_tick` (:1195-1199):
//! `due = time.monotonic() >= self._next_periodic_report` -> `auto_poll_step(app, report_due=due)`
//! (standing_consent.py:329-363) -> `app.auto_report('periodic')` -> `_auto_report` (:1138-1163),
//! which ends `self.last_report = record; self._next_periodic_report = time.monotonic() + 300`.
//! `_next_periodic_report = 0.0` at construction (:532): the first tick after a launch is due.
//! Every `_auto_report` (join, the standing loop, the agent's pre-run report, `/api/start`,
//! `/api/capability/report`) moves the next one 300 s on. The instants here are injected.
use api::app_status::{Recorded, periodic_report_period};
use serde_json::json;
use std::time::Instant;

#[test]
fn a_report_is_due_at_launch_and_again_300_s_after_the_last_one() {
    assert_eq!(periodic_report_period().as_secs(), 300, "app.py:1162");
    let launch = Instant::now();
    let mut recorded = Recorded::default();
    assert!(
        recorded.periodic_report_due(launch),
        "`_next_periodic_report = 0.0`: the first tick after a launch reports"
    );
    let record = json!({"purpose": "join", "state": "delivered", "error": null});
    recorded.reported(record.clone(), launch);
    assert_eq!(recorded.last_report, Some(record));
    let period = periodic_report_period();
    assert!(!recorded.periodic_report_due(launch));
    assert!(!recorded.periodic_report_due(launch + period - period / 1000));
    assert!(recorded.periodic_report_due(launch + period));
    // A later report (here a failed one: `_auto_report` records it all the same) moves it on.
    let later = launch + period;
    recorded.reported(json!({"purpose": "periodic", "state": "failed"}), later);
    assert!(!recorded.periodic_report_due(later + period / 2));
    assert!(recorded.periodic_report_due(later + period));
}
