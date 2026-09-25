# olmOCR-Bench (screening subset): failure analysis per model

Run `screen`, 42 PDFs (6 per category), official scorer olmocr 0.4.27. Three analysts re-ran the
scorer with per-test failure output on a scratch copy; a verifier re-checked the quantified claims.
Findings led to pipeline 0.3.1 changes (profiles run each model the way its authors do) and to the
extra candidate `olmocr-2-7b-v2`.

# olmOCR-Bench screen: failure-mode synthesis

## Verification
I re-checked these claims myself and they hold:
- **olmocr-2-7b rescore:** 93 of 281 tests fail. For 5 candidates, my lists of failed tests match the analysts' exactly.
- **Header-only pages:** 9 pages returned only the header (8 at 33 tokens, 1 at 46). All 9 ended with `stop`. They account for 46 fails, including 8 baseline fails.
- **Counterfactual scores:** 80.3, 75.3 and 66.9 all reproduce.
- **Prompt order:** our setup puts the image first. mlx-vlm maps `qwen2_5_vl → LIST_WITH_IMAGE_FIRST`, and openai_compat.py line 222 sends the image first. The official `build_page_query` puts the text first and retries up to 8 times.
- **Telemetry:** the `empty` field is stale.
- **glm-ocr tables:** all 22 table fails say "No tables found".
- **Ground truth:** two tests have errors.
- **nanonets:** its math on 3 old-scan pages has no LaTeX delimiters, and one page loops (one 8-word sequence repeated 241 times).
- **qwen3-vl-8b:** 14 headers/footers fails.

Corrections to the analyst reports:
- **paddleocr-vl tables:** 27 fails (not 28) say "No tables found". The other 3 come from splitting cells.
- **nanonets `<footer>` tags:** they appear on 4 of 6 headers/footers pages, not 5.
- **BASF page:** it holds 9 of the headers/footers tests, not 10. Only 5 of the 8 other models fail 7 or more of them; qwen3.5 fails 1 and glm fails 0.
- **paddleocr-vl, 4_pg48:** 12 fails, not 11.

## Failure modes per model
Category codes used below:
- AM: arxiv_math
- OSM: old_scans_math
- TB: tables
- OS: old_scans
- HF: headers_footers
- MC: multi_column
- LT: long_tiny_text

Every model fails 13–20 OS tests (handwriting and scan misreads). 12 of these tests fail for all top four models, including the "gropings" ground-truth error.

**qwen3.5-9b** (83.0; 43 fails)
| Mode | Fails |
|---|---|
| Running head/footer kept | HF 3 |
| Left column skipped | MC 4 |
| Header-row merge; LaTeX-wrapped range | TB 2 |
| Entries split into headings; typo "corrected" | LT 4 |

**qwen3.5-4b** (80.2; 49)
| Mode | Fails |
|---|---|
| JSME header, BASF footer, PCI head | HF 6 |
| First table dropped (pg49) | TB 5 |
| Misreads, hyphenation, inline LaTeX | MC 5 |

**qwen3-vl-8b** (79.4; 49)
| Mode | Fails |
|---|---|
| Header blocks kept (BASF 8, JSME 4, J. Oleo 2) | HF 14 |
| Table as whitespace rows (a692) | TB 5 |

**glm-ocr** (75.2; 68)
| Mode | Fails |
|---|---|
| Tables dropped/flattened, 4 PDFs | TB 22 |
| Symbol misreads (10 on 3_pg39) | OSM 15, AM 11 |

**qwen3-vl-4b** (69.9; 76)
| Mode | Fails |
|---|---|
| Old scans (letter skipped, 90.pdf) | OS 18 |
| Running heads, page numbers | HF 15 |
| Math symbol slips | AM 13, OSM 12 |
| Checkbox column merged (cefac431) | TB 6 |

**nanonets-ocr2-3b** (69.6; 93)
| Mode | Fails |
|---|---|
| Math as undelimited text (3 pages) | OSM 37 of 44 |
| Page furniture kept | HF 20 |
| 8000-token loop | 1 AM page |

