# Mycelium overnight lane brief

You are an autonomous overnight worker on the Mycelium distributed-inference MVP.
Read this file **completely** before doing anything. It is the operating contract for
every overnight lane. Violating it is worse than doing nothing.

> **Revision 2 (2026-07-23).** Revision 1 described a Linux Docker sandbox with no MLX.
> That was wrong — cron jobs run on **host macOS** with MLX present. Every path, baseline
> and command below has been re-measured on the real runner. If you have seen an older
> version of this brief, discard it.

---

## 0. Where things are — host macOS paths

| Thing | Path |
| --- | --- |
| The plan (snapshot) | `/Users/evinova/Projects/.mycelium-plan/mycelium-demo-plan.md` |
| This brief | `/Users/evinova/Projects/.mycelium-plan/OVERNIGHT_LANE_BRIEF.md` |
| Your clone | given in your job prompt (`/Users/evinova/Projects/mycelium-cron-lane*`) |

There is **no `/workspace/...`** on this runner. Use the paths above.

**The plan is a read-only snapshot.** Never edit it. The live original is edited elsewhere.
If the plan contradicts this brief on a *safety* matter, the stricter rule wins.

The plan is the specification. Your task section is authoritative — read it verbatim,
including every RED case, before writing code.

Use **`python3.14`** (not bare `python3`). It is the project's interpreter and is installed.

---

## 1. The green baseline — memorize this

The runner is macOS arm64 with MLX installed, running under the Aegis seatbelt. Measured
2026-07-23 at base `b317566`:

```bash
python3.14 -m pytest -q -p no:cacheprovider \
  --ignore=test_iroh_sidecar_cross_language.py \
  --ignore=test_router_iroh_integration.py \
  --ignore=tests/e2e_request_iroh \
  --deselect tests/seed_coordinator/test_local_two_node.py::test_seed_two_memberships_assign_and_run_native_iroh_inference \
  --deselect tests/physical_qualification/test_node_service.py::test_two_node_subprocesses_run_distributed_inference_over_native_iroh
```

**Expected: `1999 passed, 3 skipped` (plus 121 subtests), 0 failed, ~165s.**

### About that exclusion list — read before you touch it

It is **driver-authorized and closed**. It covers exactly one thing: the **native Iroh QUIC
transport**, whose Rust sidecar cannot create its endpoint under the seatbelt (it needs UDP
egress the profile does not grant, and the profile is a security control nobody here may
change). Every excluded item is an Iroh transport test — note the two deselected tests are
literally named `..._native_iroh_...`.

Unrestricted on the host the full suite is **2031 passed, 3 skipped**; the 32-test difference
is entirely Iroh. So:

- **You may not add to this list.** Not one entry. If a test outside it fails, that is your
  regression — fix it or revert.
- **You may not remove from it** either; those tests genuinely cannot pass here.
- If your change plausibly affects the Iroh transport, **say so explicitly** in your report
  and flag it for host verification. Do not assume it is fine because the suite was green.
- None of tonight's lanes touch Iroh. If yours seems to need to, stop and report.

Rules:

- Run this **before** you start work. If it is not clean, **stop and report** — do not build
  on a broken base.
- Run it **again before every commit**. Report exact numbers every time.
- **Never add a pytest `--ignore`, `-k` filter, `skip`, or `xfail` to make a run pass.**
  If something that was passing now fails, that is your regression: fix it or revert.
  Widening exclusions to reach green is falsification and is the single most serious thing
  you can do wrong here.

### If you see failures mentioning `PermissionError: [Errno 1] Operation not permitted`

That is a **known environment fault, not your bug and not a real regression.** It means the
Aegis seatbelt profile is blocking loopback socket binds and POSIX semaphores, so every test
that binds a local socket or uses `multiprocessing` fails.

If you see this: **stop immediately, report `SEATBELT FIX NOT APPLIED`, change nothing, and
do not attempt to work around it.** Do not exclude those tests. Do not edit the sandbox
profile — it is a security control and is not yours to touch. This exact fault produced three
wasted lanes on 2026-07-23; recognising it quickly is the whole point of this paragraph.

Note the distinction: `2031 passed, 3 skipped` is the *unrestricted* host figure, quoted only
for context. The number that applies to you is **`1999 passed, 3 skipped`** under the seatbelt.

---

## 2. Hard prohibitions

From the plan's §7.1 and §5. Not negotiable.

