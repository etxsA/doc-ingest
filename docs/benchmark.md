# OCR model benchmark

The question: which local vision-language model should transcribe scanned pages for the research agent, and at what cost? Every number here comes from runs on one Apple M4 Pro with 24 GB. The runs are reproducible with `docingest bench` (see the end of this document).

<!-- RESULTS -->

## Methodology

### Candidates
There are 9 models, each pinned to an exact Hugging Face commit in `config/benchmark.toml` and run in-process with mlx-vlm 0.7.3 (4-bit MLX conversions). Each model uses its **own profile**: the prompt, image size, output clean-up and generation limits from its model card or official repository. A model is never judged on a prompt it was not built for.

| Candidate | Profile | Notes |
|---|---|---|
| Qwen3-VL-2B / 4B / 8B Instruct | `markdown` | general VLMs; our Markdown + LaTeX prompt |
| Qwen3.5-4B / 9B | `markdown` | natively multimodal; thinking disabled by the chat template |
| olmOCR-2-7B-1025 (AllenAI) | `olmocr` | official v4 prompt, 1288 px, YAML front matter stripped |
| Nanonets-OCR2-3B | `nanonets` | official prompt; `<page_number>`/`<img>` tags handled |
| GLM-OCR | `glm-ocr` | `Text Recognition:` with the official (thinking-enabled) template |
| PaddleOCR-VL-1.6 | `paddleocr-vl` | `OCR:`; its processor caps the input at about 1 MP |

All candidates use greedy decoding (temperature 0) and the same retry ladder. If a page hits its token limit, which usually means a repetition loop, it is retried with a little temperature and a stronger repetition penalty, seeded by the page image. Telemetry records whether the first attempt was truncated, the number of attempts and the decode speed.

### Suite 1: synthetic degraded scans (ground truth known exactly)
- **Pages:** 12 pages from 3 born-digital arXiv papers (Transformer, PaperQA2, olmOCR). They cover a title page, math, tables, dense author lists and body text.
- **Rendering:** each page is rasterized at 200 dpi and degraded at three deterministic levels:
  - **clean:** raster only;
  - **light:** blur, noise and a ±1.2° rotation;
  - **heavy:** stronger blur and noise plus JPEG at quality 35.
- **Reference:** the page's own text layer.
- **Fairness normalisation**, applied at scoring time to both the reference and the model output:
  - Page furniture is stripped: running headers, bare page numbers and the arXiv margin stamp. The `markdown` prompt tells models to omit them, so leaving them in would penalise obedience.
  - Markdown and HTML markup are stripped, including `<img>` descriptions and figure placeholders.
  - Hyphenation is ignored.
  - A small LaTeX-to-Unicode normaliser runs, so `$\alpha$` and `α` score the same.
- **Metrics:**
  - **CER** (primary) and **WER**, both capped at 1.0 so a runaway loop cannot dominate a mean;
  - **word-F1** (order-insensitive);
  - **char-3-gram F1** (order-insensitive but still sensitive to spelling).

### Suite 2: olmOCR-Bench subset (industry benchmark, real scans)
- **Dataset:** `allenai/olmOCR-bench`, pinned to dataset commit `54a96a6f`.
- **Categories:** arXiv math, old scans, old scans with math, tables, headers and footers, multi-column, and long tiny text.
- **Sample:** a seeded sample of PDFs per category. Samples are nested, so the 6-per-category screening sample is contained in the 12-per-category deep sample.
- **Scoring:** the **official scorer** (`olmocr==0.4.27`, in an isolated venv with headless Chromium for the math-rendering tests). Scores are therefore comparable in kind with the published leaderboard, although a subset has wider uncertainty than the full benchmark (about 1,400 PDFs).
- **Test types:** unit tests on the Markdown output. They check that specific text is present, that headers and footers are absent, that reading order is correct, that table cells are placed correctly, and that math renders to the same result as the reference. There are also baseline sanity tests.

### Statistics
- **Clustered bootstrap.** Every confidence interval resamples **clusters**, never single observations. On the synthetic suite, one page with all three of its degradation levels is one cluster. On olmOCR-Bench, one PDF with all its tests is one cluster, resampled within its category. Treating those as independent made intervals falsely narrow. In a null simulation the false-positive rate went from 26.5% down to the nominal 5.2%.
- **Paired comparisons** against the best candidate use a paired cluster bootstrap for the interval and an exact sign-flip test over cluster means for the p-value.
- With few clusters, p-values are shown but "significant" is suppressed. With k clusters the smallest possible p is 2/2^k.
- The official scorer's own interval, which resamples tests and is narrower, is reported separately for comparison with published numbers.

### Throughput
Throughput is wall-clock seconds per page (median and p90), decode tokens per second, peak GPU memory per page (MLX, reset before each page), truncation and retry rates, and empty-output rate. Every model runs in the same process, one after another, with unloading and cache clearing in between, on AC power with the Mac kept awake.

### Reproduce

```bash
./scripts/setup_bench_scorer.sh
uv run docingest bench prepare --preset screen
uv run docingest bench run --preset screen --run-id screen     # resumable
uv run docingest bench report --run-id screen                  # summary.json + report.md
uv run python scripts/bench_charts.py data/bench/runs/screen docs/benchmark
```

Each run directory records the suite fingerprints, candidate specs, package versions and machine info, and it refuses to resume with different settings.
