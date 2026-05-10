#!/usr/bin/env bash
set -euo pipefail

commit="unknown"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  commit="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
fi

echo "STABLE_GIT_COMMIT ${commit}"
