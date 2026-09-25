# OCR benchmark: run `screen`

Generated 2026-09-25T11:01:20+00:00 · docingest 0.1.0 (pipeline 0.3.0) · mlx-vlm 0.7.3 · Apple M4 Pro, 24.0 GB RAM · macOS-26.6.2-arm64-arm-64bit

Intervals are 95% percentile bootstrap CIs that resample independent clusters, not single units: a synthetic page with all its degradation levels, an olmOCR-bench PDF with all its tests (within its category). Paired comparisons take the units both candidates scored: a cluster bootstrap CI (10000 resamples, seed 0) and a two-sided sign-flip test over clusters (every pattern up to 16 clusters, else 10000 random ones); *significant* means p < 0.05. With k clusters the smallest attainable p is 2/2^k.

## Candidates

| candidate | model | revision | profile | adapter | max_side | max_tokens |
|---|---|---|---|---|---|---|
| glm-ocr | mlx-community/GLM-OCR-4bit | 97f5875069 | glm-ocr | mlx-vlm | – | – |
| nanonets-ocr2-3b | mlx-community/Nanonets-OCR2-3B-4bit | fe1396cbeb | nanonets | mlx-vlm | – | – |
| olmocr-2-7b | mlx-community/olmOCR-2-7B-1025-mlx-4bit | c0eaffb784 | olmocr | mlx-vlm | – | – |
| paddleocr-vl | mlx-community/PaddleOCR-VL-1.6-4bit | c8987b277b | paddleocr-vl | mlx-vlm | – | – |
| qwen3-vl-2b | mlx-community/Qwen3-VL-2B-Instruct-4bit | 9c4f5209e5 | markdown | mlx-vlm | – | – |
| qwen3-vl-30b-a3b | mlx-community/Qwen3-VL-30B-A3B-Instruct-4bit | 0555d34cb1 | markdown | mlx-vlm | – | – |
| qwen3-vl-4b | mlx-community/Qwen3-VL-4B-Instruct-4bit | 2fd8dacbdb | markdown | mlx-vlm | – | – |
| qwen3-vl-8b | mlx-community/Qwen3-VL-8B-Instruct-4bit | defcdea7cc | markdown | mlx-vlm | – | – |
| qwen3.5-4b | mlx-community/Qwen3.5-4B-MLX-4bit | 32f3e8ecf6 | markdown | mlx-vlm | – | – |
| qwen3.5-9b | mlx-community/Qwen3.5-9B-MLX-4bit | 938d891994 | markdown | mlx-vlm | – | – |

## Suite `synthetic`

Fingerprint `29176f1a8b5b`, 36 samples, scoring version 3.

Primary metric: **cer** (lower is better).

### Overall

| rank | candidate | cer | wer | word_f1 | char3_f1 | outputs |
|---|---|---|---|---|---|---|
| 1 | nanonets-ocr2-3b | 0.011 [0.004, 0.020] | 0.044 [0.029, 0.061] | 0.968 [0.957, 0.978] | 0.988 [0.982, 0.992] | 33/36 |
| 2 | qwen3.5-9b | 0.014 [0.004, 0.030] | 0.039 [0.020, 0.064] | 0.975 [0.961, 0.987] | 0.991 [0.983, 0.996] | 33/36 |
| 3 | qwen3.5-4b | 0.021 [0.007, 0.042] | 0.047 [0.031, 0.066] | 0.971 [0.963, 0.979] | 0.989 [0.985, 0.993] | 33/36 |
| 4 | paddleocr-vl | 0.027 [0.009, 0.050] | 0.054 [0.028, 0.087] | 0.965 [0.947, 0.980] | 0.983 [0.972, 0.992] | 33/36 |
| 5 | qwen3-vl-30b-a3b | 0.030 [0.004, 0.071] | 0.060 [0.025, 0.112] | 0.974 [0.963, 0.983] | 0.990 [0.984, 0.994] | 33/36 |
| 6 | glm-ocr | 0.030 [0.009, 0.054] | 0.047 [0.023, 0.074] | 0.970 [0.954, 0.983] | 0.981 [0.968, 0.993] | 33/36 |
| 7 | qwen3-vl-8b | 0.044 [0.003, 0.116] | 0.076 [0.024, 0.161] | 0.976 [0.966, 0.984] | 0.992 [0.987, 0.996] | 33/36 |
| 8 | olmocr-2-7b | 0.069 [0.011, 0.137] | 0.097 [0.032, 0.169] | 0.946 [0.887, 0.980] | 0.960 [0.900, 0.993] | 33/36 |
| 9 | qwen3-vl-4b | 0.073 [0.011, 0.157] | 0.098 [0.038, 0.178] | 0.940 [0.895, 0.973] | 0.957 [0.909, 0.991] | 33/36 |
| 10 | qwen3-vl-2b | 0.094 [0.030, 0.179] | 0.107 [0.048, 0.189] | 0.940 [0.887, 0.972] | 0.954 [0.904, 0.983] | 33/36 |

