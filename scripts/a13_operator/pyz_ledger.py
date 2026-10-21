#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-module PYZ ledger: which SOURCE TREE does a shipped bundle's executed bytecode match?

WHY THIS EXISTS
    A green deploy digest proves the deploy copied what the build produced. It does NOT prove
    the build used the sources you edited. This tool answers the question that actually matters:
    for EVERY module a change touches, is the change in the artifact that runs?

    Markers are constants present in one source tree and absent in the other, so "which tree" is a
    measurement. TWO KIND OF MARKER ARE NEEDED, and the pre-fix tool only had the first:

      * str constants.
      * BYTE-STRING constants.  Measured on the R1 fix (preview.83 vs preview.82, 2026-09-21):
        that change adds ZERO new str constants to mycelium_a13_macos/app.py (317 -> 317) because
        every literal it introduces is a bytes literal (BUSY_BODY = b'{"error":"server_busy"}',
        BUSY_RESPONSE = b'HTTP/1.1 503 Service Unavailable...').  A str-only needle cannot see it.

    THE ROW SET IS THE ARCHIVE'S REAL TABLE OF CONTENTS.  The pre-fix tool printed
    "PYZ modules: 1888" and then walked a hardcoded 9-entry dict, so the one module R1 changed
    (mycelium_a13_macos.app) could not appear in its output no matter how much it changed.  And
    with a str-only needle that row would have read "no-divergence" even if it had.  Both blind
    spots produced the same shipped failure (runs/wp8-02-useful-two-mac/pyz-ledger-pre83.txt):
    every "vs baseline" delta a dot -- not one '+' in nine rows -- while a module had changed,
    measured independently by a full-TOC byte differential (exactly one differing module,
    71255 -> 72286 decompressed bytes).

    So every row of the TOC is now enumerated, each module is paired to source by its OWN NAME
    (never a curated list), and a module with no source pairing is reported UNCOVERED -- an
    instrument must never imply coverage it does not have.

CONTROL (unchanged concept, strengthened)
    Each mapped module carries a control marker that exists in BOTH source trees: if the control is
    missing you are reading the wrong module and the row means nothing.  Both kinds are controlled
    (str and bytes), and when a --baseline is given the controls must hold on BOTH sides of the
    differential -- a control that is absent from the baseline controls nothing.

WHAT THIS DOES NOT PROVE (read this before quoting a green row)
    * It proves COVERAGE and CONTENT: which module's shipped bytecode carries a fact that only one
      of the two source trees has.  It does NOT prove the change is CORRECT, complete, or that the
      app exercises it at runtime.  A '+' marker is a statement about bytes at rest.
    * A row is a statement about the module, not about behaviour: no network, no round, no laptop.
    * The row set is the ARCHIVE's table of contents, so a source module the build never put into
      the PYZ is invisible here: this instrument cannot see a module that is missing entirely.
    * UNCOVERED rows and rows with no marker are uncovered -- they are not evidence of no change.
      Only a marker that moves between two bundles, with its controls holding, proves a change
      shipped.
    * A row prints up to 4 markers per kind, longest first, and notes any beyond that as "+N-more".
      The verdict, the attribution and the baseline delta are taken over EVERY marker, not only the
      ones with room on the line; "+N moved beyond the 8 shown" says so when it matters.

