# Independent counter for FX-L12-gate-* (not the xtask parser): sums libtest "test result" lines.
/^test result: / { for (i = 1; i <= NF; i++) { if ($(i+1) ~ /^passed;?$/) p += $i; if ($(i+1) ~ /^failed;?$/) f += $i; if ($(i+1) ~ /^ignored;?$/) g += $i } r++ }
/^test .* \.\.\. FAILED$/ { names++ }
END { printf "passed=%d failed=%d ignored=%d result_lines=%d failed_test_lines=%d\n", p, f, g, r, names }
