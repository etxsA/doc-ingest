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

For the whole path from a clone to an answer, including a shared GPU machine and troubleshooting, see the [run guide](../docs/run-guide.md).

## Requirements

The defaults need two GPUs with 48 GB each: one for the answering model, one for the embedder and the reranker together. The figures below are `nvidia-smi` totals per GPU, measured with the default models on such machines (NVIDIA driver 535.230.02, CUDA 12.2 as `nvidia-smi` reports it).

| Role | GPU memory in use (MiB) | Model download (about) | Also measured |
|---|---|---|---|
| `llm` | 45,147 and 44,155 (two runs) at `LLM_GPU_MEM=0.9` | 20 GB | weights 19.24 GiB; KV cache at the default context of 16,384 tokens: 17.06 GiB without eager mode, 22.2 GiB with `LLM_EAGER=1` |
| `embed` | 22,664 at `EMBED_GPU_MEM=0.45` | 8 GB | |
| `rerank` | 21,608 at `RERANK_GPU_MEM=0.45` | 16 GB | |

- **One GPU for the pair.** The embedder and the reranker together used 43,017 to 43,019 MiB (two runs). The answering model does not fit next to them on a 48 GB GPU. The GPU 1 figures (embed, rerank, pair) include about 650 MiB that another process held; it was not subtracted.
- **Smaller GPUs.** Lower the `*_GPU_MEM` fractions or pick a smaller model from `models.tsv`. No other size was measured.
- **Disk.** The three models are about 44 GB. The vLLM environment ([below](#install-vllm)) is about 11 GB and the compile caches about 235 MB. uv keeps its own cache of the downloaded wheels; after the workspace and two vLLM installs it held about 21 GB (uv hard-links from it when it is on the same disk). Where each of these goes by default and how to move it is under [Configuration](#configuration).
- **Software.** Linux with an NVIDIA driver and `nvidia-smi`, and a vLLM environment with Python 3.12.

## Install vLLM

vLLM is not part of the uv workspace. It brings its own torch and CUDA libraries, so it lives in a separate environment that `serve.sh` reaches through `VLLM_PYTHON`, `VLLM_BIN` and `SERVING_EXTRA_PATH` (or through `vllm` on `PATH`). The install below ran on the lab machines:

| Package | Version |
|---|---|
| vLLM | 0.30.0+cu129 (CUDA 12.9 build) |
| torch | 2.13.0+cu129 |
| transformers | 5.17.0 on the first install, 5.19.0 on a fresh install of the recipe (not pinned; both ran) |
| flashinfer_python | 0.6.18.post1 |

Besides the Python packages, vLLM needs a Python 3.12 **with its headers** (Triton compiles kernels at start-up) and `ninja` on `PATH`. uv's own Python builds include the headers; a Python from the operating system may need its development package (on Debian and Ubuntu, `python3.12-dev`).

Install the CUDA 12.9 build from the vLLM release page, and let uv take torch from the matching PyTorch index:

```bash
uv venv --managed-python --python 3.12 /path/to/vllm-env
uv pip install --python /path/to/vllm-env/bin/python --torch-backend cu129 \
  https://github.com/vllm-project/vllm/releases/download/v0.30.0/vllm-0.30.0+cu129-cp38-abi3-manylinux_2_28_x86_64.whl ninja
```

Why not `uv pip install vllm==0.30.0`? On PyPI that version is the CUDA 13.0 build (torch 2.13.0+cu130), which needs a newer driver than 535. On driver 535 it installs without error, `torch.cuda.is_available()` even prints `True`, and the first real CUDA call fails with `The NVIDIA driver on your system is too old (found version 12020)`, which in the server log ends as `Engine core initialization failed`. The CUDA 12.9 build exists only as the release wheel above. `--torch-backend cu129` is needed as well: adding the PyTorch index with `--extra-index-url` makes uv stop with `No solution found when resolving dependencies`. On a driver new enough for CUDA 13.0 the PyPI build may be the simpler choice; that was not tested. `ninja` from PyPI puts a `ninja` program into the environment's `bin` folder.

Check the result. The first command runs a real CUDA operation and shows which build you got:

```bash
/path/to/vllm-env/bin/python -c "import vllm, torch; print(vllm.__version__, torch.__version__, torch.version.cuda, torch.zeros(1, device='cuda').device)"
/path/to/vllm-env/bin/python -c "import sysconfig, pathlib; print((pathlib.Path(sysconfig.get_paths()['include']) / 'Python.h').exists())"
```

The first should print `0.30.0 2.13.0+cu129 12.9 cuda:0`; the second `True`. `The NVIDIA driver on your system is too old` means the build does not match the driver.

Then point `serve.sh` at the environment in `serving/env.local`:

```bash
VLLM_PYTHON=/path/to/vllm-env/bin/python
VLLM_BIN=/path/to/vllm-env/bin/vllm
SERVING_EXTRA_PATH=/path/to/vllm-env/bin     # puts ninja on PATH
```

An existing vLLM install can be reused instead. Set the same three lines to its Python 3.12, its `vllm` and the folder that holds `ninja`, or activate its environment before running `serve.sh` and leave them unset, so that `vllm` is found on `PATH`. `VLLM_SITE_PACKAGES` is for a Python that should load the packages of another environment through `PYTHONPATH`.

## First start

The first `serve.sh <role> start` downloads the pinned revision from `models.tsv`, so it needs network access and the disk space above. Where it downloads: into `HF_HOME` if that is set, otherwise into `$SERVING_CACHE_DIR/huggingface` (by default `~/.cache/research-engine/huggingface`), because `serve.sh` sets `XDG_CACHE_HOME` to its cache folder. Put the cache on a large disk and export `HF_HOME` in the shell that runs `serve.sh`; a plain assignment in `env.local` does not reach the server, so write `export HF_HOME=...` there if you keep it in that file. Once the three models are present, export `HF_HUB_OFFLINE=1` so that a start never contacts the Hub. A model that is already on disk can be used without a download: see `LLM_MODEL_PATH` under [Configuration](#configuration).

```bash
export HF_HOME=/big/disk/huggingface
serving/serve.sh embed start
serving/serve.sh rerank start
serving/serve.sh llm start
```

`start` returns when the server answers on `/health` and prints `ready after N s`. N is a multiple of 5 and can be up to 5 s above the real time. After 10 minutes `start` gives up waiting and prints the log path, but the server keeps running: follow the log and check with `status`. Times measured with the models already downloaded. The compile cache was empty in the second run and for the lab `llm` start; for the lab `embed` and `rerank` starts its state was not recorded:

| Role | Lab run | Second run, fresh install |
|---|---|---|
| `embed` | 75 s | 125 s |
| `rerank` | 80 s | 100 s |
| `llm` | about 231 s | 385 s |

In the second run, `llm` spent 70 s in `torch.compile`, 105 s in the profiling warm-up and about 75 s in two CUDA graph captures. The compile results are kept in `SERVING_CACHE_DIR`, so later starts skip the compilation: `embed` started again with its cache present in 60 s. A warm-cache start of `llm` without eager mode was not timed. With `LLM_EAGER=1` (no CUDA graphs, no `torch.compile`) the start took about 90 s, but single-stream decoding was about 3 times slower. Download time comes on top of every time in the table.

## Configuration

Copy `env.example` to `env.local` (ignored by git) and set what differs on your machine: the Python and vLLM install,
the cache and state folders, the Hugging Face cache, or a local snapshot per role (`LLM_MODEL_PATH` and so on).
Nothing in this folder names a machine, a user or an absolute path.

To use a model that is already on the machine instead of downloading it, point its role at the snapshot folder
(`EMBED_MODEL_PATH` and `RERANK_MODEL_PATH` likewise; `--revision` is then left out and the revision is the one of that folder):

```bash
LLM_MODEL_PATH=/path/to/models--cyankiwi--Qwen3.8-27B-AWQ-INT4/snapshots/<revision>
```

Where things go by default, and how to move them. All of them default to folders under your home, which can be a small disk:

| What | Default | Size | Move it with |
|---|---|---|---|
| pid files and server logs | `~/.local/state/research-engine` | small | `SERVING_STATE_DIR` |
| vLLM, Triton and `torch.compile` caches | `~/.cache/research-engine` | about 235 MB | `SERVING_CACHE_DIR` |
| downloaded models | `$SERVING_CACHE_DIR/huggingface` | about 44 GB | `HF_HOME` |
| uv's cache of downloaded wheels | uv's default | up to about 21 GB | `UV_CACHE_DIR` |

## Notes

- `llm` starts with the chat template's thinking mode off, so answers and OCR output contain no reasoning text.
  `LLM_EAGER=1` adds `--enforce-eager` (slower decoding; only as a fallback if CUDA graph capture fails).
- The two rerankers based on causal language models (Qwen3-Reranker, mxbai-rerank-v2) are scored through two token
  logits (`--hf-overrides` in `models.tsv`) and need the chat templates in `templates/`. See
  [templates/README.md](templates/README.md) for why those differ from vLLM's examples by one newline.
- Qwen3-Reranker takes a task instruction per request (`"instruction"` field of `/rerank`); without one it uses
  "Given a web search query, retrieve relevant passages that answer the query".
