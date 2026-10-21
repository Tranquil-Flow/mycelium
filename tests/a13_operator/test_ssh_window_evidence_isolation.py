# SPDX-License-Identifier: AGPL-3.0-only
"""RED-by-design isolation tests for the p84r1 repair's FAIL-CLOSED gaps.

Written FIRST, against the copied candidate (`ssh_window_evidence.py` sha256
5c173662904ac17fbeb96233ea9496f91d052fb2c1841985673c59831bf409f1), to demonstrate four
gaps that the candidate's own 14 tests do not reach:

  gap1  the candidate treats "the shim file is executable" as "the recorder works".
        A shim that is executable but does not record, or records to a log other than the
        one the chain measures, still yields `measured=true, count=0` -- a clean zero that
        proves nothing (and a missing log then reads as zero for the wrong reason).
  gap2  `finish()` may be handed a DIFFERENT log than `begin()` used, and nothing checks
        that the log is still an append-only continuation of the log the window opened
        against. A truncated or replaced log therefore yields a negative/meaningless delta
        instead of UNAVAILABLE.
  gap3  `begin(..., force=True)` / `--force` overwrites an existing retained artifact.
  gap4  `begin()` is exists()-then-os.replace(), so two concurrent begins can both pass the
        existence check and the second silently clobbers the first.

These tests are INERT: temp files only. The recorder fixture is a synthetic shell script;
the real shim is exercised only by the separate real-shim rehearsal.

v2 NOTE (2026-09-21): the successor launcher `chain-preview85e.sh` is RETIRED and is not part
of this tree -- nothing here reads, imports, prepares or executes it. The four assertions that
used to pin the caller by grepping that file are replaced by a BOUNDED LOCAL HARNESS
(`local_window_harness`): a tiny shell script in `tmp_path` that plays the caller's role
against the REUSABLE measurement contract -- arrange the recorder binding, drive the public
CLI, read the durable artifact, and refuse (exit 9) to spend a soak on a window it cannot
measure. Recorder, binding, window and isolation coverage is therefore still exercised, but
behaviourally, on the contract that survives, instead of on a retired file's text.
"""
import errno
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HELPER = ROOT / 'scripts/a13_operator/ssh_window_evidence.py'

_spec = importlib.util.spec_from_file_location('ssh_window_evidence_isolation', HELPER)
window = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(window)

RECORDER_BODY = """#!/bin/sh
# A faithful stand-in for scripts/a13_operator/ssh-shim/ssh: honour A13_SSH_LOG, append
# exactly one timestamp-prefixed invocation line, then exec an INERT real binary.
log="${A13_SSH_LOG:-/tmp/asm/ssh-calls.log}"
mkdir -p "$(dirname "$log")" 2>/dev/null
cmd=$(printf '%s' "$*" | tr '\\n' ' ')
printf '%s %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$cmd" >> "$log" 2>/dev/null
exec "${A13_SSH_REAL:-/usr/bin/ssh}" "$@"
"""

SILENT_BODY = """#!/bin/sh
# Executable, exits 0, records NOTHING. The silent-instrument trap.
exit 0
"""

HARDCODED_BODY = """#!/bin/sh
# Records, but to a log of its own choosing -- ignores A13_SSH_LOG entirely.
printf '%s x\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "__OTHER_LOG__"
exit 0
"""


