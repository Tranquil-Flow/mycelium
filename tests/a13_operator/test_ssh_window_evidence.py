# SPDX-License-Identifier: AGPL-3.0-only
"""The criterion-3 SSH window must survive the chain's lossy stdout.

p84r1 lost its before/after/delta boundary counts because the chain pipes the whole
measurement block through `tail -30`, and ROUND-RESULT.json correctly recorded
`soak_ssh_window: null` for the missing numbers. Retaining the full audit log was not
enough: a number that only exists inside a truncated stdout window is not evidence.

These tests pin the repair on the INERT measurement helper
(`scripts/a13_operator/ssh_window_evidence.py`) with real temp files. They never run a
chain, never import an operator entrypoint, and never touch the network, ssh, or an app.
The helper's whole job is to read the retained audit log and write a durable document,
so ordinary filesystem fixtures exercise it exactly as the chain will.

r2 NOTE (the law this module now enforces): `_installed_shim` used to be a two-line script
that exits 0 and records nothing, and the suite called that "the recorder installed". A stat
cannot tell a working recorder from a dead one, so that fixture asserted the very gap that
let a missing log read as a clean zero. The recorder fixture below now RECORDS (it honours
A13_SSH_LOG and appends one timestamped line), the helper PROVES it by making it record once
into a private probe log, and a silent-but-executable shim is pinned as UNAVAILABLE in
`test_ssh_window_evidence_isolation.py`.

v2 NOTE (2026-09-21): the successor launcher `chain-preview85e.sh` is RETIRED and is not part
of this tree. The launcher-shaped assertions that used to read it as text are replaced by a
BOUNDED LOCAL HARNESS in `test_ssh_window_evidence_isolation.py`, which drives the REUSABLE
contract -- arrange the recorder binding, call the public CLI, read the durable artifact,
refuse to spend a soak on a window that cannot be measured -- instead of grepping a retired
file. This module now pins that same contract from the durable side: the caller consumes the
artifact, not the `tail -30` line. The helper's bytes, and therefore its semantics, are
unchanged.
"""
import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = ROOT / 'scripts/a13_operator/ssh_window_evidence.py'
CHAIN_84E = ROOT / 'scripts/a13_operator/chain-preview84e.sh'
# chain-preview85e.sh is RETIRED (v2, 2026-09-21) and is not part of this tree: no test here
# may read, import, prepare or execute it. The contract those tests pinned is exercised
# behaviourally through the bounded local harness in the isolation module.

_spec = importlib.util.spec_from_file_location('ssh_window_evidence', HELPER)
window = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(window)


def _installed_shim(tmp_path):
    """The instrument the chain installs: a shim that RECORDS, not merely one that exists.

    Faithful to `scripts/a13_operator/ssh-shim/ssh`: honour A13_SSH_LOG, append exactly one
    timestamp-prefixed line, then exec an inert real binary (the helper pins A13_SSH_REAL).
    """
    shim = tmp_path / 'shim' / 'ssh'
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        '#!/bin/sh\n'
        'log="${A13_SSH_LOG:-/tmp/asm/ssh-calls.log}"\n'
        'mkdir -p "$(dirname "$log")" 2>/dev/null\n'
        'cmd=$(printf \'%s\' "$*" | tr \'\\n\' \' \')\n'
        'printf \'%s %s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$cmd" >> "$log" 2>/dev/null\n'
        'exec "${A13_SSH_REAL:-/usr/bin/true}" "$@"\n')
    shim.chmod(0o755)
    return shim


def _audit_log(tmp_path, calls):
    """An audit log in the shim's own format: '<ISO8601Z> <normalised command>'."""
    log = tmp_path / 'criterion3-ssh-calls.log'
    log.write_text(''.join(
        '2026-09-21T10:0%d:00Z ssh member-mac true\n' % index
        for index in range(calls)))
    return log


@pytest.fixture
def bound_shim(tmp_path, monkeypatch):
    """The installed recorder AND the ambient binding the chain arranges (r3).

    The chain does `export PATH=$REPO/scripts/a13_operator/ssh-shim:$PATH` before it measures,
    and the r3 helper now REQUIRES the recorder it proves to be the `ssh` the measured PATH
    resolves. A test that asserts a measured number must therefore arrange that binding,
    exactly as the chain does.
    """
    shim = _installed_shim(tmp_path)
    monkeypatch.setenv('PATH', '%s:%s' % (shim.parent, os.environ.get('PATH', '')))
    return shim


def _unavailable_log(tmp_path):
    """A `log` path that exists but cannot be read as an audit log."""
    path = tmp_path / 'criterion3-ssh-calls.log'
    path.mkdir()
    return path


