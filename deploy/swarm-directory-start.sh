#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# TEMPLATE. Not installed, not run by anything in this repo.
#
# Start script of the always-on swarm directory unit (deploy/swarm-directory.plist).
# Runs the directory on 127.0.0.1 only, with the hosted-demo defaults named in
# mycelium_swarm_directory/hosted_defaults.py (port, public origin, trusted proxy).
#
#   MYCELIUM_SWARM_DIR_STATE_DIR   required; owner-private state dir (sqlite is created 0600 inside)
#   MYCELIUM_PYTHON                python with this repo on its path (default: python3)
#   MYCELIUM_SWARM_DIR_DRY_RUN=1   print the command and exit; creates nothing
set -euo pipefail
: "${MYCELIUM_SWARM_DIR_STATE_DIR:?set MYCELIUM_SWARM_DIR_STATE_DIR to the owner-private state directory}"
python_bin="${MYCELIUM_PYTHON:-python3}"
cmd=(env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1
     "$python_bin" -B -m mycelium_swarm_directory --hosted --db "$MYCELIUM_SWARM_DIR_STATE_DIR/directory.sqlite3")
if [ "${MYCELIUM_SWARM_DIR_DRY_RUN:-}" = "1" ]; then
  printf '%s ' "${cmd[@]}"
  printf '\n'
  exit 0
fi
mkdir -p "$MYCELIUM_SWARM_DIR_STATE_DIR"
chmod 700 "$MYCELIUM_SWARM_DIR_STATE_DIR"
exec "${cmd[@]}"
