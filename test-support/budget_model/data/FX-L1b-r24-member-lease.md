# FX-L1b-r24-member-lease

External oracle `f4e366e98a1f705dfaf182897044500d290dbd7f`; `round24/member_probe_final.py` sha256 `b4d86cc4d059de13e6a9c37c592771575fcf4c79f82f5905f1d0a41b629cf09f`.
Paired `r24-probe-2.out` / `r24-probe-3.out` identical sha256 `19694f2420ef577288e7c3c77e5b9ed7d6d567cc1abb0277e1fd37030bf2e89c`; 17 cases. Fixture projection sha256 `1b09c033d7fe0be0b56ecf736b603a0fead292598462b5d5de43982a2bf81f7d`.
Real SQLite, coordinator join/resume/heartbeat/signature verification, FrozenPlacementSource and member_authority_guard; no network, fabricated response or product Python. Fresh isolated HOME/TMPDIR and audit fence.
The first attempt `r24-probe-1.out` had a resume call-shape TypeError, retained as harness failure, not source refusal; original `member_probe.py` retained.
Finite signed +/-1e100 survive restore and real frozen placement. Positive huge lease grants member authority, expired negative refuses. Signed committed replay max retains old1e100 alongside renewal1400; a new heartbeat instead refuses seed_deadline_unavailable. Restore consumes save_member inputs, not a manufactured invite-domain proof. Join/resume mint1300/1400 are bounded-relative producer examples, NOT bounds on all stored values.
These are captured oracle characterizations and static inventory assertions, not execution of a Rust seed port or global fault catalogue credit.
