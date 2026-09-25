# OCR benchmark: run `deep`

Generated 2026-09-25T16:14:46+00:00 · docingest 0.3.1 (pipeline 0.3.1) · mlx-vlm 0.7.3 · Apple M4 Pro, 24.0 GB RAM · macOS-26.6.2-arm64-arm-64bit

Intervals are 95% percentile bootstrap CIs that resample independent clusters, not single units: a synthetic page with all its degradation levels, an olmOCR-bench PDF with all its tests (within its category). Paired comparisons take the units both candidates scored: a cluster bootstrap CI (10000 resamples, seed 0) and a two-sided sign-flip test over clusters (every pattern up to 16 clusters, else 10000 random ones); *significant* means p < 0.05. With k clusters the smallest attainable p is 2/2^k.

## Candidates

| candidate | model | revision | profile | adapter | max_side | max_tokens |
|---|---|---|---|---|---|---|
| glm-ocr | mlx-community/GLM-OCR-4bit | 97f5875069 | glm-ocr | mlx-vlm | – | – |
| nanonets-ocr2-3b | mlx-community/Nanonets-OCR2-3B-4bit | fe1396cbeb | nanonets | mlx-vlm | – | – |
| qwen3-vl-30b-a3b | mlx-community/Qwen3-VL-30B-A3B-Instruct-4bit | 0555d34cb1 | markdown | mlx-vlm | – | – |
| qwen3-vl-8b | mlx-community/Qwen3-VL-8B-Instruct-4bit | defcdea7cc | markdown | mlx-vlm | – | – |
| qwen3.5-4b | mlx-community/Qwen3.5-4B-MLX-4bit | 32f3e8ecf6 | markdown | mlx-vlm | – | – |
| qwen3.5-9b | mlx-community/Qwen3.5-9B-MLX-4bit | 938d891994 | markdown | mlx-vlm | – | – |

## Suite `olmocr-bench`

Fingerprint `3064e94ac978`, 84 samples, scoring version 3.

Primary metric: **pass_rate** (higher is better).

### Overall

| # | candidate | pass_rate | outputs |
|---|---|---|---|
| 1 | qwen3.5-4b | 79.4% [74.2, 83.9] | 84/84 |
| 2 | qwen3.5-9b | 79.3% [74.3, 83.5] | 84/84 |
| 3 | qwen3-vl-8b | 76.2% [70.8, 81.5] | 84/84 |
| 4 | qwen3-vl-30b-a3b | 74.9% [68.9, 80.1] | 84/84 |
| 5 | glm-ocr | 72.1% [65.8, 77.9] | 84/84 |
| 6 | nanonets-ocr2-3b | 67.6% [61.0, 74.2] | 84/84 |

One cluster = a PDF with all its tests, drawn within its category; 84 clusters.

Official scorer CI, to compare with the leaderboard's ±: olmocr.bench: tests resampled within each jsonl file, unseeded; treats the tests of one PDF as independent, so it is narrower than ours.

| candidate | official CI | official ± |
|---|---|---|
| qwen3.5-4b | [76.2, 82.7] | 3.3% |
| qwen3.5-9b | [76.0, 82.4] | 3.2% |
| qwen3-vl-8b | [72.7, 79.6] | 3.4% |
| qwen3-vl-30b-a3b | [71.2, 78.4] | 3.6% |
| glm-ocr | [68.9, 75.4] | 3.3% |
| nanonets-ocr2-3b | [63.9, 71.1] | 3.6% |

### By category (pass_rate)

