-- SPDX-License-Identifier: AGPL-3.0-only
-- DDL port from managed_control.py:838-878 and mailbox_retention.py:47-65
-- at f4e366e98a1f705dfaf182897044500d290dbd7f. Values below are synthetic.
CREATE TABLE managed_metadata(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL) WITHOUT ROWID;
INSERT INTO managed_metadata VALUES ('schema_version','1'),('swarm_id','fixture-swarm'),('owner_node_id','fixture-owner'),('owner_endpoint_id','fixture-endpoint'),('owner_key_digest','fixture-key');
CREATE TABLE managed_commands (
 command_id TEXT PRIMARY KEY NOT NULL,envelope_digest TEXT NOT NULL,envelope_json TEXT NOT NULL,
 target_node_id TEXT NOT NULL,target_endpoint_id TEXT NOT NULL,target_key_digest TEXT NOT NULL,
 membership_generation INTEGER NOT NULL,member_incarnation TEXT NOT NULL,run_id TEXT NOT NULL,
 deployment_id TEXT NOT NULL,owner_sequence INTEGER NOT NULL,owner_nonce TEXT NOT NULL,
 command_expires_at REAL NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','claimed','reconciliation_required','completed','failed','expired')),
 claim_token TEXT,claimed_request_id TEXT,claimed_at REAL,native_response_digest TEXT,
 native_response_json TEXT,terminal_receipt_json TEXT,updated_at REAL NOT NULL,retired_at REAL
) WITHOUT ROWID;
CREATE TABLE managed_auth_requests (
 signer_key_digest TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,
 sequence INTEGER NOT NULL,nonce TEXT NOT NULL,protocol TEXT NOT NULL,response_json TEXT NOT NULL,
 valid_until REAL,PRIMARY KEY(signer_key_digest,request_id),UNIQUE(signer_key_digest,sequence),UNIQUE(signer_key_digest,nonce)
) WITHOUT ROWID;
CREATE TABLE managed_replay_history (signer_key_digest TEXT NOT NULL,page INTEGER NOT NULL,bits BLOB NOT NULL,PRIMARY KEY(signer_key_digest,page)) WITHOUT ROWID;
