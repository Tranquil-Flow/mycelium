#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Prove every module a change touched is in the SHIPPED bytes, and that those bytes CHANGED.

Two discriminators, because one size does not fit:
  * PYZ module  -> symbol markers that must be NEW (absent in the OLD preview) with module-LOCAL controls
    that must exist in BOTH.  A control that is merely imported proves nothing.
  * runtime source -> the file's sha256 must differ between OLD and NEW AND equal the tree (name markers
    cannot discriminate a module whose value changed inside, e.g. mycelium_deadlines, whose symbol already
    existed in the older preview).

DEFAULT CLAIM (unchanged, no flags): the D9 cleanup-budget migration, preview.78 vs preview.77.

THE .79 CLAIM, and why it needs more than a marker name: between preview.78 (built at c483739) and preview.79
the ONLY product file that changed is mycelium_live/router_port.py -- it gained the withheld-terminal record
and, one turn later, the CAUSE field.  Both are string constants of that module, so one POSITIVE marker per
new fact is the honest discriminator, with a module-local control that must be present in both previews:

    --module mycelium_live.router_port:route_error_code:_request_command_cancellation
    --module mycelium_live.router_port:mycelium.live_route_blocked_terminal.v1:_request_command_cancellation

Usage:
    verify_shipped_bytecode.py                          # the default D9 claim
    verify_shipped_bytecode.py --new 0.1.0-preview.79 --old 0.1.0-preview.78 \
        --module mycelium_live.router_port:route_error_code:_request_command_cancellation

Exit: 0 every claim PROVEN · 1 at least one NOT PROVEN (named in the table).
"""
from __future__ import annotations

import os
import argparse
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

DIST = Path(os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-g1/dist"))
TREE = Path(os.path.expanduser("~/Documents/playground/mycelium-core-pilot"))
APP = "Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"
RESOURCES = "Mycelium A13 Demo.app/Contents/Resources/a13-runtime-source"

DEFAULT_NEW, DEFAULT_OLD = "0.1.0-preview.78", "0.1.0-preview.77"
DEFAULT_MODULES = {
    "mycelium_live.router_port": ("_MAX_CANCELLATION_AND_CLEANUP_SECONDS", "_request_command_cancellation"),
    "mycelium_live.command_controller": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "MAX_CLEANUP_RESULT_BYTES"),
    "mycelium_live.a4_contracts": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "PRODUCT_QUALIFICATION_PROTOCOL"),
    "mycelium_live.a4_install": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "PROTOCOLS"),
    "mycelium_product_spine.projector": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "validate_product_snapshot"),
    "mycelium_live.route": ("_MAX_CANCELLATION_AND_CLEANUP_SECONDS", "admit_peer_cleanup_bound"),
}
DEFAULT_RUNTIME_SOURCES = ("mycelium_deadlines.py", "physical_inference_node.py")


def symbols(code, out, depth=0):
    if depth > 8:
        return
    out.update(code.co_names)
    for item in code.co_consts:
        if isinstance(item, str):
            out.add(item)
        elif hasattr(item, "co_names"):
            symbols(item, out, depth + 1)


def load(exe, tmp):
    Path(tmp).write_bytes(CArchiveReader(str(exe)).extract("PYZ.pyz"))
    return ZlibArchiveReader(tmp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new", default=DEFAULT_NEW, help="preview version whose bytes must carry the change")
    parser.add_argument("--old", default=DEFAULT_OLD, help="the previous preview: the marker must be ABSENT here")
    parser.add_argument("--module", action="append", default=None,
                        help="name:marker:control (repeatable).  Default: the D9 claim.")
    parser.add_argument("--runtime-source", action="append", default=None,
                        help="name of a runtime source that must differ from --old and equal the tree")
    args = parser.parse_args()

    modules = {}
    if args.module:
        for spec in args.module:
            name, marker, control = spec.split(":", 2)
            modules[name] = (marker, control)
    else:
        modules = DEFAULT_MODULES
    sources = tuple(args.runtime_source) if args.runtime_source else (
        DEFAULT_RUNTIME_SOURCES if not args.module else ())

    new_exe, old_exe = DIST / args.new / APP, DIST / args.old / APP
    new_rs, old_rs = DIST / args.new / RESOURCES, DIST / args.old / RESOURCES
    print(f"claim: {args.new} (built with the change) vs {args.old} (without it)")
    new_z = load(new_exe, f"/tmp/asm/pyz-new-{args.new}.pyz")
    old_z = load(old_exe, f"/tmp/asm/pyz-old-{args.old}.pyz")

    fails = 0
    print(f"{'module':<44} {'new':<6} {'old':<6} {'ctrl':<6} verdict")
    print("-" * 80)
    for module, (marker, control) in modules.items():
        sets = []
        for archive in (new_z, old_z):
            found = set()
            try:
                symbols(archive.extract(module), found)
            except Exception as exc:
                print(f"  (extract failed for {module}: {type(exc).__name__})")
            sets.append(found)
        new_s, old_s = sets
        m_new, m_old = marker in new_s, marker in old_s
        ctrl = control in new_s and control in old_s
        ok = m_new and not m_old and ctrl
        fails += 0 if ok else 1
        print(f"{module:<44} {str(m_new):<6} {str(m_old):<6} {str(ctrl):<6} {'PROVEN' if ok else 'NOT PROVEN'}")

    if sources:
        import hashlib

        def sha(path):
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()

        print()
        print(f"=== runtime sources: shipped bytes changed vs {args.old} AND equal the tree ===")
        for name in sources:
            try:
                old_h, new_h, tree_h = sha(old_rs / name), sha(new_rs / name), sha(TREE / name)
            except OSError as exc:
                fails += 1
                print(f"  {name:<30} UNREADABLE {exc}")
                continue
            changed, same_as_tree = old_h != new_h, new_h == tree_h
            ok = changed and same_as_tree
            fails += 0 if ok else 1
            print(f"  {name:<30} changed={changed} tree_match={same_as_tree} -> {'PROVEN' if ok else 'NOT PROVEN'}")

    print()
    print("SHIPPED_BYTECODE_" + ("PROVEN" if fails == 0 else f"INCOMPLETE({fails})"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
