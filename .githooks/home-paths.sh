#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-only
# Shared by .githooks/pre-commit and .githooks/commit-msg: refuse personal home-folder paths.
# Rule (same as xtask/src/no_home_paths.rs): /Users/<name> or /home/<name>/ is refused unless <name> is
# listed in xtask/data/home-path-placeholders.txt; so is this account's own user name (6+ chars).
home_paths_check() { # home_paths_check <label> < text
  top=$(git rev-parse --show-toplevel) || return 2
  list="$top/xtask/data/home-path-placeholders.txt"
  allow=$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$list" 2>/dev/null | grep -v '^$' || true)
  [ -n "$allow" ] || allow=$(printf 'Shared\nmycelium-dev\njoinmac\n')
  text=$(cat)
  bad=""
  for name in $(printf '%s\n' "$text" | grep -oE '/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+/' |
                sed -E 's#^/(Users|home)/##; s#/$##' | LC_ALL=C sort -u); do
    printf '%s\n' "$allow" | grep -Fxq -- "$name" || bad="$bad $name"
  done
  me=$(id -un 2>/dev/null || true)
  if [ ${#me} -ge 6 ] && ! printf '%s\n' "$allow" | grep -Fxq -- "$me" &&
     printf '%s\n' "$text" | grep -Fq -- "$me"; then bad="$bad $me(user-name)"; fi
  if [ -n "$bad" ]; then
    echo "$1: refused, personal home path or user name:$bad" >&2
    echo "Use ~/ or \$HOME, or a placeholder from xtask/data/home-path-placeholders.txt." >&2
    return 1
  fi
  return 0
}
