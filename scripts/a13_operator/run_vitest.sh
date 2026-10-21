#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
cd $HOME/Documents/playground/mycelium-core-pilot/ui/web || exit 1
./node_modules/.bin/vitest run --pool=forks --maxWorkers=1 2>&1 | tail -18