def _write_shim(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _recorder(tmp_path, name='recorder'):
    return _write_shim(tmp_path / name / 'ssh', RECORDER_BODY)


def _log(tmp_path, calls, name='criterion3-ssh-calls.log'):
    log = tmp_path / name
    log.write_text(''.join(
        '2026-09-21T10:0%d:00Z ssh member-mac true\n' % i for i in range(calls)))
    return log


@pytest.fixture
def bound(tmp_path, monkeypatch):
    """A recorder that IS the `ssh` the MEASURED environment resolves -- the chain's binding.

    r3: the probe no longer invents a PATH of its own, so a recorder that is not what name
    resolution finds is a finding. These tests therefore arrange the environment the whole
    test claims to measure (`tmp_path`'s shim first on PATH), exactly as the chain's
    `export PATH=$REPO/scripts/a13_operator/ssh-shim:$PATH` does.
    """
    shim = _recorder(tmp_path)
    monkeypatch.setenv('PATH', '%s:%s' % (shim.parent, os.environ.get('PATH', '')))
    return shim


# ---------------------------------------------------------------- gap 1: recorder liveness

def test_executable_but_silent_shim_is_not_a_measured_zero(tmp_path):
    """gap1: 'executable' is not 'recording'. A dead recorder yields no clean zero."""
    shim = _write_shim(tmp_path / 'silent' / 'ssh', SILENT_BODY)
    snap = window.snapshot(tmp_path / 'never-written.log', shim_path=shim)
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap


def test_shim_that_records_to_another_log_is_not_bound(tmp_path):
    """gap1: a shim ignoring A13_SSH_LOG cannot vouch for the log we measure."""
    other = tmp_path / 'somewhere-else.log'
    shim = _write_shim(tmp_path / 'hardcoded' / 'ssh', HARDCODED_BODY.replace('__OTHER_LOG__', str(other)))
    snap = window.snapshot(tmp_path / 'never-written.log', shim_path=shim)
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap


def test_the_real_recorder_is_proven_active_by_an_inert_probe(bound, tmp_path):
    """The positive control: a faithful recorder IS proven, by recording, not by stat()."""
    snap = window.snapshot(tmp_path / 'never-written.log', shim_path=bound)
    assert snap['measured'] is True, snap
    assert snap['count'] == 0
    assert snap.get('recorder_active') is True, snap


# --------------------------------------------------- gap 2: log identity + prefix continuity

def test_truncated_log_between_boundaries_is_unavailable(tmp_path, bound):
    """gap2: a log that shrank is not a measurement; delta must be null, never negative."""
    log = _log(tmp_path, calls=3)
    shim = bound
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    log.write_text('2026-09-21T10:00:00Z ssh member-mac true\n')  # truncated
    closed = window.finish('p85r1', log, out_dir, shim_path=shim)
    assert closed['measured'] is False, closed
    assert closed['delta'] is None, closed
    assert closed['claimed_zero_ssh'] is False


def test_replaced_log_between_boundaries_is_unavailable(tmp_path, bound):
    """gap2: a different file at the same path is a different instrument."""
    log = _log(tmp_path, calls=3)
    shim = bound
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    log.unlink()
    log.write_text(''.join(  # same path, new inode, rewritten history
        '2026-09-21T12:0%d:00Z ssh other-host true\n' % i for i in range(5)))
    closed = window.finish('p85r1', log, out_dir, shim_path=shim)
    assert closed['measured'] is False, closed
    assert closed['delta'] is None, closed


def test_appending_to_the_same_log_keeps_the_window_measured(tmp_path, bound):
    """The positive control for gap2: a genuine append-only continuation must still measure."""
    log = _log(tmp_path, calls=3)
    shim = bound
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    log.write_text(log.read_text() + '2026-09-21T11:00:00Z ssh other-host true\n')
    closed = window.finish('p85r1', log, out_dir, shim_path=shim)
    assert closed['measured'] is True, closed
    assert closed['delta'] == 1


def test_finish_against_a_different_log_path_is_unavailable(tmp_path, bound):
    """gap2: the window opens against ONE log; finish must not quietly measure another."""
    log_a = _log(tmp_path, calls=2, name='log-a.log')
    log_b = _log(tmp_path, calls=7, name='log-b.log')
    shim = bound
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log_a, out_dir, shim_path=shim)
    closed = window.finish('p85r1', log_b, out_dir, shim_path=shim)
    assert closed['measured'] is False, closed
    assert closed['delta'] is None, closed
    assert closed['source_log'] == str(log_a)


# ------------------------------------------------------------------ gap 3: unsafe overwrite

def test_force_cannot_overwrite_a_retained_artifact(tmp_path):
    """gap3: force=True must be refused outright; retained evidence is not overwritable."""
    log = _log(tmp_path, calls=1)
    shim = _recorder(tmp_path)
    out_dir = tmp_path / 'evidence'
    window.begin('p85r1', log, out_dir, shim_path=shim)
    before = window.artifact_path(out_dir, 'p85r1').read_bytes()
    with pytest.raises(Exception) as caught:
        window.begin('p85r1', log, out_dir, shim_path=shim, force=True)
    assert not isinstance(caught.value, AttributeError), caught.value
    assert window.artifact_path(out_dir, 'p85r1').read_bytes() == before


def test_the_cli_refuses_force_instead_of_overwriting(tmp_path, bound):
    """gap3: --force must not be a working overwrite switch on the command line."""
    log = _log(tmp_path, calls=1)
    shim = bound
    out_dir = tmp_path / 'evidence'
    common = [sys.executable, '-B', str(HELPER), 'begin', '--label', 'p85r1',
              '--log', str(log), '--out-dir', str(out_dir), '--shim', str(shim)]
    assert subprocess.run(common, capture_output=True, text=True).returncode == 0
    article = window.artifact_path(out_dir, 'p85r1').read_bytes()
    forced = subprocess.run([*common, '--force'], capture_output=True, text=True)
    assert forced.returncode != 0, forced.stdout
    assert window.artifact_path(out_dir, 'p85r1').read_bytes() == article


# --------------------------------------------------------------------- gap 4: begin() race

