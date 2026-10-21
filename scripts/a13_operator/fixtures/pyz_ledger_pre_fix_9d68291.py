#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-module PYZ ledger: which SOURCE TREE does a shipped bundle's executed bytecode match?

WHY THIS EXISTS
    A green deploy digest proves the deploy copied what the build produced. It does NOT prove
    the build used the sources you edited. This tool answers the question that actually matters:
    for EVERY module a change touches, is the change in the artifact that runs?

    Markers are computed as string constants present in one tree and absent in the other, so
    "which tree" is a measurement. Each module also carries a CONTROL marker present in BOTH
    trees: if the control is missing you are reading the wrong module and the row means nothing.

USAGE (interpreter must be the one matching the bundle's bytecode, e.g. python3.14)
    /opt/homebrew/bin/python3 tools/pyz_ledger.py --exe "/path/Contents/MacOS/Mycelium A13 Demo" \
        --label preview.73
    /opt/homebrew/bin/python3 tools/pyz_ledger.py --exe ... --label preview.74 --baseline <earlier exe>

    --baseline gives the differential: a marker that is present in BOTH builds discriminates
    nothing. Always run a baseline for the change you are trying to prove.

NO PyInstaller REQUIRED: the PYZ is located by its magic and its TOC is marshalled directly.
"""
import argparse, marshal, os, struct, subprocess, sys, types, zlib

PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")
DEMO = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")

# module in the PYZ -> source path relative to a tree root
MODULES = {
    "mycelium_a13_macos.control_session": "mycelium_a13_macos/control_session.py",
    "mycelium_a13_macos.workflow": "mycelium_a13_macos/workflow.py",
    "mycelium_a13_macos.serving": "mycelium_a13_macos/serving.py",
    "mycelium_a13_macos.managed_observations": "mycelium_a13_macos/managed_observations.py",
    "mycelium_live.supervisor": "mycelium_live/supervisor.py",
    "mycelium_live.route": "mycelium_live/route.py",
    "mycelium_seed.plan_binding": "mycelium_seed/plan_binding.py",
    "mycelium_seed.http": "mycelium_seed/http.py",
    "physical_inference_qualification": "physical_inference_qualification.py",
}


class Pyz:
    """Minimal reader for a PyInstaller PYZ (works on a bare .pyz or on a copy of the exe)."""

    def __init__(self, blob, base=0):
        self.raw = blob[base:]
        if self.raw[:4] != b"PYZ\0":
            raise SystemExit("no PYZ magic at base+0 (base=%d)" % base)
        self.toc_pos = struct.unpack("!I", self.raw[8:12])[0]
        entries = marshal.loads(self.raw[self.toc_pos:])
        self.toc = {name: (pos, length) for name, (_typ, pos, length) in entries}

    @classmethod
    def from_exe(cls, path):
        with open(path, "rb") as fh:
            data = fh.read()
        base = data.find(b"PYZ\0")
        if base < 0:
            raise SystemExit("no PYZ found inside %s" % path)
        return cls(data, base)

    def extract(self, name):
        pos, length = self.toc[name]
        chunk = self.raw[pos:pos + length]
        try:
            return marshal.loads(zlib.decompress(chunk))
        except Exception:
            return marshal.loads(chunk)


def walk(code, strings, names):
    for const in code.co_consts:
        if isinstance(const, str):
            strings.add(const)
        elif isinstance(const, types.CodeType):
            walk(const, strings, names)
    names.update(code.co_names)
    return strings, names


def strings_of_source(path):
    with open(path, "rb") as fh:
        data = fh.read()
    return walk(compile(data, path, "exec"), set(), set())[0]


def strings_of_git(rel, rev="HEAD"):
    r = subprocess.run(["git", "-C", PILOT, "show", "%s:%s" % (rev, rel)], capture_output=True)
    if r.returncode != 0:
        return set()
    return walk(compile(r.stdout, rel, "exec"), set(), set())[0]


def markers_for(rel):
    pilot = strings_of_git(rel)
    demo = strings_of_source(os.path.join(DEMO, rel))
    return sorted(pilot - demo, key=len, reverse=True)[:4], sorted(pilot & demo, key=len, reverse=True)[:1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, help="path to the app executable OR a bare .pyz")
    ap.add_argument("--label", default="bundle")
    ap.add_argument("--baseline", help="earlier exe/pyz for the differential")
    args = ap.parse_args()

    pyz = Pyz.from_exe(args.exe)
    base = Pyz.from_exe(args.baseline) if args.baseline else None
    print("=== %s :: %s" % (args.label, args.exe))
    print("    PYZ modules: %d" % len(pyz.toc))
    for mod, rel in MODULES.items():
        pilot_only, control = markers_for(rel)
        if mod not in pyz.toc:
            print("  %-44s NOT IN PYZ" % mod)
            continue
        got = walk(pyz.extract(mod), set(), set())[0]
        # Deterministic order: pilot_only is a set, and its iteration order varies per process, so two
        # ledger runs over the SAME bundle could print the same evidence differently (measured 2026-09-20).
        ordered = sorted(pilot_only)
        bits = ["%s%s" % ("+" if m in got else "-", m[:34]) for m in ordered]
        ctrl = "+" if control and control[0] in got else "-"
        verdict = "==PILOT" if pilot_only and all(m in got for m in pilot_only) else \
                  ("==OLD/none" if pilot_only else "no-divergence")
        line = "  %-44s pilot[%s] ctrl%s -> %s" % (mod, " ".join(bits), ctrl, verdict)
        if base is not None and mod in base.toc:
            bgot = walk(base.extract(mod), set(), set())[0]
            delta = "".join("+" if (m in got) != (m in bgot) else "." for m in ordered)
            line += "  | vs baseline: %s" % delta
        print(line)
    print("\n  LEGEND: '+' = the marker exists in this bytecode; '.' = unchanged vs baseline.")
    print("  A change is PROVEN SHIPPED only when its own marker is '+' here, the baseline row")
    print("  shows it changing, and the module's control is '+'. Anything else proves nothing.")


if __name__ == "__main__":
    main()
