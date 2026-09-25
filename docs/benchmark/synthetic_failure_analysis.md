# Synthetic suite: failure analysis of every outlier (CER >= 0.15)

Screening run `screen`, scoring version 2 (before the v3 normalizer fixes this analysis motivated).
Three analysts classified all 44 outlier units; a verifier re-checked 14 of them against the raw outputs,
telemetry and rendered pages (no class changed). Evidence quotes are per unit in `synthetic_failure_analysis.json`.

## Synthesis: OCR outlier failures (44 units, 9 models)

### 1. Verification (14 units re-checked against the raw files, telemetry, the rendered page and the benchmark's `metrics.py`)

| Unit | Analyst class | Verdict | Evidence |
|---|---|---|---|
| qwen3-vl-2b pq_p001_heavy | loop | Confirmed | 4096 tokens, finish_reason=length; `## 1 Introduction` appears 15 times |
| qwen3-vl-4b pq_p003_heavy | loop | Confirmed | Hits the 4096 cap; the output ends by repeating "We attribute the performance difference to the agent's better recall" |
| qwen3-vl-4b pq_p007_heavy | loop + truncation | Confirmed | Cycles on "The source paper, Kriegel et al., (2010)" until the cap |
| qwen3-vl-2b ol_p001_clean | loop | Confirmed | `## Contact` appears 163 times; hits the 4096 cap |
| olmocr-2-7b pq_p011_heavy | empty | Confirmed | The whole output is the YAML front matter. 34 tokens, stop, yet telemetry says empty=false |
| glm-ocr att_p010_clean | omission | Confirmed | Output starts at "increased the maximum output length". No Table 4 row (e.g. "Vinyals & Kaiser") appears |
| paddleocr-vl att_p008_light | hallucination | Confirmed | `IN THEteNet` appears 9 times, plus "15 min" and 必要组别. The reference says "3.5 days on 8 P100" |
| qwen3-vl-8b att_p001_clean | reading_order | Confirmed | The `*Equal contribution` footnote comes before `## Abstract`. The reference has the abstract first |
| qwen3.5-4b att_p001_clean | reading_order | Confirmed | `**Ashish Vaswani**` is emitted after "31st Conference" |
| glm-ocr ol_p001 ×3 | omission | Confirmed | Output starts at "PDF documents have the potential"; clean and light are byte-identical (same md5) |
| ol_p001 reference_artifact (all) | ref_artifact | Confirmed, with a nuance | A perfect transcription that skips the figure scores CER 0.3423 / word_f1 0.806 (reproduced). The figure text is visible on the page but tiny, and in the text layer its spaces are lost: "Loremipsumdolorsitamet" |
| qwen3-vl-2b ol_p001_light | ref_art + formatting | Refined | Fixing only the image regex drops CER from 0.443 to 0.362. With the figure-stripped reference as well, it is 0.031. The analyst's 0.155 leftover was mostly the normalizer leaking the alt text |
| qwen3-vl-8b ol_p001_clean | ref_art | Confirmed | The leaked alt text makes the score better: 0.324 as scored vs 0.349 with the regex fixed |
| qwen3.5-9b ol_p001_light | reading_order + halluc | Kept (boundary is soft) | Invents a caption "**Table 1**: Example transformation…" and a "simplified version" note. It is the only unit that transcribes the figure, so it scores 0.562 even against the figure-stripped reference |

