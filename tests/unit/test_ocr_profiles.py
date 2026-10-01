"""OCR profile clean-up and the cache key: an edit to a profile's code must reach it.

The engine fingerprints record a profile's ``code_version``, not the code of its
``postprocess`` / ``valid`` functions. The pins below make an edit to that code fail
here until someone decides whether cached OCR output has to be redone.
"""

import hashlib
import inspect
import json
import re
import types
from dataclasses import replace

import pytest

from docingest.adapters.ocr.mlx_vlm import MlxVlmOcr
from docingest.adapters.ocr.openai_compat import OpenAICompatibleOcr
from docingest.adapters.ocr.profiles import PROFILES
from docingest.domain.models import ModelRef

MODEL = ModelRef(repo_id="org/model", revision="a" * 40)

# profile -> (code_version, hash of its postprocess / valid code). When a test below
# fails: if the edit can change what the functions return, bump that profile's
# code_version in profiles.py (cached OCR output is redone); either way pin the new hash.
PINNED = {
    "markdown": (1, "7acc3e0a73f0"),
    "olmocr": (1, "2228c47f76de"),
    "nanonets": (1, "f569010b1740"),
    "glm-ocr": (1, "7acc3e0a73f0"),
    "paddleocr-vl": (1, "7acc3e0a73f0"),
}


def _names(code: types.CodeType) -> set[str]:
    """Global names a function uses, including those of its nested code (genexprs)."""
    out = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out |= _names(const)
    return out


def code_hash(*functions) -> str:
    """Source of the functions and of every module-level function / constant they reach."""
    parts: dict[str, str] = {}
    todo = [f for f in functions if f is not None]
    while todo:
        fn = todo.pop()
        key = f"{fn.__module__}.{fn.__qualname__}"
        if key in parts:
            continue
        parts[key] = inspect.getsource(fn)
        for name in sorted(_names(fn.__code__)):
            value = fn.__globals__.get(name)
            if isinstance(value, types.FunctionType) and value.__module__ == fn.__module__:
                todo.append(value)
            elif isinstance(value, re.Pattern):
                parts[name] = f"{value.pattern!r} {value.flags}"
            elif isinstance(value, str | int | float | tuple):
                parts[name] = repr(value)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:12]


def test_every_built_in_profile_is_pinned():
    assert set(PINNED) == set(PROFILES)


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_profile_code_changes_come_with_a_decision(name):
    profile = PROFILES[name]
    version, pinned = PINNED[name]
    actual = code_hash(profile.postprocess, profile.valid)
    assert (profile.code_version, actual) == (version, pinned), (
        f"the clean-up code of OCR profile {name!r} changed: if its results can change,"
        f" bump code_version in profiles.py; then pin ({profile.code_version}, {actual!r})"
    )


@pytest.mark.parametrize(
    "make",
    [
        lambda p: MlxVlmOcr(MODEL, p),
        lambda p: OpenAICompatibleOcr(MODEL, p, base_url="http://127.0.0.1:8000/v1"),
    ],
    ids=["mlx-vlm", "openai-compatible"],
)
def test_a_new_profile_code_version_is_a_new_engine_fingerprint(make):
    # Old: the fingerprint named the profile only, so a fixed clean-up kept serving the
    # cached output of the old one.
    profile = PROFILES["olmocr"]
    bumped = replace(profile, code_version=profile.code_version + 1)
    assert make(bumped).fingerprint != make(profile).fingerprint
    assert make(replace(profile)).fingerprint == make(profile).fingerprint
