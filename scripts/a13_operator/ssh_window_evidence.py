# SPDX-License-Identifier: AGPL-3.0-only
"""Retain the criterion-3 SSH acceptance window as a DURABLE artifact.

Why this exists (measured, p84r1): the round chain printed its before/after/delta
boundary counts to STDOUT, and the chain pipes that whole measurement block through
`tail -30` (chain-preview84e.sh, the `env ... <<'PY' 2>&1 | tail -30` line). The two
boundary lines fell off the tail, so ROUND-RESULT.json recorded
`soak_ssh_window: null` while the full audit log
(`criterion3-ssh-calls-p84r1.log`, 119 invocations) was retained on disk. `null` was
the honest answer and the assembler was right not to read it as 0 -- but the
criterion-3 measurement itself was LOST.

The repair is NOT to widen the tail. Stdout is a lossy channel there by design, and a
number that only exists inside a `tail -30` window is not evidence. (Widening the tail
would also re-introduce the interpreter-contamination bug the clean-environment commit
fb0314c fixed.) The repair is to make the measurement write its own durable JSON
document, the same move this project already made for the seed window (07408a3) and the
worker guard (e389a84).

SECOND REPAIR (r2) -- the first version of this module still failed OPEN in four ways,
each of which could turn a dead instrument into a clean acceptance:

  gap1  it accepted `shim is a file and executable` as proof of a working recorder. That
        is a STAT, not a measurement: an executable shim that records nothing, or records
        to some other log, still produced `measured=true, count=0` -- and a missing log
        then read as a clean zero for the wrong reason. This version PROVES the recorder
        by making it record once into a private probe log (`probe_recorder`), and binds the
        probe the way the chain binds it (the shim's directory first on PATH, `ssh`
        resolved BY NAME), so a shim that is not the `ssh` name resolution finds is caught.
  gap2  `finish()` could be handed a DIFFERENT log than `begin()` opened the window against,
        and nothing required the log to still be an append-only continuation of the one that
        was measured. A truncated or replaced log therefore produced a negative or
        meaningless delta. This version records the log's identity (device+inode) and full
        digest at `begin`, and at `finish` requires the SAME path, the SAME file, and the
        recorded bytes as an exact PREFIX -- otherwise UNAVAILABLE, delta null.
  gap3  `force=True` / `--force` overwrote retained evidence. Overwriting is now refused
        outright (`UnsafeOverwrite`; the CLI rejects `--force` as a usage error).
  gap4  `begin()` was `exists()` then `os.replace()`, a check-then-act race: two concurrent
        begins could both pass the check and the second silently clobber the first.
        `write_exclusive()` now creates the document with mkstemp + `os.link()`, which fails
        with EEXIST instead of clobbering, and `finish()` takes an exclusive lock so two
        closers cannot interleave.

THIRD REPAIR (r3) -- r2 proved that a recorder records, but it proved it with an instrument
the CONTROLLED CHAIN ENVIRONMENT might never reach, and it could still read as a clean zero:

  gapA  `probe_recorder` FORCED a PATH of its own (`shim_dir:$PATH`) before resolving `ssh`,
        so `shim_path_bound` was true by construction, and `shim_on_ambient_path` was only
        RECORDED, never required. A recorder that is not the `ssh` the chain's children
        resolve still produced `measured=true, count=0` for a log nothing was writing to.
        The probe now measures the PATH this process actually has -- the one the chain's
        children inherit -- and launches `ssh` BY NAME through it, so the binding it proves
        is the binding the window depends on.
  gapB  the probe wrote to a PRIVATE temp log, so it never established that the recorder can
        write the REAL target log. The shim appends with `>> "$log" 2>/dev/null`, so an
        unwritable evidence directory fails SILENTLY and a window that could never be
        recorded read as the honest-looking zero `no_ssh_call_was_made`.
        `probe_recorder(..., target_log=...)` now records into a private SIBLING of the real
        log, in the real log's directory, and requires the real log to be left untouched.
  gapC  the count and the digest were TWO SEPARATE READS of the log (`read_text` for the
        count, `read_bytes` for the digest), so they could describe two different instrument
        states -- continuity judged against bytes no count came from. `_audit_read` reads
        ONCE and derives both from those same bytes.
  gapD  the helper measured whichever `log` it was handed, even when the environment declared
        a different audit log (`A13_SSH_LOG`). A window measuring an instrument the chain did
        not bind is refused rather than reported as a measured number.

FOURTH REPAIR (r4) -- r3 proved the recorder could create a file in the target log's
DIRECTORY; it never proved the recorder could APPEND to the log that file already is, it
compared the real log's EXISTENCE while claiming byte-exactness, and it derived the reading's
identity from a second lookup of the pathname:

  gapB2 `_probe_target_log_write` recorded a canary into a private SIBLING of the real log.
        That proves the directory accepts a NEW file. A log that already EXISTS and is
        readable but not appendable (0444 in a writable directory) still reads
        `target_log_writable=true`, so the window reports `measured=true` while the recorder's
        own `>> "$log" 2>/dev/null` drops every call silently. The existing log is now proven
        by performing the RECORDER'S OWN open (`O_WRONLY|O_APPEND` -- the shim's append mode,
        see r5) and closing it WITHOUT writing a byte -- appending a canary to the real log is
        forbidden (retained evidence, fresh-label law) -- corroborated by the mode bits and
        `os.access(W_OK)`, which are mode/access-level evidence only, never ACL proof.
  gapB3 `target_untouched` was `path.exists() == was_present`. A probe path that appended to
        the real log left it present, so the existence-only comparison reported "untouched"
        while the prose claimed the log was left exactly as found. The comparison is now of
        the whole state -- present/is-file/device/inode/size/mtime-ns/mode/sha256 -- and the
        probe must also leave no `.ssh-window-writeprobe-*` sibling behind (`probe_residue`).
  gapC2 `_audit_read` read the bytes through `path.read_bytes()` and then derived
        device+inode with `os.stat(str(path))`: a SECOND, independent lookup of the pathname.
        A log replaced between the two yields one instrument's bytes carrying another
        instrument's identity, under this module's own "one read" claim, and `_continuity`
        then verified the append-only prefix against an identity no count came from. Bytes and
        identity now come from ONE `os.open` + `os.fstat(fd)` on the same descriptor.

FIFTH REPAIR (r5) -- r4's append proof was a CHECK ("the log exists") followed by an OPEN with
the shim's flags, `O_WRONLY|O_CREAT|O_APPEND`:

  gapD1 `O_CREAT` was justified as harmless "precisely because this is only ever called for a
        log that already exists" -- an assumption about the interval between the observation
        and the open, not a property of the open. A log unlinked in that interval was
        RESURRECTED by the very probe that is supposed to prove it can be appended to: the
        probe mutated the retained evidence, fabricating a zero-byte log exactly where the
        chain's fresh-label law (`[ ! -e "$A13_SSH_LOG" ]`) requires none. The probe now opens
        `O_WRONLY|O_APPEND` -- the recorder's append MODE without its create. A vanished target
        fails the open with `ENOENT` -> UNAVAILABLE, and the log stays gone.

Claim boundary -- enforced by the code below, not by convention:
  * an ABSENT audit log is NOT automatically zero ssh calls. `ssh_audit.report()` reads
    an absent log as NO_SSH because it is a COUNTING helper. This module is a CLAIM
    helper: it must never turn a missing instrument into a clean acceptance.
  * therefore "log absent" is only a MEASURED zero when the recorder has been proven
    ACTIVE -- it recorded a canary into a probe log, honoring A13_SSH_LOG, as the `ssh`
    that PATH resolution finds.
  * the recorder proven must be the recorder THIS window depends on: the `ssh` the measured
    PATH resolves (the chain's binding), able to write the REAL target log's path -- and when
    that log already exists, able to OPEN IT FOR APPEND (`O_WRONLY|O_APPEND`, proven without
    writing a byte to it, and WITHOUT `O_CREAT`: the proof must never create the file it is
    measuring -- r5). A writable directory is not a
    writable log. A probe that had to arrange its own PATH, that only ever wrote a private
    temp log, or that only ever created a NEW sibling file, proves nothing about this window
    and is UNAVAILABLE, never a measured zero.
  * the instrument measured must be the instrument the CONTROLLED environment declares: when
    `A13_SSH_LOG` is set, a window opened against any other path is UNAVAILABLE.
  * an unreadable log is UNAVAILABLE with the exception recorded, never 0.
  * a log that changed identity, was truncated, or is not an append-only continuation of
    the log the window opened against is UNAVAILABLE, never a delta.
  * the artifact refuses to overwrite an existing one for the same label (the chain's
    fresh-label law, `[ ! -e "$A13_SSH_LOG" ]`). A second run under a used label is a
    finding, not a silent overwrite.

INERTNESS. Importing this module performs nothing. The single process it may launch is
the RECORDING SHIM ITSELF, during `probe_recorder`, with `A13_SSH_REAL` pinned to an inert
binary -- so the shim's `exec` never reaches an ssh client and never touches the network.
No operator entrypoint is imported, no app is contacted, no chain is run.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_TEMPLATE = 'criterion3-ssh-window-%s.json'
PROTOCOL = 'mycelium.a13.criterion3_ssh_window.v2'
DEFAULT_LOG = '/tmp/asm/criterion3-ssh-calls.log'

HERE = Path(__file__).resolve().parent
# The exact shim the chain puts first on PATH. Passed explicitly by the chain; this
# default keeps the module usable on its own from the repo.
DEFAULT_SHIM = HERE / 'ssh-shim' / 'ssh'

# The probe's "real" binary. The shim ends with `exec "${A13_SSH_REAL:-<the ssh client>}"`,
# and the probe pins this variable so the exec can only ever reach an inert binary. The real
# ssh client's absolute path is deliberately NOT spelled anywhere in this file.
INERT_SSH_REAL = '/usr/bin/true'
RECORDER_CANARY = 'SSH_WINDOW_RECORDER_PROBE'

MEASURED = 'SSH_WINDOW_MEASURED'
UNAVAILABLE = 'SSH_WINDOW_UNAVAILABLE'

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DUPLICATE = 6
EXIT_MISSING_BEGIN = 7
EXIT_FINISH_FAILED = 8
EXIT_RECORDER_INACTIVE = 9
EXIT_UNSAFE_OVERWRITE = 10

PROBE_TIMEOUT_S = 20.0


class WindowEvidenceError(Exception):
    """Base class: the measurement could not be completed as asked."""


class DuplicateArtifact(WindowEvidenceError):
    """An artifact for this label already exists; refusing to overwrite."""


class MissingBegin(WindowEvidenceError):
    """finish() was called with no before() artifact on disk to measure against."""


class UnsafeOverwrite(WindowEvidenceError):
    """An overwrite was asked for. Retained evidence is never overwritten."""


def artifact_path(out_dir, label):
    """Where the durable criterion-3 window document for `label` lives."""
    return Path(out_dir) / (ARTIFACT_TEMPLATE % label)


def _load_audit():
    """Load the sibling ssh audit counter by path (same pattern as the chain)."""
    spec = importlib.util.spec_from_file_location('ssh_audit', HERE / 'ssh_audit.py')
    if spec is None or spec.loader is None:
        raise WindowEvidenceError('cannot load the ssh audit counter from %s' % (HERE / 'ssh_audit.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now():
    return datetime.now(timezone.utc).isoformat()


def _same_instrument_path(left, right):
    """Do two spellings name the same audit-log path? (macOS /tmp vs /private/tmp.)"""
    try:
        return os.path.realpath(str(left)) == os.path.realpath(str(right))
    except OSError:
        return str(left) == str(right)


def _invocation_count(raw):
    """Count the ssh invocations in bytes we ALREADY hold.

    Uses `ssh_audit`'s own predicate, so there is exactly ONE definition of what an ssh
    invocation is in this project, and no second read of the log: the count and the digest
    below are derived from the same bytes by construction (r3, gapC).
    """
    text = raw.decode('utf-8', 'replace')
    predicate = _load_audit()._is_invocation
    return sum(1 for line in text.splitlines() if line.strip() and predicate(line))


def _open_and_read_once(path):
    """Open `path` ONCE; take BOTH the bytes and the identity from that SAME descriptor.

    r3 still derived the fingerprint's device+inode with `os.stat(str(path))` AFTER reading
    the bytes through `path.read_bytes()` -- a second, independent lookup of the PATHNAME. A
    log replaced between the read and the stat then yielded one instrument's bytes carrying
    another instrument's identity, under this module's own "one read" claim, and `_continuity`
    verified the append-only prefix against an identity that no count came from. `os.fstat`
    on the descriptor the bytes came from cannot disagree with itself (r4, gapC2).

    Returns `(raw_bytes, stat_result)`. Raises `FileNotFoundError` when the log is simply not
    there (the caller reads that as an honest EMPTY instrument, not an error), and any other
    `OSError` (including a directory, which opens but cannot be read) as unreadable.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        info = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b''.join(chunks), info


