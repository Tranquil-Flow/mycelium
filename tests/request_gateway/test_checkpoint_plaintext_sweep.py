# SPDX-License-Identifier: AGPL-3.0-only
"""M5: no plaintext prompt tokens are left in the replay checkpoint directory after a crash or a shutdown.

The replay checkpoint stores the encoded prompt (token ids, trivially detokenised) and the committed output.  A6
recovery never resumes one after a restart (the start-up reconciliation aborts and discards it), so the file is a
liability from the moment its owner is gone.  FAKE (labelled): the generation backend; the gateway service and the
checkpoint store are the shipped code.
Review: R3 F9 claimed no boot-time sweep.  Found on inspection: ``*.checkpoint.json`` orphans are already journaled
(digest only) and discarded at construction, and shutdown already discards through the worker's terminal path; those two
are pinned here as regressions.  The real gap was ``.checkpoint-*.tmp`` (a crash between ``mkstemp`` and ``os.replace``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mycelium_a6_gateway import freeze_replay_checkpoint
from mycelium_a6_recovery import CheckpointStore
from mycelium_request_gateway.service import RequestGatewayService
from tests.request_gateway.test_a6_gateway import FreezeProbeBackend, _drain, _snapshot, _submission
from test_core import MutableQualificationSource, _synthetic_qualification


def _prompt_bytes(checkpoint) -> bytes:
    return json.dumps(list(checkpoint.encoded_prompt), separators=(",", ":")).encode()


def _files_holding(root: Path, needle: bytes) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_file() and needle in p.read_bytes())


def _service(root: Path, backend=None, ids=("req-1",)):
    sequence = iter(ids)
    return RequestGatewayService(
        qualification_source=MutableQualificationSource(_synthetic_qualification()),
        backend=backend or FreezeProbeBackend(()),
        request_id_source=lambda: next(sequence),
        replay_checkpoint_root=root,
    )


def _leftover_tmp(root: Path, checkpoint, name: str = ".checkpoint-crashed.tmp") -> Path:
    """What a crash between ``mkstemp`` and ``os.replace`` leaves: the serialized checkpoint, prompt ids and all."""
    path = root / name
    path.write_text(json.dumps(checkpoint._canonical_document(), sort_keys=True, separators=(",", ":")))
    path.chmod(0o600)
    return path


def test_a_crash_leftover_temp_file_with_prompt_ids_is_removed_on_boot(tmp_path):
    checkpoint = freeze_replay_checkpoint(_snapshot(), store_identity="crashed", created_at_unix_ms=1_000,
                                          expiry_unix_ms=2_000_000)
    leftover = _leftover_tmp(tmp_path, checkpoint)
    assert _files_holding(tmp_path, _prompt_bytes(checkpoint)) == [leftover.name]  # the fixture really holds the ids
    service = _service(tmp_path)
    try:
        assert not leftover.exists()
        assert _files_holding(tmp_path, _prompt_bytes(checkpoint)) == []
    finally:
        service.close()


def test_a_crash_leftover_checkpoint_is_discarded_on_boot_and_leaves_only_digests(tmp_path):
    checkpoint = freeze_replay_checkpoint(_snapshot(), store_identity="orphan", created_at_unix_ms=1_000,
                                          expiry_unix_ms=2_000_000)
    CheckpointStore(tmp_path, store_identity="orphan").write(checkpoint)
    service = _service(tmp_path)
    try:
        assert not list(tmp_path.glob("*.checkpoint.json"))
        assert _files_holding(tmp_path, _prompt_bytes(checkpoint)) == []
        assert (tmp_path / ".reconciliations.json").exists()  # the outcome is journaled: digest only
    finally:
        service.close()


def test_the_temp_sweep_touches_only_checkpoint_temporaries(tmp_path):
    keep = tmp_path / "operator-notes.txt"
    keep.write_text("not ours")
    linked = tmp_path / ".checkpoint-link.tmp"
    linked.symlink_to(keep)  # a symlink is never followed and never trusted: it is removed as a link only
    assert CheckpointStore.sweep_temporaries(tmp_path) == 1
    assert keep.read_text() == "not ours" and not linked.exists() and not linked.is_symlink()
    assert CheckpointStore.sweep_temporaries(tmp_path) == 0
    assert CheckpointStore.sweep_temporaries(tmp_path / "missing") == 0


def test_shutdown_leaves_no_file_holding_the_prompt_ids(tmp_path):
    qualification = _synthetic_qualification()
    service = _service(tmp_path, FreezeProbeBackend(("moon",)))
    request_id = service.submit(_submission(qualification))
    _drain(service.subscribe(request_id, last_event_id=None))
    checkpoint = service.freeze_replay_checkpoint(
        request_id, store_identity="caller", session_id="session", source_attempt=1, path_id="path-source-1",
        path_attempt=0, tokenizer_digest="sha256:" + "4" * 64, created_at_unix_ms=1_000, expiry_unix_ms=2_000_000)
    assert _files_holding(tmp_path, _prompt_bytes(checkpoint)), "the checkpoint is on disk while the request lives"
    service.close()
    assert _files_holding(tmp_path, _prompt_bytes(checkpoint)) == []
    assert not list(tmp_path.glob("*.checkpoint.json"))
    assert not list(tmp_path.glob(".checkpoint-*.tmp"))


def test_a_second_boot_after_a_shutdown_has_nothing_to_reconcile_beyond_the_journal(tmp_path):
    first = _service(tmp_path)
    first.close()
    second = _service(tmp_path)
    try:
        assert second.startup_recovery_reconciliations() == ()
    finally:
        second.close()
    assert os.listdir(tmp_path) in ([], [".reconciliations.json"])
