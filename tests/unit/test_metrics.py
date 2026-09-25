from docingest.application.metrics import bootstrap_ci, char3_f1, normalize, score


def test_metrics_ignore_markdown_and_hyphenation():
    assert normalize("## **Bold** `x`") == "bold x"
    s = score("the quick brown fox", "# The quick brown fox")
    assert s["cer"] == 0 and s["word_f1"] == 1.0 and s["char3_f1"] == 1.0
    assert score("transduction", "transduc-tion")["cer"] == 0
    assert score("", "anything")["cer"] is None


def test_runaway_output_is_capped():
    s = score("short", "short " + "loop " * 500)
    assert s["cer"] == 1.0 and s["wer"] == 1.0


def test_char3_f1_is_order_insensitive_but_spelling_sensitive():
    assert char3_f1("alpha beta", "beta alpha") == 1.0
    assert char3_f1("alpha beta", "alfa bta") < 0.6


def test_bootstrap_ci_brackets_the_mean_and_is_deterministic():
    vals = [0.1, 0.2, 0.3, 0.4, 0.5]
    mean, lo, hi = bootstrap_ci(vals)
    assert lo <= mean <= hi and abs(mean - 0.3) < 1e-9
    assert bootstrap_ci(vals) == (mean, lo, hi)