def test_exclusive_create_never_clobbers_an_existing_artifact(tmp_path):
    """gap4: the create primitive the race is closed with must be atomic AND exclusive."""
    path = tmp_path / 'criterion3-ssh-window-p85r1.json'
    path.write_text('SENTINEL: the first writer won')
    with pytest.raises(Exception) as caught:
        window.write_exclusive(path, {'winner': 'second'})
    assert not isinstance(caught.value, AttributeError), caught.value
    assert path.read_text() == 'SENTINEL: the first writer won'


def test_concurrent_begins_leave_exactly_one_valid_artifact(tmp_path, bound):
    """gap4: N racing begins -> exactly one winner, and the artifact stays parseable."""
    log = _log(tmp_path, calls=1)
    shim = bound
    out_dir = tmp_path / 'evidence'
    common = [sys.executable, '-B', str(HELPER), 'begin', '--label', 'p85r1',
              '--log', str(log), '--out-dir', str(out_dir), '--shim', str(shim)]
    procs = [subprocess.Popen(common, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(8)]
    codes = [p.wait() for p in procs]
    assert codes.count(0) == 1, codes
    assert all(code in (0, window.EXIT_DUPLICATE) for code in codes), codes
    document = json.loads(window.artifact_path(out_dir, 'p85r1').read_text())
    assert document['phase'] in ('before', 'complete'), document


# ------------------------------------------------------ the reusable contract, driven locally
#
# v2: the successor launcher is RETIRED, so the caller's obligations are no longer read out of
# a file -- they are EXERCISED. The harness below is the whole caller: arrange the recorder
# binding, drive the public CLI, read the durable artifact, and refuse to spend a soak on a
# window that cannot be measured. It is a temp-file script with no chain, no round and no
# physical contact, and it is the only thing the four replacements share.

HARNESS_BODY = """#!/bin/sh
# BOUNDED LOCAL HARNESS: the caller's role against the reusable SSH window contract.
set -eu
ACTION="$1"; LABEL="$2"; LOG="$3"; EVDIR="$4"
SHIM_DIR="${A13_HARNESS_SHIM_DIR:?}"
HELPER="${A13_HARNESS_HELPER:?}"
PY="${A13_HARNESS_PYTHON:?}"

if [ ! -x "$SHIM_DIR/ssh" ]; then
  echo "REFUSING to spend the soak: no executable recorder at $SHIM_DIR/ssh"
  exit 9
fi

export PATH="$SHIM_DIR:$PATH"
export A13_SSH_LOG="$LOG"
mkdir -p "$EVDIR"

set +e
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$PY" -B "$HELPER" "$ACTION" \\
    --label "$LABEL" --log "$LOG" --out-dir "$EVDIR" --shim "$SHIM_DIR/ssh" \\
    > "$EVDIR/$ACTION.out" 2>&1
helper_status=$?
set -e

if [ "$helper_status" -ne 0 ]; then
  echo "REFUSING to spend the soak: the helper exited $helper_status"
  exit 9
fi

FILE=$(sed -n 's/^SSH_WINDOW_EVIDENCE_FILE //p' "$EVDIR/$ACTION.out" | tail -1)
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "REFUSING to spend the soak: the helper persisted no artifact"
  exit 9
fi

# The decision is made on the DURABLE ARTIFACT, not on the noise in the log: at `begin` the
# document is legitimately not-yet-measured (an opened window is not a claim), so the caller
# branches on the BEFORE BOUNDARY READING -- exactly as the retired caller did -- and at
# `finish` on the window verdict.
MEASURED=$("$PY" -c '
import json, sys
with open(sys.argv[1]) as handle:
    document = json.load(handle)
if sys.argv[2] == "begin":
    print("yes" if (document.get("before") or {}).get("measured") else "no")
else:
    print("yes" if document.get("measured") else "no")
' "$FILE" "$ACTION")
if [ "$MEASURED" != "yes" ]; then
  echo "REFUSING to spend the soak: the window is UNAVAILABLE at $ACTION"
  exit 9
fi
echo "WINDOW $ACTION OK"
exit 0
"""


@pytest.fixture
def local_window_harness(tmp_path):
    """Drive the contract exactly as a caller must: bound, public, durable, fail-closed.

    `shim_dir` is BOTH the directory the harness puts first on PATH and the recorder it
    declares to the helper, so the recorder it uses is the recorder name resolution finds --
    the binding is arranged and then MEASURED by the helper, never asserted by the test.
    """
    script = _write_shim(tmp_path / 'harness' / 'run-window.sh', HARNESS_BODY)

    def run(action, label, log, out_dir, shim_dir, extra_env=None):
        env = {k: v for k, v in os.environ.items()
               if k not in ('A13_SSH_LOG', 'A13_SSH_REAL', 'PYTHONPATH', 'PYTHONHOME',
                            'VIRTUAL_ENV')}
        env.update({'A13_HARNESS_SHIM_DIR': str(shim_dir),
                    'A13_HARNESS_HELPER': str(HELPER),
                    'A13_HARNESS_PYTHON': sys.executable})
        if extra_env:
            env.update(extra_env)
        return subprocess.run([str(script), action, label, str(log), str(out_dir)],
                              capture_output=True, text=True, env=env)

    return run


def _files_under(root, skip=()):
    """path -> (size, sha256) for every FILE under `root`, minus the given subtrees."""
    state = {}
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        if any(str(path).startswith(str(entry)) for entry in skip):
            continue
        state[str(path.relative_to(root))] = (
            path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return state


def test_a_whole_window_moves_the_artifact_and_nothing_else(local_window_harness, tmp_path):
    """The contract's bounded scope, asserted BEHAVIOURALLY (v2 replacement for the retired
    launcher's "only criterion-3 measurement changed" comparison).

    One window -- opened and closed through the public CLI, around a call that really does
    land in the audit log -- must: account for exactly that call in the delta, leave the
    retained log a byte-exact append-only continuation, and create or change NOTHING outside
    its own artifact directory. The append proof itself writes no byte to the log it measures.
    """
    log = _log(tmp_path, calls=2)
    out_dir = tmp_path / 'evidence'
    before_tree = _files_under(tmp_path, skip=(tmp_path / 'harness', out_dir))

    opened = local_window_harness('begin', 'v2w1', log, out_dir, REAL_SHIM.parent)
    assert opened.returncode == 0, opened.stdout + opened.stderr
    assert 'WINDOW begin OK' in opened.stdout, opened.stdout

    # the ssh call the window exists to measure: recorded by the BOUND recorder
    subprocess.run([str(REAL_SHIM), 'member-mac', 'true'],
                   env=dict(os.environ, A13_SSH_LOG=str(log)),
                   capture_output=True, text=True)

    closed = local_window_harness('finish', 'v2w1', log, out_dir, REAL_SHIM.parent)
    assert closed.returncode == 0, closed.stdout + closed.stderr

    document = json.loads(window.artifact_path(out_dir, 'v2w1').read_text())
    assert document['phase'] == 'complete', document
    assert document['measured'] is True, document
    assert document['delta'] == 1, document            # exactly the one call in the window
    assert document['zero_ssh_within_window'] is False, document
    assert document['log_continuity']['ok'] is True, document
    assert document['before']['target_log_probe']['append_open']['bytes_written'] == 0, document
    assert document['before']['target_log_probe']['append_open']['open_flags'] == \
        'O_WRONLY|O_APPEND', document
    assert document['after']['target_log_probe']['target_untouched'] is True, document

    after_tree = _files_under(tmp_path, skip=(tmp_path / 'harness', out_dir))
    assert set(after_tree) == set(before_tree), (before_tree, after_tree)
    changed = [name for name in after_tree if after_tree[name] != before_tree[name]]
    assert changed == [str(log.relative_to(tmp_path))], (changed, before_tree, after_tree)


def test_the_reusable_caller_refuses_to_spend_a_soak_on_a_dead_recorder(
        local_window_harness, tmp_path):
    """v2 replacement for the retired launcher's fail-closed check, end to end.

    The recorder the harness arranges is executable and exits 0 but records NOTHING -- the
    silent instrument a stat cannot distinguish from a working one. The caller must not go on
    to spend anything on that window: it exits 9 saying it refuses, while the durable artifact
    still carries the honest finding -- UNAVAILABLE, no count, and never a clean zero.
    """
    dead_dir = tmp_path / 'dead'
    _write_shim(dead_dir / 'ssh', SILENT_BODY)
    log = _log(tmp_path, calls=1)
    out_dir = tmp_path / 'evidence'

    result = local_window_harness('begin', 'v2w2', log, out_dir, dead_dir)
    assert result.returncode == 9, result.stdout + result.stderr
    assert 'REFUSING to spend the soak' in result.stdout, result.stdout

    document = json.loads(window.artifact_path(out_dir, 'v2w2').read_text())
    assert document['measured'] is False, document
    assert document['zero_ssh_within_window'] is False, document
    assert document['claimed_zero_ssh'] is False, document
    before = document['before']
    assert before['recorder_active'] is False, before
    assert before['count'] is None, before
    assert before['recorder_reason'] == 'ssh_shim_ran_but_wrote_no_log', before


def test_the_helper_can_never_reach_real_ssh():
    """The probe launches the shim, so the module must force an INERT real binary."""
    text = HELPER.read_text()
    assert '/usr/bin/ssh' not in text, 'the helper must not name the ssh binary'
    assert "A13_SSH_REAL" in text, 'the helper must pin A13_SSH_REAL itself'
    assert 'INERT_SSH_REAL' in text or '/usr/bin/true' in text, text[:400]
    assert 'socket' not in text


# =====================================================================================
# r3 -- the CLAIM INSTRUMENT BINDING. r2 proved a recorder records; it did not prove
# that the recorder it proved is the one the CONTROLLED CHAIN ENVIRONMENT arranges, nor
# that the recorder can write the log the chain actually measures. A private probe log
# and a PATH the probe invented for itself can both vouch for an instrument that the
# chain's own children would never reach -- and an absent log then reads as a clean zero.
# =====================================================================================


def test_a_recorder_that_is_not_the_ambient_ssh_is_not_a_measured_zero(tmp_path):
    """gap A: the chain's children resolve `ssh` through PATH; the probe must not invent one.

    r2 forced `PATH=shim_dir:$PATH` before resolving `ssh`, so `shim_path_bound` was true
    by construction and an executable-but-unbound recorder still produced
    `measured=true, count=0` for a log nothing was writing to. That is the false clean.
    """
    shim = _recorder(tmp_path)                       # records, but its dir is NOT on PATH
    log = tmp_path / 'never-written.log'
    snap = window.snapshot(log, shim_path=shim)
    assert snap['shim_on_ambient_path'] is False, snap
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap
    assert snap['reason'] == 'ssh_shim_is_not_the_ssh_that_path_resolution_finds', snap


def test_a_recorder_that_cannot_write_the_target_log_is_not_a_measured_zero(bound, tmp_path):
    """gap B: a private temp probe log cannot vouch for the REAL target log's write path.

    The shim appends with `>> "$log" 2>/dev/null`, so an unwritable target directory fails
    SILENTLY. r2 measured such a window as a clean zero ("no ssh call was made") when in
    truth no call could ever have been recorded.
    """
    locked = tmp_path / 'locked-evidence'
    locked.mkdir()
    log = locked / 'criterion3-ssh-calls.log'
    locked.chmod(0o555)                              # ground truth: prove it is unwritable
    try:
        subprocess.run([str(bound), 'member-mac', 'true'],
                       env=dict(os.environ, A13_SSH_LOG=str(log), A13_SSH_REAL='/usr/bin/true'),
                       capture_output=True, text=True)
        assert not log.exists(), 'the fixture must really stop the recorder writing'
        snap = window.snapshot(log, shim_path=bound)
    finally:
        locked.chmod(0o755)
    assert snap['recorder_active'] is True, snap      # the private probe still passes ...
    assert snap['target_log_writable'] is False, snap  # ... the real target log cannot be written
    assert snap['measured'] is False, snap            # ... but the TARGET log is not writable
    assert snap['count'] is None, snap


def test_the_measured_log_must_be_the_declared_audit_log(bound, tmp_path, monkeypatch):
    """gap D: the instrument measured must be the instrument the environment declares.

    The chain binds the audit log with `export A13_SSH_LOG=...` and the shim reads the same
    variable. A window opened against some OTHER path is measuring a different instrument --
    it must be UNAVAILABLE, not a measured zero.
    """
    declared = tmp_path / 'criterion3-ssh-calls-declared.log'
    monkeypatch.setenv('A13_SSH_LOG', str(declared))
    snap = window.snapshot(tmp_path / 'some-other.log', shim_path=bound)
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap
    assert snap['reason'] == 'measured_log_is_not_the_declared_audit_log', snap


def test_count_and_the_fingerprint_describe_one_read_of_the_log(bound, tmp_path, monkeypatch):
    """gap C: two reads let the count and the digest describe two different instrument states.

    r2 counted through `ssh_audit.invocations()` (a `read_text`) and digested through a
    SEPARATE `read_bytes`. Here the log answers those two reads differently: the count and
    the fingerprint disagree, so continuity could be judged against bytes that no count came
    from. One read is the fix.
    """
    log = _log(tmp_path, calls=3)
    state_a = ''.join('2026-09-21T10:0%d:00Z ssh member-mac true\n' % i for i in range(3))
    state_b = ''.join('2026-09-21T12:0%d:00Z ssh other-host true\n' % i for i in range(7))
    real_read_text, real_read_bytes = pathlib.Path.read_text, pathlib.Path.read_bytes

    def read_text(self, *args, **kwargs):
        return state_a if str(self) == str(log) else real_read_text(self, *args, **kwargs)

    def read_bytes(self, *args, **kwargs):
        return state_b.encode() if str(self) == str(log) else real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, 'read_text', read_text)
    monkeypatch.setattr(pathlib.Path, 'read_bytes', read_bytes)
    snap = window.snapshot(log, shim_path=bound)
    expected = {3: hashlib.sha256(state_a.encode()).hexdigest(),
                7: hashlib.sha256(state_b.encode()).hexdigest()}
    assert snap['count'] in expected, snap
    assert snap['log_fingerprint']['sha256'] == expected[snap['count']], snap


def test_the_probe_does_not_force_its_own_path():
    """gap A, structurally: the probe must measure the environment, not arrange one.

    `env['PATH'] = '%s:%s' % (shim_dir, ambient)` made the PATH binding vacuous -- it asked
    whether the shim is on a PATH that was constructed to contain it.
    """
    text = HELPER.read_text()
    assert "env['PATH'] = '%s:%s' % (shim_dir, ambient)" not in text
    assert "which('ssh', path=env['PATH'])" not in text


def test_the_probe_binds_the_real_target_log_write_path():
    """gap B, structurally: the probe must be told which log the window will measure."""
    text = HELPER.read_text()
    assert 'def probe_recorder(shim_path, target_log' in text, text[:800]
    assert 'ssh_recorder_cannot_write_the_target_log' in text


def test_the_binding_the_contract_requires_is_arranged_and_measured(local_window_harness,
                                                                   tmp_path):
    """Binding, both ways (v2 replacement for the retired launcher's PATH-binding check).

    The requirement is only meaningful in both directions, so both are exercised through the
    contract itself rather than read out of a caller's text:

      * ARRANGED -- the recorder's directory is first on PATH and is the recorder declared to
        the helper, so the window is MEASURED on the binding (resolved by name, not assumed);
      * UNARRANGED -- `ssh` is not what the measured PATH resolves, and the very same call must
        come back UNAVAILABLE with a named reason. Never a clean zero.
    """
    out_dir = tmp_path / 'evidence'
    log = _log(tmp_path, calls=0)
    arranged = local_window_harness('begin', 'v2w3', log, out_dir, REAL_SHIM.parent)
    assert arranged.returncode == 0, arranged.stdout + arranged.stderr
    assert 'WINDOW begin OK' in arranged.stdout, arranged.stdout
    bound_before = json.loads(window.artifact_path(out_dir, 'v2w3').read_text())['before']
    assert bound_before['shim_path_bound'] is True, bound_before
    assert bound_before['shim_on_ambient_path'] is True, bound_before
    assert bound_before['recorder_active'] is True, bound_before
    assert bound_before['measured'] is True, bound_before

    empty_path = tmp_path / 'no-ssh-here'
    empty_path.mkdir()
    orphan_log = _log(tmp_path, calls=0, name='orphan-criterion3.log')
    orphan_dir = tmp_path / 'orphan-evidence'
    orphan_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if k != 'A13_SSH_LOG'}
    env['PATH'] = str(empty_path)
    unarranged = subprocess.run(
        [sys.executable, '-B', str(HELPER), 'begin', '--label', 'v2w4',
         '--log', str(orphan_log), '--out-dir', str(orphan_dir)],
        capture_output=True, text=True, env=env)
    assert unarranged.returncode == window.EXIT_OK, unarranged.stderr
    assert 'SSH_WINDOW_EVIDENCE_UNAVAILABLE' in unarranged.stdout, unarranged.stdout
    unbound_before = json.loads(
        window.artifact_path(orphan_dir, 'v2w4').read_text())['before']
    assert unbound_before['measured'] is False, unbound_before
    assert unbound_before['count'] is None, unbound_before
    assert unbound_before['shim_path_bound'] is False, unbound_before
    assert unbound_before['recorder_reason'] == \
        'ssh_shim_is_not_the_ssh_that_path_resolution_finds', unbound_before


