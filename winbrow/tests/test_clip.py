"""clip_middle: head/tail, omission notice, and byte-identical full copies."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.utils.clip import clip_head, clip_middle, results_dir


def _tmp_dir(tmp_path, monkeypatch):
    target = tmp_path / "results"
    monkeypatch.setenv("WINBROW_RESULTS_DIR", str(target))
    return target


def test_under_limit_is_unchanged_and_writes_nothing(tmp_path, monkeypatch):
    target = _tmp_dir(tmp_path, monkeypatch)
    assert clip_middle("hello", 100) == "hello"
    assert not target.exists()


def test_boundary_is_unchanged(tmp_path, monkeypatch):
    _tmp_dir(tmp_path, monkeypatch)
    text = "x" * 100
    assert clip_middle(text, 100) == text


def test_clip_keeps_head_and_tail_and_saves_full_copy(tmp_path, monkeypatch):
    target = _tmp_dir(tmp_path, monkeypatch)
    text = "HEAD" + ("m" * 5000) + "TAIL"
    out = clip_middle(text, 1000, label="demo")
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "chars omitted" in out
    files = list(target.glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == text


def test_clip_head_is_json_safe_and_saves(tmp_path, monkeypatch):
    target = _tmp_dir(tmp_path, monkeypatch)
    text = '{"a": "' + ("z" * 500) + '"}'
    out = clip_head(text, 100, label="mcp")
    assert out.startswith('{"a":')
    assert "truncated" in out
    assert list(target.glob("*.txt"))[0].read_text(encoding="utf-8") == text


def test_clip_uses_explicit_save_dir(tmp_path):
    target = tmp_path / "explicit"
    text = "a" * 100 + "b" * 100
    out = clip_middle(text, 50, save_dir=target, label="x")
    files = list(target.glob("*.txt"))
    assert files and files[0].read_text(encoding="utf-8") == text
    assert str(target) in out


def test_results_dir_env_override(tmp_path, monkeypatch):
    target = _tmp_dir(tmp_path, monkeypatch)
    assert results_dir() == target
