"""Shell scripts under scripts/, run for real with a stub ``uv`` on PATH (nothing served)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _serve_llm(tmp_path: Path, **env: str) -> str:
    """Run serve_llm.sh; the stub uv answers ``model-path`` and echoes the server command."""
    stub = tmp_path / "bin" / "uv"
    stub.parent.mkdir()
    stub.write_text(
        '#!/usr/bin/env bash\ncase " $* " in\n'
        '  *" model-path "*) echo /hf/snapshots/pinned ;;\n'
        '  *) echo "$@" ;;\nesac\n'
    )
    stub.chmod(0o755)
    path = f"{stub.parent}{os.pathsep}{os.environ['PATH']}"
    base = {k: v for k, v in os.environ.items() if k != "PORT" and "DOCINGEST_LLM" not in k}
    res = subprocess.run(
        ["bash", str(SCRIPTS / "serve_llm.sh")],
        env={**base, "PATH": path, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def test_serve_llm_ignores_the_litellm_model_of_docingest_ask(tmp_path):
    # Old: DOCINGEST_LLM meant both, so `ask`'s litellm string became the served model.
    out = _serve_llm(tmp_path, DOCINGEST_LLM="ollama/llama3.1")
    assert "--model /hf/snapshots/pinned " in out and "ollama" not in out


def test_serve_llm_serves_its_own_variable(tmp_path):
    out = _serve_llm(tmp_path, DOCINGEST_LLM_SERVE_MODEL="/models/other")
    assert "mlx_vlm.server --model /models/other --host 127.0.0.1 --port 8080" in out
