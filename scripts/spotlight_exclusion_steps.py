#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Print the steps that keep Spotlight (mds / mdworker) from indexing the run dirs.

Spotlight indexing of fresh worktrees, run directories and rehearsal output pushed host load to 10-55 on
2026-10-20 and failed timing-sensitive tests.  This helper only PRINTS the steps for the owner; it runs no sudo and
changes nothing.  ``--check`` additionally runs the read-only ``mdutil -s`` on each volume.

``mdutil`` acts on whole volumes: ``mdutil -s <folder>`` answers "unknown indexing state" for a folder that is not a
mount point, so the folder form of ``sudo mdutil -i off`` is not offered.  The per-folder options are Spotlight
Privacy (System Settings) and the ``.metadata_never_index`` marker; the marker's effect on a folder (as opposed
to a volume root) is reported by users but has not been verified on this host.

    python3 scripts/spotlight_exclusion_steps.py                 # the default run dirs under $HOME
    python3 scripts/spotlight_exclusion_steps.py --path /abs/dir # exactly these dirs (repeatable)
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


def default_run_dirs(home: Path | None = None) -> list[Path]:
    home = Path.home() if home is None else Path(home)
    return [
        home / "Documents" / "mycelium-core-program" / "runs",
        home / "Projects",
        home / ".hermes" / "cache" / "scratch",
        home / "Documents" / "playground",
    ]


def mount_point(path: Path) -> Path:
    """The mount point of the volume holding ``path`` (or its nearest existing parent)."""
    current = Path(os.path.abspath(path))
    while not current.exists() and current != current.parent:
        current = current.parent
    current = existing = current.resolve()
    while not os.path.ismount(current) and current != current.parent:
        current = current.parent
    # macOS: user folders live on the Data volume, which firmlinks into "/" (so "/" is the sealed system volume).
    data = Path("/System/Volumes/Data")
    if current == Path("/") and data.is_dir():  # os.path.ismount() is False for it (shared st_dev)
        try:
            if (data / existing.relative_to("/")).exists():
                return data
        except (ValueError, OSError):
            pass
    return current


def render_steps(paths: Sequence[Path], *, mount_of: Callable[[Path], Path] = mount_point) -> str:
    out: list[str] = [
        "Spotlight exclusion steps (printed only; nothing was run, no sudo was used).",
        "",
    ]
    for path in paths:
        path = Path(path)
        mount = mount_of(path)
        note = "" if path.exists() else "   (does not exist yet)"
        out += [
            f"== {path}{note}",
            "  1. Spotlight Privacy (per folder, recommended): System Settings > Spotlight > Search Privacy... >",
            f"     \"+\" > choose {path}  (add it, then Done).",
            f"  2. Marker (no sudo, per folder; effect on a folder unverified here): "
            f"touch {shlex.quote(str(path / '.metadata_never_index'))}",
            f"  3. Whole volume {mount} (every folder on it, not only this one): sudo mdutil -i off {mount}",
            f"     undo: sudo mdutil -i on {mount}    check (read-only, no sudo): mdutil -s {mount}",
            "",
        ]
    out += [
        "Notes:",
        "  - mdutil takes a volume; for a folder it reports \"unknown indexing state\", so step 3 names the volume.",
        "  - Verify step 3 with the read-only check above (it should say \"Indexing disabled\"); step 1 shows in the Privacy list.",
        "  - Do not run `sudo mdutil -E`: it erases and rebuilds the index.",
    ]
    return "\n".join(out) + "\n"


def _check(paths: Sequence[Path]) -> str:
    seen: list[Path] = []
    lines: list[str] = []
    for path in paths:
        mount = mount_point(path)
        if mount in seen:
            continue
        seen.append(mount)
        try:
            done = subprocess.run(["mdutil", "-s", str(mount)], capture_output=True, text=True, timeout=20, check=False)
            lines.append(f"mdutil -s {mount}: {(done.stdout or done.stderr).strip()}")
        except (OSError, subprocess.TimeoutExpired) as error:
            lines.append(f"mdutil -s {mount}: unavailable ({error})")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--path", action="append", default=None, help="absolute run dir (repeatable); replaces the defaults")
    parser.add_argument("--check", action="store_true", help="also run the read-only `mdutil -s` on each volume")
    args = parser.parse_args(argv)
    if args.path:
        paths = [Path(value) for value in args.path]
        for path in paths:
            if not path.is_absolute():
                parser.error(f"--path must be absolute: {path}")
    else:
        paths = default_run_dirs()
    sys.stdout.write(render_steps(paths))
    if args.check:
        sys.stdout.write("\n" + _check(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
