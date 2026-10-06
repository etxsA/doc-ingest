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
    assert (
        result.exit_code == 0
        and "mlx-vlm" in result.output
        and "openai-compatible" in result.output
    )


def test_a_missing_explicit_config_is_an_error_not_the_defaults(tmp_path):
    typo = tmp_path / "remote-ocr.tmol"
    wide = {"COLUMNS": "300"}  # keep rich's error panel on one line per message
    result = CliRunner().invoke(app, ["adapters", "--config", str(typo)], env=wide)
    assert result.exit_code == 2 and "does not exist" in result.output
    result = CliRunner().invoke(app, ["ingest", str(tmp_path), "--config", str(typo)], env=wide)
    assert result.exit_code == 2 and "does not exist" in result.output
    # Old: the bench sub-app opened the path itself, a traceback with exit code 1.
    for cmd in (["candidates"], ["report", "--run-id", "r"], ["run", "--run-id", "r"]):
        result = CliRunner().invoke(app, ["bench", *cmd, "--config", str(typo)], env=wide)
        assert result.exit_code == 2 and "does not exist" in result.output, cmd


def test_an_unusable_crawl_query_is_reported_not_a_traceback(tmp_path):
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text(f'raw_dir = "{tmp_path / "raw"}"\noutput_dir = "{tmp_path / "out"}"\n')
    result = CliRunner().invoke(app, ["crawl", "ids: ,", "--no-ingest", "-c", str(cfg_path)])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert "search failed" in result.output and "InvalidQueryError" in result.output
    assert not (tmp_path / "raw").exists()  # nothing was fetched


def test_adapters_command_lists_a_broken_plugin_instead_of_crashing(monkeypatch, tmp_path):
    class BrokenEP:
        name = "fancy-ocr"
        value = "brokenplug_mod:factory"

        @staticmethod
        def load():
            raise ModuleNotFoundError("No module named 'some_optional_gpu_lib'")

    monkeypatch.setattr(
        "docingest.bootstrap.entry_points",
        lambda group: [BrokenEP] if group == "docingest.ocr" else [],
    )
    runner = CliRunner(env={"COLUMNS": "200"})  # keep table cells on one line
    result = runner.invoke(app, ["adapters"])
    assert result.exit_code == 0, result.output
    assert "fancy-ocr (plugin)" in result.output and "broken" not in result.output
    cfg_path = tmp_path / "cfg.toml"
    cfg_path.write_text('[adapters]\nocr = "fancy-ocr"\n')
    result = runner.invoke(app, ["adapters", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert "fancy-ocr (broken: ModuleNotFoundError" in result.output
