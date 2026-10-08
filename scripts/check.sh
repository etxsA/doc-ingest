#!/usr/bin/env bash
# Every quality gate of every package in the workspace, the same ones CI runs.
# Usage: ./scripts/check.sh   (after `uv sync --locked --all-extras` in this folder)
set -euo pipefail
cd "$(dirname "$0")/.."
uv lock --check   # the one uv.lock matches every package's pyproject.toml
uv run --frozen python scripts/check_links.py   # relative links in every Markdown file
bash -n serving/serve.sh   # the model-server script parses
for check in packages/*/scripts/check.sh; do
  echo "== ${check%/scripts/check.sh}"
  "$check"
done
