# fixtures/ -- inputs for scripts/a13_operator/test_pyz_ledger_coverage.py

`pyz_ledger_pre_fix_9d68291.py` is a BYTE-IDENTICAL copy of the defective ledger as it stood at
HEAD 9d68291 (`git show 9d68291:scripts/a13_operator/pyz_ledger.py`), deliberately not edited even
to the extent of a provenance header, so its sha256 can be compared against the git blob:

    218c7596033ced7f3bd602ec617b32f5522492f57ee1d28fd830bb4692008637

It exists so the RED half of that test can run the REAL pre-fix tool -- its own marker needle, its
own verdict logic, its own control check -- and show it reporting no change for a module that
changed.  It is not live tooling: the live ledger is scripts/a13_operator/pyz_ledger.py.
