#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Build the A9 int8 kernel as a shared library plus self test (conformance hosts only).
set -eu
cd "$(dirname "$0")"
mkdir -p out
EXTRA=${MYCELIUM_INT8_CFLAGS:-}
cc -O2 -shared -fPIC $EXTRA -o out/mycelium_int8.so mycelium_int8.c
cc -O2 $EXTRA -o out/selftest selftest.c mycelium_int8.c -lm
./out/selftest
shasum -a 256 out/mycelium_int8.so 2>/dev/null || sha256sum out/mycelium_int8.so