**qwen3-vl-2b** (68.4; 83)
| Mode | Fails |
|---|---|
| Math misreads (15 on 3_pg39) | OSM 23 |
| Column omitted (4397 vs 8909 chars) | LT 13 |
| Stopped early; substitutions | MC 8 |

**olmocr-2-7b** (64.4; 93)
| Mode | Fails |
|---|---|
| Header-only output, 9 pages | 46 (8 baseline) |
| Hard tests shared with ≥5 others | 28 |
| BASF control box copied | HF 7 |
| Plain-text / malformed tables | TB 10 |

**paddleocr-vl** (55.3; 117)
| Mode | Fails |
|---|---|
| No table markup (27), cell splits (3) | TB 30 |
| Page furniture kept | HF 20 |
| Bracket/overline structure (12 on 4_pg48) | OSM 23 |
| Stray CJK tokens | baseline 2 |

## olmOCR-2 gap (64.4 here vs 82.4 published)
**Pipeline issues (fixable)**
- **No check that the output is valid.** Our retry only happens when the output hits the token limit. The official pipeline parses the header and retries up to 8 times. The 9 header-only pages cost 46 tests. Estimated scores if fixed:
  - 66.9 if only the 8 baseline tests on those pages pass.
  - 73.9–76.8 if we substitute each other model's results on those pages (their average gives 75.3).
  - 80.3 at most, if every test on those pages passes.
- **Image-first prompt order.** The official setup puts the text first. This plausibly causes the format drift: all 18 saved raw outputs start with a code fence or "# Primary language". Impact not quantified.
- **Clean-up misses ```json fences.** The regex `_OPEN_FENCE` has no `json` case, so 4 pages keep the header. The analyst simulated stripping it: the score drops to 64.1.
- **First-attempt temperature.** Ours is 0.0, the official one is 0.1. Impact not quantified.

**Runtime differences (not quantified)**
- The model runs 4-bit on MLX; the official setup uses FP8 on vLLM.
- Our retry has 3 steps (repetition penalty 1.15, then 1.25). The official one has 8 steps at 1.05.
- Page c8cdd4 took 205 s for 1470 tokens against a median of about 40 tok/s, which implies a retry that telemetry doesn't show. It still fails 3 table tests.

**Model behaviour**
- 47 fails are on pages that did produce text. 28 of them are also failed by at least 5 of the 8 other models, 16 by 2–4, and 3 only by olmocr.
- None of the 42 outputs contains an HTML table.
- About 7 points would remain after the pipeline fixes. That can't be separated from 4-bit loss and subset noise (per-PDF bootstrap CI [52.4, 74.8]).

## Prompt-dependent differences
- **HF:** the Qwen prompt says "Omit page headers/footers", so their 3–15 fails measure how well each model follows it. glm scores 100 with no such instruction. nanonets (told to tag page numbers) and paddle (prompt `OCR:`) both fail 20.
- **TB:** glm and paddle get whole-page text prompts. Their official setups use a separate table prompt on cropped regions, so their 22 and 30 fails measure text mode. nanonets, asked for HTML, fails 0.
- **MC/TB/LT:** the generic prompt's "$...$" and "headings (#)" plausibly cause LaTeX-wrapped plain text and dictionary entries split into headings.
- **OS:** these results don't depend on the prompt.
- **nanonets math:** its prompt asks for LaTeX, so the undelimited math is the model's own behaviour.

## Harness issues
1. `refresh_outputs` never updates the telemetry `chars`/`empty` fields. The report therefore shows olmocr at 0.0% empty; the real figure is 8 of 42, plus 1 header-only page.
2. The model's raw text is never saved, because `transcribe` returns already cleaned text. The `refresh_outputs` docstring says the opposite.
3. This run's telemetry has no `attempts` field. Timing suggests unlogged retries on both pages that hit the token limit:
   - nanonets: 8000 tokens in 283 s, against a median of 67 tok/s.
   - qwen3-vl-8b: 4096 tokens in 302 s, against 35 tok/s.
4. The clean-up misses ```json fences. For nanonets it strips the tags but keeps their contents, and ignores `<footer>` entirely; 4 failed HF tests are inside `<footer>` tags.
5. Two ground-truth errors, each failed by all 9 models:
   - One test expects "submersda"; the PDF's text layer says "porção submersa".
   - One test expects "groupings"; the rendered scan reads "gropings".

   I did not re-check the other ground-truth issues the analysts reported.
