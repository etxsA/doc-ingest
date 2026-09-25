import json

import pytest
from builders import LONG, text_pdf
from PIL import Image

from docingest.adapters.datasets.synthetic import make_scan
from docingest.adapters.ocr.mlx_vlm import fit_image
from docingest.adapters.ocr.profiles import PROFILES, profile_for


def test_make_scan_is_byte_identical_across_runs(tmp_path):
    src = text_pdf(tmp_path / "d.pdf", LONG)
    t1 = json.loads(make_scan(src, tmp_path / "s1.pdf", [0]).read_text())
    t2 = json.loads(make_scan(src, tmp_path / "s2.pdf", [0]).read_text())
    assert t1["scan_sha256"] == t2["scan_sha256"]
    data = (tmp_path / "s1.pdf").read_bytes()
    assert b"/CreationDate" not in data and b"/ModDate" not in data
    with pytest.raises(ValueError, match="out of range"):
        make_scan(src, tmp_path / "s3.pdf", [5])


def test_fit_image_flattens_transparency_and_bounds_size():
    img = Image.new("RGBA", (4000, 1000), (0, 0, 0, 0))
    out = fit_image(img, 1600)
    assert out.getpixel((5, 5)) == (255, 255, 255) and max(out.size) == 1600


def test_profiles_postprocess():
    olm = PROFILES["olmocr"].postprocess("---\nprimary_language: en\n---\n# Title\nBody")
    assert olm == "# Title\nBody"
    assert PROFILES["nanonets"].postprocess("Text <page_number>3</page_number>") == "Text 3"
    assert PROFILES["markdown"].postprocess("<think>hmm</think>```markdown\n# A\n```") == "# A"
    assert profile_for("markdown", max_side=900).max_side == 900
    with pytest.raises(ValueError, match="unknown OCR profile"):
        profile_for("nope")
