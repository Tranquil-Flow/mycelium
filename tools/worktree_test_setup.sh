#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Offline setup only: copy known binaries, never build/download or launch them.
# Usage: tools/worktree_test_setup.sh INTEGRATION_CHECKOUT [WORKTREE]
set -euo pipefail
if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  printf 'Usage: %s INTEGRATION_CHECKOUT [WORKTREE]\n' "$0" >&2
  exit 2
fi
TARGET=${2:-$(cd "$(dirname "$0")/.." && pwd)}
exec "${WORKTREE_TEST_PYTHON:-python3}" - "$1" "$TARGET" <<'PY'
import ctypes
import ctypes.util
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile

source = Path(sys.argv[1]).expanduser().resolve(strict=True)
target = Path(sys.argv[2]).expanduser().resolve(strict=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean(path):
    if sys.platform == 'darwin':
        getxattr = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True).getxattr
        getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                            ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        getxattr.restype = ctypes.c_ssize_t
        for p in (path.resolve(), *path.resolve().parents):
            result = getxattr(os.fsencode(p), b'com.apple.quarantine', None, 0, 0, 1)
            if result >= 0:
                raise SystemExit('Refusing quarantined prerequisite: ' + str(p))
            if ctypes.get_errno() != errno.ENOATTR:
                raise OSError(ctypes.get_errno(), 'Cannot inspect prerequisite quarantine', str(p))


specs = {
    '.build-native/release/mycelium-iroh-sidecar': [source / '.build-native/release/mycelium-iroh-sidecar'],
    'native/iroh_transport/target/debug/mycelium-iroh-sidecar': [
        source / 'native/iroh_transport/target/debug/mycelium-iroh-sidecar',
        source.parent / 'native-target/debug/mycelium-iroh-sidecar'],
    'native/mobile_core/target/debug/examples/qwen2_stage_exec': [
        source / 'native/mobile_core/target/debug/examples/qwen2_stage_exec',
        source.parent / 'native-target/debug/examples/qwen2_stage_exec'],
}
plan = []
# Preflight every input and destination before installing anything. Deliberately
# do not copy the multi-GB cargo cache or write to a shared sibling native-target.
for relative, choices in specs.items():
    src = next((p for p in choices if p.is_file()), None)
    if src is None:
        raise SystemExit('Missing offline prerequisite: ' + relative + '; build it in the configured integration checkout first')
    clean(src)
    if not os.access(src, os.X_OK):
        raise SystemExit('Prerequisite is not executable: ' + str(src))
    dst = target / relative
    for p in (dst, *dst.parents):
        if p == target:
            break
        if p.is_symlink():
            raise SystemExit('Refusing symlinked worktree destination: ' + str(p))
    sha = digest(src)
    if dst.exists() and (not dst.is_file() or digest(dst) != sha):
        raise SystemExit('Refusing to overwrite different prerequisite: ' + str(dst))
    plan.append((src, dst, sha))
for src, dst, sha in plan:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.worktree-setup-', dir=dst.parent)
        os.close(fd)
        staging = Path(name)
        try:
            shutil.copy2(src, staging)
            if digest(staging) != sha:
                raise SystemExit('Prerequisite changed during copy: ' + str(src))
            # Exclusive link refuses racing writers; no replacement of a build.
            os.link(staging, dst)
        finally:
            staging.unlink()
    clean(dst)
    if digest(dst) != sha or not os.access(dst, os.X_OK):
        raise SystemExit('Prerequisite readback failed: ' + str(dst))
    print(sha + '  ' + str(dst.relative_to(target)))
print('Offline worktree prerequisites ready. Packaged tests additionally require A13_PACKAGE_EXECUTABLE.')
PY