USAGE (interpreter must be the one matching the bundle's bytecode, e.g. python3.14)
    /opt/homebrew/bin/python3 tools/pyz_ledger.py --exe "/path/Contents/MacOS/Mycelium A13 Demo" \
        --label preview.73
    /opt/homebrew/bin/python3 tools/pyz_ledger.py --exe ... --label preview.83 --baseline <earlier exe>

    --baseline gives the differential: a marker that is present in BOTH builds discriminates
    nothing.  Always run a baseline for the change you are trying to prove.

    --pilot-repo/--pilot-rev/--demo-root point the two source trees elsewhere (defaults are the
    real ones); they exist so the row set and the needle can be exercised on a fixture.

NO PyInstaller REQUIRED: the PYZ is located by its magic and its TOC is marshalled directly.
"""
import argparse, marshal, os, struct, subprocess, sys, types, zlib

PILOT = os.path.expanduser("~/Documents/playground/mycelium-core-pilot")
DEMO = os.path.expanduser("~/Documents/playground/mycelium-a13-macos-demo")
MARKER_LIMIT = 4   # markers kept per kind, longest first (the pre-fix tool kept 4 str markers)
CLIP = 34          # characters of a marker shown in a row (the pre-fix tool's m[:34])
MAX_DEPTH = 12     # nested code objects walked


def clip(value, limit=CLIP):
    """One line, printable, bounded: a marker is arbitrary text or bytes and must not break a row."""
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or code == 0x7F:
            out.append("\\x%02x" % code)
        else:
            out.append(ch)
    text = "".join(out)
    return text[:limit] + ("..." if len(text) > limit else "")


def ctrl_field(prefix, state):
    """'ctrl+' / 'ctrl-' / 'ctrl:none' -- NO control existing is not the same as a control missing."""
    return "%s%s" % (prefix, state if state in ("+", "-") else ":" + state)


class Pyz:
    """Minimal reader for a PyInstaller PYZ (works on a bare .pyz or on a copy of the exe)."""

    def __init__(self, blob, base=0):
        self.raw = blob[base:]
        if self.raw[:4] != b"PYZ\0":
            raise SystemExit("no PYZ magic at base+0 (base=%d)" % base)
        self.toc_pos = struct.unpack("!I", self.raw[8:12])[0]
        entries = marshal.loads(self.raw[self.toc_pos:])
        # PyInstaller 6 marshals the TOC as a list of (name, (typecode, pos, length)); older
        # versions marshalled a dict.  Both are accepted -- the row set is whatever the archive
        # says it holds, never a list curated on this side.
        pairs = entries.items() if hasattr(entries, "items") else entries
        self.toc = {name: (pos, length) for name, (_typ, pos, length) in pairs}

    @classmethod
    def from_exe(cls, path):
        with open(path, "rb") as fh:
            data = fh.read()
        base = data.find(b"PYZ\0")
        if base < 0:
            raise SystemExit("no PYZ found inside %s" % path)
        return cls(data, base)

    def payload(self, name):
        """The entry's decompressed payload bytes (raw bytes for an uncompressed entry).

        Comparing payloads is the source-free byte differential: it needs no source mapping, so it
        holds for every row of the TOC, including the ones no needle covers.
        """
        pos, length = self.toc[name]
        chunk = self.raw[pos:pos + length]
        try:
            return zlib.decompress(chunk)
        except Exception:
            return chunk

    def extract(self, name):
        return marshal.loads(self.payload(name))


def walk(code, strings, blobs, depth=0):
    """str constants AND byte-string constants from every nested code object."""
    if depth > MAX_DEPTH:
        return
    for const in code.co_consts:
        if isinstance(const, str):
            strings.add(const)
        elif isinstance(const, bytes):
            blobs.append(const)
        elif isinstance(const, types.CodeType):
            walk(const, strings, blobs, depth + 1)


def consts_of_source(data, label):
    strings, blobs = set(), []
    walk(compile(data, label, "exec"), strings, blobs)
    return strings, blobs


def consts_of_code(code):
    strings, blobs = set(), []
    walk(code, strings, blobs)
    return strings, blobs


def joined(blobs):
    """The byte constants as one NUL-separated blob.

    A byte marker is tested as a SUBSTRING of this blob, not as set membership: the compiler folds
    adjacent byte literals into one constant, so const boundaries are a codegen artefact and a fact
    can straddle them (measured on BUSY_RESPONSE).  The NUL separator stops a match spanning two
    constants.
    """
    return b"\x00".join(blobs)


def pilot_files(repo, rev):
    r = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", rev], capture_output=True)
    if r.returncode != 0:
        raise SystemExit("git ls-tree failed for %s@%s: %s"
                         % (repo, rev, r.stderr.decode("utf-8", "replace").strip()[:200]))
    return set(r.stdout.decode("utf-8", "replace").split("\n")) - {""}


def pilot_source(repo, rev, rel):
    r = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, rel)], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def map_source(name, tracked, demo_root):
    """Module name -> (source path relative to a tree root, why not).

    Derived from the module's own name.  A curated mapping is exactly what hid the R1 change, so
    there is none: what cannot be derived is reported UNCOVERED instead of being dropped.
    """
    base = name.replace(".", "/")
    tried = []
    for rel in (base + ".py", base + "/__init__.py"):
        in_pilot = rel in tracked
        in_demo = os.path.isfile(os.path.join(demo_root, rel))
        if in_pilot and in_demo:
            return rel, ""
        tried.append("%s(pilot@rev=%s demo=%s)" % (rel, "y" if in_pilot else "n", "y" if in_demo else "n"))
    return None, "no source pairing, tried " + " ".join(tried)


def markers_for(rel, repo, rev, demo_root):
    """The differential's needles: what the pilot tree has that the demo tree does not.

    Returns (str_markers, bytes_markers, str_control, bytes_control) or None when either side of
    the pair is missing.  The FULL marker lists come back -- the verdict is taken over every one of
    them, not over the few a row has space to show.  Markers are ordered longest-first and a
    candidate that is only a fragment of a longer kept marker is dropped so the row cannot repeat
    one fact.
    """
    data = pilot_source(repo, rev, rel)
    demo_path = os.path.join(demo_root, rel)
    if data is None or not os.path.isfile(demo_path):
        return None
    with open(demo_path, "rb") as fh:
        demo = fh.read()
    p_strs, p_blobs = consts_of_source(data, "pilot:" + rel)
    d_strs, d_blobs = consts_of_source(demo, "demo:" + rel)
    str_markers = sorted(p_strs - d_strs, key=len, reverse=True)
    demo_blob = joined(d_blobs)
    bytes_markers, taken = [], b"\x00"
    for blob in sorted([b for b in p_blobs if b], key=len, reverse=True):
        if blob in demo_blob or blob in taken:
            continue
        bytes_markers.append(blob)
        taken += blob
    # CONTROLS: present in BOTH source trees.  A row whose control is missing is reading the wrong
    # module.  Byte-string controls close the hole the str-only needle left open.
    str_control = sorted(p_strs & d_strs, key=len, reverse=True)[:1]
    bytes_control = sorted([b for b in p_blobs if b and b in demo_blob], key=len, reverse=True)[:1]
    return str_markers, bytes_markers, str_control, bytes_control


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, help="path to the app executable OR a bare .pyz")
    ap.add_argument("--label", default="bundle")
    ap.add_argument("--baseline", help="earlier exe/pyz for the differential")
    ap.add_argument("--pilot-repo", default=PILOT, help="the source tree the change lives in (a git repo)")
    ap.add_argument("--pilot-rev", default="HEAD", help="revision of --pilot-repo to read")
    ap.add_argument("--demo-root", default=DEMO, help="the other source tree (files on disk)")
    args = ap.parse_args()

    pyz = Pyz.from_exe(args.exe)
    base = Pyz.from_exe(args.baseline) if args.baseline else None
    tracked = pilot_files(args.pilot_repo, args.pilot_rev)

    print("=== %s :: %s" % (args.label, args.exe))
    print("    PYZ TOC: %d modules (every one of them is a row below -- the row set is the archive's,"
          " not a curated list)" % len(pyz.toc))
    print("    markers: %s source tree vs %s tree | baseline: %s"
          % (args.pilot_repo + "@" + args.pilot_rev, args.demo_root,
             args.baseline if args.baseline else "(none: marker presence alone is NOT a proof)"))

    mapped_rows, uncovered_rows = [], []
    n_no_marker = 0
    proven, unattributed, unreadable, ctrl_broken = [], [], [], []
    differs, added = [], []
    changed_removed = sorted(set(base.toc) - set(pyz.toc)) if base is not None else []

    for mod in sorted(pyz.toc):
        # ---- the source-free half: did this module's bytecode change at all? ----------------
        byte_status = ""
        if base is not None:
            if mod not in base.toc:
                byte_status = "ADDED-in-this-build"
                added.append(mod)
            else:
                try:
                    now, then = pyz.payload(mod), base.payload(mod)
                    if now == then:
                        byte_status = "SAME"
                    else:
                        byte_status = "CHANGED %d->%d B" % (len(then), len(now))
                        differs.append((mod, len(then), len(now)))
                except Exception as exc:
                    byte_status = "UNREADABLE(%s)" % type(exc).__name__

        # ---- the source-mapped half: which tree does this module's content come from? -------
        rel, why = map_source(mod, tracked, args.demo_root)
        if rel is None:
            extra = " | bytecode: %s" % byte_status if byte_status and byte_status != "SAME" else ""
            if byte_status.startswith("UNREADABLE"):
                unreadable.append((mod, byte_status))
            uncovered_rows.append("  %-46s UNCOVERED (%s)%s" % (mod, why, extra))
            continue
        try:
            got_strs, got_blobs = consts_of_code(pyz.extract(mod))
        except Exception as exc:
            unreadable.append((mod, type(exc).__name__))
            uncovered_rows.append("  %-46s UNCOVERED (unreadable entry: %s) | bytecode: %s"
                                  % (mod, type(exc).__name__, byte_status or "n/a"))
            continue
        got_blob = joined(got_blobs)
        needles = markers_for(rel, args.pilot_repo, args.pilot_rev, args.demo_root)
        if needles is None:
            uncovered_rows.append("  %-46s UNCOVERED (source pairing vanished: %s)" % (mod, rel))
            continue
        str_markers, bytes_markers, str_control, bytes_control = needles

        shown_str, shown_byt = str_markers[:MARKER_LIMIT], bytes_markers[:MARKER_LIMIT]
        n_hidden = (len(str_markers) - len(shown_str)) + (len(bytes_markers) - len(shown_byt))
        bits = ["%s%s" % ("+" if m in got_strs else "-", clip(m)) for m in shown_str]
        bits += ["%sb:%s" % ("+" if m in got_blob else "-", clip(m)) for m in shown_byt]
        if n_hidden:
            bits.append("+%d-more" % n_hidden)
        # Controls: three states, because "no control exists for this module" and "the control is
        # missing" are different facts and only the second one is an alarm.
        ctrl = "none" if not str_control else ("+" if str_control[0] in got_strs else "-")
        bctrl = "none" if not bytes_control else ("+" if bytes_control[0] in got_blob else "-")
        n_markers = len(str_markers) + len(bytes_markers)
        all_present = (all(m in got_strs for m in str_markers)
                       and all(m in got_blob for m in bytes_markers))

        # A module with no marker has NO needle: the row must say so rather than imply "unchanged"
        # (the pre-fix tool printed "no-divergence" here, which is exactly the false negative that
        # hid R1).  Coverage is stated, never implied.
        if n_markers == 0:
            verdict = "no-divergence/UNCOVERED-no-marker"
            n_no_marker += 1
        elif all_present:
            verdict = "==PILOT"
        else:
            verdict = "==OLD/none"

        flags, delta, base_ctrl_ok, hidden_moved, n_moved = [], "", True, 0, 0
        if base is not None:
            if mod not in base.toc:
                delta = "new-in-this-build"
            else:
                try:
                    b_strs, b_blobs = consts_of_code(base.extract(mod))
                    b_blob = joined(b_blobs)
                    delta = "".join(
                        "+" if (m in got_strs) != (m in b_strs) else "."
                        for m in shown_str) + "".join(
                        "+" if (m in got_blob) != (m in b_blob) else "."
                        for m in shown_byt)
                    # Markers a row has no space for still count: the verdict and the attribution are
                    # taken over every marker, and their movement is reported here.
                    hidden_moved = (sum(1 for m in str_markers[MARKER_LIMIT:]
                                        if (m in got_strs) != (m in b_strs))
                                    + sum(1 for m in bytes_markers[MARKER_LIMIT:]
                                          if (m in got_blob) != (m in b_blob)))
                    n_moved = delta.count("+") + hidden_moved
                    if not delta:
                        delta = "no-markers"
                    if hidden_moved:
                        delta += " [+%d moved beyond the %d shown]" % (hidden_moved, MARKER_LIMIT * 2)
                    # The control must hold on BOTH sides of the differential: a control the
                    # baseline does not carry controls nothing.  A control that does not exist for
                    # this module is reported as such, not counted as a failure.
                    base_ctrl_ok = ((ctrl == "none" or str_control[0] in b_strs)
                                    and (bctrl == "none" or bytes_control[0] in b_blob))
                    if n_moved and byte_status.startswith("CHANGED"):
                        flags.append("attributed")
                    if byte_status.startswith("CHANGED") and not n_moved:
                        flags.append("CHANGED-UNATTRIBUTED *** no marker of this instrument moved ***")
                        unattributed.append(mod)
                    if (ctrl == "-" or bctrl == "-" or not base_ctrl_ok) and (
                            n_markers or byte_status.startswith("CHANGED")):
                        flags.append("CONTROL-BROKEN (marker/control absent in this or the baseline"
                                     " bundle) -- this row proves nothing")
                        ctrl_broken.append(mod)
                except Exception as exc:
                    delta = "UNREADABLE(%s)" % type(exc).__name__
            line = "  %-46s pilot[%s] %s %s -> %s" % (
                mod, " ".join(bits), ctrl_field("ctrl", ctrl), ctrl_field("bctrl", bctrl), verdict)
            line += "  | vs baseline: %s" % delta
        else:
            line = "  %-46s pilot[%s] %s %s -> %s" % (
                mod, " ".join(bits), ctrl_field("ctrl", ctrl), ctrl_field("bctrl", bctrl), verdict)
        if byte_status:
            line += "  | bytecode: %s" % byte_status
        # The pre-fix legend's rule, tightened: a proof needs a marker that MOVES, the controls
        # holding on both sides, and the module's own bytecode to have changed.
        controls_ok = (ctrl == "+" and bctrl in ("+", "none"))
        proven_here = (n_markers and all_present and controls_ok and base is not None
                       and mod in base.toc and byte_status.startswith("CHANGED")
                       and base_ctrl_ok and n_moved)
        if proven_here:
            flags.append("PROVEN-SHIPPED")
            proven.append((mod, rel, n_markers, n_moved))
        if flags:
            line += "  | " + " | ".join(flags)
        mapped_rows.append(line)

    # ---- output: mapped rows first, then the uncovered ones, then the accounting ------------
    print("\n  -- modules with a source pairing (marker + control per kind) --")
    for line in mapped_rows:
        print(line)
    print("\n  -- modules with NO source pairing: UNCOVERED (no needle exists for them; this is"
          " NOT a statement that they did not change) --")
    for line in uncovered_rows:
        print(line)

    print("\n  ---- ACCOUNTING (coverage of this instrument; read before quoting a row) ----")
    print("  rows: PYZ TOC=%d  source-mapped=%d  UNCOVERED=%d  (mapped rows carrying no marker at"
          " all: %d)" % (len(pyz.toc), len(mapped_rows), len(uncovered_rows), n_no_marker))
    if len(mapped_rows) + len(uncovered_rows) != len(pyz.toc):
        print("  *** ROW SET INCOMPLETE: %d + %d != %d rows of the TOC -- this ledger is not fit to"
              " quote ***" % (len(mapped_rows), len(uncovered_rows), len(pyz.toc)))
    if n_no_marker:
        print("  %d mapped row(s) carry no marker ('no-divergence/UNCOVERED-no-marker'): no needle"
              " exists for them, which is NOT the same as the module being unchanged." % n_no_marker)
    if base is not None:
        print("  baseline sweep over ALL %d rows (decompressed payload, no source mapping needed):"
              " compared=%d differing=%d added=%d removed=%d unreadable=%d"
              % (len(pyz.toc), len(pyz.toc), len(differs), len(added), len(changed_removed),
                 len(unreadable)))
        print("    CHANGED vs baseline (this list is computed over the WHOLE TOC, so no module can"
              " hide from it): %s"
              % (", ".join("%s %d->%d B" % (m, a, b) for m, a, b in differs) if differs else "(none)"))
        if added:
            print("    ADDED in this build: %s" % ", ".join(added))
        if changed_removed:
            print("    ONLY IN THE BASELINE (removed by this build): %s" % ", ".join(changed_removed))
        if unreadable:
            print("    UNREADABLE entries: %s" % ", ".join("%s(%s)" % (m, w) for m, w in unreadable))
        print("  PROVEN SHIPPED (marker '+' here, the baseline row shows it moving, control '+' on"
              " BOTH sides): %s"
              % (", ".join("%s [%d marker(s), %d changing]" % (m, n, c) for m, _r, n, c in proven)
                 if proven else "(none)"))
        print("  CHANGED BUT NOT ATTRIBUTED by any marker of this instrument: %s"
              % (", ".join(unattributed) if unattributed else "(none)"))
        if ctrl_broken:
            print("  ROWS WITH A BROKEN CONTROL (control missing on one side of the differential): %s"
                  % ", ".join(ctrl_broken))
    else:
        print("  NO --baseline: every row is marker presence only.  That is NOT a proof that a"
              " change shipped -- run the earlier bundle as --baseline.")
    print("  UNCOVERED rows are modules this instrument has no source pairing for; they are not"
          " evidence of no change, and no row above is evidence about runtime behaviour.")

    print("\n  LEGEND: '+' = the marker exists in this bytecode; 'b:' = the marker is a BYTE-STRING"
          " constant, tested as a substring of the module's NUL-joined bytes constants (the compiler"
          " folds adjacent byte literals); '.' = unchanged vs baseline; '+N-more' = markers beyond"
          " the 4 shown per kind (they still count in the verdict and the delta); 'ctrl'/'bctrl' ="
          " the str/bytes control marker present in BOTH source trees.")
    print("  A change is PROVEN SHIPPED only when its own marker is '+' here, the baseline row shows")
    print("  it moving, and the module's controls are '+' on both sides.  Anything else proves nothing.")
    print("  WHAT THIS DOES NOT PROVE: that the change is correct, complete, reachable or exercised"
          " at runtime -- a '+' marker is a statement about bytes at rest, not about behaviour.")


if __name__ == "__main__":
    main()
