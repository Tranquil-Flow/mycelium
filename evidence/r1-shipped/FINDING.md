# R1 in the shipped preview.83 bytes — PYZ byte-differential verdict

Lane: `runs/r1-shipped/`. Worktree: `/Users/evinova-self/Documents/playground/mycelium-r1-shipped-lane`
(detached from the read-only pilot at `9d68291`). Dist trees and the pilot tree were read only.

## Verdict

**YES — R1 is inside the shipped preview.83 bytes**, and the evidence is a per-module byte
differential with identical controls, not a string search. Reproduced in **both** executable
variants, `exit 0`.

| module | role | .82 vs .83 decompressed code bytes | verdict |
|---|---|---|---|
| `mycelium_a13_macos.app` | **TOUCHED (R1)** | **DIFFERS** 71255 → 72286 | differs as expected |
| `mycelium_a13_macos.serving` | control | identical 33504 → 33504 | identical as expected |
| `mycelium_a13_macos.workflow` | control | identical 23086 → 23086 | identical as expected |
| `mycelium_a13_macos.control_session` | control | identical 39431 → 39431 | identical as expected |
| `mycelium_a13_macos.managed_observations` | control | identical 14505 → 14505 | identical as expected |
| `mycelium_live.route` | control | identical 279375 → 279375 | identical as expected |
| `mycelium_seed.http` | control | identical 61179 → 61179 | identical as expected |

**FULL SWEEP (stronger than the requested controls):** all **1888** TOC modules of both builds
were compared. **Exactly 1 differs — `mycelium_a13_macos.app`.** 0 unreadable, 0 modules only in
one build. So the app module differing is not "one interesting row among many"; it is the *only*
difference in the entire 15.5 MB executable at the module level.

### Attributing the difference to R1 itself (not just "something changed")

Semantic (name/const-level) changes inside the app module — exactly 4, and they are R1:

```
<module>                        [+names BUSY_BODY, BUSY_RESPONSE, encode, len, socket, str]
<module>._Server                [+names answer_busy]
<module>._Server.answer_busy    [ADDED in .83, absent in .82]
<module>._Server.process_request [+names answer_busy]
```

New symbol names present in .83 / absent in .82: `answer_busy`, `BUSY_BODY`, `BUSY_RESPONSE`,
`MSG_DONTWAIT`, `sendall`. New bytes literals present in .83 / absent in .82:
`HTTP/1.1 503 Service Unavailable`, `Content-Type: application/json`, `Cache-Control: no-store`,
`Retry-After: 1`, `{"error":"server_busy"}`. Module-local controls present in **both**:
`process_request`, `shutdown_request`, `BoundedSemaphore`, `MAX_BODY`, bytes const `b'{}'`.

4 further code objects differ only *below* name/const level: `DemoApp`, `DemoApp.action`,
`DemoError`, `_Handler` (`__firstlineno__` const 776 → 799). These are explained and were
verified, not waved away:

* `co_firstlineno` shifts are pure bookkeeping from R1 inserting lines above them: **+11** for
  objects below R1's second hunk, **+23** for objects below its third (measured: `model_contract`
  27→38, `_Server` 185→196, `DemoError` 31→42 = +11; `DemoApp` 206→229, `_Handler` 776→799 = +23).
  No object in the module carries a smaller shift, because R1's first hunk (+1 line) sits above
  everything and is always accompanied by the second (+10).
* `DemoError`/`DemoApp` differ in **one byte** of `co_code` — the class-body `__firstlineno__`
  value the 3.13+ compiler embeds (206→229, 31→42, matching their own `co_firstlineno` shift
  exactly); instruction opnames identical.
* `DemoApp.action` (+2 instructions) is the only non-bookkeeping-looking one. It is the codegen
  form of `socket.socket()` at source line 527: .82 `LOAD_ATTR 'socket + NULL|self'; CALL`
  (method form) vs .83 `LOAD_ATTR 'socket'; PUSH_NULL; CALL` (plain form). `probe_import_repro.py`
  **reproduces this exactly** with a 6-line program on the same interpreter (3.14.4): adding a
  *module-level* `import socket` flips the emission for a function that does its own local
  `import socket`. R1 adds `import socket` at line 17, so this is a mechanical consequence of
  R1's import, on an unchanged source line — not additional semantics.

### Exact executables read

* NEW: `/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist/0.1.0-preview.83/Mycelium A13 Demo/Mycelium A13 Demo`
  sha256 `81cfa0c8b25a017ea52bb735dfdb0d8d61be1f990a45e9be6df18e3d302af299`
* OLD: `/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist/0.1.0-preview.82/Mycelium A13 Demo/Mycelium A13 Demo`
  sha256 `f6cfb56c8f8d7c9cdf517825c50058be2516ae3a0706dea19275668b75dd1ad9`
* PYZ located by magic at byte offset **119947** in both; TOC has **1888** modules in both.

**The `.app` bundle executable is a DIFFERENT FILE** (same 15 561 648 bytes, different sha256:
`.83` `3508354d4173a12058f370d994cbba7def8b973382c82d13f24bb7825c97f777`,
`.82` `e6b53da909ce0303a4e3198e9dd80ce7aef1e72ce5278bd2285d900fc55639c3`). The chain's own ledger
read *that* file, not the bare one. Both were therefore analysed, and every number above is
identical in both — so the verdict holds for the file the chain read too.

### Why the pair is clean

