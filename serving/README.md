# Model servers

The research engine runs three local [vLLM](https://docs.vllm.ai/) servers with OpenAI-compatible APIs. `serve.sh`
starts, stops and inspects them; `models.tsv` pins every model to a repository and revision.

| Role | Served name | Default model | Default GPU and port | Used for |
|---|---|---|---|---|
| `llm` | `qwen-local` | Qwen3.8-27B (AWQ 4-bit) | GPU 2, port 8001 | answers, evidence summaries, OCR |
| `embed` | `qwen-embed` | Qwen3-Embedding-4B | GPU 1, port 8002 | vectors for chunks and questions (`/v1/embeddings`) |
| `rerank` | `qwen-rerank` | Qwen3-Reranker-8B | GPU 1, port 8003 | re-scoring retrieved chunks (`/rerank`) |

```bash
serving/serve.sh llm start            # or: start <GPU> <PORT> <model name from models.tsv>
serving/serve.sh embed start
serving/serve.sh rerank start
serving/serve.sh rerank status
serving/serve.sh rerank print-cmd     # the exact vLLM command, without starting it
serving/serve.sh rerank stop          # stops only this server (it runs in its own process group)
```

Clients always use the served names, so switching a model (for example `embed start 1 8002 qwen3-embedding-8b`)
needs no client change. The embedder and the reranker fit together on one 48 GB GPU at 0.45 of its memory each.

## Configuration

Copy `env.example` to `env.local` (ignored by git) and set what differs on your machine: the Python and vLLM install,
the cache and state folders, the Hugging Face cache, or a local snapshot per role (`LLM_MODEL_PATH` and so on).
Nothing in this folder names a machine, a user or an absolute path.

## Notes

- `llm` starts with the chat template's thinking mode off, so answers and OCR output contain no reasoning text.
  `LLM_EAGER=1` adds `--enforce-eager` (slower decoding; only as a fallback if CUDA graph capture fails).
- The two rerankers based on causal language models (Qwen3-Reranker, mxbai-rerank-v2) are scored through two token
  logits (`--hf-overrides` in `models.tsv`) and need the chat templates in `templates/`. See
  [templates/README.md](templates/README.md) for why those differ from vLLM's examples by one newline.
- Qwen3-Reranker takes a task instruction per request (`"instruction"` field of `/rerank`); without one it uses
  "Given a web search query, retrieve relevant passages that answer the query".
