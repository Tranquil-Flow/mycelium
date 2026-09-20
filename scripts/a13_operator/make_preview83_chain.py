#!/usr/bin/env python3
"""Generate the preview.83 chain, round launcher and local preflight from the PROVEN preview.82 set.

LAUNCH LAW (this programme): a new version is produced by EDITING the proven predecessor, one
version per step, literals only, and every substituted literal is asserted present EXACTLY the
expected number of times -- because a missed live literal writes one round's evidence into
another round's file (the A13_SSH_LOG trap: criterion-3's audit delta would land in a previous
round's criterion3-ssh-calls-*.log).

MIRRORS, both taken from this repo's own history at c573e0f:
  * chain-preview81.sh -> chain-preview82.sh   (build chain, new preview, 8 live literals + provenance)
  * chain-preview81e.sh -> chain-preview82e.sh (round launcher, new preview, first attempt: the
    label/ssh/target/rpc/supervisor-version literals move; historical p81r1 references stay)

WHAT THIS GENERATOR DOES NOT DO: it does not run the chain, does not build or deploy a preview,
and does not launch a round. It rewrites text only. A textually complete chain proves nothing
about the build, the gate verdict, the deployment, or any round.

Round label: p83r1 -- verified FREE on disk before generating (no artifact matches `p83` or
`83r1` anywhere under mycelium-core-program/runs/). The un-prefixed family (63r2..82r1) is the
historical sequence; the p-prefix names its preview so the families cannot collide.

Deliberately NOT changed (and why):
  * historical mentions of p81r1 / p81r2 / roundp81r1-admission-status and the "63r2..82r1"
    label-list comment -- statements about past rounds stay true only if left alone;
  * chain-preview81e.sh references in the build chain header -- the smoke-refusal instrument
    ORIGINATED there; rewriting it would make the note false;
  * E-line descriptive prose ("one round on the two fixes verified on 2026-09-20") -- only the
    preview number moves; the descriptive text is inherited prose, flagged for the operator;
  * every timeout, window, tolerance and identity check -- untouched.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent

# (template, output, [(old, new, expected_template_count), ...])
FILES = [
    (
        "chain-preview82.sh",
        "chain-preview83.sh",
        [
            ("build preview.82,", "build preview.83,", 1),
            ("chain-preview81.sh (launch law)", "chain-preview82.sh (launch law)", 1),
            ("NEW=0.1.0-preview.82", "NEW=0.1.0-preview.83", 1),
            ("OLD=0.1.0-preview.81", "OLD=0.1.0-preview.82", 1),
            ("pyz-ledger-pre82.txt", "pyz-ledger-pre83.txt", 1),
            ("deploy_preview_to_laptop.py --version 82", "deploy_preview_to_laptop.py --version 83", 1),
            ("verify_preview_deployed.py --version 82", "verify_preview_deployed.py --version 83", 1),
            ("mycelium-wp8-02/preview82/", "mycelium-wp8-02/preview83/", 1),
        ],
        # post-conditions: (literal, expected_count_in_generated)
        [
            ("preview.82", 1),          # the OLD baseline variable only
            ("preview.83", 2),          # header self-ID + NEW
            ("preview.81", 0),
            ("--version 82", 0),
            ("--version 83", 2),
            ("chain-preview81e.sh", 1),  # history: instrument origin
            ("mycelium-wp8-02/preview82/", 0),  # remote path moved (bare "preview82" survives inside chain-preview82.sh provenance)
        ],
        ["chain-preview82.sh (launch law)", "chain-preview81e.sh", "OLD=0.1.0-preview.82"],
    ),
    (
        "chain-preview82e.sh",
        "chain-preview83e.sh",
        [
            ("# E: preview.82,", "# E: preview.83,", 1),
            (
                "chain-preview81e.sh (launch law), .81 -> .82, literals only.",
                "chain-preview82e.sh (launch law), .82 -> .83, literals only.",
                2,
            ),
            ("A13_LOG_LABEL=p82r1", "A13_LOG_LABEL=p83r1", 1),
            (
                "A13_TARGET_PREVIEW=preview82 A13_RPC_ROUND=rpc-round82",
                "A13_TARGET_PREVIEW=preview83 A13_RPC_ROUND=rpc-round83",
                1,
            ),
            ("A13_SSH_LOG=$EV/criterion3-ssh-calls-p82r1.log",
             "A13_SSH_LOG=$EV/criterion3-ssh-calls-p83r1.log", 1),
            ("ROUND p82r1 with --no-quit", "ROUND p83r1 with --no-quit", 1),
            ("round15_supervisor.py 82 --max-seconds", "round15_supervisor.py 83 --max-seconds", 1),
            ("setdefault('A13_TARGET_PREVIEW', 'preview82')",
             "setdefault('A13_TARGET_PREVIEW', 'preview83')", 1),
            ("setdefault('A13_RPC_ROUND', 'rpc-round82')",
             "setdefault('A13_RPC_ROUND', 'rpc-round83')", 1),
            # Forward-looking requirement: "So if THIS round refuses, it must leave
            # round<p82r1>-admission-status-smoke-failed.json". THIS round is now p83r1, and the
            # capture writes round$A13_LOG_LABEL-... -- so the literal must move with the label.
            ("roundp82r1-admission-status-smoke-failed.json",
             "roundp83r1-admission-status-smoke-failed.json", 1),
        ],
        [
            ("p83r1", 4),               # label def + forward-ref file + ssh log + [2/5] banner
            ("p82r1", 0),
            ("=preview82", 0),          # bare "preview82" legitimately survives inside chain-preview82e.sh provenance
            ("'preview82'", 0),
            ("preview83", 2),
            ("preview.82", 0),
            ("rpc-round82", 0),
            ("rpc-round83", 2),
            ("round15_supervisor.py 83", 1),
            ("63r2..82r1", 1),          # historical label list stays
            ("chain-preview81e.sh", 0), # both provenance lines moved
        ],
        ["chain-preview82e.sh (launch law), .82 -> .83, literals only.", "63r2..82r1", "p81r1"],
    ),
    (
        "local_preflight_p82.sh",
        "local_preflight_p83.sh",
        [
            ("preflight for preview.82:", "preflight for preview.83:", 1),
            ("of chain-preview82.sh, VERBATIM", "of chain-preview83.sh, VERBATIM", 1),
            ("NEW=0.1.0-preview.82", "NEW=0.1.0-preview.83", 1),
            ("OLD=0.1.0-preview.81", "OLD=0.1.0-preview.82", 1),
            ("gate-pre82-at-", "gate-pre83-at-", 1),
            ("build-pre82-at-", "build-pre83-at-", 2),
            ("pyz-ledger-pre82-at-", "pyz-ledger-pre83-at-", 1),
        ],
        [
            ("preview.82", 1),
            ("preview.83", 2),          # header self-ID + NEW variable
            ("preview.81", 0),
            ("pre83", 4),
            ("pre82", 0),
            ("chain-preview82.sh", 0),
            ("chain-preview83.sh", 1),
        ],
        ["OLD=0.1.0-preview.82"],
    ),
]

FAILURES: list[str] = []


def check(ok: bool, message: str) -> None:
    if not ok:
        FAILURES.append(message)
        print("  FAIL: %s" % message)


def substitute(template_text: str, subs: list[tuple[str, str, int]], label: str) -> str:
    text = template_text
    for old, new, expected in subs:
        found = text.count(old)
        check(found == expected,
              "%s: literal %r occurs %d times in template, expected %d" % (label, old, found, expected))
        if found != expected:
            continue
        text = text.replace(old, new)
    return text


def main() -> int:
    print("=== preview.83 generation from the proven preview.82 set (literals only) ===")
    print("source tree : %s" % HERE)
    print()

    for template_name, output_name, subs, post, allowed_residuals in FILES:
        template = HERE / template_name
        output = HERE / output_name
        template_text = template.read_text("utf-8")
        template_sha = hashlib.sha256(template_text.encode("utf-8")).hexdigest()

        print("--- %s -> %s  (template sha256 %s, %d bytes)" %
              (template_name, output_name, template_sha[:16], len(template_text)))
        generated = substitute(template_text, subs, output_name)

        # Per-literal table
        for i, (old, new, expected) in enumerate(subs, 1):
            tmpl_count = template_text.count(old)
            gen_new = generated.count(new)
            gen_old = generated.count(old)
            ok = tmpl_count == expected and gen_new == expected
            print("  %2d. %-52s tmpl=%d gen_new=%d gen_old=%d  %s" %
                  (i, old[:52], tmpl_count, gen_new, gen_old, "OK" if ok else "FAIL"))
            check(ok, "%s sub %d: counts %d/%d/%d, expected %d (old=%r)" %
                  (output_name, i, tmpl_count, gen_new, gen_old, expected, old))

        # Post-conditions
        for literal, expected in post:
            got = generated.count(literal)
            ok = got == expected
            print("     post: %-40s count=%d expected=%d  %s" %
                  (literal[:40], got, expected, "OK" if ok else "FAIL"))
            check(ok, "%s post-condition %r: %d != %d" % (output_name, literal, got, expected))

        # Residual audit: every line still matching 8[12] must be an allowed residual
        print("     residual audit (lines matching 8[12]):")
        for lineno, line in enumerate(generated.splitlines(), 1):
            if "81" in line or "82" in line:
                allowed = any(a in line for a in allowed_residuals)
                trimmed = line.strip()[:96]
                print("       %s L%-4d %s" % ("ok  " if allowed else "FAIL", lineno, trimmed))
                check(allowed, "%s L%d: unexpected residual %r" % (output_name, lineno, trimmed))

        # ROUND-TRIP PROOF: reverse the substitutions and require byte-identical reconstruction.
        rebuilt = generated
        for old, new, expected in reversed(subs):
            check(rebuilt.count(new) == expected,
                  "%s round-trip: %r count %d != %d" % (output_name, new, rebuilt.count(new), expected))
            rebuilt = rebuilt.replace(new, old)
        rebuilt_sha = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
        ok = rebuilt == template_text
        print("     round-trip: reverse-substituted sha256 %s vs template %s  %s" %
              (rebuilt_sha[:16], template_sha[:16], "OK (byte-identical)" if ok else "FAIL"))
        check(ok, "%s round-trip reconstruction != template" % output_name)

        output.write_text(generated, "utf-8")
        output.chmod(0o644)  # mirrors the .82 trio's tracked mode
        gen_sha = hashlib.sha256(generated.encode("utf-8")).hexdigest()
        print("     wrote %s (%d bytes, sha256 %s)" % (output_name, len(generated), gen_sha[:16]))
        print()

    if FAILURES:
        print("=== %d ASSERTION FAILURE(S) -- nothing is deliverable ===" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    print("=== ALL ASSERTIONS PASSED -- 3 files generated by substitution, each round-trip proven ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