6. The scorer's baseline repeat check did not flag the nanonets loop page.
7. Retries happen only on the token limit, and olmocr gets the image before the text.
8. With 6 PDFs per category, one PDF holds 9 of the 25 HF tests.

Files are in <local scratch dir>/olmfail/synth/

## Analyst reports

### olmocr2

**olmocr-2-7b: why it scores 64.4 here vs 82.4 published**

About half of the gap comes from one failure mode that our pipeline could fix. The model's output drifts away from the format it was trained on, and on 9 of 42 pages it returns only its metadata header (the "front matter") and no text. The rest of the gap is ordinary model errors that the other models also make, plus the small subset. I re-ran the official scorer on a scratch copy and got 64.45%: 93 of 281 tests fail.

Where the 93 failures are:
- **46 are on the 9 pages with no text.** These include all 8 failed baseline tests.
  - 8 pages stopped after 33 tokens with only a header block, e.g. "```yaml\nprimary_language: en ... is_diagram: False\n```". The clean-up then leaves the file empty.
  - long_tiny_text/13_pg162 returned only a ```json header block (46 tokens).
  - These are normal text pages. Other models pass them: for example 4_pg281 and 3b18 fail 0 tests for 7 of the 8 other models.
  - All 42 records say finish_reason stop, so no retry was ever triggered.
- **47 are on pages that did produce text.** Of those, 28 tests are also failed by at least 5 of the 8 other models, 16 are failed by 2–4 of them, and only 3 are specific to olmocr.

**The model is not writing the format it was trained on**
- The 18 saved raw outputs all start with "```yaml", "```markdown" or "# Primary language". The trained format starts with "---\nprimary_language:".
- 4 more cleaned files still contain a ```json header block, 3 of them followed by a "**Text:**" label.
- The 8 empty pages are exactly the 8 raw outputs that start with a ```yaml fence and have no "---" lines.
- None of the 42 outputs contains an HTML table, even though the prompt asks for HTML tables and the model was trained on them. 8 outputs use Markdown pipe tables instead.
- One output copies the prompt's placeholder literally: "page_startx_starty_width_height.png".
- The same thing shows in the synthetic suite: at least 16 of 36 outputs have code fences, and paperqa2_p011_heavy is also header-only (34 tokens).

**(a) Our pipeline and clean-up: fixable**

1. **Prompt order.** mlx-vlm puts the image before the text for this model type (prompt_utils.py maps qwen2_5_vl to image-first). Our OpenAI-compatible adapter does the same (openai_compat.py lines 220–224). The official setup puts the text first everywhere:
   - training config `qwen25_vl_olmocrv4_rotation_1epoch_mix_1025_filtered.yaml` sets `prompt_first: true`;
   - the RL training script (grpo_train.py) and the inference code (`build_page_query`) put text before image.
   - **Fix:** add a `prompt_first` flag to the profile, on for olmocr. In `MlxVlmOcr.transcribe`, build the prompt with `self._processor.apply_chat_template([{"role":"user","content":[{"type":"text","text":P},{"type":"image"}]}], tokenize=False, add_generation_prompt=True)`. Swap the order in openai_compat.py too.
   - **Effect:** it probably causes the format drift, but I can't prove that without running the model.
2. **No check that the output is valid before accepting it.** The official pipeline rejects any output that doesn't start with "---\n" plus all five header keys, and retries at temperatures 0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0 (up to 8 tries). Ours retries only when the output hits the token limit.
   - **Fix:** in the `_attempts` loop, also retry when the cleaned text has no letters or digits, or when the header can't be parsed. Optionally, start the assistant turn with "---\n" (similar to the official `--guided_decoding` option).
   - **Effect:** this would have caught exactly the 9 pages. If all their tests passed the score would be 80.3 (upper bound). If they pass at the other 8 models' average rate on those same tests, the score would be 75.3, about 33 tests flipped. Substituting each other model's result gives 73.9–76.8. Just making those pages non-empty flips the 8 baseline tests: 66.9.
3. **Clean-up leaves the ```json header block and "**Text:**" label.** I simulated stripping them and re-scored: no content test flips, and one baseline test goes from pass to fail because 13_pg162 becomes empty (64.4 → 64.1). Today's score is slightly inflated by that leftover. Separately, 4 "absent" tests pass only because their pages are empty.
4. **Bookkeeping.** `refresh_outputs` rewrites the output files but not the telemetry `chars`/`empty` fields. The report therefore shows 0.0% empty for olmocr on olmOCR-bench; the real figure is 8 of 42 (19%). Also, the true model output is never saved, only the cleaned text, so the format of 20 of 42 pages can't be checked.
5. **First attempt is greedy (temperature 0.0); the official first attempt is 0.1.** Cheap to align; the effect is probably small and not quantified.