# =====================================================================================
# r4 -- the REAL-LOG WRITE PROOF. r3 proved the recorder can CREATE a file in the target
# log's directory; it never proved the recorder can APPEND to the target log that already
# exists, never required the real log to be left BYTE-EXACT, and derived the reading's
# identity from a SECOND lookup of the pathname instead of the descriptor it read.
# =====================================================================================

REAL_SHIM = ROOT / 'scripts/a13_operator/ssh-shim/ssh'


@pytest.fixture
def real_bound(tmp_path, monkeypatch):
    """The REAL repo recorder, bound exactly as the chain binds it (its directory on PATH)."""
    monkeypatch.setenv('PATH', '%s:%s' % (REAL_SHIM.parent, os.environ.get('PATH', '')))
    assert REAL_SHIM.is_file(), REAL_SHIM
    return REAL_SHIM


def _state(path):
    """The FULL state of the log a real mutation would disturb -- not just its existence."""
    if not path.exists():
        return {'present': False}
    info = path.stat()
    return {'present': True, 'inode': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def test_an_existing_readonly_target_log_is_not_a_measured_zero(real_bound, tmp_path):
    """r4, the false clean: a writable DIRECTORY says nothing about an unwritable LOG.

    r3 proved the write path by recording into a private SIBLING of the real log. For a log
    that ALREADY EXISTS and is readable but not appendable (0444), the sibling probe passes
    while the recorder's own `>> "$log"` can never land -- so every ssh call the window was
    supposed to count would be dropped silently, and the window still read `measured=true`.

    Ground truth is measured here, not assumed: the REAL shim is run against the 0444 log and
    the log is shown byte-identical afterwards.
    """
    if os.geteuid() == 0:
        pytest.skip('as root the 0444 write bit does not deny the recorder: the fixture cannot fail closed')
    log = _log(tmp_path, calls=4)
    log.chmod(0o444)
    before = _state(log)
    subprocess.run([str(real_bound), 'member-mac', 'true'],
                   env=dict(os.environ, A13_SSH_LOG=str(log), A13_SSH_REAL='/usr/bin/true'),
                   capture_output=True, text=True)
    assert _state(log) == before, 'ground truth: the real recorder cannot append to this log'
    snap = window.snapshot(log, shim_path=real_bound)
    assert snap['target_log_writable'] is False, snap
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap
    assert snap['reason'] == 'ssh_recorder_cannot_write_the_target_log', snap
    probe = snap['target_log_probe']
    assert probe['evidence'] == 'non_destructive_append_open', probe
    assert probe['append_open']['append_open_ok'] is False, probe
    assert probe['append_open']['append_open_errno'] is not None, probe
    assert probe['append_open']['bytes_written'] == 0, probe
    assert probe['append_open']['mode_octal'] == '0444', probe
    assert probe['target_untouched'] is True, probe
    assert _state(log) == before, 'the measurement itself must leave the retained log as found'


def test_the_untouched_comparison_is_of_the_whole_state_not_just_existence(tmp_path):
    """r4: `target_untouched` was `path.exists() == was_present`, while the prose claimed
    byte-exactness. An append to the real log leaves it present, so the existence-only
    comparison reported "untouched" for a log whose bytes had changed underneath it.

    The comparison must be of the whole state -- identity, size, mtime and content digest --
    so that an append between two readings is visible even though presence is not.
    """
    log = _log(tmp_path, calls=2)
    compare = getattr(window, '_target_log_state', None)
    assert callable(compare), (
        'the candidate compares only path.exists(), so an appended-to real log still '
        'reports target_untouched=true; a whole-state comparison is required')
    before = compare(log)
    assert compare(log) == before, 'an untouched log must compare equal to itself'
    with open(str(log), 'ab') as handle:                # presence is unchanged by an append
        handle.write(b'2026-09-21T13:00:00Z ssh appended-behind-the-measurement\n')
    after = compare(log)
    assert after['present'] is True and before['present'] is True, (before, after)
    assert after != before, (before, after)
    assert after['size'] > before['size']
    assert after['mtime_ns'] >= before['mtime_ns']
    assert after['sha256'] != before['sha256']


def test_a_probe_that_writes_to_the_real_log_is_not_untouched(bound, tmp_path, monkeypatch):
    """r4, fault injection: a probe that APPENDS to the real log must not read as untouched.

    The proof of writability is the recorder's own `O_WRONLY|O_CREAT|O_APPEND` open on the real
    log. This test makes that open leak a line into the real log -- the behaviour the prose
    forbids -- and requires the measurement to catch it. Under the r3 candidate the log is
    opened for append nowhere, so the injection cannot even fire: there was no such proof.
    """
    log = _log(tmp_path, calls=2)
    before = _state(log)
    injected = []
    real_open = os.open

    def leaking_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path) == str(log) and (flags & os.O_APPEND):
            leak = real_open(str(log), os.O_WRONLY | os.O_APPEND)   # the forbidden write
            try:
                os.write(leak, b'2026-09-21T13:00:00Z ssh leaked-into-the-real-log\n')
            finally:
                os.close(leak)
            injected.append(True)
        return fd

    monkeypatch.setattr(os, 'open', leaking_open)
    snap = window.snapshot(log, shim_path=bound)
    assert injected, ('the proof never opened the real log for append, so it never proved the '
                      'log this window is measured from can be appended to')
    assert _state(log) != before, 'the fixture must really have changed the real log'
    assert snap['target_log_probe']['target_untouched'] is False, snap
    assert snap['target_log_writable'] is False, snap
    assert snap['measured'] is False, snap
    assert snap['count'] is None, snap


