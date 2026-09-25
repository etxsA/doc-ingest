import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # tests/fakes.py, tests/builders.py


def pytest_collection_modifyitems(config, items):
    """Model tests load multi-GB weights: opt in with DOCINGEST_MODEL_TESTS=1."""
    if os.environ.get("DOCINGEST_MODEL_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set DOCINGEST_MODEL_TESTS=1 to run model tests")
    for item in items:
        if "model" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def cfg(tmp_path):
    from docingest.config import AppConfig

    return AppConfig(output_dir=str(tmp_path / "out"), raw_dir=str(tmp_path / "raw"))
