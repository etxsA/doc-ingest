"""OpenAICompatibleEmbedder against a scripted server: requests, vectors, errors."""

import base64

import httpx
import numpy as np
import pytest
from servers import EmbeddingServer, json_response, no_sleep

from docingest_index.embedder import (
    OpenAICompatibleEmbedder,
    embedder_fingerprint,
    format_query,
    normalize,
)
from docingest_index.http import RemoteServiceError
from docingest_index.settings import EmbedderSettings


def make(settings, server, sleep=None):
    return OpenAICompatibleEmbedder(
        settings, transport=server.transport, sleep=sleep or (lambda _: None)
    )


def test_documents_go_in_batches_with_the_request_of_the_measured_script(
    embedder_settings, embedding_server
):
    settings = embedder_settings.model_copy(update={"served_model": "qwen-embed", "batch_size": 4})
    texts = [f"text number {i}" for i in range(10)]
    vectors = make(settings, embedding_server).embed_documents(texts)
    assert len(vectors) == 10
    bodies = sorted(embedding_server.bodies, key=lambda b: b["input"][0])
    assert sorted(len(b["input"]) for b in bodies) == [2, 4, 4]
    for body in bodies:  # exactly the keys of embed() in the measured script, nothing else
        assert set(body) == {"model", "input", "encoding_format"}
        assert body["model"] == "qwen-embed" and body["encoding_format"] == "base64"
    sent = [
        t
        for b in sorted(embedding_server.bodies, key=lambda b: texts.index(b["input"][0]))
        for t in b["input"]
    ]
    assert sent == texts
    assert {str(r.url) for r in embedding_server.requests} == {
        "http://127.0.0.1:8002/v1/embeddings"
    }


def test_vectors_come_back_in_input_order_even_when_the_server_reverses_a_batch(embedder_settings):
    server = EmbeddingServer(shuffle=True)
    texts = [f"word{i} shared" for i in range(9)]
    settings = embedder_settings.model_copy(update={"batch_size": 3})
    got = np.asarray(make(settings, server).embed_documents(texts), dtype=np.float32)
    want = normalize(np.vstack([server.vector(t) for t in texts]))
    assert np.array_equal(got, want)


def test_vectors_are_normalized_exactly_like_the_measured_script(
    embedder_settings, embedding_server
):
    embedder = make(embedder_settings, embedding_server)
    text = "attention is all you need"
    raw = embedding_server.vector(text)
    # the script: m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-12, in float32
    expected = raw.reshape(1, -1) / (
        np.linalg.norm(raw.reshape(1, -1), axis=1, keepdims=True) + 1e-12
    )
    assert np.array_equal(np.asarray(embedder.embed_documents([text]), dtype=np.float32), expected)
    query = np.asarray(embedder.embed_query(text), dtype=np.float32)
    assert np.array_equal(query, raw / (np.linalg.norm(raw) + 1e-12))
    assert abs(float(np.linalg.norm(query)) - 1.0) < 1e-6


def test_a_question_is_one_float_input_with_no_prefix_by_default(
    embedder_settings, embedding_server
):
    make(embedder_settings, embedding_server).embed_query("What limits T1?")
    assert embedding_server.bodies == [
        {"model": "qwen-embed", "input": ["What limits T1?"], "encoding_format": "float"}
    ]


@pytest.mark.parametrize(
    ("instruction", "sent"),
    [
        ("", "What limits T1?"),
        ("Find the passage", "Instruct: Find the passage\nQuery:What limits T1?"),
        (
            "web",
            "Instruct: Given a web search query, retrieve relevant passages that answer the query"
            "\nQuery:What limits T1?",
        ),
    ],
)
def test_the_query_instruction_is_wrapped_in_the_qwen3_format(
    embedder_settings, embedding_server, instruction, sent
):
    settings = embedder_settings.model_copy(update={"query_instruction": instruction})
    embedder = make(settings, embedding_server)
    embedder.embed_query("What limits T1?")
    assert embedding_server.bodies[0]["input"] == [sent]
    assert format_query(instruction, "What limits T1?") == sent
    embedder.embed_documents(["What limits T1?"])  # documents never get it
    assert embedding_server.bodies[1]["input"] == ["What limits T1?"]
    assert embedder.query_instruction == (
        ""
        if not instruction
        else settings.query_instruction
        if instruction != "web"
        else sent.split("\n")[0][10:]
    )


def test_the_query_instruction_is_not_in_the_fingerprint_but_the_model_is(embedder_settings):
    plain = embedder_fingerprint(embedder_settings)
    assert plain == embedder_fingerprint(
        embedder_settings.model_copy(
            update={"query_instruction": "web", "base_url": "http://x/v1", "batch_size": 8}
        )
    )
    assert plain != embedder_fingerprint(
        embedder_settings.model_copy(update={"revision": "b" * 40})
    )
    assert plain != embedder_fingerprint(
        embedder_settings.model_copy(update={"model": "test/other"})
    )
    assert embedder_settings.revision in plain and "test/embed" in plain


