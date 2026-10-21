#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The per-module PYZ ledger must SEE a bytes-only change, and must never imply coverage it lacks.

Both halves are needed, because both can fail:
  A) THE PRE-FIX TOOL (pinned byte-identical copy in fixtures/, sha checked against git 9d68291)
     must MISS the change.  If it did not, this test would be testing nothing -- a test that cannot
     fail proves nothing (ERRATA-6).
  B) THE FIXED TOOL must report the change, AND must state the rows it cannot speak to.  A row set
     that hides a module, or a needle that cannot see the literal a change is made of, is worse
     than no instrument: it is confidently wrong in a format designed to be trusted.

THE CASE, measured on the retained artifacts (read-only):
  preview.83 vs preview.82, the R1 fix.  A full-TOC byte differential over all 1888 modules finds
  exactly ONE differing module: mycelium_a13_macos.app, 71255 -> 72286 decompressed bytes.
  R1 adds ZERO new str constants to its source (317 -> 317, every literal it adds is bytes), so the
  pre-fix needle has NO marker for the module that changed, and its hardcoded 9-entry row set has no
  row for it either.  The shipped ledger (runs/wp8-02-useful-two-mac/pyz-ledger-pre83.txt) is
  reproduced byte-for-byte by A1 below: every "vs baseline" delta a dot.

Legs:
  A1  the pre-fix tool, real pair          -> 9 rows, 0 positives, no row for the changed module
  A2  the pre-fix tool GIVEN the app row   -> "pilot[] ctrl+ -> no-divergence | vs baseline:" (empty)
  A3  the fixed tool, same real pair       -> 1888 rows, the app row PROVEN-SHIPPED off BYTE markers
  A4  the fixed tool's own sweep numbers are cross-checked against this test's independent sweep
  B1  hermetic fixture (PYZ pair + two source trees, built here): a fixture module whose ONLY
      change is a bytes literal -> pre-fix tool says no-divergence, fixed tool proves it shipped
  B2  the fixture also carries a module with no source anywhere and a module only in the baseline,
      so the fixed tool's UNCOVERED / REMOVED reporting is exercised on purpose

Run: env -u PYTHONPATH /opt/homebrew/bin/python3 -B scripts/a13_operator/test_pyz_ledger_coverage.py
      (that interpreter is 3.14, the one the bundle's bytecode is marshalled for)

Exit: 0 every check passed - 1 at least one FAIL - 3 cannot run (wrong interpreter / evidence gone)
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED = os.path.join(HERE, "pyz_ledger.py")
PINNED = os.path.join(HERE, "fixtures", "pyz_ledger_pre_fix_9d68291.py")
# sha256 of the fixture as published (SPDX header added, two absolute paths made relative to ~);
# ORIGIN_SHA256 is the digest of the original blob at PINNED_REV.
PINNED_SHA256 = "9647a2802a01e1d52a9d71f02ac008d38e07265e711ff889ac85652bf9185125"
ORIGIN_SHA256 = "218c7596033ced7f3bd602ec617b32f5522492f57ee1d28fd830bb4692008637"
PINNED_REV = "9d68291"
PINNED_REL = "scripts/a13_operator/pyz_ledger.py"

DIST = "/Users/owner/Documents/mycelium-core-program/runs/wp8-02-g1/dist"
APP = "Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo"
NEW_V, OLD_V = "0.1.0-preview.83", "0.1.0-preview.82"
NEW_EXE, OLD_EXE = os.path.join(DIST, NEW_V, APP), os.path.join(DIST, OLD_V, APP)
TOUCHED = "mycelium_a13_macos.app"
TOUCHED_REL = "mycelium_a13_macos/app.py"
# The one fact R1 introduces that a str-only needle cannot see, and the size of the change.
R1_BYTES_LITERAL = b'{"error":"server_busy"}'
EXPECTED_SIZES = (71255, 72286)

FAILS = 0
UNABLE = 0


def check(label, ok, detail=""):
    global FAILS
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label,
                          ("  " + str(detail)) if detail != "" else ""))
    if not ok:
        FAILS += 1


def note(label, detail):
    print("  ....  %s  %s" % (label, detail))


