#!/usr/bin/env bash
# Local OpenAI-compatible LLM server for PaperQA2. Serves the pinned local snapshot
# (same Qwen3-VL commit as OCR, see config/pipeline.toml) so nothing is pulled from `main`.
# DOCINGEST_LLM_SERVE_MODEL serves another snapshot path / model id instead. It is not
# DOCINGEST_LLM, which `docingest ask` reads as a litellm model string.
set -euo pipefail
cd "$(dirname "$0")/.."
MODEL="${DOCINGEST_LLM_SERVE_MODEL:-$(uv run --all-extras docingest model-path llm)}"
export HF_HUB_OFFLINE=1
exec uv run --all-extras python -m mlx_vlm.server --model "$MODEL" --host 127.0.0.1 --port "${PORT:-8080}"
