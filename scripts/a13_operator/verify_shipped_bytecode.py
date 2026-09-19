#!/usr/bin/env python3
"""Prove every module this change touched is in the SHIPPED bytes, and that those bytes CHANGED.

Two discriminators, because one size does not fit:
  * PYZ modules  -> a symbol marker that must be NEW (absent in preview.77) with a module-LOCAL control
    that must exist in BOTH.  A control that is merely imported proves nothing.
  * runtime source -> the file's sha256 must differ between preview.77 and preview.78 AND equal the tree
    (name markers cannot discriminate a module the value changed inside, e.g. mycelium_deadlines, whose
    symbol existed in .77 already).
"""
import hashlib
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

DIST = Path("/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist")
NEW = DIST / "0.1.0-preview.78/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"
OLD = DIST / "0.1.0-preview.77/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"
NEW_RS = DIST / "0.1.0-preview.78/Mycelium A13 Demo.app/Contents/Resources/a13-runtime-source"
OLD_RS = DIST / "0.1.0-preview.77/Mycelium A13 Demo.app/Contents/Resources/a13-runtime-source"
TREE = Path("/Users/evinova-self/Documents/playground/mycelium-core-pilot")

PYZ_MODULES = {
    "mycelium_live.router_port": ("_MAX_CANCELLATION_AND_CLEANUP_SECONDS", "_request_command_cancellation"),
    "mycelium_live.command_controller": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "MAX_CLEANUP_RESULT_BYTES"),
    "mycelium_live.a4_contracts": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "PRODUCT_QUALIFICATION_PROTOCOL"),
    "mycelium_live.a4_install": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "PROTOCOLS"),
    "mycelium_product_spine.projector": ("COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS", "validate_product_snapshot"),
    "mycelium_live.route": ("_MAX_CANCELLATION_AND_CLEANUP_SECONDS", "admit_peer_cleanup_bound"),
}
RUNTIME_SOURCES = ("mycelium_deadlines.py", "physical_inference_node.py")


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


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


new_z, old_z = load(NEW, "/tmp/asm/pyz78b.pyz"), load(OLD, "/tmp/asm/pyz77b.pyz")
fails = 0
print(f"{'module':<38} {'new':<6} {'old':<6} {'ctrl':<6} verdict")
print("-" * 74)
for module, (marker, control) in PYZ_MODULES.items():
    sets = []
    for archive in (new_z, old_z):
        s = set()
        try:
            symbols(archive.extract(module), s)
        except Exception:
            pass
        sets.append(s)
    new_s, old_s = sets
    m_new, m_old = marker in new_s, marker in old_s
    ctrl = control in new_s and control in old_s
    ok = m_new and not m_old and ctrl
    fails += 0 if ok else 1
    print(f"{module:<38} {str(m_new):<6} {str(m_old):<6} {str(ctrl):<6} {'PROVEN' if ok else 'NOT PROVEN'}")
print()
print("=== runtime sources: shipped bytes CHANGED and equal the tree ===")
for name in RUNTIME_SOURCES:
    old_h, new_h, tree_h = sha(OLD_RS / name), sha(NEW_RS / name), sha(TREE / name)
    changed, same_as_tree = old_h != new_h, new_h == tree_h
    ok = changed and same_as_tree
    fails += 0 if ok else 1
    print(f"  {name:<30} changed={changed} tree_match={same_as_tree} -> {'PROVEN' if ok else 'NOT PROVEN'}")
print()
print("SHIPPED_BYTECODE_" + ("PROVEN" if fails == 0 else f"INCOMPLETE({fails})"))
