# fixtures/ -- inputs for scripts/a13_operator/test_pyz_ledger_coverage.py

`pyz_ledger_pre_fix_9d68291.py` is a copy of the defective ledger as it stood at HEAD 9d68291
(`git show 9d68291:scripts/a13_operator/pyz_ledger.py`, sha256
`218c7596033ced7f3bd602ec617b32f5522492f57ee1d28fd830bb4692008637`). It is deliberately not edited
beyond what publication needed: an SPDX header line and two absolute home-directory constants made
relative to `~`. The published file has sha256:

    9647a2802a01e1d52a9d71f02ac008d38e07265e711ff889ac85652bf9185125

It exists so the RED half of that test can run the REAL pre-fix tool -- its own marker needle, its
own verdict logic, its own control check -- and show it reporting no change for a module that
changed.  It is not live tooling: the live ledger is scripts/a13_operator/pyz_ledger.py.
