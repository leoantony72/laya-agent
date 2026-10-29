"""Wallpaper layer: search defaults, apply flow, and tool schema."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import wallpaper
from winbrow.image_tools import ImageCandidate, ImageToolError
from winbrow.wallpaper import (
    WallpaperError,
    clean_query,
    find_wallpapers,
    get_wallpaper_tools,
    search_wallpapers,
    set_wallpaper,
)


def _hit(hit_id: str = "abc123", width: int = 1920, height: int = 1080) -> ImageCandidate:
    return ImageCandidate(
        id=hit_id,
        url=f"https://w.wallhaven.cc/full/ab/wallhaven-{hit_id}.jpg",
        page=f"https://wallhaven.cc/w/{hit_id}",
        width=width,
        height=height,
        source="Wallhaven",
    )


def test_clean_query_drops_filler_words():
    assert clean_query("something related anime wallpaper") == "anime"
    assert clean_query("wallpaper") == ""


def test_search_relaxes_then_reports_failure(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.delenv("WINBROW_WALLPAPER_QUERY", raising=False)

    def fake_search(query, **kwargs):
        calls.append((query, kwargs.get("source", "")))
        raise ImageToolError(f"no matches for '{query}'")

    monkeypatch.setattr(wallpaper, "_search_images", fake_search)
    with pytest.raises(WallpaperError) as excinfo:
        search_wallpapers("one piece luffy gear five")
    assert "No wallpaper found" in str(excinfo.value)
    wallhaven_queries = [q for q, source in calls if source == "wallhaven"]
    assert wallhaven_queries[:2] == ["one piece luffy gear five", "one"]
    assert wallhaven_queries[-1] == wallpaper._default_query()
    assert calls[-1] == ("anime", "nekosapi")


def test_search_returns_relaxed_hits(monkeypatch):
    def fake_search(query, **kwargs):
        assert kwargs.get("landscape") is True
        if query == "one piece":
            raise ImageToolError("no matches")
        return [_hit()]

    monkeypatch.setattr(wallpaper, "_search_images", fake_search)
    hits = search_wallpapers("one piece")
    assert hits and hits[0].id == "abc123"


def test_search_falls_back_to_safe_anime_source(monkeypatch):
    def fake_search(query, **kwargs):
        if kwargs.get("source") == "wallhaven":
            raise ImageToolError("wallhaven unavailable")
        assert query == "anime" and kwargs.get("source") == "nekosapi"
        return [ImageCandidate("n1", "https://cdn.nekosapi.com/a.webp", "",
                               0, 0, "NekosAPI")]

    monkeypatch.setattr(wallpaper, "_search_images", fake_search)
    hits = search_wallpapers("anime")
    assert [hit.source for hit in hits] == ["NekosAPI"]


# --------------------------------------------------------------------------- apply
def test_set_wallpaper_from_local_file_never_hits_the_network(monkeypatch, tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    source = tmp_path / "mine.png"
    PIL.new("RGB", (1920, 1080), (5, 5, 5)).save(source)
    applied: list[str] = []

    monkeypatch.setattr(wallpaper, "set_desktop_wallpaper",
                        lambda path: applied.append(str(path)) or str(path))
    monkeypatch.setattr(wallpaper, "_search_images",
                        lambda *a, **k: pytest.fail("must not search for a local path"))

    out = set_wallpaper(str(source))
    assert out["success"] and out["source"] == "local"
    assert applied == [str(source)]


def test_set_wallpaper_from_query_downloads_then_applies(monkeypatch, tmp_path):
    applied: list[str] = []
    monkeypatch.setattr(wallpaper, "search_wallpapers", lambda *a, **k: [_hit()])

    def fake_download(url, folder=None, stem="wallpaper"):
        path = tmp_path / f"{stem}.jpg"
        path.write_bytes(b"x")
        return {"path": str(path), "width": 1920, "height": 1080, "bytes": 1, "url": url}

    monkeypatch.setattr(wallpaper, "download_wallpaper", fake_download)
    monkeypatch.setattr(wallpaper, "set_desktop_wallpaper",
                        lambda path: applied.append(str(path)) or str(path))

    out = set_wallpaper("anime wallpaper")
    assert out["success"]
    assert out["source"] == "Wallhaven"
    assert out["query"] == "anime"
    assert applied == [str(tmp_path / "anime-abc123.jpg")]
    assert "Wallhaven" in out["output"]


def test_set_wallpaper_skips_candidates_that_fail(monkeypatch, tmp_path):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"x")
    attempts: list[str] = []

    def fake_download(url, folder=None, stem="wallpaper"):
        attempts.append(url)
        if url.endswith("abc123.jpg"):
            raise WallpaperError("HTTP 404")
        return {"path": str(good), "width": 1920, "height": 1080, "bytes": 1, "url": url}

    monkeypatch.setattr(wallpaper, "search_wallpapers",
                        lambda *a, **k: [_hit("abc123"), _hit("zzz999")])
    monkeypatch.setattr(wallpaper, "download_wallpaper", fake_download)
    monkeypatch.setattr(wallpaper, "set_desktop_wallpaper", lambda path: str(path))

    out = set_wallpaper("anime")
    assert out["success"]
    assert len(attempts) == 2


def test_set_wallpaper_rejects_missing_path_without_searching(monkeypatch):
    monkeypatch.setattr(wallpaper, "search_wallpapers",
                        lambda *a, **k: pytest.fail("paths must not be searched"))
    with pytest.raises(WallpaperError) as excinfo:
        set_wallpaper(r"C:\does\not\exist.jpg")
    assert "No image found" in str(excinfo.value)


def test_download_wallpaper_tool_saves_without_applying(monkeypatch, tmp_path):
    monkeypatch.setenv("WINBROW_WALLPAPER_DIR", str(tmp_path))
    monkeypatch.setattr(wallpaper, "search_wallpapers", lambda *a, **k: [_hit()])
    monkeypatch.setattr(
        wallpaper, "download_wallpaper",
        lambda url, folder=None, stem="wallpaper": {
            "path": str(tmp_path / "a.jpg"), "width": 1920, "height": 1080,
            "bytes": 1, "url": url,
        },
    )
    monkeypatch.setattr(wallpaper, "set_desktop_wallpaper",
                        lambda path: pytest.fail("download only must not apply"))

    out = find_wallpapers("anime", 1)
    assert out["success"] and out["files"]


# --------------------------------------------------------------------------- tools
def test_tools_are_native_and_point_elsewhere_for_plain_images():
    tools = {tool.name: tool for tool in get_wallpaper_tools()}
    assert set(tools) == {"set_wallpaper", "download_wallpaper"}
    for tool in tools.values():
        assert tool.impl == wallpaper.WALLPAPER_IMPL
        assert tool.script == ""
        assert "browser" in tool.description.lower()
    assert "download_images" in tools["set_wallpaper"].description
    assert "anime" in tools["set_wallpaper"].examples[0]


def test_executor_returns_failure_dict_instead_of_raising(monkeypatch):
    def explode(*args, **kwargs):
        raise WallpaperError("No wallpaper found for 'zzz'")

    monkeypatch.setattr(wallpaper, "search_wallpapers", explode)
    out = wallpaper.execute_wallpaper_tool("set_wallpaper", {"image": "zzz"})
    assert out["success"] is False
    assert "No wallpaper found" in out["output"]