**(b) Runtime differences we can't fix cheaply**
- **Quantization:** the language model here is 4-bit (MLX, group size 64); the vision part is BF16. The official bench runner defaults to the FP8 model on vLLM. 4-bit may cause or add to the format drift; separating it from the prompt-order effect needs a rerun.
- **Retry ladder:** the official one allows up to 8 attempts, some in parallel. On table c8cdd4 I infer one hidden retry: 205 s for 1470 tokens against a median of 40.6 tok/s implies about 6,850 extra tokens, i.e. a first attempt that hit the 8,000-token limit. Our retry raises the repetition penalty to 1.15; the official one keeps 1.05. That page still produced repeated rows ("24.3 | 7.8 | 10.6 | ND | ND") and fails 3 table tests.
- **Rotation retry:** no effect here (0 tests). Every header reports rotation valid, rotation 0, and no page in the subset is rotated.
- **Unchanged:** image size (1288 px; ours is downscaled from a 2048 px render instead of rendered directly), max tokens 8000, repetition penalty 1.05 from the model's settings, and figure-tag handling (kept on both sides, 0 tests).

**(c) Genuine model behaviour (47 failures on pages with text)**
- **Headers/footers, page 8dd368:** 7 failures; it copies the document-control box ("issued: 2019-04-17", "Revision 2"). 6 of the 8 other models fail it too. This single page accounts for the whole headers/footers drop (72.0 vs 96.1 published).
- **Order tests (36%, 9 of 25 pass):** 16 failures.
  - 7 are on empty pages.
  - 2 on old_scans/34, which misreads "Your Ky heart" as "Your thy heart".
  - 1 on old_scans/29, a capitalisation difference against the answer key.
  - 6 on multi-column pages: wrong reference page ranges ("157–164" vs "81–98"), a changed spelling ("entrepreneuriaul"), an omitted sentence, and one test (0904c) that all 9 models fail.
  - Fixing the empty pages caps order at 64%.
- **Tables (53.1%, 17 of 32 pass):** 15 failures.
  - 5 on 3b18, which is empty.
  - 7 on cefac431, a table written as aligned plain text with no table markup, so the scorer finds no table (3 other models also fail at least 5).
  - 3 on c8cdd4: a Markdown table with the caption merged into the header row and repeated rows.
  - The tables score would be 68.8% if 3b18 were fixed; published is 84.9.
- **Math:** misread symbols, e.g. `\epsilon_\xi` written for `\iota_\xi`. The arxiv score of 83.3 matches the published 83.0.

**Residual gap.** Even with the pipeline fixes, about 7 points would remain (75.3 estimated vs 82.4). That is ordinary model errors, possible 4-bit loss, and subset noise: 6 PDFs per category, overall bootstrap CI [52.4, 74.8]. The split between them can't be measured from this data.