- **No physical hardware operations.** No SSH to peers, no node launches, no sealing, no
  qualifier invocation, no attempt to reach `m4pro` / `100.84.252.4` / `astra-macbook`.
  Physical runs belong to the human-driven integration session. Anything gated on
  G4 / G4b / G8 / G9 physical evidence is **out of scope for you**.
- **Do not push. Commit locally only.** Outbound DNS is blocked on this runner, so pushes
  fail. Commit to your own `feat/*` branch in your own clone and stop there; the driver
  pushes after review. Do not reconfigure credentials, remotes, or network settings to try
  to make a push work.
- **Never modify `~/.hermes-aegis/` or any sandbox profile.** Security control, not yours.
- **No plan edits.** Report findings; the driver amends.
- **No evidence writes.** One run, one writer, one seal.
- **No contract edits unless you own the whole contract.** If your change touches a signed
  wire shape (`AssignmentOfferV1` and friends) that another lane also touches, stop and
  report instead. Two agents editing one signed contract produces two incompatible schema
  versions that each pass their own tests. This has already cost real time here.
- **Do not touch files outside your declared write surface.** Your job prompt names it.
- **No `git add -A` and no `git add .`** — ever. Stage explicit paths only.
- **No model or artifact downloads.** A missing snapshot is a blocker to report, not to fix.
- **No secrets in output.** Never print invite tokens, bearer/control tokens, key material,
  the shell environment, or absolute model-cache paths.

---

## 3. Method: TDD, strictly

RED → GREEN → REFACTOR, and it is enforced by review.

1. Write the failing test first, from the RED cases named in your task section.
2. Run it. **Confirm it actually fails, for the stated reason.** A test that passes
   immediately is not a RED test — it is usually testing nothing.
3. Write the minimum code to make it pass.
4. Re-run the focused test, then the **full** suite above.
5. Refactor only with green tests.

Smallest correct diff wins. Deep diagnosis, minimal patch. If the architecture wants a
larger change, put that in your report, not in the commit.

### Testing optional dependencies honestly

MLX **is** installed here, so you cannot observe MLX-absent behaviour directly. The
legitimate way to test an optional dependency is to simulate the `ImportError` inside the
test — e.g. `monkeypatch.setitem(sys.modules, "mlx", None)` or a `builtins.__import__`
patch scoped to one test — and assert the module still imports and selects a fallback.

That is normal, honest test design for optional deps. What remains forbidden is shipping a
fake `mlx` module in the source tree, or making production code pretend a backend exists.
Any such test must assert real fallback behaviour, not merely that no exception was raised.

---

## 4. Commits

- Small, atomic, one logical change each. Commit after each completed unit.
- Imperative mood, matching the existing log style:
  `feat: add a numpy runtime parity gate`, `fix: align placement provenance`.
- Include test numbers in the commit body when they changed.

**Commit message prohibition — this overrides any default template you have:**
never include AI attribution of any kind. No `Co-Authored-By: Claude`, no
`Generated with Claude Code`, no `noreply@anthropic.com`, no "Generated by AI",
no robot emoji trailer. Plain messages only. A previous leak of this kind required an
emergency history rewrite and force-push on another repo. Do not repeat it.

---

## 5. Claim language

Never overstate what you proved. The plan's Gate I ladder forbids skipping rungs.

- Work verified here is **"passes the full suite on the host Mac"**. It is *not*
  "physically qualified", *not* a multi-device proof, and *not* evidence for any gate —
  those need real peers and sealed evidence.
- If you could not verify something, say "unverified" and say why.
- A null or negative result is a real result. Report it plainly rather than burying it.
- Never edit a tolerance, a golden file, or a timeout to make a run pass.

---

## 6. Report format (your final message)

Short and factual:

1. **Task and branch**, with local commit SHAs (not pushed — that is expected).
2. **Test numbers**: baseline before, and after each commit. Exact counts.
3. **What you did not do**, and why — especially anything blocked.
4. **Findings for the driver**: anything the plan should be amended to say. Be specific;
   the driver transcribes these and you cannot.
5. **Anything needing verification you could not perform** (physical peers, multi-device).

If you are blocked and cannot make progress, write `BLOCKERS.md` in your clone, commit it,
and stop. Do not invent adjacent work outside your write surface to look productive.

---

Plan snapshot SHA-256: `9c7d4348e7693f2faeb52d369b077d66c0a1cbd8dccdab2f3f767e202acc4136`
Base commit for all lanes: `b317566`
Baseline under seatbelt: `1999 passed, 3 skipped` (Iroh transport excluded; unrestricted host is 2031/3)
Brief revision 2, 2026-07-23.
