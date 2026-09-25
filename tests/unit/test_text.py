from docingest.domain.text import (
    alpha_ratio,
    clean_text_layer,
    garbage_ratio,
    split_pages,
)


def test_garbled_and_symbolic_text_detection():
    assert garbage_ratio("(cid:12)(cid:7)(cid:99) ok") > 0.5
    assert garbage_ratio("clean text") == 0
    assert garbage_ratio("transduc\x02tion") == 0  # pdfium hyphen marker is not garbage
    assert alpha_ratio("∑∫≈ 1234 ## %%") < 0.5
    assert alpha_ratio("plain english words") == 1.0


def test_clean_text_layer_handles_pdfium_hyphen_marker():
    raw = (
        "The transduc\x02tion model. Transduction works.\r\n"
        "A sequence\x02aligned RNN between 2019-\r\n2020 and Qwen-2.5-\r\nVL-7B."
    )
    out = clean_text_layer(raw)
    assert "\x02" not in out
    assert "transduction model" in out  # joined: the word occurs elsewhere
    assert "sequence-aligned" in out  # compound kept: no "sequencealigned" anywhere
    assert "2019-2020" in out and "Qwen-2.5-VL-7B" in out  # identifiers keep their hyphen


def test_clean_text_layer_uses_document_vocabulary():
    assert clean_text_layer("transduc\x02tion", vocab={"transduction"}) == "transduction"
    assert clean_text_layer("transduc\x02tion", vocab=set()) == "transduc-tion"


def test_split_pages_roundtrip_ignores_marker_like_text():
    md = (
        "# T\n\n<!-- page 1 | method=text_layer -->\nhello <!-- page 9 --> world\n\n"
        "<!-- page 2 | method=vlm_ocr -->\nsecond\n"
    )
    assert split_pages(md) == {1: "hello <!-- page 9 --> world", 2: "second"}