def _audit_read(log):
    """Read the audit log ONCE, and derive BOTH the count and the fingerprint from it.

    r2 counted through `ssh_audit.invocations()` (a `read_text`) and digested through a
    separate `read_bytes`. Two reads of a file that is being appended to are two different
    instrument states, so the count and the fingerprint could disagree -- and continuity
    would then be judged against bytes that no count came from. r3 made it one read but kept
    the identity in a second lookup of the pathname; r4 takes the bytes AND the identity from
    the same open descriptor. Returns a dict:

      present/readable/count/raw/error, plus `fingerprint` (identity AND content digest, so
      `finish` can tell an append from a rewrite).
    """
    path = Path(log)
    result = {'present': False, 'readable': False, 'count': None, 'raw': None, 'error': None,
              'fingerprint': {'present': False, 'size': None, 'sha256': None,
                              'inode': None, 'device': None}}
    try:
        raw, info = _open_and_read_once(path)
    except FileNotFoundError:
        # Absent is an honest EMPTY read (zero invocations), not an error: whether that zero
        # may be CLAIMED is decided by the recorder's proven liveness, upstream of here.
        result['readable'] = True
        result['count'] = 0
        return result
    except OSError as exc:
        result['present'] = bool(path.exists())
        result['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:200])
        return result
    result['present'] = True
    result['raw'] = raw
    result['fingerprint'] = {'present': True, 'size': len(raw),
                             'sha256': hashlib.sha256(raw).hexdigest(),
                             'inode': info.st_ino, 'device': info.st_dev}
    try:
        result['count'] = _invocation_count(raw)
    except Exception as exc:  # a read we cannot interpret is a finding, not a zero
        result['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:200])
        return result
    result['readable'] = True
    return result


def _prefix_sha256(log, length):
    """Digest of the first `length` bytes of `log`, or None if it is shorter than that."""
    digest = hashlib.sha256()
    remaining = int(length)
    try:
        with open(str(log), 'rb') as handle:
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _continuity(before, after, log, after_raw=None):
    """Is `after` the SAME log as `before`, still an append-only continuation of it?

    Returns (ok, reason). `reason` is None when the window may be measured.

    `after_raw` is the exact text `after` was read from (r3, gapC): when it is given, the
    prefix is verified against THOSE bytes, so continuity is judged on the same read that
    produced `after` -- not on a second look at a file that may have moved on since.
    """
    before = before or {}
    after = after or {}
    if not before.get('present'):
        # Nothing was on disk when the window opened: a log appearing now is the recorder
        # doing its job, not a truncation. The honest measured zero still stands.
        return True, None
    if not after.get('present'):
        return False, 'audit_log_vanished_between_boundaries'
    if (before.get('device'), before.get('inode')) != (after.get('device'), after.get('inode')):
        return False, 'audit_log_replaced_between_boundaries'
    if (after.get('size') or 0) < (before.get('size') or 0):
        return False, 'audit_log_truncated_between_boundaries'
    if (after.get('size') or 0) == (before.get('size') or 0) and after.get('sha256') != before.get('sha256'):
        return False, 'audit_log_rewritten_between_boundaries'
    if after_raw is not None:
        prefix = hashlib.sha256(after_raw[:before.get('size') or 0]).hexdigest()
    else:
        prefix = _prefix_sha256(log, before.get('size') or 0)
    if prefix != before.get('sha256'):
        # The recorded bytes are not intact at the head of the file any more: whatever this
        # is, it is not an append-only continuation of the log the window opened against.
        return False, 'audit_log_is_not_an_append_only_continuation'
    return True, None


def _target_log_state(path):
    """The FULL state of the real target log, so "left exactly as found" is MEASURED.

    r3 compared `path.exists() == was_present` and called it `target_untouched`, while the
    prose claimed the real log was left exactly as it was found. An existence-only comparison
    reports a probe that APPENDED to the real log -- or replaced it with a same-name file -- as
    untouched (r4, gapB3). The whole state is compared instead: presence, regularity, identity,
    size, mtime, mode and the content digest.

    `present: None` means the state could not be observed at all (an `OSError` other than
    ENOENT). It is deliberately not `False`: an unobservable state must not compare equal to
    anything, so the comparison fails closed.
    """
    state = {'present': False, 'is_file': False, 'inode': None, 'device': None, 'size': None,
             'mtime_ns': None, 'mode': None, 'sha256': None, 'error': None}
    try:
        info = os.stat(str(path))
    except FileNotFoundError:
        return state
    except OSError as exc:
        state['present'] = None
        state['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:160])
        return state
    state.update({'present': True, 'is_file': stat.S_ISREG(info.st_mode),
                  'inode': info.st_ino, 'device': info.st_dev, 'size': info.st_size,
                  'mtime_ns': info.st_mtime_ns, 'mode': stat.S_IMODE(info.st_mode)})
    if state['is_file']:
        try:
            with open(str(path), 'rb') as handle:
                state['sha256'] = hashlib.sha256(handle.read()).hexdigest()
        except OSError as exc:
            state['sha256'] = None
            state['error'] = '%s: %s' % (type(exc).__name__, str(exc)[:160])
    return state


def _append_open_probe(target_path):
    """Prove the recorder's own `>> "$log"` open SUCCEEDS on the log that already exists.

    The shim appends with `>> "$log" 2>/dev/null`, i.e. an `O_WRONLY|O_CREAT|O_APPEND` open,
    and it fails silently -- so nothing downstream can tell a dropped call from no call at all.
    The proof is therefore the SAME append-mode open on the same file, performed and closed
    IMMEDIATELY, WITHOUT writing a byte: appending a canary to the real log is forbidden (it is
    retained evidence, and the chain's fresh-label law is `[ ! -e "$A13_SSH_LOG" ]`).

    `O_CREAT` is deliberately NOT passed (r5, gapD1). r4 called it harmless "precisely because
    this is only ever called for a log that already exists" -- but that is an assumption about
    the interval between the observation that saw the log and this open, not a property of the
    open. If the log is unlinked in that interval, `O_CREAT` RESURRECTS it: the probe meant to
    prove the log is appendable would instead CREATE the log, mutating the retained evidence
    and fabricating a zero-byte file exactly where the chain's fresh-label law requires none.
    Without `O_CREAT` a vanished target fails the open with `ENOENT` -- reported as UNAVAILABLE,
    with the file left gone. Nothing is lost from the proof: what an EXISTING log needs proven
    is that it can be APPENDED to, which is what `O_WRONLY|O_APPEND` on that file tests;
    creating a file is not part of it, and a file this probe created could say nothing about a
    file it did not find.

    The mode bits and `os.access(W_OK)` are recorded as corroboration and are used to fail
    closed when they disagree with the open. They are MODE/ACCESS-LEVEL evidence: this module
    asserts nothing about ACLs, which it cannot see -- the syscall the process actually made is
    the authority, and the two lookups are named separately in the evidence so a reader can
    tell which one denied the append.
    """
    evidence = {'open_flags': 'O_WRONLY|O_APPEND', 'append_open_ok': None,
                'append_open_errno': None, 'append_open_error': None,
                'access_w_ok': None, 'mode_octal': None, 'write_bits_set': None,
                'bytes_written': 0, 'evidence_level': 'syscall+mode/access (not ACL proof)'}
    try:
        mode = stat.S_IMODE(os.stat(str(target_path)).st_mode)
        evidence['mode_octal'] = '%04o' % mode
        evidence['write_bits_set'] = bool(
            mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError as exc:
        evidence['append_open_error'] = 'target_mode_unreadable: %s' % (str(exc)[:120],)
    try:
        evidence['access_w_ok'] = bool(os.access(str(target_path), os.W_OK))
    except OSError:
        evidence['access_w_ok'] = None
    try:
        # No O_CREAT: an existing-target probe must never create the file it measures (r5).
        fd = os.open(str(target_path), os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        evidence['append_open_ok'] = False
        evidence['append_open_errno'] = getattr(exc, 'errno', None)
        evidence['append_open_error'] = '%s: %s' % (type(exc).__name__, str(exc)[:160])
        return evidence
    try:
        evidence['append_open_ok'] = True   # opened and closed; not one byte was written
    finally:
        os.close(fd)
    return evidence


def _record_one_canary_into(probe_log, timeout, outcome):
    """Make the recorder record ONCE into `probe_log`; require exactly that one canary line.

    Returns `(exit_code, wrote_canary, canary_count)`; a failure is recorded on `outcome`.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ('A13_SSH_LOG', 'A13_SSH_REAL')}
    env['A13_SSH_LOG'] = str(probe_log)
    env['A13_SSH_REAL'] = INERT_SSH_REAL
    try:
        completed = subprocess.run(['ssh', RECORDER_CANARY], env=env,
                                   capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        outcome['error'] = 'target_log_probe_timeout_after_%ss' % timeout
        return None, False, None
    except OSError as exc:
        outcome['error'] = 'target_log_probe_could_not_run: %s' % (str(exc)[:160],)
        return None, False, None
    reading = _audit_read(str(probe_log))
    if reading['error']:
        outcome['error'] = 'target_log_probe_log_unreadable: %s' % reading['error']
        return completed.returncode, False, reading['count']
    wrote = bool((reading['count'] or 0) == 1 and reading['raw'] is not None
                 and RECORDER_CANARY.encode() in reading['raw'])
    if not wrote:
        outcome['error'] = ('ssh_recorder_wrote_%s_lines_not_the_probe_invocation'
                            % reading['count'])
    return completed.returncode, wrote, reading['count']


def _probe_target_log_write(target_log, timeout):
    """Prove the recorder can write the REAL target log's path, WITHOUT touching that log.

    A probe into a private temp directory proves the shim honours `A13_SSH_LOG`. It does NOT
    prove the chain's own audit log can be written: the shim writes with
    `mkdir -p "$(dirname "$log")" 2>/dev/null` and `>> "$log" 2>/dev/null`, so a missing or
    unwritable evidence directory fails SILENTLY -- and the never-written log then reads as
    `no_ssh_call_was_made`. That is gapB, and r3 closed it for a log that does not exist yet by
    recording a canary into a private dotted SIBLING inside the real log's directory.

    r4 (gapB2): a sibling proves the DIRECTORY accepts a new file. It does not prove the
    recorder can append to the log this window is measured from when that log ALREADY EXISTS:
    a readable-but-not-appendable log (0444 in a writable directory) still read
    `target_log_writable=true`, so the window was `measured` while every call the recorder
    dropped would have been invisible. The evidence path used is now named in `evidence`:

      * target EXISTS    -> `non_destructive_append_open`: the recorder's own
                            `O_WRONLY|O_APPEND` open on THAT file (r5: no `O_CREAT`, so a log
                            that vanishes between the observation and the open is never
                            recreated by the probe), closed without writing a byte,
                            corroborated by mode bits and `os.access(W_OK)`.
      * target IS ABSENT -> `private_sibling_canary`: exactly one canary line recorded by the
                            recorder into a private dotted sibling in the real log's directory
                            (the real log must NOT be created here: the chain's fresh-label law
                            is `[ ! -e "$A13_SSH_LOG" ]`), which is the write path an absent
                            log depends on, because the shim `mkdir -p`s the directory.

    Both evidence paths require the real log to be left EXACTLY as found -- the whole state
    (identity, size, mtime, mode, digest), not merely its existence (r4, gapB3) -- and require
    no `.ssh-window-writeprobe-*` sibling to be left behind.
    """
    outcome: dict = {'probe_log': None, 'evidence': None, 'target_existed': None,
                     'target_state_before': None, 'target_state_after': None,
                     'target_untouched': None, 'probe_residue': None, 'append_open': None,
                     'exit_code': None, 'wrote_canary': None, 'canary_count': None,
                     'directory_access_ok': None, 'error': None}
    target_path = Path(str(target_log))
    before = _target_log_state(target_path)
    outcome['target_state_before'] = before
    outcome['target_existed'] = bool(before['present'])
    try:
        outcome['directory_access_ok'] = bool(os.access(str(target_path.parent),
                                                        os.W_OK | os.X_OK))
    except OSError:
        outcome['directory_access_ok'] = None
    writable = False
    try:
        if before['present']:
            outcome['evidence'] = 'non_destructive_append_open'
            outcome['wrote_canary'] = False
            if not before['is_file']:
                # Not a regular file (a directory smuggled in as the log, say). r3 left this
                # to the READER, which fails closed on it; this proof says nothing about it
                # rather than reporting a second, invented reason for one unreadable
                # instrument -- and an append-open on a FIFO could block.
                outcome['evidence'] = 'non_regular_target_left_to_the_reader'
                writable = None
            else:
                outcome['append_open'] = _append_open_probe(target_path)
                writable = bool(outcome['append_open']['append_open_ok']
                                and outcome['append_open']['access_w_ok'])
                if not writable:
                    outcome['error'] = ('recorder_append_open_denied errno=%s mode=%s access_w_ok=%s'
                                        % (outcome['append_open']['append_open_errno'],
                                           outcome['append_open']['mode_octal'],
                                           outcome['append_open']['access_w_ok']))
        else:
            outcome['evidence'] = 'private_sibling_canary'
            outcome['probe_log'] = str(target_path.parent / ('.ssh-window-writeprobe-%s.log'
                                                             % uuid.uuid4().hex[:12]))
            outcome['exit_code'], outcome['wrote_canary'], outcome['canary_count'] = \
                _record_one_canary_into(outcome['probe_log'], timeout, outcome)
            writable = bool(outcome['wrote_canary'])
    finally:
        sibling = outcome['probe_log']
        if sibling:
            try:
                os.unlink(str(sibling))
            except (OSError, TypeError):
                pass
        try:
            outcome['probe_residue'] = any(
                name.startswith('.ssh-window-writeprobe-')
                for name in os.listdir(str(target_path.parent)))
        except OSError:
            outcome['probe_residue'] = None
        after = _target_log_state(target_path)
        outcome['target_state_after'] = after
        # Exact state, not existence: `present: None` (unobservable) never equals anything.
        outcome['target_untouched'] = bool(after == before)
        if not outcome['target_untouched'] and not outcome['error']:
            # Only when nothing more specific was found. A target that vanished before the open
            # is not a modification BY this probe: the append error (ENOENT) names what actually
            # happened and must not be buried under a false accusation (r5).
            outcome['error'] = 'target_log_probe_modified_the_real_log'
        if outcome['probe_residue']:
            writable = False
            outcome['error'] = 'target_log_probe_left_a_sibling_behind'
    if writable is None:
        # The write path is not what is wrong with this instrument; the reader reports it.
        return None, outcome
    return bool(writable and outcome['target_untouched']), outcome


def probe_recorder(shim_path, target_log=None, timeout=PROBE_TIMEOUT_S):
    """Prove the recorder WORKS -- as the recorder THIS window depends on -- by recording.

    `is_file() and os.access(X_OK)` is a stat, and a stat cannot distinguish a working
    recorder from a dead one -- the whole gap this function closes. The probe:

      1. resolves `ssh` on the MEASURED PATH -- the PATH this process actually has, which is
         the one the chain's children inherit. r2 prepended the shim's own directory to a
         PATH of its own before asking the question, so `shim_path_bound` was true by
         construction and a recorder the chain's children would never reach could still vouch
         for the window (gapA). A shim that is not what name resolution finds is a finding;
      2. records once into a PRIVATE temp log (`A13_SSH_LOG`), so the retained evidence log is
         never touched by the probe -- and launches `ssh` BY NAME through that PATH, so the
         name-resolution binding the chain relies on is the binding exercised. `ssh` is only
         launched after resolution is PROVEN to find the shim, and `A13_SSH_REAL` is pinned to
         an INERT binary, so the shim's trailing `exec` cannot reach an ssh client or the
         network;
      3. requires exactly ONE timestamp-prefixed line, carrying the canary;
      4. and, when `target_log` is given, proves the recorder can write the REAL target log:
         `O_WRONLY|O_APPEND` -- the recorder's append MODE, WITHOUT `O_CREAT` (r5: the proof
         must never CREATE the log it measures, so a target that vanishes between the
         observation and the open is reported UNAVAILABLE and left gone) -- on the log that
         ALREADY EXISTS, opened without writing a byte (r4), or a private sibling canary when
         it does not exist yet. `target_log_writable=False`, with `recorder_reason` naming it,
         is UNAVAILABLE.
         `target_log_writable` stays None only when the target is not a regular file at all, so
         the reader -- which fails closed on it -- remains the reporter.

    Only `probe_recorder` ever launches a process, and it is always this inert one.
    """
    record = {
        'shim': str(shim_path) if shim_path is not None else None,
        'shim_present': False,
        'shim_executable': False,
        'shim_path_bound': False,
        'shim_on_ambient_path': False,
        'recorder_active': False,
        'recorder_reason': None,
        'recorder_probe': None,
        'target_log': str(target_log) if target_log is not None else None,
        'target_log_writable': None,
        'target_log_probe': None,
    }
    if shim_path is None:
        record['recorder_reason'] = 'ssh_shim_absent_or_not_executable'
        return record
    shim = Path(shim_path)
    try:
        record['shim_present'] = bool(shim.is_file())
        record['shim_executable'] = bool(os.access(str(shim), os.X_OK))
    except OSError:
        pass
    if not (record['shim_present'] and record['shim_executable']):
        record['recorder_reason'] = 'ssh_shim_absent_or_not_executable'
        return record

    # r3, gapA: the binding is MEASURED on the PATH this process actually has. `shim_path_bound`
    # IS `shim_on_ambient_path` now -- r2 had a second, self-arranged PATH, and there is only
    # one binding worth a claim, so the second key is kept as an alias for the chain's printout.
    measured_path = os.environ.get('PATH', '')
    resolved = shutil.which('ssh', path=measured_path)
    bound = bool(resolved and _same_file(resolved, shim))
    record['shim_path_bound'] = bound
    record['shim_on_ambient_path'] = bound
    if not bound:
        record['recorder_reason'] = 'ssh_shim_is_not_the_ssh_that_path_resolution_finds'
        return record

    probe_dir = tempfile.mkdtemp(prefix='ssh-window-recorder-probe-')
    try:
        probe_log = Path(probe_dir) / 'probe-ssh-calls.log'
        env = {k: v for k, v in os.environ.items()
               if k not in ('A13_SSH_LOG', 'A13_SSH_REAL')}
        env['A13_SSH_LOG'] = str(probe_log)
        env['A13_SSH_REAL'] = INERT_SSH_REAL
        try:
            # BY NAME, through the measured PATH: this is the chain's own binding, and the
            # resolution above proved this name reaches the shim (never a real ssh client).
            completed = subprocess.run(['ssh', RECORDER_CANARY], env=env,
                                       capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            record['recorder_reason'] = 'ssh_shim_probe_timeout_after_%ss' % timeout
            return record
        except OSError as exc:
            record['recorder_reason'] = 'ssh_shim_probe_could_not_run: %s' % (str(exc)[:160],)
            return record
        record['recorder_probe'] = {'exit_code': completed.returncode,
                                    'stdout': completed.stdout[:200],
                                    'stderr': completed.stderr[:200]}
        if completed.returncode != 0:
            record['recorder_reason'] = 'ssh_shim_probe_exit_%s' % completed.returncode
            return record
        if not probe_log.exists():
            record['recorder_reason'] = 'ssh_shim_ran_but_wrote_no_log'
            return record
        try:
            found = _load_audit().invocations(str(probe_log))
        except Exception as exc:
            record['recorder_reason'] = 'ssh_shim_probe_log_unreadable: %s' % (str(exc)[:160],)
            return record
        if len(found) != 1:
            record['recorder_reason'] = 'ssh_shim_recorded_%d_lines_not_1' % len(found)
            return record
        if RECORDER_CANARY not in found[0]:
            record['recorder_reason'] = 'ssh_shim_recorded_but_not_the_probe_invocation'
            return record
        record['recorder_active'] = True
        if target_log is not None:
            # gapB/gapB2: the private log proves the recorder records; only this proves it can
            # write the log THIS window will be measured from -- by opening that very log for
            # append, without writing a byte to it.
            writable, outcome = _probe_target_log_write(target_log, timeout)
            record['target_log_writable'] = writable
            record['target_log_probe'] = outcome
            if writable is False:
                record['recorder_reason'] = 'ssh_recorder_cannot_write_the_target_log'
        return record
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _same_file(left, right):
    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        return False


def snapshot(log, shim_path=DEFAULT_SHIM):
    """One boundary reading of the audit log, honest about its own limits.

    Returns a dict with `measured` False and `count` None whenever the reading cannot
    support the claim that the count is real. `reason` names which of those happened.
    """
    record, _raw = _boundary_reading(str(log), shim_path)
    return record


def _boundary_reading(log, shim_path):
    """`snapshot`, plus the exact bytes its count and its digest were derived from.

    `finish` uses those bytes to judge append-only continuity on the SAME read it measured,
    instead of going back to a file that may have moved on since (r3, gapC).
    """
    record = {
        'log': log,
        'log_present': False,
        'log_readable': False,
        'count': None,
        'measured': False,
        'reason': None,
        'captured_at': _now(),
        'log_fingerprint': None,
    }
    record.update(probe_recorder(shim_path, target_log=log))

    if not record['recorder_active'] or record.get('target_log_writable') is False:
        # No recorder usable for THIS window: either it is not proven active, or it cannot
        # write the log this window is measured from. Either way this run's ssh calls would
        # never have been logged, so a missing-or-empty log here proves nothing.
        # UNAVAILABLE, never a clean zero.
        record['reason'] = record['recorder_reason'] or 'ssh_recorder_inactive'
        if os.path.exists(log):
            record['log_present'] = True
        return record, None

    declared = os.environ.get('A13_SSH_LOG')
    if declared and not _same_instrument_path(log, declared):
        # r3, gapD: the chain binds its audit log with `export A13_SSH_LOG=...` and the shim
        # reads the same variable. A window opened against some other path is measuring a
        # different instrument than the controlled environment arranged.
        record['declared_log'] = declared
        record['reason'] = 'measured_log_is_not_the_declared_audit_log'
        if os.path.exists(log):
            record['log_present'] = True
        return record, None

    record['log_present'] = os.path.exists(log)
    reading = _audit_read(log)   # ONE read: the count AND the fingerprint come from these bytes
    if reading['error']:         # a read error is a finding, not a zero
        record['reason'] = 'ssh_audit_read_error: %s' % reading['error']
        return record, None
    record['log_readable'] = True
    record['count'] = reading['count']
    record['log_fingerprint'] = reading['fingerprint']
    record['measured'] = True
    if not record['log_present']:
        # Recorder PROVEN active AND no log: the honest measured zero (criterion 3 held).
        record['reason'] = 'audit_log_absent_because_no_ssh_call_was_made'
    return record, reading['raw']


def _write_atomic(path, document):
    """Write the document so a reader never sees a half-written artifact.

    Used only for the CLOSE of a window whose artifact this writer already owns (the
    exclusive lock in `finish` makes it the single writer).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(document, handle, indent=2)
            handle.write('\n')
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_exclusive(path, document):
    """Create `path` from `document`, atomically, and ONLY if it does not already exist.

    mkstemp + `os.link()` -- not mkstemp + `os.replace()`: `os.replace` would happily
    clobber a document another writer created between our existence check and our write,
    while `link()` fails with EEXIST and leaves the winner's bytes untouched. That is the
    whole difference between closing the check-then-act race and merely narrowing it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(document, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            raise DuplicateArtifact(
                'criterion-3 window artifact already exists (created by another writer): %s' % path)
    except BaseException:
        raise
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def read(label, out_dir):
    """The artifact for `label`, or None when there is none."""
    path = artifact_path(out_dir, label)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def begin(label, log, out_dir, shim_path=DEFAULT_SHIM, force=False):
    """Open the window: persist the BEFORE count under a fresh label.

    Refuses to overwrite an existing artifact, and refuses `force` outright. Refusing is
    the point: a re-used label means the earlier measurement belongs to a different round
    and must not be lost -- and an overwrite switch on retained evidence is an escape
    hatch out of the fresh-label law, so it does not exist any more.
    """
    if force:
        raise UnsafeOverwrite(
            'overwrite refused: the criterion-3 window artifact for label %r is retained '
            'evidence and is never overwritten (force is not a supported escape)' % (label,))
    path = artifact_path(out_dir, label)
    if path.exists():
        raise DuplicateArtifact(
            'criterion-3 window artifact already exists for label %r: %s' % (label, path))
    document = {
        'protocol': PROTOCOL,
        'label': label,
        'phase': 'before',
        'source_log': str(log),
        'opened_at': _now(),
        'before': snapshot(log, shim_path),
        'after': None,
        'delta': None,
        'measured': False,
        'verdict': UNAVAILABLE,
        'zero_ssh_within_window': False,
        'claimed_zero_ssh': False,
    }
    write_exclusive(path, document)
    return document


def finish(label, log, out_dir, shim_path=DEFAULT_SHIM):
    """Close the window: persist the AFTER count and the delta.

    The delta is only a number when BOTH boundaries were measured AND the log is still the
    same log, intact and append-only. It is None -- never 0 -- otherwise.
    """
    path = artifact_path(out_dir, label)
    if not path.exists():
        raise MissingBegin(
            'no before() artifact for label %r at %s; the window was never opened' % (label, path))
    # One closer at a time: without this, two concurrent finishes could both read the
    # `before` document and both write, and the second would overwrite the first's delta.
    lock = path.with_name(path.name + '.lock')
    try:
        lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise DuplicateArtifact(
            'another writer is already closing the criterion-3 window for label %r: %s' % (label, lock))
    try:
        document = json.loads(path.read_text())
        if document.get('phase') == 'complete':
            raise DuplicateArtifact(
                'criterion-3 window artifact for label %r is already complete: %s' % (label, path))

        before = document.get('before') or {}
        after, after_raw = _boundary_reading(str(log), shim_path)

        reasons = [r for r in (before.get('reason'), after.get('reason')) if r]
        measured = bool(before.get('measured')) and bool(after.get('measured'))

        continuity = {'ok': None, 'reason': None}
        if not _same_instrument_path(log, document.get('source_log')):
            # The window was opened against ONE instrument. Closing it against another would
            # compare two different logs and call the difference a measurement.
            measured = False
            continuity = {'ok': False, 'reason': 'finish_used_a_different_log_than_begin'}
        elif measured:
            ok, reason = _continuity(before.get('log_fingerprint'),
                                     after.get('log_fingerprint'), str(log), after_raw)
            continuity = {'ok': ok, 'reason': reason}
            if not ok:
                measured = False
        if continuity.get('reason'):
            reasons.append(continuity['reason'])

        delta = None
        if measured:
            delta = int(after['count']) - int(before['count'])

        document.update({
            'phase': 'complete',
            'closed_at': _now(),
            'after': after,
            'source_log_at_finish': str(log),
            'log_continuity': continuity,
            'delta': delta,
            'measured': measured,
            'verdict': MEASURED if measured else UNAVAILABLE,
            # The only shape that may be called a clean criterion-3 window. A missing or dead
            # recorder, an unreadable log, or a changed/truncated log can never reach here,
            # because `measured` would be False.
            'zero_ssh_within_window': bool(measured and delta == 0),
            'claimed_zero_ssh': bool(measured and delta == 0),
            'unavailable_reasons': [] if measured else reasons,
            'note': ('delta is None, NOT 0: the recorder, the audit log, or the log\'s '
                     'append-only continuity was not intact across the window, so no zero '
                     'may be claimed'
                     if not measured else
                     'both boundaries measured from the same retained ssh audit log, which is '
                     'still an append-only continuation of the log the window opened against'),
        })
        _write_atomic(path, document)
        return document
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            os.unlink(str(lock))
        except OSError:
            pass


def _parse_args(argv):
    """Tiny CLI: `ssh_window_evidence.py {begin|finish} --label L --log F --out-dir D`.

    `--force` is NOT accepted: an overwrite switch on retained evidence was gap3, so the CLI
    treats it as a usage error (exit 2) rather than a way to destroy a prior measurement.
    """
    if not argv or argv[0] not in ('begin', 'finish'):
        return None
    action = argv[0]
    opts = {'label': None, 'log': DEFAULT_LOG, 'out_dir': None,
            'shim': str(DEFAULT_SHIM)}
    rest = argv[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in ('--label', '--log', '--out-dir', '--shim'):
            if index + 1 >= len(rest):
                return None
            opts[token.lstrip('-').replace('-', '_')] = rest[index + 1]
            index += 2
            continue
        return None
    if not opts['label'] or not opts['out_dir']:
        return None
    return action, opts


def main(argv):
    parsed = _parse_args(argv)
    if parsed is None:
        print('usage: ssh_window_evidence.py {begin|finish} --label L --log F --out-dir D [--shim S]')
        return EXIT_USAGE
    action, opts = parsed
    try:
        if action == 'begin':
            document = begin(opts['label'], opts['log'], opts['out_dir'], shim_path=opts['shim'])
        else:
            document = finish(opts['label'], opts['log'], opts['out_dir'], shim_path=opts['shim'])
    except UnsafeOverwrite as exc:
        print('SSH_WINDOW_EVIDENCE_FAILED reason=unsafe_overwrite detail=%s' % exc)
        return EXIT_UNSAFE_OVERWRITE
    except DuplicateArtifact as exc:
        print('SSH_WINDOW_EVIDENCE_FAILED reason=duplicate_artifact detail=%s' % exc)
        return EXIT_DUPLICATE
    except MissingBegin as exc:
        print('SSH_WINDOW_EVIDENCE_FAILED reason=missing_begin detail=%s' % exc)
        return EXIT_MISSING_BEGIN
    if not document.get('measured') and not document.get('before', {}).get('recorder_active', True):
        # Loud, and separately codeable, so the chain can refuse to spend its soak on a
        # window that can never produce a claim.
        print('SSH_WINDOW_EVIDENCE_RECORDER_INACTIVE reason=%s'
              % (document.get('before', {}).get('recorder_reason'),))
    if not document.get('measured'):
        # r3: a window that could not be measured for ANY reason -- an unbound recorder, an
        # unwritable target log, a log that is not the declared instrument, a read error -- must
        # be loud here too. The durable artifact is the evidence, but the chain only sees this
        # block through `tail -30`, and it branches on the reason.
        print('SSH_WINDOW_EVIDENCE_UNAVAILABLE reason=%s'
              % (document.get('reason') or (document.get('before') or {}).get('reason'),))
    print('SSH_WINDOW_EVIDENCE_%s %s' % (action.upper(), json.dumps(document)))
    print('SSH_WINDOW_EVIDENCE_FILE', artifact_path(opts['out_dir'], opts['label']))
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
