import json

from fixtures import messy_watch_rows, takeout
from reflex.cli import main


def test_cli_inspect_json(tmp_path, capsys):
    z = tmp_path / "t.zip"
    z.write_bytes(takeout(messy_watch_rows()))
    assert main(["inspect", str(z), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["watch_rows_kept"] == 11


def test_cli_reports_named_error(tmp_path, capsys):
    z = tmp_path / "t.zip"
    z.write_bytes(b"nope")
    assert main(["inspect", str(z)]) == 2
    assert "[notzip]" in capsys.readouterr().err
