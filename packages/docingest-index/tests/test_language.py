import pytest

from docingest_index.language import looks_english


@pytest.mark.parametrize(
    "question",
    [
        "What is the coherence time of a transmon qubit?",
        'Paper "Attention Is All You Need": how many layers does the encoder have?',
        "how do qubits decohere in the environment",
        "T1 coherence transmon",  # a keyword query: no function words, plain ASCII
        "surface code threshold",
    ],
)
def test_english_questions(question):
    assert looks_english(question)


@pytest.mark.parametrize(
    "question",
    [
        "¿Cuál es el tiempo de coherencia de un qubit transmon?",
        "Qué es la decoherencia y por qué importa",
        "Quel est le temps de cohérence d'un qubit ?",
        "Wie lange ist die Kohärenzzeit eines Qubits?",
        "decoherencia cuántica en circuitos superconductores",  # no function words, accents
        "Qual é o tempo de coerência de um qubit?",
        "Qual è il tempo di coerenza di un qubit?",
    ],
)
def test_other_languages(question):
    assert not looks_english(question)


def test_an_english_question_that_quotes_a_foreign_title_stays_english():
    assert looks_english('What does "El gato" mean in the paper and how is it used?')