No primary class needed changing. The per-unit claims about whether content survived hold up: every reading_order unit has word_f1 ≥ 0.92. One labelling issue: `model_scored.txt` holds the text before normalization (it still contains ``` and ---).

### 2. Failure taxonomy (primary class counts)

| Model | Outliers (excl. ol_p001) | ref_artifact | reading_order | omission | halluc/loop | empty |
|---|---|---|---|---|---|---|
| glm-ocr | 4 (1) | 0 | 0 | 4 | 0 | 0 |
| nanonets-ocr2-3b | 3 (0) | 3 | 0 | 0 | 0 | 0 |
| olmocr-2-7b | 7 (4) | 3 | 3 | 0 | 0 | 1 |
| paddleocr-vl | 4 (1) | 3 | 0 | 0 | 1 | 0 |
| qwen3-vl-2b | 8 (5) | 2 | 4 | 0 | 2 | 0 |
| qwen3-vl-4b | 5 (2) | 3 | 0 | 0 | 2 | 0 |
| qwen3-vl-8b | 5 (2) | 3 | 2 | 0 | 0 | 0 |
| qwen3.5-4b | 4 (1) | 3 | 1 | 0 | 0 | 0 |
| qwen3.5-9b | 4 (1) | 2 | 1 | 0 | 1 | 0 |
| **Total** | **44 (17)** | **22** | **11** | **4** | **6** | **1** |

Truncation, formatting_residue and misrecognition never show up as a primary class. They appear only as secondary: truncation 4 times, formatting_residue 9, misrecognition 2.

### 3. How each model fails

- **glm-ocr:** It drops whole blocks without any marker. On ol_p001 it outputs only the abstract at every level (figure-stripped CER 0.180), and on att_p010_clean it drops all of Table 4 (the rest of that page scores CER 0.0004). The failures repeat exactly across levels, and the text it does emit is verbatim.
- **nanonets-ocr2-3b:** Its only outliers are the three ol_p001 units, all at the figure floor (figure-stripped CER 0.009–0.017). Figures become `<img>` descriptions, which the normalizer strips correctly.
- **olmocr-2-7b:** Most of its outliers come from output conventions, not misreads. It inlines footnotes as `\footnote{}` (about 0.18 CER on att_p004), moves the footnote block before the abstract, and emits YAML front matter in 15 of 36 outputs. One output is front matter only. Its real misreads are rare but hit the paper's own name ("oImOCR" 8 times).
- **paddleocr-vl:** One failure, and it is severe. On att_p008_light, Table 2 turns into repeated "IN THEteNet", and the prose is paraphrased with wrong facts ("15 min for each group"). The same page at clean scores 0.041.
- **qwen3-vl-2b:** Has the most outliers, in two modes. On title pages it rearranges the page into a template: it invents `## Authors` and `## Footnote` headings, drops the red permission notice, and keeps the content (word_f1 ≥ 0.925). In two units it runs away and hits the 4096-token cap, one of them on a clean image.
- **qwen3-vl-4b:** Two sentence-level loops at heavy degradation (the sentence repeats about 118 and 76 times). Both follow an accurate prefix, and one loses the last 27% of the page. These two units alone add 0.056 to its mean CER of 0.096.
- **qwen3-vl-8b:** No outlier loses content. The problems are where it places the ∗†‡ footnote block (it gets the order right at heavy), plus link targets and alt text that end up scored.
- **qwen3.5-4b:** One reading-order unit: the author block is moved to the end of the page, and CER falls from 0.339 to 0.004 once that is fixed. Its ol_p001 units are at or below the floor.
- **qwen3.5-9b:** It adds text of its own: a placeholder "[Reference 1]…" trailer ending "End of Document", a made-up figure caption, and invented URLs. Its transcription of the actual page text is near-perfect.

### 4. The olmOCR p001 page: report it separately

- **Where the CER comes from:** the page's Figure 1 has 1,103 of the 3,225 reference characters, and they are corrupted in the text layer. A flawless transcription that skips the figure scores 0.342, and even an ideal transcription that includes the figure only gets down to 0.176.
- **How much of the outlier set it makes up:** 27 of the 44 outliers.
- **How it changes mean CER** (as scored → without p001):

| Model | As scored | Without p001 |
|---|---|---|
| qwen3.5-9b | 0.053 | 0.014 |
| glm-ocr | 0.066 | 0.030 |
| nanonets-ocr2-3b | 0.039 | 0.011 |

  With p001 in, qwen3.5-9b ranks 5th; without it, 2nd.
- **It rewards lucky matches:** qwen3-vl-8b and qwen3.5-4b score below the floor because text they wrote about the figure happens to match reference words.

### 5. Problems with the benchmark's scoring

| Severity | Issue |
|---|---|
| High | The ol_p001 figure text is in the reference. It needs a figure-masked reference or a per-page exclusion. |
| High | Capping CER at 1.0 scores a loop with a correct beginning the same as an empty page, and a few such units dominate the means (5 units at CER ≥ 0.97). Loop rate and finish_reason=length should be reported separately. |
| Medium | CER is very sensitive to order. Moving one block of about 1,000 characters costs 0.3–0.6 CER. All 11 reading_order outliers have word_f1 ≥ 0.92, so word_f1 and char3_f1 should sit next to CER. |
| Medium | Models make different layout choices for the same page at different degradation levels (olmocr-2-7b att_p001: 0.004 at clean vs 0.607 at light). With 12 pages, the per-level comparisons are fragile. |
| Medium | The image regex only matches images written as `![alt](src)` with no spaces in src, so unclosed `![alt` text gets scored. Verified: normalize() keeps "! figure showing…". This affects 3 outputs, adding up to 0.08 CER or lowering it. |
| Low–Med | Markdown link targets are scored: "code (https://github.com/allenai/olmocr)" survives normalization. This affects 4 outputs. |
| Low–Med | olmOCR's YAML front matter is not stripped (15 of 36 olmocr-2-7b outputs). Telemetry also marks the front-matter-only output as empty=false. |
| Low | Some table and markup tokens survive normalization: `<fcel>`/`<lcel>` table-cell tokens, the word "footnote" from `\footnote`, and "markdown" from a ```markdown fence. The page number glued to the footer line is not stripped. |