**Diagnostic run, once the GPU benchmark is finished.** Re-run only the 9 empty pages and the 13 pages with code-fenced headers (22 pages, about 10 minutes per setting), text-first vs image-first. That would show whether prompt order or quantization causes the drift.

Everything is in `<local scratch dir>/olmfail/olmocr27b/`:
- `failed_base.jsonl`, `score_base.log` – baseline re-score with the failed-test list
- `counterfactual.py`, `fails_all.json` – what-if estimates and per-test failures for all 9 models
- `bench/olmocr-2-7b-fix1/`, `score_fix1.log`, `failed_fix1.jsonl` – the simulated clean-up fix
- `degenerate_sheet.png`, `sheet2.png` – page renders

### categories

**Top-group gaps on the olmOCR-bench subset (qwen3.5-9b, qwen3.5-4b, qwen3-vl-8b, glm-ocr)**

I ran the official scorer's test classes on the scored outputs for these four candidates and got per-test results with the scorer's explanations. Every category score matches `report.md`. Only olmocr-2-7b has files under `raw_outputs`, so the later clean-up fix did not change any of these four.

Each category has 6 PDFs, so failures cluster by page. In headers_footers, one page (BASF) holds 10 of the 25 tests.

## headers_footers: glm 100, 9b 88, 4b 76, 8b 44
All 24 scored tests are "absent" tests (plus one baseline test): the header or footer text must not appear in the output.
- **qwen3-vl-8b (14 fails, from 3 PDFs):**
  - **BASF spec sheet:** it copied the whole header block ("Isocyanate & Precursors Europe / Revision 2 / supersedes: Revision 1 / issued: 2019-04-17 …"), failing 7 tests. The 'CM-FECL3-E' test survived only because it wrote "CM-FECL3 -E".
  - **Japanese JSME page:** it kept "The Japan Society of Mechanical Engineers", "Copyright ©2007 社団法人 日本機械学会" and the footer "NII-Electronic Library Service" (4 fails).
  - **J. Oleo Sci. page:** it kept the running head "W. J. Zhang, K. Yang, C. X. You, et al." and the footer "J. Oleo Sci. 64, (3) 299-307 (2015)" (2 fails).
  - **BASF footer:** it kept "More information? Please visit us at www.monomers.basf.com" (1 fail).
- **qwen3.5-4b (6 fails):** the same 4 JSME tests, the BASF footer, and the PCI running header "PEER COMMUNITY IN ECOLOGY | DOI: 10…".
- **qwen3.5-9b (3 fails):** the BASF footer, plus the J. Oleo Sci. header and footer. It even made the running head a heading: "# W. J. Zhang, K. Yang…".
- **glm-ocr:** 0 fails. It drops every header, footer and watermark.

**Prompt vs capability:**
- The Qwen models all got GENERIC_PROMPT, which says "Omit page headers/footers". Their spread is therefore how well each follows that instruction: 9b complies best, 8b ignores it on 3 of 6 pages.
- glm's prompt is only "Text Recognition:" and says nothing about headers, so its 100% comes from the model itself, not the prompt.
- For context, the generalist models whose prompts contain no omit instruction score 20% (paddleocr-vl with "OCR:", and nanonets, whose prompt asks it to tag page numbers).
- The three Qwen models all treated the BASF bottom line as body text.

## table_tests: 9b 94, 4b 84, 8b 84, glm 31
The scorer only reads Markdown pipe tables or HTML `<table>`.
- **glm-ocr:** 22 fails, all "No tables found in the content", on 4 PDFs. It is not an HTML-versus-Markdown issue: on the 2 other PDFs glm wrote Markdown pipe tables and passed 10 of 10.
  - **a692 (sgl paper):** Table 2 and its caption are left out entirely; the output starts at "5. Software" (5 tests).
  - **6871 (palm oil):** the large TABLE 2 and its title and notes are left out. The output ends at "(Zou et al.," with finish_reason "stop" (5 tests).
  - **a3a448 (yawning):** the table comes out as plain lines such as "Situation Likely Unlikely Unknown" (5 tests).
  - **pg49 (attribute tables):** the tables come out as bullets such as "- driverTypeAddress Address of the Driver Type" (7 tests).
