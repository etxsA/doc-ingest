"""The retrieval contract of docingest, run against the real adapters.

The tests are the ones in docingest's ``tests/contract/test_retrieval_contract.py``; this module
imports them and overrides the three fixtures they use, so each runs with the OpenAI-compatible
embedder, the local index and the vLLM reranker, over scripted servers (no network, no GPU).
"""

import pytest
from servers import EmbeddingServer, RerankServer
from test_retrieval_contract import *

from docingest_index.embedder import OpenAICompatibleEmbedder
from docingest_index.local import LocalIndex
from docingest_index.reranker import VllmReranker
from docingest_index.settings import RerankerSettings


@pytest.fixture
def make_embedder(embedder_settings):
    server = EmbeddingServer()

    def make(query_instruction: str = ""):
        settings = embedder_settings.model_copy(update={"query_instruction": query_instruction})
        return OpenAICompatibleEmbedder(settings, transport=server.transport)

    return make


@pytest.fixture
def index(tmp_path, embedder_settings):
    return LocalIndex(
        tmp_path / "index",
        corpus=tmp_path / "corpus",
        embedder=embedder_settings,
        chunk_chars=60,
        overlap=10,
    )


@pytest.fixture
def reranker():
    return VllmReranker(RerankerSettings(), transport=RerankServer().transport)