def test_a_target_log_that_vanishes_before_the_open_is_not_recreated(bound, tmp_path, monkeypatch):
    """r5, the check/open race: the append proof is a CHECK (`the log exists`) then an OPEN.

    The proof opens the existing log with the shim's own flags
    `O_WRONLY|O_CREAT|O_APPEND`. `O_CREAT` is only "harmless" for as long as the log is still
    there: a log unlinked between the observation that saw it and the open itself is
    RESURRECTED by the instrument that is supposed to prove it can be appended to. That is a
    probe MUTATING the retained evidence -- it fabricates a zero-byte log exactly where the
    chain's fresh-label law (`[ ! -e "$A13_SSH_LOG" ]`) requires none -- and the whole-state
    comparison cannot see it as a creation, only as "some different file is there now".

    Reproduced deterministically at the `os.open` seam: the log is unlinked immediately before
    the append open. The window must read UNAVAILABLE, and the log must NOT exist afterwards.
    """
    log = _log(tmp_path, calls=2)
    real_open = os.open
    vanished = []

    def vanishing_open(path, flags, *args, **kwargs):
        if (str(path) == str(log) and flags & os.O_WRONLY and flags & os.O_APPEND
                and not vanished):
            vanished.append(True)
            os.unlink(str(log))          # the target disappears between check and open
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', vanishing_open)
    snap = window.snapshot(log, shim_path=bound)
    assert vanished, ('the proof never opened the real log for append, so it never proved the '
                      'log this window is measured from can be appended to')
    assert not log.exists(), ('the probe RECREATED the log it was supposed to leave alone: a '
                              'created file is a mutation of the retained evidence')
    probe = snap['target_log_probe']
    assert snap['measured'] is False, snap
    assert snap['target_log_writable'] is False, snap
    assert snap['count'] is None, snap
    assert snap['reason'] == 'ssh_recorder_cannot_write_the_target_log', snap
    assert probe['target_state_after']['present'] is False, probe
    assert probe['append_open']['append_open_ok'] is False, probe
    assert probe['append_open']['append_open_errno'] == errno.ENOENT, probe
    assert probe['append_open']['bytes_written'] == 0, probe
    assert probe['append_open']['open_flags'] == 'O_WRONLY|O_APPEND', probe
    # The log vanished by someone else's hand: the probe must not accuse itself of the change
    # and bury the append error that names what actually happened (ENOENT on that path).
    assert 'modified' not in (probe['error'] or ''), probe