One cluster = a page with all its degradation levels; 11 clusters.

### By category (cer)

| candidate | clean | heavy | light |
|---|---|---|---|
| nanonets-ocr2-3b | 0.007 [0.003, 0.014] | 0.013 [0.005, 0.021] | 0.014 [0.005, 0.026] |
| qwen3.5-9b | 0.004 [0.002, 0.006] | 0.014 [0.003, 0.030] | 0.024 [0.004, 0.056] |
| qwen3.5-4b | 0.046 [0.005, 0.108] | 0.010 [0.003, 0.018] | 0.007 [0.004, 0.012] |
| paddleocr-vl | 0.016 [0.006, 0.029] | 0.029 [0.011, 0.048] | 0.035 [0.007, 0.079] |
| qwen3-vl-30b-a3b | 0.080 [0.005, 0.203] | 0.004 [0.003, 0.006] | 0.006 [0.003, 0.009] |
| glm-ocr | 0.038 [0.007, 0.083] | 0.027 [0.005, 0.055] | 0.024 [0.003, 0.051] |
| qwen3-vl-8b | 0.061 [0.003, 0.169] | 0.009 [0.003, 0.022] | 0.062 [0.003, 0.170] |
| olmocr-2-7b | 0.026 [0.004, 0.064] | 0.099 [0.003, 0.281] | 0.081 [0.007, 0.196] |
| qwen3-vl-4b | 0.013 [0.005, 0.025] | 0.195 [0.011, 0.460] | 0.011 [0.004, 0.023] |
| qwen3-vl-2b | 0.061 [0.020, 0.122] | 0.161 [0.036, 0.366] | 0.060 [0.016, 0.124] |

### Paired comparison vs best (`nanonets-ocr2-3b`)

| candidate | Δ cer (candidate − best) [CI] | p | significant | pairs | clusters |
|---|---|---|---|---|---|
| qwen3.5-9b | +0.003 [-0.008, 0.018] | 0.7998 | no | 33 | 11 |
| qwen3.5-4b | +0.010 [0.001, 0.025] | 0.0469 | yes | 33 | 11 |
| paddleocr-vl | +0.015 [-0.000, 0.038] | 0.1416 | no | 33 | 11 |
| qwen3-vl-30b-a3b | +0.018 [-0.001, 0.054] | 0.3633 | no | 33 | 11 |
| glm-ocr | +0.019 [0.003, 0.038] | 0.1211 | no | 33 | 11 |
| qwen3-vl-8b | +0.033 [-0.002, 0.100] | 0.4922 | no | 33 | 11 |
| olmocr-2-7b | +0.057 [0.003, 0.125] | 0.0527 | no | 33 | 11 |
| qwen3-vl-4b | +0.062 [-0.002, 0.150] | 0.1865 | no | 33 | 11 |
| qwen3-vl-2b | +0.083 [0.021, 0.162] | 0.0010 | yes | 33 | 11 |

### Throughput and failure modes

| candidate | pages | median s/page | p90 s/page | gen tok/s | peak GB | truncated 1st try | truncated final | retried | empty | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| glm-ocr | 36 | 7.892 | 10.898 | ≥134.8 | 2.41 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| nanonets-ocr2-3b | 36 | 19.261 | 24.448 | ≥59.6 | 4.13 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| olmocr-2-7b | 36 | 30.212 | 40.085 | ≥35.6 | 6.56 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| paddleocr-vl | 36 | 3.608 | 5.556 | ≥247.1 | 1.68 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3-vl-2b | 36 | 11.082 | 19.389 | ≥70.4 | 2.99 | ≥5.6% | 5.6% | n/a | 0.0% | 0 |
| qwen3-vl-30b-a3b | 36 | 21.538 | 32.207 | 67.6 | 19.33 | 2.8% | 0.0% | 2.8% | 0.0% | 0 |
| qwen3-vl-4b | 36 | 20.275 | 33.323 | ≥37.1 | 4.45 | ≥5.6% | 5.6% | n/a | 0.0% | 0 |
| qwen3-vl-8b | 36 | 32.099 | 43.703 | ≥32.7 | 6.93 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3.5-4b | 36 | 19.767 | 28.953 | ≥51.6 | 4.92 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3.5-9b | 36 | 30.511 | 40.995 | ≥34.8 | 7.72 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |

