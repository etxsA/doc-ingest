"""VllmReranker against a scripted /rerank server."""

import pytest
from docingest.ports import Chunk
from servers import json_response

from docingest_index.http import RemoteServiceError
from docingest_index.reranker import VllmReranker
from docingest_index.settings import RerankerSettings

INSTRUCTION = "Given a question about a research paper, retrieve the passage that answers it"


def chunk(text, n=1):
    return Chunk("d" * 64, f"{'d' * 16} pages {n}-{n}", text, n, n)


def make(server, **settings):
    return VllmReranker(
        RerankerSettings(**settings), transport=server.transport, sleep=lambda _: None
    )


def test_the_request_is_the_one_of_the_measured_script(rerank_server):
    chunks = [chunk("softmax attention weights"), chunk("qubits decohere", 2)]
    make(rerank_server).rerank("how are attention weights computed", chunks)
    [request] = rerank_server.requests
    assert str(request.url) == "http://127.0.0.1:8003/rerank"
    assert rerank_server.bodies == [
        {
            "model": "qwen-rerank",
            "query": "how are attention weights computed",
            "documents": ["softmax attention weights", "qubits decohere"],
            "instruction": INSTRUCTION,
        }
    ]


def test_scores_come_back_in_the_order_of_the_chunks_not_of_the_reply(rerank_server):
    chunks = [chunk("qubits decohere"), chunk("attention weights softmax"), chunk("unrelated")]
    scores = make(rerank_server).rerank("attention weights", chunks)
    assert scores == [0.0, 1.0, 0.0]  # the server listed index 1 first


def test_a_missing_result_scores_zero_and_no_chunks_means_no_request(rerank_server):
    rerank_server.replies.append(
        json_response(200, {"results": [{"index": 2, "relevance_score": 0.5}]})
    )
    assert make(rerank_server).rerank("q", [chunk("a"), chunk("b"), chunk("c")]) == [0.0, 0.0, 0.5]
    assert make(rerank_server).rerank("q", []) == []
    assert len(rerank_server.requests) == 1


def test_the_instruction_and_names_come_from_the_settings(rerank_server):
    reranker = make(
        rerank_server,
        served_model="my-rerank",
        instruction="Find it",
        base_url="http://127.0.0.1:9000/",
    )
    reranker.rerank("q", [chunk("a")])
    assert rerank_server.bodies[0]["model"] == "my-rerank"
    assert rerank_server.bodies[0]["instruction"] == "Find it"
    assert str(rerank_server.requests[0].url) == "http://127.0.0.1:9000/rerank"


@pytest.mark.parametrize(
    "reply",
    [
        {"detail": "x"},
        {"results": [{"index": 5, "relevance_score": 1.0}]},
        {"results": [{"index": -1, "relevance_score": 1.0}]},
        {"results": [{"index": 0}]},
        {"results": [{"index": 0, "relevance_score": "high"}]},
    ],
)
def test_a_reply_without_usable_results_is_an_error(rerank_server, reply):
    rerank_server.replies.append(json_response(200, reply))
    with pytest.raises(RemoteServiceError, match="without usable results"):
        make(rerank_server).rerank("q", [chunk("a")])


def test_server_errors_are_retried_and_then_reported(rerank_server):
    rerank_server.replies += [json_response(500, {"error": "oom"})] * 6
    with pytest.raises(RemoteServiceError, match="HTTP 500"):
        make(rerank_server).rerank("q", [chunk("a")])
    assert len(rerank_server.requests) == 6  # the first try and five retries
