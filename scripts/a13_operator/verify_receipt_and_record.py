# SPDX-License-Identifier: AGPL-3.0-only
import sqlite3, os

MB = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/authority/managed-control/mailbox.sqlite3')
con = sqlite3.connect('file:%s?mode=ro' % MB, uri=True)
rows = list(con.execute(
    "select command_id, state, terminal_receipt_json is not null as has_receipt, "
    "native_response_digest, claimed_at, updated_at from managed_commands "
    "where command_id like 'cmd-695d679788a3f2%' or command_id like 'cmd-6cc050147ddf8f%'"))
print('=== the two p76r1 reconciliation rows, with receipt state ===')
for r in rows:
    print('  id=%s state=%s terminal_receipt=%s native_digest=%s claimed=%s updated=%s'
          % (r[0][:20], r[1], r[2], r[3], r[4], r[5]))
print('  rows found:', len(rows))
con.close()

LOG = os.path.expanduser('~/Documents/mycelium-core-program/status/decisions.log')
entry = ("\n2026-09-19 ~20:10 CEST -- ERRATA-15 WRITTEN (program dir, ERRATA-15.md). It RETRACTS the "
         "'what is actually defective' section of ERRATA-14: THERE WAS NO DROPPED CLAIM. The 17:35:12 flip is "
         "the mailbox recording the peer's own ack with disposition='reconciliation_required' "
         "(managed_control.py:1446-1522, then :1550-1558 writing a terminal receipt so polls stop re-demanding), "
         "and the peer retires the claim locally right after that ack by design (concurrent_pump.py:240-250). "
         "The protocol behaved correctly on both sides. Root cause is upstream: the peer's NATIVE could not "
         "answer, and this run's native-exit.private.json records error_code worker_native_exited, returncode 0, "
         "error_detail null, exception_types [], frames [] -- the fourth 'records that and where, never why' and "
         "this time the direct cause of a round failure. NEXT STEP: make the native's exit carry a reason when a "
         "command was in flight (TDD, W4's shape). Do NOT instrument the decline paths for a vanishing claim; "
         "that record already exists as the ack plus the terminal receipt. "
         "Falsified this session by measurement: stale owner state; mid-run re-enrollment as the drop's cause; a "
         "silently dropped claim.\n")
with open(LOG, 'a') as fh:
    fh.write(entry)
print('appended', len(entry), '-> decisions.log now', os.path.getsize(LOG), 'bytes')
