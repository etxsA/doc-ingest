import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # tests/fakes.py, tests/builders.py


OPT_IN = {
    "model": "DOCINGEST_MODEL_TESTS",  # loads multi-GB OCR weights (Apple Silicon)
    "network": "DOCINGEST_NETWORK_TESTS",  # talks to arXiv / Hugging Face
}


def pytest_collection_modifyitems(config, items):
    """Expensive test classes are opt-in via environment variables."""
    for marker, env in OPT_IN.items():
        if os.environ.get(env) == "1":
            continue
        skip = pytest.mark.skip(reason=f"set {env}=1 to run {marker} tests")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def cfg(tmp_path):
    from docingest.config import AppConfig

    return AppConfig(output_dir=str(tmp_path / "out"), raw_dir=str(tmp_path / "raw"))
