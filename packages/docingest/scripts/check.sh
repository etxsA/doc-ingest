#!/usr/bin/env bash
# All quality gates, the same ones CI runs. Usage: ./scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen lint-imports
uv run --frozen pyright
uv run --frozen pytest --cov --cov-report=term-missing:skip-covered -q
