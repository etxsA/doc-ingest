#!/usr/bin/env bash
# Every quality gate of every package in the workspace, the same ones CI runs.
# Usage: ./scripts/check.sh   (after `uv sync --locked --all-extras` in this folder)
set -euo pipefail
cd "$(dirname "$0")/.."
uv lock --check   # the one uv.lock matches every package's pyproject.toml
for check in packages/*/scripts/check.sh; do
  echo "== ${check%/scripts/check.sh}"
  "$check"
done
