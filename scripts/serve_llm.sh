#!/usr/bin/env bash
# Local OpenAI-compatible LLM server for PaperQA2 (same Qwen3-VL model used for OCR).
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL="${DOCINGEST_LLM:-mlx-community/Qwen3-VL-4B-Instruct-4bit}"
exec uv run --extra qa python -m mlx_vlm.server --model "$MODEL" --host 127.0.0.1 --port "${PORT:-8080}"