def test_absent_log_with_the_shim_installed_is_a_measured_zero(bound_shim, tmp_path):
    """The one honest zero: the recorder was there and nothing was recorded."""
    log = tmp_path / 'never-written.log'
    snap = window.snapshot(log, shim_path=bound_shim)
    assert snap['measured'] is True
    assert snap['count'] == 0
    assert snap['log_present'] is False
    assert snap['log_readable'] is True


def test_absent_log_WITHOUT_the_shim_is_not_a_clean_zero(tmp_path):
    """No recorder means a missing log proves nothing -- null, not 0."""
    log = tmp_path / 'never-written.log'
    snap = window.snapshot(log, shim_path=tmp_path / 'shim' / 'ssh')
    assert snap['measured'] is False
    assert snap['count'] is None
    assert snap['shim_present'] is False
    assert 'shim' in snap['reason']


def test_unreadable_log_records_unavailable_rather_than_zero(bound_shim, tmp_path):
    """A read error is a finding about the instrument, and must not read as clean."""
    snap = window.snapshot(_unavailable_log(tmp_path), shim_path=bound_shim)
    assert snap['measured'] is False
    assert snap['count'] is None
    assert snap['reason'].startswith('ssh_audit_read_error')


def test_before_after_delta_is_persisted_with_label_and_source_log(bound_shim, tmp_path):
    log = _audit_log(tmp_path, calls=3)
    shim = bound_shim
    out_dir = tmp_path / 'evidence'
    opened = window.begin('p85r1', log, out_dir, shim_path=shim)
    assert opened['phase'] == 'before'
    assert opened['before']['count'] == 3
    log.write_text(log.read_text() + '2026-09-21T11:00:00Z ssh other-host true\n')
    closed = window.finish('p85r1', log, out_dir, shim_path=shim)
    assert closed['measured'] is True
    assert closed['before']['count'] == 3
    assert closed['after']['count'] == 4
    assert closed['delta'] == 1
    assert closed['verdict'] == window.MEASURED
    assert closed['zero_ssh_within_window'] is False
    # Label and the SOURCE of truth are both in the durable document.
    assert closed['label'] == 'p85r1'
    assert closed['source_log'] == str(log)


def test_the_durable_file_not_stdout_carries_the_delta(bound_shim, tmp_path):
    """p84r1's failure mode: stdout truncated, measurement gone. Read it from disk."""
    log = _audit_log(tmp_path, calls=2)
    shim = bound_shim
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    window.finish('p85r1', log, out_dir, shim_path=shim)
    # Nothing is read from stdout: the artifact on disk is the evidence.
    on_disk = json.loads(window.artifact_path(out_dir, 'p85r1').read_text())
    assert on_disk['delta'] == 0
    assert on_disk['measured'] is True
    assert on_disk['zero_ssh_within_window'] is True


def test_unavailable_boundary_yields_null_delta_never_zero(tmp_path):
    """A window with no readable log must not be reported as a clean 0 ssh."""
    log = tmp_path / 'never-written.log'
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=tmp_path / 'shim' / 'ssh')
    closed = window.finish('p85r1', log, out_dir, shim_path=tmp_path / 'shim' / 'ssh')
    assert closed['measured'] is False
    assert closed['delta'] is None
    assert closed['delta'] != 0
    assert closed['zero_ssh_within_window'] is False
    assert closed['claimed_zero_ssh'] is False
    assert closed['verdict'] == window.UNAVAILABLE
    assert closed['unavailable_reasons']
    assert 'NOT 0' in closed['note']


def test_a_duplicate_label_is_refused_and_the_first_document_survives(tmp_path):
    log = _audit_log(tmp_path, calls=1)
    shim = _installed_shim(tmp_path)
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    try:
        window.begin('p85r1', log, out_dir, shim_path=shim)
    except window.DuplicateArtifact:
        pass
    else:  # pragma: no cover - the refusal is the assertion
        raise AssertionError('a second begin() under a used label must refuse')
    assert json.loads(window.artifact_path(out_dir, 'p85r1').read_text())['phase'] == 'before'


def test_a_completed_window_is_not_overwritten(tmp_path):
    log = _audit_log(tmp_path, calls=1)
    shim = _installed_shim(tmp_path)
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    window.finish('p85r1', log, out_dir, shim_path=shim)
    try:
        window.finish('p85r1', log, out_dir, shim_path=shim)
    except window.DuplicateArtifact:
        pass
    else:  # pragma: no cover
        raise AssertionError('finishing a complete window must refuse to overwrite it')


def test_finish_without_begin_is_refused(tmp_path):
    log = _audit_log(tmp_path, calls=1)
    try:
        window.finish('p85r1', log, tmp_path / 'evidence', shim_path=_installed_shim(tmp_path))
    except window.MissingBegin:
        pass
    else:  # pragma: no cover
        raise AssertionError('finish() with no before() must refuse, not invent a delta')