def run(cmd, **kw):
    """Run a command, echo it, return the CompletedProcess with stdout/stderr merged."""
    print("    $ " + " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def load_pyz(path):
    """Independent re-implementation of the container read, so the tool cannot grade itself."""
    data = open(path, "rb").read()
    base = data.find(b"PYZ\0")
    if base < 0:
        raise SystemExit("no PYZ magic in %s" % path)
    raw = data[base:]
    entries = marshal.loads(raw[struct.unpack("!I", raw[8:12])[0]:])
    pairs = entries.items() if hasattr(entries, "items") else entries
    return raw, {n: (p, l) for n, (_t, p, l) in pairs}


def payload(raw, entry):
    pos, length = entry
    chunk = raw[pos:pos + length]
    try:
        return zlib.decompress(chunk)
    except Exception:
        return chunk


def sweep(new_path, old_path):
    """Every module of the TOC, decompressed and byte-compared: the source-free ground truth."""
    raw_n, toc_n = load_pyz(new_path)
    raw_o, toc_o = load_pyz(old_path)
    differ, unreadable = [], []
    for mod in sorted(set(toc_n) & set(toc_o)):
        try:
            a, b = payload(raw_n, toc_n[mod]), payload(raw_o, toc_o[mod])
            if a != b:
                differ.append((mod, len(b), len(a)))
        except Exception as exc:
            unreadable.append((mod, type(exc).__name__))
    return {"toc": len(toc_n), "only_new": sorted(set(toc_n) - set(toc_o)),
            "only_old": sorted(set(toc_o) - set(toc_n)), "differ": differ, "unreadable": unreadable}


ROW_RE = re.compile(r"^  (\S+)\s+(?:pilot\[|UNCOVERED \()")


def rows_of(out):
    """The tool's per-module rows only: '<module> pilot[...]' or '<module> UNCOVERED (...)'."""
    return [l for l in out.split("\n") if ROW_RE.match(l)]


def row_for(out, module):
    hits = [l for l in rows_of(out) if re.match(r"^  %s\s" % re.escape(module), l)]
    return hits[0] if hits else None


def uncovered_rows_of(out):
    """Rows printed in the UNCOVERED form (a mapped row with no marker also contains the word)."""
    return [l for l in out.split("\n") if re.match(r"^  \S+\s+UNCOVERED \(", l)]


def pilot_markers(row):
    """The markers inside pilot[...] as (sign, text) pairs."""
    m = re.search(r"pilot\[(.*?)\]", row)
    if not m:
        return []
    return [(tok[0], tok[1:]) for tok in m.group(1).split() if tok[:1] in "+-"]


def delta_field(row):
    m = re.search(r"\| vs baseline:\s*(\S*)", row)
    return m.group(1) if m else ""


def accounting(out, key):
    """The integer an ACCOUNTING field reports, or None when the field is not there at all."""
    m = re.search(r"%s\s*=\s*(\d+)" % re.escape(key), out)
    return int(m.group(1)) if m else None


def acct(out, key):
    value = accounting(out, key)
    return -1 if value is None else value


def pre_fix_runner(tmp, inject, argv):
    """Generate a runner that loads the PINNED pre-fix tool and injects ONLY what is named.

    The injection is the point of the RED legs: A1 injects nothing at all (the tool as shipped),
    A2/B1 add the row set and the paths the tool was missing.  Its needle, verdict and control
    logic are its own, unmodified code.
    """
    path = os.path.join(tmp, "run_pre_fix.py")
    with open(path, "w") as fh:
        fh.write(
            "import importlib.util, json, sys\n"
            "spec = importlib.util.spec_from_file_location('pyz_ledger_pre_fix', %r)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules['pyz_ledger_pre_fix'] = mod\n"
            "spec.loader.exec_module(mod)\n"
            "inject = json.loads(%r)\n"
            "for key in ('MODULES', 'PILOT', 'DEMO'):\n"
            "    if key in inject:\n"
            "        setattr(mod, key, inject[key])\n"
            "sys.argv = ['pyz_ledger_pre_fix.py'] + json.loads(%r)\n"
            "mod.main()\n" % (PINNED, json.dumps(inject), json.dumps(argv)))
    return path


def build_pyz(path, entries):
    """Write a real PyInstaller (ZlibArchive) PYZ: magic, bytecode magic, TOC offset, entries, TOC.

    Layout copied from PyInstaller/archive/writers.py: 12+5 reserved header bytes, zlib-compressed
    marshal payloads, marshalled TOC = list of (name, (typecode, pos, length)).
    """
    header = bytearray(b"PYZ\0" + b"\0" * 4 + b"\0" * 4 + b"\0" * 5)
    body, toc = bytearray(), []
    for name, _typecode, code in entries:
        blob = zlib.compress(marshal.dumps(code), 6)
        toc.append((name, (_typecode, len(header) + len(body), len(blob))))
        body += blob
    out = header + body + marshal.dumps(toc)
    struct.pack_into("!i", out, 8, len(header) + len(body))
    with open(path, "wb") as fh:
        fh.write(bytes(out))


INIT_SRC = '"""Fixture package: exists only inside this test\'s temp directory."""\nVERSION = "f1"\n'
KEEP_BYTES = b"X-Fixture-Keep: 1"          # in BOTH trees: the byte-string CONTROL
CHANGED_BYTES = b'{"error":"server_busy"}'  # in the pilot tree only: the byte-string MARKER
MOD_OLD = '''"""Fixture module. The two source trees differ BYTES-ONLY, on purpose.

The bytes-only delta is the whole point: a str-only needle has nothing to look at here, which is
exactly the R1 failure this test pins down.
"""
KEEP = %(keep)r
STATE = "idle"


def serve(request):
    """Answer the request."""
    return KEEP + STATE.encode("ascii")
''' % {"keep": KEEP_BYTES}
MOD_NEW = MOD_OLD + '''

BUSY_BODY = %(body)r
BUSY_RESPONSE = (b"HTTP/1.1 503 Service Unavailable\\r\\n"
                 b"Content-Type: application/json\\r\\n")


def answer_busy(request):
    return BUSY_RESPONSE + BUSY_BODY
''' % {"body": CHANGED_BYTES}
STABLE_SRC = '"""Fixture module identical in both trees."""\nKEEP = b"X-Fixture-Keep: 1"\nSTATE = "idle"\n'
SOLO_SRC = '"""Fixture module that exists in the PYZ but in neither source tree."""\nX = b"solo"\n'
REMOVED_SRC = '"""Fixture module the new build no longer carries."""\nY = b"removed"\n'


def build_fixture(tmp):
    """A PYZ pair plus the two source trees, so the row set and the needle can be driven at will."""
    root = os.path.join(tmp, "fixture")
    pilot, demo = os.path.join(root, "pilot"), os.path.join(root, "demo")
    for side in (pilot, demo):
        os.makedirs(os.path.join(side, "fixture_pkg"), exist_ok=True)
        with open(os.path.join(side, "fixture_pkg", "__init__.py"), "w") as fh:
            fh.write(INIT_SRC)
    with open(os.path.join(pilot, "fixture_pkg", "mod.py"), "w") as fh:
        fh.write(MOD_NEW)
    with open(os.path.join(demo, "fixture_pkg", "mod.py"), "w") as fh:
        fh.write(MOD_OLD)
    for side in (pilot, demo):
        with open(os.path.join(side, "fixture_pkg", "stable.py"), "w") as fh:
            fh.write(STABLE_SRC)
    # In the pilot tree but NOT in the demo tree: must be reported UNCOVERED, with the reason.
    with open(os.path.join(pilot, "fixture_pkg", "onlypilot.py"), "w") as fh:
        fh.write(SOLO_SRC)
    env = ["-c", "user.name=fixture", "-c", "user.email=fixture@local", "-c", "commit.gpgsign=false"]
    for cmd in (["git", "-C", pilot, "init", "-q", "-b", "main"],
                ["git", "-C", pilot, *env, "add", "-A"],
                ["git", "-C", pilot, *env, "commit", "-qm", "fixture"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("fixture git failed: %s\n%s" % (cmd, r.stderr))

    def comp(name, src):
        co_filename = os.path.join(*name.split(".")) + ".py"
        return compile(src, co_filename, "exec")

    new_pyz, old_pyz = os.path.join(root, "new.pyz"), os.path.join(root, "old.pyz")
    build_pyz(new_pyz, [("fixture_pkg", 1, comp("fixture_pkg", INIT_SRC)),
                        ("fixture_pkg.mod", 0, comp("fixture_pkg.mod", MOD_NEW)),
                        ("fixture_pkg.stable", 0, comp("fixture_pkg.stable", STABLE_SRC)),
                        ("fixture_pkg.onlypilot", 0, comp("fixture_pkg.onlypilot", SOLO_SRC))])
    build_pyz(old_pyz, [("fixture_pkg", 1, comp("fixture_pkg", INIT_SRC)),
                        ("fixture_pkg.mod", 0, comp("fixture_pkg.mod", MOD_OLD)),
                        ("fixture_pkg.stable", 0, comp("fixture_pkg.stable", STABLE_SRC)),
                        ("fixture_pkg.removed", 0, comp("fixture_pkg.removed", REMOVED_SRC))])
    return {"root": root, "pilot": pilot, "demo": demo, "new": new_pyz, "old": old_pyz}


def consts_of(src):
    strs, blobs = set(), []

    def walk(code, depth=0):
        if depth > 12:
            return
        for c in code.co_consts:
            if isinstance(c, str):
                strs.add(c)
            elif isinstance(c, bytes):
                blobs.append(c)
            elif hasattr(c, "co_consts"):
                walk(c, depth + 1)

    walk(compile(src, "<fixture>", "exec"))
    return strs, b"\x00".join(blobs)


def main():
    global UNABLE
    print("=== pyz_ledger coverage test: does the instrument see the change it exists to prove? ===")
    if sys.version_info[:2] < (3, 14):
        print("  UNABLE  interpreter is %s; the bundle's bytecode needs 3.14 (use"
              " /opt/homebrew/bin/python3)" % sys.version.split()[0])
        return 3
    for label, path in (("the fixed ledger", FIXED), ("the pinned pre-fix ledger", PINNED),
                        ("the .83 executable", NEW_EXE), ("the .82 executable", OLD_EXE)):
        if not os.path.exists(path):
            print("  UNABLE  %s is missing: %s" % (label, path))
            return 3

    print("\n-- the pinned pre-fix tool is the tool at %s, byte for byte --" % PINNED_REV)
    sha = hashlib.sha256(open(PINNED, "rb").read()).hexdigest()
    check("fixtures/pyz_ledger_pre_fix_%s.py sha256" % PINNED_REV, sha == PINNED_SHA256, sha)
    repo = subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True)
    if repo.returncode == 0:
        blob = subprocess.run(["git", "-C", repo.stdout.strip(), "show",
                               "%s:%s" % (PINNED_REV, PINNED_REL)], capture_output=True)
        if blob.returncode == 0:  # the revision exists only in the original (private) history
            check("matches `git show %s:%s`" % (PINNED_REV, PINNED_REL),
                  hashlib.sha256(blob.stdout).hexdigest() == ORIGIN_SHA256)

    # ================= A: the real retained artifacts ============================================
    print("\n-- A1: the PRE-FIX tool on the real %s vs %s pair --" % (NEW_V, OLD_V))
    r = run([sys.executable, "-B", PINNED, "--exe", NEW_EXE, "--label", NEW_V, "--baseline", OLD_EXE])
    red = r.stdout
    red_rows = rows_of(red)
    red_pos = sum(d.count("+") for d in re.findall(r"\| vs baseline:\s*(\S*)", red))
    check("the pre-fix tool ran", r.returncode == 0 and "PYZ modules:" in red)
    check("it claims a %d-module archive" % 1888, "PYZ modules: 1888" in red)
    check("while printing rows for only its curated dict", len(red_rows) == 9, "%d rows" % len(red_rows))
    check("no row for the module that changed", row_for(red, TOUCHED) is None,
          "%s absent from %d rows" % (TOUCHED, len(red_rows)))
    check("not one '+' in any baseline delta (the shipped ledger's failure, reproduced)",
          red_pos == 0, "%d positives across %d delta fields" % (red_pos, len(red_rows)))
    shipped = os.path.join("/Users/owner/Documents/mycelium-core-program/runs",
                           "wp8-02-useful-two-mac", "pyz-ledger-pre83.txt")
    if os.path.exists(shipped):
        same = open(shipped).read().split("\n", 1)[1] == red.split("\n", 1)[1]
        check("reproduces the shipped pyz-ledger-pre83.txt body byte for byte", same)

    truth = sweep(NEW_EXE, OLD_EXE)
    print("  ....  this test's own sweep: TOC=%d differing=%s" % (truth["toc"], truth["differ"]))
    check("ground truth: exactly one module differs, and it is %s" % TOUCHED,
          truth["differ"] == [(TOUCHED, EXPECTED_SIZES[0], EXPECTED_SIZES[1])])
    check("ground truth: the pre-fix tool reported no change while that module changed",
          truth["differ"] and red_pos == 0)

    print("\n-- A2: the SAME pre-fix tool, GIVEN the row it never had --")
    tmp = tempfile.mkdtemp(prefix="pyz-ledger-test-")
    try:
        runner = pre_fix_runner(tmp, {"MODULES": {TOUCHED: TOUCHED_REL}},
                                ["--exe", NEW_EXE, "--label", NEW_V, "--baseline", OLD_EXE])
        r2 = run([sys.executable, "-B", runner])
        row = row_for(r2.stdout, TOUCHED)
        check("the row now exists", row is not None)
        check("its marker list is empty (the needle is str-only; R1 adds no str constant)",
              bool(row) and pilot_markers(row) == [], repr(pilot_markers(row) if row else None))
        check("it says 'no-divergence' -- confidently wrong, with a '+' control making it look sane",
              bool(row) and "no-divergence" in row and "ctrl+" in row, row)
        check("its baseline delta is empty, so nothing at all is reported as changing",
              bool(row) and delta_field(row) == "", repr(delta_field(row) if row else None))

        # ================= B: the fixed tool, same artifacts =====================================
        print("\n-- A3: the FIXED tool on the same pair --")
        r3 = run([sys.executable, "-B", FIXED, "--exe", NEW_EXE, "--label", NEW_V,
                  "--baseline", OLD_EXE])
        green = r3.stdout
        check("the fixed tool ran", r3.returncode == 0)
        grow = row_for(green, TOUCHED)
        check("the changed module has a row", grow is not None)
        marks = pilot_markers(grow) if grow else []
        bmarks = [t for sign, t in marks if t.startswith("b:") and sign == "+"]
        check("its BYTE-STRING markers are the needle, and they are '+'", len(bmarks) >= 1,
              "%d byte marker(s): %s" % (len(bmarks), bmarks[:2]))
        check("its baseline delta shows the markers MOVING", bool(grow) and "+" in delta_field(grow),
              repr(delta_field(grow) if grow else None))
        check("its controls hold ('=' on both sides)", bool(grow) and "ctrl+" in grow and "bctrl+" in grow)
        check("it is reported PROVEN-SHIPPED", bool(grow) and "PROVEN-SHIPPED" in grow)
        check("the tool's own sweep agrees with this test's independent sweep",
              "differing=%d" % len(truth["differ"]) in green
              and "%d->%d B" % EXPECTED_SIZES in green)
        check("with no un-attributed change left over", "CHANGED BUT NOT ATTRIBUTED by any marker"
              " of this instrument: (none)" in green)
        check("the row set is the whole archive", accounting(green, "PYZ TOC") == truth["toc"],
              "PYZ TOC=%s" % accounting(green, "PYZ TOC"))
        n_uncovered = len(uncovered_rows_of(green))
        check("rows cover every module of the TOC (%d mapped + %d uncovered == %d)"
              % (len(rows_of(green)) - n_uncovered, n_uncovered, truth["toc"]),
              len(rows_of(green)) == truth["toc"] and n_uncovered == acct(green, "UNCOVERED")
              and acct(green, "source-mapped") + acct(green, "UNCOVERED") == truth["toc"],
              "rows=%d mapped=%s uncovered=%s" % (len(rows_of(green)), accounting(green, "source-mapped"),
                                                  accounting(green, "UNCOVERED")))
        check("no 'ROW SET INCOMPLETE' self-alarm", "ROW SET INCOMPLETE" not in green)
        check("a module it cannot pair to source is reported UNCOVERED, not silently dropped",
              bool(row_for(green, "json.decoder")) and "UNCOVERED" in row_for(green, "json.decoder"),
              row_for(green, "json.decoder"))
        check("no row claims coverage it lacks: a changed row is never bare 'no-divergence'",
              all("no-divergence" not in l for l in rows_of(green) if "CHANGED" in l))

        # ================= B1/B2: the hermetic fixture ===========================================
        print("\n-- B1: hermetic fixture: a PYZ pair whose ONLY delta is a bytes literal --")
        fx = build_fixture(tmp)
        s_new, b_new = consts_of(MOD_NEW)
        s_old, b_old = consts_of(MOD_OLD)
        check("fixture property: the two source trees add NO str constant", s_new == s_old,
              "str_added=%d" % len(s_new - s_old))
        check("fixture property: the only delta is a byte-string constant",
              CHANGED_BYTES in b_new and CHANGED_BYTES not in b_old)
        fx_new_raw, fx_new_toc = load_pyz(fx["new"])
        fx_old_raw, fx_old_toc = load_pyz(fx["old"])
        n_payload = payload(fx_new_raw, fx_new_toc["fixture_pkg.mod"])
        o_payload = payload(fx_old_raw, fx_old_toc["fixture_pkg.mod"])
        check("fixture property: the PYZ payloads differ", n_payload != o_payload,
              "%d -> %d B" % (len(o_payload), len(n_payload)))

        runner = pre_fix_runner(tmp, {"MODULES": {"fixture_pkg.mod": "fixture_pkg/mod.py"},
                                      "PILOT": fx["pilot"], "DEMO": fx["demo"]},
                                ["--exe", fx["new"], "--label", "fixture", "--baseline", fx["old"]])
        r4 = run([sys.executable, "-B", runner])
        frow = row_for(r4.stdout, "fixture_pkg.mod")
        check("RED: the pre-fix tool reports no-divergence for the fixture change",
              bool(frow) and "no-divergence" in frow and pilot_markers(frow) == [], frow)
        check("RED: zero positives in its baseline delta",
              sum(d.count("+") for d in re.findall(r"\| vs baseline:\s*(\S*)", r4.stdout)) == 0)

        print("\n-- B2: the FIXED tool on the same fixture --")
        r5 = run([sys.executable, "-B", FIXED, "--exe", fx["new"], "--label", "fixture",
                  "--baseline", fx["old"], "--pilot-repo", fx["pilot"], "--pilot-rev", "HEAD",
                  "--demo-root", fx["demo"]])
        fout = r5.stdout
        grow = row_for(fout, "fixture_pkg.mod")
        marks = pilot_markers(grow) if grow else []
        check("the bytes-only change has a row with a '+' BYTE marker",
              bool(grow) and any(sign == "+" and t.startswith("b:") for sign, t in marks),
              "%s" % (marks,))
        check("its delta moves", bool(grow) and "+" in delta_field(grow), repr(delta_field(grow) if grow else None))
        check("it is PROVEN-SHIPPED", bool(grow) and "PROVEN-SHIPPED" in grow)
        check("the sweep finds exactly the fixture module changed",
              "fixture_pkg.mod" in fout and "differing=1" in fout)
        solo = row_for(fout, "fixture_pkg.onlypilot")
        check("a module in the pilot tree only is UNCOVERED with the reason",
              bool(solo) and "UNCOVERED" in solo and "demo=n" in solo, solo)
        check("a module the new build dropped is reported (it has no row; it is not in this TOC)",
              row_for(fout, "fixture_pkg.removed") is None
              and "ONLY IN THE BASELINE (removed by this build): fixture_pkg.removed" in fout)
        check("a module this build added is reported too",
              "ADDED in this build: fixture_pkg.onlypilot" in fout)
        check("the row set is again the whole archive (4 modules, 4 rows)",
              accounting(fout, "PYZ TOC") == 4 and len(rows_of(fout)) == 4,
              "TOC=%s rows=%d" % (accounting(fout, "PYZ TOC"), len(rows_of(fout))))
        check("no baseline -> it refuses to call marker presence a proof",
              bool(row_for(r5.stdout, "fixture_pkg.mod"))
              and "NOT a proof that a change shipped" in run(
                  [sys.executable, "-B", FIXED, "--exe", fx["new"], "--label", "fixture-no-baseline",
                   "--pilot-repo", fx["pilot"], "--demo-root", fx["demo"]]).stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n  ---------------------------------------------------------------")
    if FAILS:
        print("  PYZ_LEDGER_COVERAGE_FAILED(%d)" % FAILS)
        return 1
    print("  PYZ_LEDGER_COVERAGE_PROVEN: the pre-fix tool misses the bytes-only change (real pair and"
          " fixture) and the fixed tool reports it while stating what it does not cover.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
