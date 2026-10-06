#!/usr/bin/env bash
# Isolated environment for the official olmOCR-bench scorer (`docingest bench score`).
#
# The scorer lives in the `olmocr` package, whose dependencies (torch, vllm, boto3, ...)
# we neither need nor want in the project venv. So: a separate venv, olmocr installed
# with --no-deps, plus the exact closure of what olmocr.bench imports (pinned), plus a
# headless Chromium for the math tests (KaTeX rendering through Playwright).
#
#   ./scripts/setup_bench_scorer.sh          # idempotent; ~200 MB of Chromium on first run
#
# Everything stays inside the repo (gitignored): .bench-venv/{bin,pw,home}. The scorer
# caches rendered equations under $HOME/.cache/olmocr; the bench CLI runs it with
# HOME=.bench-venv/home so that cache stays here too.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${DOCINGEST_BENCH_VENV:-$PWD/.bench-venv}"
OLMOCR_VERSION="0.4.27"
PY="$VENV/bin/python"

uv venv --python 3.12 --allow-existing "$VENV"
uv pip install --python "$PY" --no-deps "olmocr==$OLMOCR_VERSION"
uv pip install --python "$PY" --no-deps -r scripts/bench_scorer_requirements.txt

# The scorer launches chromium headless, which Playwright serves from the headless shell.
PLAYWRIGHT_BROWSERS_PATH="$VENV/pw" "$PY" -m playwright install --only-shell chromium
mkdir -p "$VENV/home"

# Smoke check: the scorer imports, and a KaTeX equation renders in the installed browser.
HOME="$VENV/home" PLAYWRIGHT_BROWSERS_PATH="$VENV/pw" "$PY" - <<'EOF'
import olmocr.bench.benchmark  # noqa: F401
from olmocr.bench.katex.render import render_equation

eq = render_equation("x^2 + y^2 = z^2", use_cache=False)
assert eq is not None and not eq.error, eq
print("olmOCR-bench scorer ready")
EOF
echo "scorer python: $PY"