- **qwen3-vl-8b (5 fails, all a692):** it wrote the table as plain whitespace rows, "Cancer 3.9k 217 1.14", even though the prompt asks for Markdown tables.
- **qwen3.5-4b (5 fails, all pg49):** it left out the whole first (driver) table; its output starts at "# Controller Asset".
- **qwen3.5-9b (2 fails):**
  - It merged the "#" column into the header row ("# Attribute | Description"), so the 'Attribute' cell only matched in the second table.
  - It wrapped a plain range in LaTeX: "Sometimes True: $2.0-2.9$" (similarity 0.92).

**Prompt vs capability:**
- glm's result depends on the prompt and pipeline. GLM-OCR's model card (zai-org/GLM-OCR, fetched) lists a separate "Table Recognition:" prompt. Its official SDK first runs a layout model (PP-DocLayoutV3) and then uses a separate prompt for each region. We send the whole page with the text prompt, so 31% measures text mode, not glm's table ability. I could not test table mode, because loading models is not allowed while the GPU benchmark runs.
- The 8b and 4b misses are capability: a table without ruled lines was not recognised as a table, and one table was dropped.
- 9b's `$…$` wrapping is plausibly encouraged by the prompt line "Render math as LaTeX ($...$)".

## multi_column: 8b 80, 9b 73, glm 73, 4b 67 (15 tests; the spread is 1–2 tests)
No failure is a reading-order inversion. Every failure is "text not found", so column order was correct for all four models.
- **Bad ground truth (all four fail):** the test expects "implicações na gênese da submersda". The PDF's own text layer says "porção submersa", as all four models wrote.
- **Buffalo newsletter (all four fail, for different reasons):**
  - 9b, 4b and glm left out the photo caption "Come volunteer and stand with wild buffalo!".
  - 8b kept the caption but dropped the word "up" ("back your call with an email"), which is 3 edits against a limit of 2.
- **Kink-oscillation paper (all four fail):**
  - 9b and glm skipped the whole left column (two figures, their captions and the paragraph under them). Their output starts at the right column: "not produce any damping."
  - 4b and 8b transcribed that column but used inline LaTeX ("$r = R$"), while the test expects plain "r = R".
- **Single-model misses:**
  - 9b turned "AFTA" into "AFT${}^{\text{R…".
  - 4b misread "Cremonini" as "Concremin".
  - 4b kept line-break hyphenation ("hasil pe- nelitian") although the prompt says to omit it.
  - glm dropped a table row label, "Text-surface-based only with Answer Justification" (same table dropping as above).
- **Truncation:** 8b hit the 4096-token limit on the references page (finish_reason "length"). It did not affect the score.

**Prompt vs capability:** mixed. Leaving out captions and figure-column text is model behaviour. The LaTeX over-use is plausibly prompt-driven; the hyphenation miss is a model ignoring the prompt.

## old_scans: 8b 59, 4b 56, glm 53, 9b 50 (32 tests; the spread is 13–16 fails)
- **All four fail the same 12 tests**, so no model can score above 62.5%.
  - 9 of these are exact-match tests on cursive handwriting: 42.pdf, an 1862 letter (4 tests); 34.pdf (3); 90.pdf, a letter plus envelope (2).
  - 3 are ground-truth problems:
    - The test expects "blind groupings", but the scan reads "gropings", which all four wrote.
    - The test keeps the typewriter space in "you ? Address".
    - One test expects lowercase "at this time, after having", while a sibling test uses "At this time".
