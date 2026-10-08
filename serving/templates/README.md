# Reranker chat templates

`qwen3_reranker.jinja` and `mxbai_rerank_v2.jinja` are the vLLM v0.30.0 example templates
(`examples/pooling/score/template/`, Apache-2.0) with one trailing newline added.

The template renderer drops a single trailing newline. The unmodified files therefore end the prompt one `\n` short of
the format in the model cards: Qwen3-Reranker sees `</think>\n` instead of `</think>\n\n`, and mxbai-rerank-v2 sees
`assistant` instead of `assistant\n`. The extra newline restores the exact prompts. Both rerankers score the next token
after the prompt, so the missing newline lowers ranking quality.

| File | sha256 |
|---|---|
| `qwen3_reranker.jinja` | `c186053018dc5ac320a68c67843208a8aeec56ad2ebace0e591be6ffea535097` |
| `mxbai_rerank_v2.jinja` | `6922504e8481a822f50ecdf80da9d41f09cb01601c82d1acae04bdba654deafc` |

Do not add comments or whitespace to these files: every character is part of the prompt.
