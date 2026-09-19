#!/bin/bash
cd /Users/evinova-self/Documents/playground/mycelium-core-pilot/ui/web || exit 1
./node_modules/.bin/vitest run --pool=forks --maxWorkers=1 2>&1 | tail -18