def test_importing_the_helper_is_inert():
    """No side effects at import: the module must not create or touch anything."""
    spec = importlib.util.spec_from_file_location('ssh_window_evidence_probe', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PROTOCOL == 'mycelium.a13.criterion3_ssh_window.v2'
    assert callable(module.begin) and callable(module.finish)


def test_the_cli_exit_codes_let_the_chain_stop_on_a_used_label(bound_shim, tmp_path):
    log = _audit_log(tmp_path, calls=1)
    shim = bound_shim
    out_dir = tmp_path / 'evidence'
    common = [str(HELPER), 'begin', '--label', 'p85r1', '--log', str(log),
              '--out-dir', str(out_dir), '--shim', str(shim)]
    first = subprocess.run([sys.executable, '-B', *common], capture_output=True, text=True)
    assert first.returncode == window.EXIT_OK, first.stderr
    assert 'SSH_WINDOW_EVIDENCE_BEGIN' in first.stdout
    second = subprocess.run([sys.executable, '-B', *common],
                            input='', capture_output=True, text=True)
    assert second.returncode == window.EXIT_DUPLICATE
    assert 'duplicate_artifact' in second.stdout
    # An argument error must not look like a successful measurement either.
    usage = subprocess.run([sys.executable, '-B', str(HELPER), 'nonsense'],
                           capture_output=True, text=True)
    assert usage.returncode == window.EXIT_USAGE


def test_the_helper_module_never_shells_out_or_reaches_for_ssh():
    """Structural, not behavioural: the measurement must not itself drive ssh.

    r2: `subprocess` is now imported ON PURPOSE -- `probe_recorder` has to make the shim
    record once, and no stat can distinguish a live recorder from a dead one. The
    invariant that matters is therefore not "no process" but "no ssh": the module never
    names the ssh client's path, and it pins A13_SSH_REAL to an inert binary so the shim's
    trailing exec can only reach that. See test_ssh_window_evidence_isolation.py for the
    behavioural half.
    """
    text = HELPER.read_text()
    tree = ast.parse(text)
    modules = set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or '')
        if isinstance(node, ast.Name):
            names.add(node.id)
    assert 'socket' not in modules
    assert 'target' not in names  # `target` is the ssh transport in round14_rpc
    assert '/usr/bin/ssh' not in text, 'the helper must not name the ssh client'
    assert "env['A13_SSH_REAL'] = INERT_SSH_REAL" in text
    assert "INERT_SSH_REAL = '/usr/bin/true'" in text


def test_the_window_is_persisted_durably_for_the_caller_not_only_printed(bound_shim, tmp_path):
    """v2: the caller consumes the ARTIFACT, never the `tail -30` line.

    The retired launcher's lesson is kept without the launcher: run the helper as its own
    process -- so nothing survives in memory -- and then require that the file on disk carries
    the very document that was printed, under the label the caller chose. A number that only
    exists inside a truncated stdout window is not evidence, so the printed bytes and the
    durable bytes must be the same bytes.
    """
    log = _audit_log(tmp_path, calls=0)
    out_dir = tmp_path / 'evidence'
    result = subprocess.run(
        [sys.executable, '-B', str(HELPER), 'begin', '--label', 'v2r1',
         '--log', str(log), '--out-dir', str(out_dir), '--shim', str(bound_shim)],
        capture_output=True, text=True)
    assert result.returncode == window.EXIT_OK, result.stderr
    assert 'SSH_WINDOW_EVIDENCE_FILE ' in result.stdout, result.stdout

    path = window.artifact_path(out_dir, 'v2r1')
    assert str(path) in result.stdout, result.stdout      # the caller is told the durable path
    document = json.loads(path.read_text())               # a fresh process re-reads it whole
    assert document['label'] == 'v2r1', document
    assert document['phase'] == 'before', document
    assert document['measured'] is False, document        # an opened window is not a claim
    assert document['before']['measured'] is True, document
    assert document['before']['count'] == 0, document
    assert document['before']['recorder_active'] is True, document

    printed = [line for line in result.stdout.splitlines()
               if line.startswith('SSH_WINDOW_EVIDENCE_BEGIN ')]
    assert printed, result.stdout
    assert json.loads(printed[0].split(' ', 1)[1]) == document, printed


def test_the_retained_historical_launcher_is_untouched():
    """p84r1's launcher stays exactly as it ran, and was never retrofitted.

    v2: there is no successor launcher to differ from any more (p85 is retired and absent), so
    the guard that still carries meaning is kept -- the retained historical launcher keeps its
    own identity and does not quietly become the measurement caller.
    """
    if not CHAIN_84E.exists():
        pytest.skip("the retained historical launcher is not part of the published tree")
    text = CHAIN_84E.read_text()
    assert 'A13_LOG_LABEL=p84r1' in text
    assert 'A13_TARGET_PREVIEW=preview84' in text
    assert 'ssh_window_evidence' not in text
