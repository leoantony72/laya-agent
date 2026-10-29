"""General image search/download: sources, validation, folders, no browser."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import image_tools
from winbrow.image_tools import (
    ImageToolError,
    clean_query,
    download_image,
    download_images,
    get_image_tools,
    resolve_folder,
    search_images,
    search_wallhaven,
)


def _pnpm_png(width: int = 1600, height: int = 900) -> bytes:
    PIL = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    PIL.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(buffer, "PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, raw: bytes, content_type: str = "image/png", status: int = 200):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.content = raw


# --------------------------------------------------------------------------- query
def test_clean_query_drops_request_filler():
    assert clean_query("download some pictures of a golden retriever") == "golden retriever"
    assert clean_query("save 3 images of the eiffel tower") == "eiffel tower"
    assert clean_query("images") == ""
    assert clean_query("") == ""


# --------------------------------------------------------------------------- sources
def test_wallhaven_search_filters_size_and_orientation(monkeypatch):
    captured: dict = {}

    def fake_json(url, params=None, timeout=15):
        captured.update(params or {})
        return {"data": [
            {"id": "good", "path": "https://w.wallhaven.cc/full/aa/wallhaven-good.png",
             "dimension_x": 3840, "dimension_y": 2160, "file_size": 4_000_000,
             "url": "https://wallhaven.cc/w/good"},
            {"id": "tiny", "path": "https://w.wallhaven.cc/full/bb/wallhaven-tiny.jpg",
             "dimension_x": 400, "dimension_y": 300, "file_size": 100_000,
             "url": "https://wallhaven.cc/w/tiny"},
            {"id": "portrait", "path": "https://w.wallhaven.cc/full/cc/wallhaven-p.jpg",
             "dimension_x": 1080, "dimension_y": 1920, "file_size": 900_000,
             "url": "https://wallhaven.cc/w/portrait"},
            {"id": "huge", "path": "https://w.wallhaven.cc/full/dd/wallhaven-h.jpg",
             "dimension_x": 7680, "dimension_y": 4320, "file_size": 90_000_000,
             "url": "https://wallhaven.cc/w/huge"},
        ]}

    monkeypatch.setattr(image_tools, "http_json", fake_json)
    hits = search_wallhaven("anime", 5, min_width=800, landscape=True)
    assert [hit.id for hit in hits] == ["good"]
    assert captured["purity"] == "100"        # SFW is never relaxed
    assert captured["q"] == "anime"


def test_auto_search_falls_back_to_licensed_wikimedia(monkeypatch):
    def fake_json(url, params=None, timeout=15):
        return {"data": []}                    # Wallhaven has nothing

    monkeypatch.setattr(image_tools, "http_json", fake_json)

    from winbrow import image_search

    class Result:
        url = "https://upload.wikimedia.org/eiffel.jpg"
        page = "https://commons.wikimedia.org/wiki/File:Eiffel.jpg"
        width, height = 2000, 1333
        source = "Wikimedia Commons"
        title = "Eiffel Tower"
        license = "CC BY-SA 4.0"
        credit = "Eiffel Tower — Jane Doe (CC BY-SA 4.0), Wikimedia Commons"

    monkeypatch.setattr(image_search, "search_images", lambda *a, **k: [Result()])
    hits = search_images("eiffel tower", count=2)
    assert [hit.source for hit in hits] == ["Wikimedia Commons"]
    assert hits[0].credit.startswith("Eiffel Tower")
    assert hits[0].license == "CC BY-SA 4.0"


def test_auto_search_falls_back_to_safe_anime(monkeypatch):
    def fake_json(url, params=None, timeout=15):
        if "wallhaven" in url:
            return {"data": []}
        assert params["rating"] == "safe"
        return [{"id": "n1", "url": "https://cdn.nekosapi.com/a.webp",
                 "rating": "safe", "width": None, "height": None}]

    monkeypatch.setattr(image_tools, "http_json", fake_json)
    from winbrow import image_search

    monkeypatch.setattr(image_search, "search_images", lambda *a, **k: [])
    hits = search_images("anime", count=2)
    assert [hit.source for hit in hits] == ["NekosAPI"]


def test_unknown_source_is_rejected():
    with pytest.raises(ImageToolError) as excinfo:
        search_images("cats", source="bing")
    assert "Unknown image source" in str(excinfo.value)


def test_explicit_wikimedia_source(monkeypatch):
    from winbrow import image_search

    called: dict = {}

    def fake_search(query, limit=5, min_width=900):
        called.update({"query": query, "limit": limit, "min_width": min_width})
        return []

    monkeypatch.setattr(image_search, "search_images", fake_search)
    with pytest.raises(ImageToolError):
        search_images("golden retriever", count=2, source="wikimedia", min_width=1200)
    assert called == {"query": "golden retriever", "limit": 2, "min_width": 1200}


# --------------------------------------------------------------------------- download
def test_download_image_keeps_png_bytes(monkeypatch, tmp_path):
    raw = _pnpm_png(1920, 1080)
    monkeypatch.setattr(image_tools, "http_get", lambda *a, **k: _Response(raw))
    info = download_image("https://example.com/pic.png", folder=tmp_path, filename="cat")
    path = Path(info["path"])
    assert path.is_file() and path.suffix == ".png" and path.read_bytes() == raw
    assert (info["width"], info["height"]) == (1920, 1080)


def test_download_image_transcodes_webp_when_not_allowed(monkeypatch, tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    PIL.frombytes("RGB", (1920, 1080), os.urandom(1920 * 1080 * 3)).save(buffer, "WEBP")
    monkeypatch.setattr(
        image_tools, "http_get",
        lambda *a, **k: _Response(buffer.getvalue(), "image/webp"),
    )
    info = download_image(
        "https://example.com/pic.webp", folder=tmp_path, filename="art",
        allowed_formats=("JPEG", "PNG"),
    )
    assert Path(info["path"]).suffix == ".png"
    with PIL.open(info["path"]) as image:
        assert image.size == (1920, 1080)


def test_download_image_rejects_html(monkeypatch, tmp_path):
    monkeypatch.setattr(
        image_tools, "http_get",
        lambda *a, **k: _Response(b"<html>" + b"x" * 4000, "text/html"),
    )
    with pytest.raises(ImageToolError) as excinfo:
        download_image("https://example.com/page.html", folder=tmp_path)
    assert "Not an image" in str(excinfo.value)


def test_download_image_rejects_small_images(monkeypatch, tmp_path):
    monkeypatch.setattr(
        image_tools, "http_get",
        lambda *a, **k: _Response(_pnpm_png(320, 200)),
    )
    with pytest.raises(ImageToolError):
        download_image("https://example.com/small.png", folder=tmp_path, min_width=800)


def test_download_image_rejects_non_url(tmp_path):
    with pytest.raises(ImageToolError):
        download_image("not-a-url", folder=tmp_path)


def test_download_image_resizes_when_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(
        image_tools, "http_get",
        lambda *a, **k: _Response(_pnpm_png(4000, 2250)),
    )
    info = download_image(
        "https://example.com/huge.png", folder=tmp_path, max_dimension=2400,
    )
    assert max(info["width"], info["height"]) == 2400


# --------------------------------------------------------------------------- folders
def test_resolve_folder_alias_and_override(monkeypatch, tmp_path):
    from winbrow import files

    monkeypatch.delenv("WINBROW_IMAGE_DIR", raising=False)
    monkeypatch.setattr(files, "_special_folder", lambda key: str(tmp_path / key))
    pictures = resolve_folder("Pictures")
    assert pictures == tmp_path / "pictures" / "WinBrow Images"
    downloads = resolve_folder("Downloads")
    assert downloads == tmp_path / "downloads"
    monkeypatch.setenv("WINBROW_IMAGE_DIR", str(tmp_path / "imgs"))
    assert resolve_folder("Pictures") == tmp_path / "imgs"
    explicit = tmp_path / "custom"
    assert resolve_folder(str(explicit)) == explicit


# --------------------------------------------------------------------------- orchestration
def test_direct_url_download_saves_one_image(monkeypatch, tmp_path):
    monkeypatch.setattr(
        image_tools, "http_get",
        lambda *a, **k: _Response(_pnpm_png(1200, 800)),
    )
    out = download_images("https://example.com/cat.png", folder=str(tmp_path))
    assert out["success"] and len(out["files"]) == 1
    assert out["results"][0]["source"] == "URL"
    assert Path(out["files"][0]).is_file()


def test_subject_download_honours_count_and_skips_failures(monkeypatch, tmp_path):
    hits = [
        image_tools.ImageCandidate("a", "https://x/a.jpg", "", 1200, 800, "Wallhaven"),
        image_tools.ImageCandidate("b", "https://x/b.jpg", "", 1200, 800, "Wallhaven"),
        image_tools.ImageCandidate("c", "https://x/c.jpg", "", 1200, 800, "Wallhaven"),
    ]
    monkeypatch.setattr(image_tools, "search_images", lambda *a, **k: hits)

    def fake_download(url, **kwargs):
        if url.endswith("a.jpg"):
            raise ImageToolError("HTTP 404")
        return {"path": str(tmp_path / f"{url[-5]}.jpg"), "filename": f"{url[-5]}.jpg",
                "width": 1200, "height": 800, "bytes": 1, "format": "JPG",
                "url": url, "folder": str(tmp_path)}

    monkeypatch.setattr(image_tools, "download_image", fake_download)
    out = download_images("cats", count=2, folder=str(tmp_path))
    assert out["success"] and len(out["files"]) == 2
    assert [item["source"] for item in out["results"]] == ["Wallhaven", "Wallhaven"]


def test_subject_download_reports_total_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(image_tools, "search_images", lambda *a, **k: [])

    def explode(url, **kwargs):
        raise ImageToolError("HTTP 500")

    monkeypatch.setattr(image_tools, "download_image", explode)
    with pytest.raises(ImageToolError):
        download_images("cats", folder=str(tmp_path))


def test_empty_query_is_rejected():
    with pytest.raises(ImageToolError):
        download_images("   ")


# --------------------------------------------------------------------------- tools
def test_tool_is_native_and_browser_free():
    tools = {tool.name: tool for tool in get_image_tools()}
    tool = tools["download_images"]
    assert tool.impl == image_tools.IMAGE_IMPL
    assert tool.script == ""
    assert "no browser is opened" in tool.description.lower()
    assert "download_file" in tool.description
    arg_names = [arg.name for arg in tool.args]
    assert arg_names == ["query", "count", "folder", "source", "min_width"]


def test_executor_returns_failure_dict_instead_of_raising(monkeypatch):
    def explode(*args, **kwargs):
        raise ImageToolError("No image found for 'zzz'")

    monkeypatch.setattr(image_tools, "search_images", explode)
    out = image_tools.execute_image_tool("download_images", {"query": "zzz"})
    assert out["success"] is False
    assert "No image found" in out["output"]
