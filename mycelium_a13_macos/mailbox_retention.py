# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded mailbox maintenance, always inside its caller's write transaction.

Keep sequence high-water rows and all still-valid signed retries. Pruning does
not authorize work, prove cleanup, or discard reconciliation records. SQLite
reuses freed pages; never VACUUM the multi-GB database on the request path.
"""
import json
import hashlib

AUTH_KEEP = 256
AUTH_CAPACITY = 8192
BATCH = 128
COMMAND_AGE = 86400.0
# Fixed 8 MiB per signer. Bits are NEVER cleared: false positives refuse work,
# but historical nonce/request-id reuse can never become a false negative.
HISTORY_BITS = 8 * 1024 * 1024 * 8
PAGE_BYTES = 512


def _positions(kind, value):
    digest = hashlib.sha256((kind + '\0' + value).encode('utf-8')).digest()
    return [int.from_bytes(digest[n:n+4], 'big') % HISTORY_BITS for n in range(0,16,4)]


def historical(db, signer, kind, value, *, remember=False):
    present = True
    for position in _positions(kind, value):
        page, bit = divmod(position, PAGE_BYTES*8)
        row = db.execute('SELECT bits FROM managed_replay_history WHERE signer_key_digest=? AND page=?',
            (signer,page)).fetchone()
        data = bytearray(PAGE_BYTES) if row is None else bytearray(row[0])
        if len(data) != PAGE_BYTES:
            raise ValueError('managed_control_state_corrupt')
        offset, shift = divmod(bit,8)
        mask = 1 << shift
        if not data[offset] & mask:
            present = False
            if remember:
                data[offset] |= mask
                db.execute('INSERT INTO managed_replay_history VALUES (?,?,?) '
                    'ON CONFLICT(signer_key_digest,page) DO UPDATE SET bits=excluded.bits',
                    (signer,page,bytes(data)))
    return present


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS managed_replay_history ('
        'signer_key_digest TEXT NOT NULL,page INTEGER NOT NULL,bits BLOB NOT NULL,'
        'PRIMARY KEY(signer_key_digest,page)) WITHOUT ROWID')
    db.execute('CREATE INDEX IF NOT EXISTS managed_command_run ON managed_commands (run_id,deployment_id)')
    columns = {row[1] for row in db.execute('PRAGMA table_info(managed_auth_requests)')}
    if 'valid_until' not in columns:
        db.execute('ALTER TABLE managed_auth_requests ADD COLUMN valid_until REAL')
    columns = {row[1] for row in db.execute('PRAGMA table_info(managed_commands)')}
    if 'retired_at' not in columns:
        db.execute('ALTER TABLE managed_commands ADD COLUMN retired_at REAL')
    db.execute('CREATE INDEX IF NOT EXISTS managed_command_binding ON managed_commands '
        '(target_node_id,run_id,deployment_id,target_endpoint_id,target_key_digest,'
        'membership_generation,member_incarnation,state,owner_sequence)')
    db.execute('CREATE INDEX IF NOT EXISTS managed_command_claim_request ON managed_commands '
        '(target_key_digest,claimed_request_id) WHERE state IN (\'claimed\',\'reconciliation_required\')')
    db.execute('CREATE INDEX IF NOT EXISTS managed_command_retired ON managed_commands '
        '(retired_at,updated_at) WHERE retired_at IS NOT NULL '
        'AND state IN (\'completed\',\'failed\',\'expired\')')


def prune_auth(db, signer, now):
    from .managed_control import MAX_TTL_SECONDS, MAX_CLOCK_SKEW_SECONDS
    cutoff = db.execute('SELECT sequence FROM managed_auth_requests WHERE signer_key_digest=? '
        'ORDER BY sequence DESC LIMIT 1 OFFSET ?', (signer, AUTH_KEEP)).fetchone()
    if cutoff is None:
        return 0
    rows = db.execute('SELECT request_id,nonce,valid_until,response_json FROM managed_auth_requests a '
        'WHERE signer_key_digest=? AND sequence<=? AND NOT EXISTS ('
        'SELECT 1 FROM managed_commands c WHERE c.target_key_digest=a.signer_key_digest '
        'AND c.claimed_request_id=a.request_id AND c.state IN (\'claimed\',\'reconciliation_required\')) '
        'ORDER BY sequence LIMIT ?', (signer, cutoff[0], BATCH)).fetchall()
    removed = 0
    for row in rows:
        until = row['valid_until']
        if until is None:
            # Legacy rows have no request expiry. The owner-signed response time
            # plus a whole maximum TTL and both skew sides is conservative.
            until = json.loads(row['response_json'])['statement']['issued_at'] + MAX_TTL_SECONDS + 2*MAX_CLOCK_SKEW_SECONDS
        if until < now:
            # History and deletion commit together; even a freshly re-signed
            # request cannot reuse a retired nonce or request id.
            historical(db, signer, 'nonce', row['nonce'], remember=True)
            historical(db, signer, 'request', row['request_id'], remember=True)
            removed += db.execute('DELETE FROM managed_auth_requests WHERE signer_key_digest=? AND request_id=?',
                (signer, row['request_id'])).rowcount
    return removed


def prune_commands(db, now):
    return db.execute('DELETE FROM managed_commands WHERE command_id IN ('
        'SELECT command_id FROM managed_commands WHERE retired_at IS NOT NULL '
        'AND state IN (\'completed\',\'failed\',\'expired\') AND retired_at<? AND updated_at<? '
        'ORDER BY retired_at,updated_at LIMIT ?)', (now-COMMAND_AGE,now-COMMAND_AGE,BATCH)).rowcount
