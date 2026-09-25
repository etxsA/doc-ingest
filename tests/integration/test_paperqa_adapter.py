import pytest

pytest.importorskip("paperqa")

from docingest.adapters.qa.paperqa import embedding_path, token_windows  # noqa: E402
from docingest.config import AppConfig  # noqa: E402


def test_token_windows_cover_long_chunks_within_limit():
    from paperqa.types import Doc, Text
    from transformers import AutoTokenizer

    try:
        tok = AutoTokenizer.from_pretrained(embedding_path(AppConfig()))
    except Exception:
        pytest.skip("embedder not in local HF cache")
    doc = Doc(docname="d", dockey="d", citation="c")
    long = " ".join(f"word{i}" for i in range(600))
    out = token_windows([Text(text=long, name="d pages 1-1", doc=doc)], tok, 254)
    assert len(out) > 1 and all(t.name == "d pages 1-1" for t in out)
    assert all(len(tok(t.text, add_special_tokens=False)["input_ids"]) <= 254 for t in out)
    assert out[0].text.startswith("word0") and out[-1].text.endswith("word599")
