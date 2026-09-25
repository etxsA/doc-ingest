import json

from builders import LONG
from typer.testing import CliRunner

from docingest.entrypoints.cli import app


def test_cli_batch_survives_bad_file_and_writes_index(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a_note.md").write_text("# Note\n" + LONG)
    (raw / "b_corrupt.pdf").write_bytes(b"%PDF-1.4\n1 0 obj\n<<")  # truncated
    (raw / "c_note.txt").write_text(LONG)
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'output_dir = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(app, ["ingest", str(raw), "--config", str(cfg_path)])
    assert result.exit_code == 1, result.output
    index = json.loads((tmp_path / "out" / "index.json").read_text())
    assert sorted(v["source_name"] for v in index.values()) == ["a_note.md", "c_note.txt"]
    failed = result.output.split("Failed inputs", 1)
    assert len(failed) == 2 and "b_corrupt.pdf" in failed[1] and "DocumentOpenError" in failed[1]


def test_cli_rejects_invalid_max_pages(tmp_path):
    result = CliRunner().invoke(app, ["ingest", str(tmp_path), "--max-pages", "0"])
    assert result.exit_code != 0


def test_adapters_command_lists_ports():
    result = CliRunner().invoke(app, ["adapters"])
    assert result.exit_code == 0 and "mlx-vlm" in result.output and "openai-compatible" in result.output
