"""Read settings from the environment, falling back to a local `.env` file.

The key is never printed, logged, or put in a URL (it travels in a header).
"""
from __future__ import annotations

import os
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def get_setting(name: str, env_file: Path | None = None) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    candidates = [env_file] if env_file else [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    for p in candidates:
        if p is not None:
            v = _read_env_file(p).get(name)
            if v:
                return v
    return None


def youtube_keys(env_file: Path | None = None) -> list[str]:
    """YOUTUBE_API_KEY, then YOUTUBE_API_KEY_1, _2, ... in order, without duplicates.

    Use keys from different Google Cloud projects: keys in one project share a single quota.
    """
    names = ["YOUTUBE_API_KEY"] + [f"YOUTUBE_API_KEY_{i}" for i in range(1, 10)]
    out: list[str] = []
    for n in names:
        v = get_setting(n, env_file)
        if v and v not in out:
            out.append(v)
    return out