def test_the_reading_identity_comes_from_the_descriptor_it_read(tmp_path):
    """r4: `_audit_read` read the bytes from the PATH and then stat'ed the PATHNAME.

    Two lookups of one pathname are two instruments: a log replaced between them yields the
    old file's bytes carrying the new file's identity, under a single "one read" claim --
    and `_continuity` then judges the append-only prefix against an identity no count came
    from. Identity must come from an `fstat` of the SAME descriptor the bytes were read from.
    """
    log = _log(tmp_path, calls=3)
    original = log.read_bytes()
    original_inode = log.stat().st_ino
    decoy = _log(tmp_path, calls=7, name='decoy.log')
    swapped = []
    real_read, real_read_bytes = os.read, pathlib.Path.read_bytes

    def swap_once():
        if not swapped:
            swapped.append(True)
            os.replace(str(decoy), str(log))      # replacement lands at the log's path

    def replacing_read(fd, count):
        chunk = real_read(fd, count)
        swap_once()
        return chunk

    def replacing_read_bytes(self, *args, **kwargs):
        data = real_read_bytes(self, *args, **kwargs)
        if str(self) == str(log):
            swap_once()
        return data

    os.read = replacing_read
    pathlib.Path.read_bytes = replacing_read_bytes
    try:
        reading = window._audit_read(str(log))
    finally:
        os.read = real_read
        pathlib.Path.read_bytes = real_read_bytes
    assert swapped, 'the fixture must really replace the log during the read'
    assert log.stat().st_ino != original_inode, 'the replacement must really have landed'
    assert reading['raw'] == original
    assert reading['count'] == 3
    fingerprint = reading['fingerprint']
    assert fingerprint['inode'] == original_inode, fingerprint
    assert fingerprint['size'] == len(reading['raw'])
    assert fingerprint['sha256'] == hashlib.sha256(reading['raw']).hexdigest()


def test_the_measurement_leaves_the_retained_log_byte_identical(bound, tmp_path):
    """The positive control: proving writability must not itself touch the retained log."""
    log = _log(tmp_path, calls=5)
    before = _state(log)
    snap = window.snapshot(log, shim_path=bound)
    assert snap['measured'] is True, snap
    assert snap['count'] == 5
    assert snap['target_log_probe']['target_untouched'] is True, snap
    assert snap['target_log_probe']['probe_residue'] is False, snap
    assert _state(log) == before


def test_the_append_proof_opens_the_real_log_without_writing_to_it():
    """r4, structurally: the append proof IS an open of the existing log in append mode.

    A canary appended to the real log is forbidden (retained evidence, fresh-label law), and
    the shim is never pointed at the real log -- so the proof has to be the non-destructive
    open the shim's own `>>` performs, plus an `fstat`-based read.
    """
    text = HELPER.read_text()
    assert 'os.O_APPEND' in text, 'the append proof must mirror the shim\'s own open flags'
    assert 'os.fstat' in text, 'the identity must come from the descriptor that was read'
    assert "env['A13_SSH_LOG'] = str(target_log)" not in text
    assert "env['A13_SSH_LOG'] = target_log" not in text
