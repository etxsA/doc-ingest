"""``[index] candidates`` and ``contexts``, read by docingest without the index adapter."""

import pytest

from docingest.config import (
    DEFAULT_CANDIDATES,
    DEFAULT_CONTEXTS,
    AppConfig,
    IndexConfig,
    index_config,
)
from docingest.domain.errors import DocingestError, InvalidConfigError


def test_the_defaults_are_the_measured_ones_and_an_absent_table_uses_them():
    assert (DEFAULT_CANDIDATES, DEFAULT_CONTEXTS) == (50, 10)
    assert index_config(AppConfig()) == IndexConfig(candidates=50, contexts=10)


def test_it_reads_its_two_keys_and_ignores_the_ones_that_belong_to_the_index_adapter():
    cfg = AppConfig.model_validate(
        {
            "index": {
                "candidates": 30,
                "contexts": 5,
                "dir": "data/index",
                "bm25": "never",
                "max_chunks_per_paper": 3,
                "a_key_of_a_future_adapter": True,
            }
        }
    )
    assert index_config(cfg) == IndexConfig(candidates=30, contexts=5)
    assert cfg.model_extra is not None and cfg.model_extra["index"]["bm25"] == "never"


def test_one_key_alone_keeps_the_default_of_the_other():
    assert index_config(AppConfig.model_validate({"index": {"contexts": 4}})).candidates == 50


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"candidates": 0}, r"\[index\] candidates"),
        ({"contexts": -1}, r"\[index\] contexts"),
        ({"candidates": "many"}, r"\[index\] candidates"),
    ],
)
def test_a_bad_value_names_the_table_and_the_key(table, message):
    with pytest.raises(InvalidConfigError, match=message) as info:
        index_config(AppConfig.model_validate({"index": table}))
    assert isinstance(info.value, DocingestError) and isinstance(info.value, ValueError)


def test_a_table_that_is_not_a_table_is_an_error():
    with pytest.raises(InvalidConfigError, match=r"\[index\]"):
        index_config(AppConfig.model_validate({"index": 3}))