Build heads read from each bundle's own
`.../Mycelium A13 Demo.app/Contents/Resources/third_party/compiler-source-identity.json`:
`.83` ← `9d68291`, `.82` ← `1330ae9`. `git merge-base --is-ancestor c43a2ab 1330ae9` is **FALSE**,
so R1 is not in `.82`'s lineage. Between the two heads the only changed application-package `.py`
is `mycelium_a13_macos/app.py` (3 hunks, 24 insertions / 1 deletion = exactly R1); the only other
changed file is `docs/handover/mycelium-completion-checklist.v1.json`, which is not a module.
That is why "every other module identical" is a *prediction* the run could have falsified.

## Why the chain's own ledger omitted the app module

The ledger is `scripts/a13_operator/pyz_ledger.py`, invoked at chain step `[4/6]`
(`chain-preview83.sh:90`), output `runs/wp8-02-useful-two-mac/pyz-ledger-pre83.txt`.
Unmodified vs HEAD; one commit in its history (`da033a0`); its `MODULES` dict has **9** entries.

**The row set is CURATED (a hardcoded allowlist).** `pyz_ledger.py:29-39` is a literal
`MODULES = {module: source_path}` dict of nine modules. The tool iterates *that dict* — it never
enumerates the PYZ table of contents (it even prints `PYZ modules: 1888` and then ignores them)
and never derives rows from the git diff. `mycelium_a13_macos.app` is not a key, so R1's module
produced **no row at all**. The omission is silent by construction: an unlisted module cannot
produce a failing row, a warning, or an exit code.

Measured on the actual output: **9 rows** — 8 `==PILOT`, 1 `no-divergence`
(`mycelium_a13_macos.managed_observations`). `mycelium_a13_macos.app` appears **0 times**.
(The brief said "ten rows, eight proven"; the artifact holds nine rows.)

**And it is needle-driven, so R1 would have been invisible even with a row.** Two independent
layers:

1. **Wrong discriminator.** `markers_for()` (`pyz_ledger.py:94-97`) computes
   `pilot_only = str-constants(pilot source) − str-constants(demo tree)`, and `walk()` collects
   only `isinstance(const, str)`. R1's app.py hunk adds **zero new `str` constants** —
   measured: 317 before, 317 after, 0 added, 0 removed — because every new literal it introduces
   is a **`bytes`** literal. So `pilot_only` would be empty, and the verdict expression
   (`pyz_ledger.py:122-124`) falls through to `no-divergence` — the same non-answer the
   `managed_observations` row already returns.
2. **Names are collected but discarded.** R1 does add 7 `co_names` entries (`BUSY_BODY`,
   `BUSY_RESPONSE`, `MSG_DONTWAIT`, `answer_busy`, `range`, `recv`, `sendall`). `walk()` collects
   `co_names` into its `names` set, but `strings_of_source()` returns only `[0]` — the strings
   set — so no name ever reaches the marker set.

**A third, sharper point: the ledger's own differential columns recorded nothing.** Every
`vs baseline` delta in `pyz-ledger-pre83.txt` is pure dots (`..`, `...`, `....`) — not one `+`
character across all nine rows. The ledger therefore printed "no watched module's markers changed
between .82 and .83" while the one module that did change was not a row. The `.82→.83` instrument
was self-consistent and blind at the same time.

This is an **instrument-coverage** gap, not an execution mistake: the chain ran the ledger it
had. Its own header (`chain-preview83.sh:7-8`) states the instrument intended to replace the old
single-module differential ("the old differential checked one module and reported a green that
said nothing about the module the change was in, ERRATA-6"). The replacement has the same class
of defect one level up: a curated nine-module watchlist plus a str-only needle, against a change
that lands in a tenth module and adds only bytes literals. Fixing it means deriving rows from the
diff (or sweeping the TOC, as this run does) and using a discriminator that is not str-only.

## What this method does NOT prove

* **NOT that the deployed laptop copy carries R1.** Only the two dist trees were read. The
  chain's `[6/6]` compares sha256 of the local and remote executables, which proves the **copy**,
  not the **content** — a digest match says nothing about what is inside.
* **NOT that the app exercises the 503 path at runtime**, nor that the refusal behaves correctly
  under a genuinely saturated accept loop (e.g. that the write lands as a FIN rather than a RST
  on real hardware). No round was launched or touched. This is a statement about bytes at rest.
* **NOT that the R1 hunk is sufficient** for the round it was written for, nor that it fixes the
  client-side symptom end to end.
* **NOT that identical decompressed bytes for the controls imply identical runtime behaviour** —
  only that those modules' code objects are byte-for-byte the same, which is what makes them
  usable as controls.
* **NOT anything about `_internal/` payloads, resources or the runtime-source tree** — only the
  PYZ inside the executable was compared.

## Reproduce

```
/opt/homebrew/bin/python3 -B /Users/evinova-self/Documents/mycelium-core-program/runs/r1-shipped/verify_r1_shipped.py
# exit 0; full output in verify_r1_shipped.out
```

Supporting probes (all in this directory, all re-runnable):
`probe_fields.py` (which field of each changed code object), `probe_code_diff.py`,
`probe_action.py`, `probe_action_detail.py` (the `DemoApp.action` codegen site),
`probe_import_repro.py` (the 6-line reproduction of that codegen switch),
`probe_ledger_markers.py` (the str-constant / `co_names` blindness measurement).