- **What separates the models (1–3 tests each):**
  - 9b transcribed the top page number "3.".
  - 9b and 4b wrote "Mr. Roosevelt," with a comma; the test expects none.
  - Only 8b read "State Department"; 9b wrote "War Department" and glm wrote "Mato Department".
  - glm misread "Roosevett" and "affection" (for "appreciation"), and transcribed only the envelope on 90.pdf.
- **Verdict:** a shared capability floor plus bad ground truth. No formatting cause, and the prompt doesn't matter here.

## Other categories where glm differs
- **glm lags in math** (arxiv_math 69 vs 8b 89; old_scans_math 78 vs 87–91). The cause is misread symbols, not how equations are delimited: e.g. "ea^\mu eb^\mu" for "ea^\mu e\mu^b", "g'O" for "g'C", "d'v'" for "d'x'". 10 of the 26 tests on one scanned page (3_pg39) fail. This is capability.
- **glm leads in long_tiny_text** (97 vs 88–91), for two reasons:
  - glm copied the printed typo "ineresting", which the ground truth also keeps. The Qwen models corrected it to "interesting" and failed.
  - 9b split dictionary entries into headings ("### LUTHER LEAGUE" followed by "A religious association…"), which breaks the expected "LUTHER LEAGUE, a religious association". The prompt's "headings (#)" plausibly encourages this.

## Common thread
- **glm-ocr, run on the whole page with "Text Recognition:",** acts as a body-text extractor. One behaviour explains both extremes: it drops page furniture (headers_footers 100) and also drops or flattens tables, captions and figure-column text (table_tests 31, several multi_column misses, the 90.pdf letter).
- **The Qwen models' header and footer results** depend on the prompt's omit instruction and on how well each model follows it. Their smaller losses come from LaTeX over-use and restructuring text into headings, both plausibly nudged by the generic prompt.

Files are in `<local scratch dir>/olmfail/topgroup/`:
- `pertest.json`: every test for the four candidates, with pass/fail and the scorer's explanation
- `score_<candidate>.log`: the official scorer's output
- `failed_<candidate>.jsonl`: the failed tests from the official `--output_failed` flag (no explanations)
- `show.py`: lists a category's tests and why each failed
- `near.py`: shows the closest matching text in each output
- `png/`: rendered pages

### rest

**Per-model failure characterization: nanonets-ocr2-3b, qwen3-vl-4b, qwen3-vl-2b, paddleocr-vl (olmOCR-bench screen subset)**

I re-ran the official scorer with `--output_failed` on a scratch copy of the bench, and the scores match the reported ones exactly. I also scored the other 5 candidates, so that each model's failures can be split into two kinds. "Model-specific" failures are tests that at least 6 of the other 8 models pass. "Hard" failures are tests that at least 5 of the other 8 models also fail.

Across the four models there was only one loop and only two baseline failures. Most of the gap comes from category-specific behaviour, described per model below.

**nanonets-ocr2-3b** (93 failures; 37 model-specific, 39 hard)
- **Math:** 29 of its 37 model-specific failures are in old_scans_math. On 3 of the 6 pages (3_pg39, 4_pg281, 5_pg281) it writes the equations as plain text with no `$`/`\(` delimiters, so the math test cannot find any equation. Example: "a^m + (1/2) Aa^(m-1)b + (1/2) Ba^(m-2)b^2". The other models write 11–22 delimited spans on those same pages.
- **Headers/footers (5/25):** it transcribes page furniture. Examples are "Page 2 / 2", "94", and the running head "W. J. Zhang, K. Yang, C. X. You, et al.". It wraps some footers in `<footer>` tags (5 of 6 pages), but our clean-up only strips `page_number`/`watermark`/`signature` tags. Its prompt asks for page numbers to be tagged, not dropped.
- **Loop:** this is the only loop among the four models. arxiv_math/2503.07373 hit the 8000-token cap after 283 s, with one 8-gram repeated 241 times. The scorer's baseline repeat check did not flag it (baseline 100%).
- **Tables:** 32/32. It outputs HTML tables.

