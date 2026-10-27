from reflex.config import get_setting


def test_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("# comment\nOTHER=1\nYOUTUBE_API_KEY=\"abc123\"\n", encoding="utf-8")
    assert get_setting("YOUTUBE_API_KEY", f) == "abc123"


def test_environment_wins_over_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("YOUTUBE_API_KEY=fromfile", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fromenv")
    assert get_setting("YOUTUBE_API_KEY", f) == "fromenv"


def test_missing_or_empty_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("YOUTUBE_API_KEY=\n", encoding="utf-8")
    assert get_setting("YOUTUBE_API_KEY", f) is None
    assert get_setting("YOUTUBE_API_KEY", tmp_path / "nope") is None