def test_no_texts_means_no_request(embedder_settings, embedding_server):
    assert make(embedder_settings, embedding_server).embed_documents([]) == []
    assert embedding_server.requests == []


def test_a_server_that_answers_floats_instead_of_base64_is_accepted(embedder_settings):
    server = EmbeddingServer()
    server.replies.append(
        json_response(200, {"data": [{"index": 0, "embedding": [3.0] + [4.0] + [0.0] * 14}]})
    )
    [vector] = make(embedder_settings, server).embed_documents(["x"])
    assert vector[:2] == pytest.approx([0.6, 0.8])


def test_batches_of_different_vector_lengths_are_an_error(embedder_settings):
    server = EmbeddingServer()
    server.replies.append(json_response(200, {"data": [{"index": 0, "embedding": _vector(16)}]}))
    server.replies.append(json_response(200, {"data": [{"index": 0, "embedding": _vector(8)}]}))
    settings = embedder_settings.model_copy(update={"batch_size": 1, "concurrency": 1})
    with pytest.raises(RemoteServiceError, match="different lengths"):
        make(settings, server).embed_documents(["a", "b"])


def _vector(n=16):
    return base64.b64encode(np.ones(n, dtype=np.float32).tobytes()).decode()


@pytest.mark.parametrize(
    "reply",
    [
        {"error": "nope"},
        {"data": []},
        {"data": [{"index": 1, "embedding": _vector()}]},
        {"data": [{"index": 0, "embedding": _vector()}, {"index": 0, "embedding": _vector()}]},
        {"data": [{"index": 0}]},
        {"data": [{"index": 0, "embedding": "!!not base64!!"}]},
        {"data": [{"index": 0, "embedding": _vector(15)}, {"index": 1, "embedding": _vector(16)}]},
    ],
)
def test_a_reply_that_is_not_the_embeddings_asked_for_is_an_error(embedder_settings, reply):
    server = EmbeddingServer()
    server.replies.append(json_response(200, reply))
    with pytest.raises(RemoteServiceError, match=r"not 2 embedding"):
        make(embedder_settings, server).embed_documents(["a", "b"])


def test_busy_servers_are_retried_with_a_growing_pause(embedder_settings):
    pauses, sleep = no_sleep()
    server = EmbeddingServer()
    server.replies += [json_response(503, {"error": "busy"}), json_response(429, {"error": "slow"})]
    assert len(make(embedder_settings, server, sleep).embed_documents(["a"])) == 1
    assert pauses == [1.0, 2.0] and len(server.requests) == 3


def test_connection_errors_are_retried_then_reported(embedder_settings):
    pauses, sleep = no_sleep()
    server = EmbeddingServer()
    server.replies += [httpx.ConnectError("refused")] * 3
    settings = embedder_settings.model_copy(update={"retries": 2})
    with pytest.raises(RemoteServiceError, match="unreachable after 3 attempt"):
        make(settings, server, sleep).embed_query("x")
    assert pauses == [1.0, 2.0]


def test_a_client_error_is_not_retried_and_names_the_status(embedder_settings):
    server = EmbeddingServer()
    server.replies.append(json_response(400, {"error": {"message": "input too long"}}))
    with pytest.raises(RemoteServiceError, match="HTTP 400") as caught:
        make(embedder_settings, server).embed_query("x")
    assert caught.value.status == 400 and "input too long" in str(caught.value)
    assert len(server.requests) == 1


def test_an_api_key_is_read_from_the_named_variable_and_sent_as_bearer(
    embedder_settings, monkeypatch
):
    settings = embedder_settings.model_copy(update={"api_key_env": "TEST_EMBED_KEY"})
    server = EmbeddingServer()
    embedder = make(settings, server)
    monkeypatch.delenv("TEST_EMBED_KEY", raising=False)
    with pytest.raises(RemoteServiceError, match="'TEST_EMBED_KEY', not set"):
        embedder.embed_query("x")
    assert server.requests == []
    monkeypatch.setenv("TEST_EMBED_KEY", "s3cret")
    embedder.embed_query("x")
    assert server.requests[0].headers["authorization"] == "Bearer s3cret"
    server.replies.append(json_response(401, {"error": "bad key"}))
    with pytest.raises(RemoteServiceError, match="check api_key_env"):
        embedder.embed_query("x")


def test_a_redirect_is_an_error_not_a_replay(embedder_settings):
    server = EmbeddingServer()
    server.replies.append(httpx.Response(302, headers={"location": "http://elsewhere/"}))
    with pytest.raises(RemoteServiceError, match="HTTP 302"):
        make(embedder_settings, server).embed_query("x")
    assert len(server.requests) == 1


def test_a_reply_that_is_not_json_is_an_error(embedder_settings):
    server = EmbeddingServer()
    server.replies.append(httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(RemoteServiceError, match="non-JSON"):
        make(embedder_settings, server).embed_query("x")


def test_the_settings_default_to_the_local_servers_url():
    embedder = OpenAICompatibleEmbedder(EmbedderSettings(base_url="http://127.0.0.1:9000/v1/"))
    assert embedder.url == "http://127.0.0.1:9000/v1/embeddings"