**qwen3-vl-4b** (76 failures; only 17 model-specific, 41 hard)
- **No loops:** every page finished with `stop`, the largest at 4022 tokens.
- **Headers/footers (40%):** the prompt says "Omit page headers/footers", but it still keeps running heads and page numbers. Examples are "94 Revue d'anthropologie des connaissances – 2014/I" and "304 / J. Oleo Sci. 64…". It also keeps first-page header blocks such as the BASF revision block and the Japanese society header.
- **Tables:** all 6 table failures are on one page (cefac431). It merged the checkbox column into the attribute cell ("✓ manufactureYear") and wrote a 3-column header over 2-column rows.
- **Other:** the remaining failures are symbol-level math slips, for example dropping the |…| bars in "$\{i \in [n] : f(i) > n\} = k$". On old_scans/90 it skipped the handwritten letter entirely and output only the printed envelope (121 chars).

**qwen3-vl-2b** (83 failures; 28 model-specific, 42 hard)
- **Omissions:** it has no loops (largest output 3564 tokens, all `stop`), but it drops blocks of text.
  - On long_tiny_text/13_pg162 it skipped the whole first column: 4397 chars against 8909 for qwen3.5-9b, and 4 "present" tests fail with match ratios around 0.47.
  - On multi_column/06dda it stopped after "5 CONCLUSIONS" and dropped the reference list.
  - These omissions account for most of its model-specific long_tiny_text (9) and multi_column order (3) failures.
- **Hallucinated text:** it substitutes content, for example "Coyne, J.A., 1859" for "Darwin, C., 1859" and "Volynovia" for "Volhynia".
- **Headers/footers (68%):** better than the 4b. It drops page numbers and footers, and fails only on the BASF header block and one running head, which all four models fail.
- **Tables:** 3 failures, from merging "Likely Unlikely Unknown" into one cell.

**paddleocr-vl** (117 failures; 57 model-specific, 39 hard)
- **Baseline (2 failures):** it inserts stray CJK tokens into Latin text: "cks包装er", "palaeocint车站的roração", "of Hawaii,滑县". Tamil glyphs also appear on the references page.
- **Math:** the notable weakness is bracket and overline structure on 4_pg48 (11 failures). Example: "2x-{((x-(x-y)-\left[x-\overline{x-y}\right]-y]}".
- **Speed:** fastest of the four, at a median of 3.1 s per page.

**Does full-page use explain paddleocr-vl's tables (6%) and headers (20%)?**
- **Tables: yes, largely.** We send `OCR:` to the whole page. Upstream, tables are handled by a separate "Table Recognition:" task on layout crops; that part comes from the upstream model card, which I did not check here.
  - 28 of its 30 table failures are "No tables found in the content".
  - No output contains `<table>` or the model's table-structure tokens (`<fcel>`, `<nl>`, …). Those tokens are in its vocabulary and are not marked special, so they would have appeared in the text if the model had produced them.
  - It writes tables as space-separated lines, for example "Situation Likely Unlikely Unknown", and loses the column positions of empty cells.
  - Sometimes it drops a table entirely: on headers_footers page 6 the whole Table 4 body is missing and only the caption and footnote remain. I rendered that page to confirm the table is there.
  - Only 1 of the 6 table pages uses pipes, and there it splits multi-line cells into separate rows (the other 2 failures).
- **Headers/footers: partly.** In `OCR:` mode it transcribes page furniture verbatim on every page ("Page 2 / 2", "304", "PEER COMMUNITY IN ECOLOGY | DOI…"). In its intended pipeline, deciding which regions are headers and footers is the layout stage's job.
  - This is not an inherent limit of full-page inference. nanonets, also run full-page, scores the same 20%. glm-ocr, also full-page with a one-line "Text Recognition:" prompt, drops them and scores 100%.

Files are in <local scratch dir>/olmfail/weak4/:
- failed_{nanonets-ocr2-3b,qwen3-vl-4b,qwen3-vl-2b,paddleocr-vl}.jsonl (plus the other 5 models)
- score_*.log
- overlap.py
- hf_page6.png
