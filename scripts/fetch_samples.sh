#!/usr/bin/env bash
# Download the demo corpus and verify checksums (reproducible inputs).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/raw
fetch() { [ -f "data/raw/$1" ] || curl -fsSL -o "data/raw/$1" "$2"; }
fetch attention_1706.03762.pdf        https://arxiv.org/pdf/1706.03762v7
fetch paperqa2_2409.13740.pdf         https://arxiv.org/pdf/2409.13740v2
fetch olmocr_2502.18443.pdf           https://arxiv.org/pdf/2502.18443v3
# Genuinely scanned, image-only: Shannon (1948), BSTJ vol. 27 no. 4 (archive.org)
fetch shannon1948_bstj_scanned.pdf    https://archive.org/download/bstj27-4-623/bstj27-4-623.pdf
fetch_html() { [ -f "data/samples/$1" ] || curl -fsSL -o "data/samples/$1" "$2"; }
fetch_html paperqa2_arxiv.html https://arxiv.org/html/2409.13740v2
(cd data/raw && shasum -a 256 -c ../../scripts/samples.sha256)
