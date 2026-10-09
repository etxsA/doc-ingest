import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
# The retrieval contract and the fakes live in the docingest package's tests; this package
# runs the same contract against its real adapters (see test_retrieval_contract.py).
DOCINGEST_TESTS = HERE.parents[1] / "docingest" / "tests"
sys.path.insert(0, str(HERE))
# Appended, so a module of this package's tests always wins over a same-named one there.
sys.path += [str(DOCINGEST_TESTS), str(DOCINGEST_TESTS / "contract")]

from servers import EmbeddingServer, RerankServer  # noqa: E402

from docingest_index.settings import EmbedderSettings  # noqa: E402

REVISION = "a" * 40


@pytest.fixture
def embedder_settings() -> EmbedderSettings:
    return EmbedderSettings(model="test/embed", revision=REVISION)


@pytest.fixture
def embedding_server() -> EmbeddingServer:
    return EmbeddingServer()


@pytest.fixture
def rerank_server() -> RerankServer:
    return RerankServer()