| candidate | arxiv_math | baseline | headers_footers | long_tiny_text | multi_column | old_scans | old_scans_math | table_tests |
|---|---|---|---|---|---|---|---|---|
| qwen3.5-4b | 83.6% [74.0, 92.1] | 100.0% [100.0, 100.0] | 73.3% [51.2, 90.3] | 89.0% [83.3, 95.2] | 82.5% [68.6, 93.0] | 35.4% [16.3, 56.9] | 83.5% [71.8, 92.8] | 87.7% [69.3, 100.0] |
| qwen3.5-9b | 83.6% [72.9, 92.4] | 100.0% [100.0, 100.0] | 75.6% [51.5, 91.3] | 87.7% [77.6, 98.2] | 82.5% [67.7, 94.9] | 33.9% [15.4, 53.0] | 83.5% [70.8, 91.4] | 87.7% [78.1, 96.4] |
| qwen3-vl-8b | 88.1% [78.1, 97.1] | 100.0% [100.0, 100.0] | 51.1% [29.3, 76.2] | 84.9% [72.3, 95.3] | 77.5% [65.7, 88.6] | 36.9% [16.1, 58.5] | 87.6% [78.2, 94.0] | 83.1% [60.0, 100.0] |
| qwen3-vl-30b-a3b | 83.6% [74.7, 92.1] | 100.0% [100.0, 100.0] | 64.4% [44.2, 81.6] | 82.2% [70.7, 92.8] | 75.0% [60.0, 87.8] | 38.5% [19.4, 58.5] | 86.0% [73.3, 93.5] | 69.2% [42.2, 92.5] |
| glm-ocr | 65.7% [41.3, 84.9] | 98.8% [96.3, 100.0] | 93.3% [77.5, 100.0] | 89.0% [74.1, 100.0] | 77.5% [66.7, 87.2] | 33.9% [14.5, 54.7] | 83.5% [71.1, 95.2] | 35.4% [8.1, 64.4] |
| nanonets-ocr2-3b | 59.7% [32.4, 80.7] | 98.8% [96.3, 100.0] | 51.1% [36.9, 68.4] | 89.0% [78.3, 98.4] | 85.0% [73.0, 95.1] | 40.0% [18.8, 62.3] | 29.8% [2.6, 58.4] | 87.7% [74.6, 97.2] |

### Paired comparison vs `qwen3.5-4b` (highest mean)

| candidate | Δ pass_rate (candidate − `qwen3.5-4b`) [CI] | p | significant | pairs | clusters |
|---|---|---|---|---|---|
| qwen3.5-9b | -0.1% [-4.4, 4.2] | 0.9713 | no | 558 | 84 |
| qwen3-vl-8b | -3.2% [-8.9, 2.7] | 0.3391 | no | 558 | 84 |
| qwen3-vl-30b-a3b | -4.5% [-9.5, 0.2] | 0.0767 | no | 558 | 84 |
| glm-ocr | -7.2% [-13.4, -1.1] | 0.0528 | no | 558 | 84 |
| nanonets-ocr2-3b | -11.7% [-18.0, -5.1] | 0.0048 | yes | 558 | 84 |

### Throughput and failure modes

| candidate | pages | median s/page | p90 s/page | gen tok/s | peak GB | truncated 1st try | truncated final | retried | empty | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| glm-ocr | 84 | 6.253 | 15.939 | 184.1 | 2.57 | 1.2% | 1.2% | 1.2% | 0.0% | 0 |
| nanonets-ocr2-3b | 84 | 14.801 | 27.586 | 96.6 | 4.16 | 2.4% | 0.0% | 2.4% | 0.0% | 0 |
| qwen3-vl-30b-a3b | 84 | 20.524 | 39.224 | 60.2 | 19.49 | 2.4% | 0.0% | 2.4% | 0.0% | 0 |
| qwen3-vl-8b | 84 | 23.644 | 49.169 | 46.4 | 7.06 | 1.2% | 1.2% | 1.2% | 0.0% | 0 |
| qwen3.5-4b | 84 | 14.583 | 31.131 | 78.5 | 4.95 | 2.4% | 0.0% | 2.4% | 0.0% | 0 |
| qwen3.5-9b | 84 | 22.693 | 49.774 | 47.5 | 7.76 | 0.0% | 0.0% | 0.0% | 0.0% | 0 |
