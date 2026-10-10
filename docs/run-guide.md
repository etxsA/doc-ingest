# Run guide

This guide takes you from a clone of the repository to a cited answer from your own corpus, on a Linux machine with NVIDIA GPUs. It runs every step of the research engine: install, the three model servers, papers in, the chunk index, and `docingest ask`. Each step says what it prints and how long it took in the measurements.

The guide links to the reference for each part instead of repeating it:

| For | Read |
|---|---|
| GPU memory, disk, the vLLM install, start times, default folders | [serving/README.md](../serving/README.md) |
| Every configuration key | [packages/docingest/config/README.md](../packages/docingest/config/README.md) |
| Every command and option | [packages/docingest/README.md](../packages/docingest/README.md#cli-overview) |
| How the index works and what it stores | [packages/docingest-index/README.md](../packages/docingest-index/README.md) and [ADR 0004](../packages/docingest/docs/adr/0004-chunk-index-and-two-stage-retrieval.md) |

## Contents

- [1. Check the machine](#1-check-the-machine)
- [2. Install the workspace](#2-install-the-workspace)
- [3. Install vLLM and start the model servers](#3-install-vllm-and-start-the-model-servers)
- [4. Write your configuration](#4-write-your-configuration)
- [5. Get papers in](#5-get-papers-in)
- [6. Build the index](#6-build-the-index)
- [7. Ask](#7-ask)
- [8. Add papers later, and stop the servers](#8-add-papers-later-and-stop-the-servers)
- [Measured times](#measured-times)
- [Sharing a machine](#sharing-a-machine)
- [Troubleshooting](#troubleshooting)

## 1. Check the machine

The default setup needs two GPUs with 48 GB each: one for the answering model (about 45,000 MiB in use) and one for the embedder and the reranker together (about 43,000 MiB in use). It also needs about 44 GB of disk for the model downloads, and more for the vLLM environment and caches. The details and the measurements are in [Requirements](../serving/README.md#requirements).

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
```

Look at which GPUs are free before you start anything. If other people use the machine, read [Sharing a machine](#sharing-a-machine) first.

What needs a GPU:

| Work | GPU |
|---|---|
| Ingesting born-digital PDFs, LaTeX sources, Markdown and text | No |
| Crawling arXiv | No |
| Ingesting scanned pages and images (OCR) | Yes, in this configuration through the answering model's server |
| Building the index | Yes, the embedder's server |
| `docingest ask` with the index | Yes, all three servers |

A machine without GPUs can still ingest and crawl. `ask` needs a model that speaks the OpenAI API, running somewhere you can reach. [`config/examples/ollama-qa.toml`](../packages/docingest/config/examples/ollama-qa.toml) shows PaperQA2 answering with Ollama models; that setup has no chunk index. The other options are in the [docingest README](../packages/docingest/README.md#paperqa2-integration).

## 2. Install the workspace

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and git. uv installs Python 3.12 if the machine has none.

```bash
git clone https://github.com/etxsA/research-engine.git
cd research-engine
uv sync --locked --all-extras
```

This installs docingest, docingest-index and the PaperQA2 extra into one environment (`.venv` at the root). It does not install vLLM. The workspace environment is about 6 GB.

Set the Hugging Face cache once, in the shell you will use for the rest of the guide, and put it on a large disk. The model servers download into it, and docingest's `ask` loads a small embedding model from it.

```bash
export HF_HOME=/big/disk/huggingface
```

## 3. Install vLLM and start the model servers

vLLM is a separate environment, not part of the workspace. Follow [Install vLLM](../serving/README.md#install-vllm): a Python 3.12 environment with headers, `ninja` and the CUDA 12.9 build of vLLM 0.30.0, and three lines in `serving/env.local` that point `serve.sh` at it. If vLLM is already installed on the machine, you can reuse it instead; the same section says how.

Two more settings go in `serving/env.local` when they apply:

- A model that is already on disk, for example in a shared models folder, can be used without downloading it: point its role at the snapshot with `LLM_MODEL_PATH`, `EMBED_MODEL_PATH` or `RERANK_MODEL_PATH`. See [Configuration](../serving/README.md#configuration).
- `serve.sh` writes its pid files, logs and compile caches under your home by default. If your home is on a small disk, move them with `SERVING_STATE_DIR` and `SERVING_CACHE_DIR`. The same section lists the defaults and sizes.

Then start the servers, one after another, from the repository root (each command returns when its server is ready):

```bash
serving/serve.sh embed start
serving/serve.sh rerank start
serving/serve.sh llm start
```

Each command prints the model, the GPU, the port and the log file, then the time until the server answered:

```text
starting qwen-embed (qwen3-embedding-4b) on GPU 1 at http://127.0.0.1:8002 (pid N), log <state dir>/qwen-embed.log
ready after 75 s
```

The first start of each role also downloads its model, which makes it longer on a slow connection. The measured start times are in [First start](../serving/README.md#first-start): with the models already downloaded (and, for most of them, an empty compile cache), 75 to 125 s for `embed`, 80 to 100 s for `rerank` and 231 to 385 s for `llm`, so 6.5 to 10 minutes for the three. Check them:

```bash
serving/serve.sh embed status        # running (pid N, port 8002, model qwen3-embedding-4b)
curl -sf http://127.0.0.1:8001/health && echo llm ok
curl -sf http://127.0.0.1:8002/health && echo embed ok
curl -sf http://127.0.0.1:8003/health && echo rerank ok
```

The default GPUs are 2 for `llm` and 1 for `embed` and `rerank`. On a machine with other GPU numbers, pass them: `serving/serve.sh llm start 0 8001`. If you change a port, change it in the configuration too (step 4).

## 4. Write your configuration

[`lab-server.toml`](../packages/docingest/config/examples/lab-server.toml) already points at ports 8001 to 8003 and turns the index on. Copy it outside the repository and set two paths:

```bash
cp packages/docingest/config/examples/lab-server.toml "$HOME/lab.toml"
```

```toml
output_dir = "/path/to/work/normalized"   # the corpus: one folder per document

[index]
dir = "/path/to/work/index"               # the chunk indexes
```

Use absolute paths so the file works from any folder. The index folder is named after the corpus path, the embedder and the chunk settings, so set both paths once and keep them: moving the corpus starts a new, empty index. `raw_dir` (where `crawl` downloads) can stay `data/raw`, relative to the folder you run docingest from, which is git-ignored.

The rest of the guide runs from `packages/docingest` and passes the file with `-c`:

```bash
cd packages/docingest
CONFIG="$HOME/lab.toml"
```

Pass `-c "$CONFIG"` to every command. Without it, docingest uses `config/pipeline.toml`, which is the Mac setup: another corpus folder (`data/normalized` under the current folder), no index, an answering model expected on port 8080 and MLX OCR. Nothing in this guide works with it. What you see when you forget it is listed under [Troubleshooting](#troubleshooting).

## 5. Get papers in

Either crawl arXiv or ingest your own files. Both write `document.md` and `manifest.json` per paper into `output_dir`.

```bash
uv run docingest crawl 'cat:quant-ph AND ti:transmon' --limit 20 -c "$CONFIG"
uv run docingest ingest /path/to/papers -c "$CONFIG"      # PDF, scans, LaTeX, Office, Markdown; folders are walked
```

`crawl` downloads the LaTeX source of each paper (PDF if there is none) into `<raw_dir>/arxiv`, waits at least 3 seconds between requests as the arXiv terms require, and ingests what it fetched. LaTeX and born-digital PDFs need no GPU. The query syntax is in the [docingest README](../packages/docingest/README.md#arxiv-crawl). arXiv asks crawlers to identify themselves: add a `[arxiv]` table with `contact = "you@example.org"` to your configuration and the address goes into the User-Agent ([key reference](../packages/docingest/config/README.md#arxiv)). This matters most on a shared machine, where many people crawl from one address.

What the commands print:

- `crawl` starts with the number of records (`20 record(s) for 'cat:quant-ph AND ti:transmon'`) and then logs each record it fetched and ingested.
- `ingest` prints a line per file and, when it succeeds, where the result is: `ok -> <output_dir>/<first 16 characters of the doc id>/document.md`. A file that was ingested before with the same settings also prints a `[cache] <file> -> <folder> (config <hash>)` line before its `ok ->` line, and is not processed again.
- Both end with an `Ingestion summary` table (document, kind, pages, page methods, seconds, doc id). `crawl` adds `fetched N, ingested N`.
- A file that fails is listed in a `Failed inputs` table and the command exits with status 1 after it has processed the others.

## 6. Build the index

```bash
uv run docingest index build -c "$CONFIG"
```

This chunks every paper, sends the chunks to the embedder and commits the index. It prints one line. For 100 papers (9,404 chunks):

```text
added 100, updated 0, unchanged 0, pruned 0; embedded 9404 chunks in 200.3s
```

The time is the build's own; with Python start-up the command took 201.1 s. Run it again and nothing is embedded:

```text
added 0, updated 0, unchanged 100, pruned 0; embedded 0 chunks in 0.2s; nothing to commit
```

That took 0.90 s with start-up. A smaller example: 5 papers, 388 chunks, `embedded 388 chunks in 8.3s`.

Run one build at a time. `build` takes the lock that keeps writers apart when it starts, before it embeds anything, so a second build started at the same time stops at once with `error: another process is writing to the index <folder>; wait for it to finish`. See [Sharing a machine](#sharing-a-machine).

`index status` shows what is indexed, what is searchable and what a build would do:

```bash
uv run docingest index status -c "$CONFIG"
```

Look at the rows `indexed`, `searchable`, `corpus` and `to add`. After a complete build, `indexed` and `searchable` equal the corpus and `to add` is 0.

## 7. Ask

```bash
uv run docingest ask "What limits T1 in transmons?" -c "$CONFIG"
```

`ask` checks the index first, embeds the question, takes the 50 best chunks (dense vectors plus BM25 for English questions), reranks them and gives the best 10 to PaperQA2, which summarizes them and writes the answer. The output is the answer with its sources cited inline as `(<doc id prefix> pages N-M)`, followed by a reference list with one entry per cited paper and its pages. `warning:` lines come first if the index is behind the corpus. `ask` prints no timings; measure it with `time`.

Before the answer you may also see lines that are harmless:

- a `Loading weights` progress bar: the small CPU embedding model (`sentence-transformers/all-MiniLM-L6-v2`) of PaperQA2. Only `ask` without an index (or with `--no-index`) loads it; with the index, PaperQA2 gets the chunks as given and embeds nothing. It is fetched into `HF_HOME` the first time; `uv run docingest model-path embedding` fetches it ahead of time.
- about a dozen lines `Failed to calculate cost for qwen-local`: litellm has no price for a local model.
- on a driver that supports only CUDA 12 (such as 535), a torch `CUDA initialization: The NVIDIA driver on your system is too old` warning. The workspace's torch is a CUDA 13 build, and `ask` runs it on the CPU only. It is not related to the vLLM install.

None of them affects the answer. Two options change one question: `--no-index` answers with PaperQA2's own retrieval, and `--bm25 english|always|never` overrides the keyword setting. See [Spanish questions](#spanish-questions) for when to use the second.

## 8. Add papers later, and stop the servers

After the first build, `--index` keeps the index in step as papers arrive, with no separate build:

```bash
uv run docingest crawl 'cat:quant-ph AND ti:qubit' --limit 20 --index -c "$CONFIG"
uv run docingest ingest /path/to/more-papers --index -c "$CONFIG"
```

Each prints a line like `index: added 1, updated 0, unchanged 1; embedded 30 chunks in 0.9s` after the `Ingestion summary` table (for `crawl`, before `fetched N, ingested N`). If the index update fails, the papers stay stored and the message says to run `index build`.

Stop the servers you started when you are done. Go back to the repository root first (`cd ../..`). Each command stops only that server:

```bash
serving/serve.sh llm stop
serving/serve.sh embed stop
serving/serve.sh rerank stop
```

## Measured times

Each row says what it was measured with. The first block is the servers, the second the commands. Where two numbers are given, they are two measurements: a lab run and a second run on a fresh install (48 GB GPUs, driver 535).

| Step | Time | Conditions |
|---|---|---|
| Start `embed` | 75 s and 125 s | models already downloaded; compile cache empty in the second run, not recorded for the lab run |
| Start `rerank` | 80 s and 100 s | same |
| Start `llm` | about 231 s and 385 s | models already downloaded, empty compile cache in both; CUDA graph capture and `torch.compile` |
| Start `llm` with `LLM_EAGER=1` | about 90 s | no CUDA graphs; decoding about 3 times slower. A warm-cache start without eager mode was not timed |
| `index build`, first time | 200.3 s printed, 201.1 s with start-up | 100 papers, 9,404 chunks |
| `index build`, nothing changed | 0.2 s printed, 0.90 s with start-up | same corpus |
| `index build`, small corpus | 8.3 s printed, 9.4 s with start-up | 5 papers, 388 chunks |
| `crawl --limit 5` with ingest | 32 s | 5 LaTeX sources, the 3 s spacing between requests included |
| `ask`, the first question after the servers started | 52 s | 5 papers; includes loading the small MiniLM model (measured before the index path stopped loading it) |
| `ask`, later questions | 30 to 35 s | 5 papers; about 7 s of it is program start-up (measured before the index path stopped loading the small MiniLM model) |
| One question inside the evaluation harness | 18.5 s and 21.2 s (medians) | another corpus (QASPER) and the 9,404-chunk corpus; no CLI start-up, so not comparable with the rows above |

The times of the commands depend on the papers and, for `crawl`, on arXiv.

## Sharing a machine

Several people may use one GPU machine. These rules keep your work and theirs apart:

1. **Look before you start.** `nvidia-smi` shows the memory in use per GPU. `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` shows which processes hold it.
2. **Reuse running servers.** If the three servers already answer on 127.0.0.1 ports 8001, 8002 and 8003, do not start more:

   ```bash
   for port in 8001 8002 8003; do curl -sf -m 2 "http://127.0.0.1:$port/health" > /dev/null && echo "$port up" || echo "$port down"; done
   curl -s http://127.0.0.1:8001/v1/models      # the served name: qwen-local (qwen-embed on 8002, qwen-rerank on 8003)
   ```

   The served names are the ones in `lab-server.toml`, so it works unchanged. The `model` and `revision` in `[embedder]` are only recorded in the index name, not checked against the server: make sure they name what the running embedder serves, or the index will claim vectors of a model that did not make them. GPU 1 holds one embedder and one reranker; a second pair does not fit next to it.
3. **Use your own folders.** Keep your configuration file with your own `output_dir` and `[index] dir`. The pid files, logs and compile caches of `serve.sh` default to folders under your home (`SERVING_STATE_DIR`, `SERVING_CACHE_DIR`), so they are already yours. So is uv's cache unless you share it.
4. **One writer per index.** A build (and `ingest --index`, `crawl --index`) takes the index's lock when it starts, before it embeds anything. While another process holds the index, it fails at once with `error: another process is writing to the index <folder>; wait for it to finish`, and does no embedding. Start one build at a time. `ingest --index` and `crawl --index` report a held lock as `error: the index was not updated: ...`; the papers stay stored. Reading (`ask`, `index status`) is safe next to a build. Two people who share one `output_dir` and one `[index] dir` share one index folder; give each person their own unless you mean to share.
5. **Never stop other people's processes.** `serve.sh stop` ends only the servers recorded in your own state folder. Do not kill a vLLM process that you did not start. If a GPU is busy, start your server on another one (`serve.sh embed start 0 8012`) or with a lower memory fraction (`EMBED_GPU_MEM`, `RERANK_GPU_MEM`), and point your configuration at the new port: `[embedder] base_url`, `[reranker] base_url`, and for the answering model both `[ocr] base_url` and `[qa] llm_base`.

## Troubleshooting

### Model servers

- **`serve.sh` prints `already running (pid N, <model>)`.** A server recorded in your state folder is alive, so `start` does nothing and does not change its settings. Stop it first (`serve.sh <role> stop`) to restart with other settings.
- **`vLLM exited; last log lines:`.** The five lines are only the end of the log. Open the whole log; its path was printed when you started the server (`<state dir>/<served name>.log`, by default under `~/.local/state/research-engine`). Usual causes:
  - `RuntimeError: The NVIDIA driver on your system is too old (found version 12020)` near the top of the log, ending in `Engine core initialization failed`: the vLLM build does not match the driver. Reinstall as in [Install vLLM](../serving/README.md#install-vllm).
  - GPU memory: another process holds it. Check `nvidia-smi`, then lower the fraction (`LLM_GPU_MEM`, `EMBED_GPU_MEM`, `RERANK_GPU_MEM`) or use another GPU: `serving/serve.sh embed start <GPU> <PORT>`.
  - The port is taken by another server: pick another port and update the configuration.
  - A kernel compile error at start-up: missing Python headers or `ninja`. See [Install vLLM](../serving/README.md#install-vllm).
  - CUDA graph capture fails for the answering model: `LLM_EAGER=1 serving/serve.sh llm start` as a fallback (slower decoding). It has no effect if `env.local` sets `LLM_EAGER=0`, because `env.local` is read after the environment.
- **`not ready after 10 minutes, see <log>`.** The server may still be downloading or compiling. It keeps running; follow the log with `tail -f` and use `curl .../health`.
- **It downloads into the wrong folder.** Without `HF_HOME` the models go to `$SERVING_CACHE_DIR/huggingface`, not `~/.cache/huggingface`. Check that `HF_HOME` is exported in the shell that runs `serve.sh`.
- **It does not download.** Either `HF_HUB_OFFLINE=1` is set before the models are in the cache, or the machine cannot reach the Hub.
- **Odd rerank scores.** The Qwen3 reranker needs the chat template in `serving/templates/` with its exact newlines. Do not edit the template or start the reranker without it. See [templates/README.md](../serving/templates/README.md).

### Commands that talk to the servers

- **`error: embedding server http://127.0.0.1:8002/v1/embeddings unreachable after 6 attempt(s)`**, or the same for `reranker server http://127.0.0.1:8003/rerank`. The server is not running or is on another port. The command retried for about 30 seconds before it gave up. Check `serve.sh embed status` and `curl .../health`, and compare the port with `base_url` in your configuration.
- **A traceback of thousands of lines from `ask`; its last lines say `AllModelsExhaustedError` and `Connection error`.** The answering model's server (`qwen-local`, port 8001) is unreachable. Check `serve.sh llm status` and `[qa] llm_base`. During `ingest`, a scanned page fails in a different way: the file lands in the `Failed inputs` table with `OCR server http://127.0.0.1:8001/v1/... unreachable after N attempt(s)`. Check `[ocr] base_url` for that one.
- **`... answered HTTP 401 (check api_key_env)`, or 403.** The server wants a key: set the variable named in `api_key_env` of that table.

### The index

- **`error: the index is empty`.** The index was never built, or the settings now select another index folder. The folder depends on `output_dir`, the `[embedder]` `model` and `revision`, `[qa]` `chunk_chars` and `overlap`, and the chunker version, so a docingest update that changes the chunker also counts. A change to any of them starts an empty index beside the old one. Run `docingest index build`, or restore the old settings.
- **`error: the index holds N documents but none is committed`.** An earlier build was interrupted before it committed. Run `index build`.
- **`error: the index holds vectors of embedder ...`** (it goes on: `but the configured one is ...`). The folder was written with another embedder identity than the one in your `[embedder]` table. Restore those settings, or use another `[index] dir`, then run `index build`. Vectors of two models are never mixed.
- **`warning: K of N documents of the corpus are not in the index`.** You ingested papers after the last build. The answer ignores them. Run `index build`.
- **`warning: the index has changes that are not committed yet`** and **`warning: M of K retrieved chunks belong to documents that are no longer in the corpus`.** Run `index build`; it commits pending changes and prunes the papers that left the corpus.
- **`error: another process is writing to the index <folder>; wait for it to finish`.** One writer at a time; the lock is taken when a build starts: see rule 4 of [Sharing a machine](#sharing-a-machine). Wait for the other build, or find out who holds the folder.
- **`error: the index <folder> kept changing while it was being opened; ask again`.** Builds committed repeatedly while `ask` or `status` opened the index. Run the command again.
- **`error: no ingested documents`** from `index build` (it goes on: run `docingest ingest` first), or a traceback ending with the same text from `ask`. The corpus at `output_dir` is empty. Check that you passed `-c "$CONFIG"` and that `output_dir` in it is the folder you ingested into.
- **`Invalid value: --index needs [adapters] index and embedder to be set`** (exit status 2). `ingest --index` or `crawl --index` ran without `-c "$CONFIG"`, or with a configuration that has no index.
- **`ask` takes minutes, loads weights and builds the corpus every time.** It ran with `--no-index` or a configuration without `[adapters] index`, so PaperQA2 embeds the corpus on every question.

### Spanish questions

The keyword (BM25) part of the retrieval is on only for questions that look English, because in the measurements it helped English questions and added noise to Spanish ones. A Spanish question is answered from the dense ranking and the reranker, and in a test it got a cited answer in Spanish. `--bm25 always` turns the keyword part on for it, `--bm25 never` turns it off for an English one. The reasons are in [ADR 0004](../packages/docingest/docs/adr/0004-chunk-index-and-two-stage-retrieval.md).
