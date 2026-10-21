# SPDX-License-Identifier: AGPL-3.0-only
"""Resolve the installed or locked lane-built Iroh sidecar, never a peer path."""
import os
from pathlib import Path
import stat
import sys


def installed_sidecar():
    module=Path(__file__).absolute().parent
    if getattr(sys,'frozen',False):
        candidate=module/'native/mycelium-iroh-sidecar'
        boundary=Path(sys.executable).resolve().parents[2]
    else:
        boundary=module.parent.resolve()
        candidate=boundary/'.build-native/release/mycelium-iroh-sidecar'
    path=candidate.resolve(strict=True)
    info=path.lstat()
    if (not path.is_relative_to(boundary) or not stat.S_ISREG(info.st_mode)
        or info.st_uid!=os.geteuid() or not os.access(path,os.X_OK)
        or info.st_mode&0o022):
        raise ValueError('native_sidecar_installation_invalid')
    return path