_≥ tok/s: decode time was not measured, so this is generated tokens over the whole page time (prefill included; for older telemetry, only the final attempt's tokens over the time of every attempt): a lower bound._
_≥ / n/a: telemetry written before the retry ladder was recorded. A page whose first attempt hit max_tokens and whose retry finished looks clean there, so first-try truncation is a lower bound and retries are unknown._

## Suite `olmocr-bench`

Fingerprint `19bfffecbd53`, 42 samples, scoring version 3.

Primary metric: **pass_rate** (higher is better).

### Overall

| rank | candidate | pass_rate | outputs |
|---|---|---|---|
| 1 | qwen3.5-9b | 83.0% [76.1, 88.2] | 42/42 |
| 2 | qwen3.5-4b | 80.2% [73.0, 86.5] | 42/42 |
| 3 | qwen3-vl-8b | 79.4% [72.0, 86.8] | 42/42 |
| 4 | glm-ocr | 75.2% [67.9, 82.0] | 42/42 |
| 5 | qwen3-vl-30b-a3b | 74.9% [66.6, 82.4] | 42/42 |
| 6 | nanonets-ocr2-3b | 71.1% [63.2, 78.1] | 42/42 |
| 7 | qwen3-vl-4b | 69.9% [61.8, 78.0] | 42/42 |
| 8 | qwen3-vl-2b | 68.4% [59.3, 77.2] | 42/42 |
| 9 | olmocr-2-7b | 64.4% [52.4, 74.8] | 42/42 |
| 10 | paddleocr-vl | 55.3% [47.5, 61.9] | 42/42 |

One cluster = a PDF with all its tests, drawn within its category; 42 clusters.

Official scorer CI, to compare with the leaderboard's ±: olmocr.bench: tests resampled within each jsonl file, unseeded; treats the tests of one PDF as independent, so it is narrower than ours.

| candidate | official CI | official ± |
|---|---|---|
| qwen3.5-9b | [78.2, 87.2] | 4.5% |
| qwen3.5-4b | [74.9, 85.2] | 5.2% |
| qwen3-vl-8b | [74.8, 83.9] | 4.6% |
| glm-ocr | [70.5, 79.5] | 4.5% |
| qwen3-vl-30b-a3b | [69.4, 80.1] | 5.4% |
| nanonets-ocr2-3b | [66.1, 75.9] | 4.9% |
| qwen3-vl-4b | [64.4, 75.4] | 5.5% |
| qwen3-vl-2b | [62.7, 73.7] | 5.5% |
| olmocr-2-7b | [58.6, 70.3] | 5.9% |
| paddleocr-vl | [50.1, 60.9] | 5.4% |

### By category (pass_rate)

| candidate | arxiv_math | baseline | headers_footers | long_tiny_text | multi_column | old_scans | old_scans_math | table_tests |
|---|---|---|---|---|---|---|---|---|
| qwen3.5-9b | 83.3% [61.1, 97.4] | 100.0% [100.0, 100.0] | 88.0% [66.7, 100.0] | 87.5% [72.7, 100.0] | 73.3% [43.8, 94.1] | 50.0% [22.2, 70.6] | 88.2% [76.2, 94.0] | 93.8% [86.8, 100.0] |
| qwen3.5-4b | 80.6% [61.1, 92.9] | 100.0% [100.0, 100.0] | 76.0% [46.2, 95.5] | 90.6% [86.1, 96.5] | 66.7% [40.0, 88.2] | 56.2% [30.4, 79.0] | 86.8% [77.6, 96.0] | 84.4% [58.3, 100.0] |
| qwen3-vl-8b | 88.9% [67.5, 100.0] | 100.0% [100.0, 100.0] | 44.0% [20.0, 87.5] | 87.5% [78.6, 94.6] | 80.0% [54.5, 94.4] | 59.4% [28.0, 82.5] | 91.2% [87.0, 96.5] | 84.4% [50.0, 100.0] |
| glm-ocr | 69.4% [35.7, 86.7] | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 96.9% [91.4, 100.0] | 73.3% [50.0, 88.9] | 53.1% [28.6, 77.1] | 77.9% [65.2, 92.9] | 31.2% [0.0, 66.7] |
| qwen3-vl-30b-a3b | 83.3% [64.9, 96.5] | 100.0% [100.0, 100.0] | 64.0% [33.3, 87.0] | 75.0% [55.6, 90.6] | 66.7% [38.5, 93.3] | 56.2% [29.6, 78.1] | 91.2% [84.6, 94.3] | 62.5% [27.8, 100.0] |
| nanonets-ocr2-3b | 75.0% [44.8, 91.7] | 100.0% [100.0, 100.0] | 32.0% [23.1, 42.1] | 90.6% [86.1, 96.5] | 80.0% [58.3, 94.1] | 56.2% [24.0, 82.3] | 35.3% [3.9, 72.7] | 100.0% [100.0, 100.0] |
| qwen3-vl-4b | 63.9% [34.5, 81.8] | 100.0% [100.0, 100.0] | 40.0% [21.9, 68.4] | 75.0% [61.1, 92.9] | 73.3% [43.8, 93.8] | 43.8% [13.0, 71.0] | 82.3% [74.6, 92.9] | 81.2% [50.0, 100.0] |
| qwen3-vl-2b | 69.4% [41.9, 84.4] | 100.0% [100.0, 100.0] | 68.0% [38.5, 100.0] | 59.4% [36.8, 80.0] | 46.7% [14.3, 73.3] | 46.9% [16.7, 75.8] | 66.2% [44.6, 87.9] | 90.6% [70.0, 100.0] |
| olmocr-2-7b | 83.3% [47.8, 100.0] | 80.5% [68.3, 92.7] | 72.0% [41.7, 100.0] | 71.9% [37.1, 96.4] | 46.7% [13.3, 75.0] | 37.5% [18.8, 56.7] | 70.6% [21.4, 85.3] | 53.1% [19.4, 90.0] |
| paddleocr-vl | 66.7% [40.9, 77.1] | 95.1% [87.8, 100.0] | 20.0% [5.9, 35.0] | 78.1% [63.3, 91.7] | 66.7% [28.6, 94.1] | 43.8% [16.7, 67.7] | 66.2% [53.8, 89.3] | 6.2% [0.0, 20.0] |

### Paired comparison vs best (`qwen3.5-9b`)

| candidate | Δ pass_rate (candidate − best) [CI] | p | significant | pairs | clusters |
|---|---|---|---|---|---|
| qwen3.5-4b | -2.9% [-8.6, 3.3] | 0.3933 | no | 281 | 42 |
| qwen3-vl-8b | -3.6% [-10.0, 3.8] | 0.4940 | no | 281 | 42 |
| glm-ocr | -7.8% [-14.2, -0.5] | 0.1156 | no | 281 | 42 |
| qwen3-vl-30b-a3b | -8.2% [-16.1, -0.3] | 0.0747 | no | 281 | 42 |
| nanonets-ocr2-3b | -11.9% [-18.4, -4.8] | 0.0432 | yes | 281 | 42 |
| qwen3-vl-4b | -13.1% [-19.0, -6.1] | 0.0015 | yes | 281 | 42 |
| qwen3-vl-2b | -14.6% [-21.4, -6.7] | 0.0009 | yes | 281 | 42 |
| olmocr-2-7b | -18.6% [-28.7, -8.7] | 0.0007 | yes | 281 | 42 |
| paddleocr-vl | -27.7% [-33.5, -20.9] | 0.0001 | yes | 281 | 42 |

### Throughput and failure modes

| candidate | pages | median s/page | p90 s/page | gen tok/s | peak GB | truncated 1st try | truncated final | retried | empty | errors |
|---|---|---|---|---|---|---|---|---|---|---|
| glm-ocr | 42 | 5.761 | 12.742 | ≥109.7 | 2.57 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| nanonets-ocr2-3b | 42 | 15.171 | 32.99 | ≥55.6 | 4.14 | ≥2.4% | 2.4% | n/a | 0.0% | 0 |
| olmocr-2-7b | 42 | 20.04 | 44.381 | ≥33.9 | 6.6 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| paddleocr-vl | 42 | 3.142 | 6.706 | ≥209.1 | 1.69 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3-vl-2b | 42 | 7.146 | 23.133 | ≥88.2 | 3.06 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3-vl-30b-a3b | 42 | 15.423 | 36.308 | 66.8 | 19.49 | 2.4% | 0.0% | 2.4% | 0.0% | 0 |
| qwen3-vl-4b | 42 | 14.889 | 53.9 | ≥43.6 | 4.45 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3-vl-8b | 42 | 23.499 | 50.236 | ≥30.3 | 7.06 | ≥2.4% | 2.4% | n/a | 0.0% | 0 |
| qwen3.5-4b | 42 | 14.453 | 31.265 | ≥53.9 | 4.95 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |
| qwen3.5-9b | 42 | 22.541 | 51.485 | ≥35.5 | 7.76 | ≥0.0% | 0.0% | n/a | 0.0% | 0 |

_≥ tok/s: decode time was not measured, so this is generated tokens over the whole page time (prefill included; for older telemetry, only the final attempt's tokens over the time of every attempt): a lower bound._
_≥ / n/a: telemetry written before the retry ladder was recorded. A page whose first attempt hit max_tokens and whose retry finished looks clean there, so first-try truncation is a lower bound and retries are unknown._
