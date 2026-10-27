"""Web image search: license filtering, local download, optional live search."""

from __future__ import annotations

import functools
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.image_search import (
    ImageSearchError,
    _clean,
    _license_ok,
    download_image,
    search_images,
)


def test_license_filter():
    assert _license_ok("Public domain")
    assert _license_ok("CC BY-SA 4.0")
    assert _license_ok("CC0")
    assert not _license_ok("Non-free")
    assert not _license_ok("Fair use")


def test_clean_strips_html_and_entities():
    assert _clean("<div>Ada &amp; Grace</div>") == "Ada & Grace"


def test_download_image_from_local_http(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    PIL.new("RGB", (1600, 900), (30, 80, 120)).save(source_dir / "image.png")

    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(source_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        target = tmp_path / "downloaded.png"
        info = download_image(
            f"http://127.0.0.1:{port}/image.png", target, min_width=600,
        )
        assert Path(info["path"]).is_file()
        with PIL.open(info["path"]) as image:
            assert image.size == (1600, 900)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_download_rejects_non_image(tmp_path):
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "text.txt").write_text("not an image", encoding="utf-8")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(source_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with pytest.raises(ImageSearchError):
            download_image(
                f"http://127.0.0.1:{port}/text.txt",
                tmp_path / "bad.png", min_width=10, timeout=10,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(
    os.environ.get("WINBROW_LIVE_TESTS") != "1",
    reason="set WINBROW_LIVE_TESTS=1 for live Wikimedia image search",
)
def test_live_wikimedia_search_and_download(tmp_path):
    results = search_images("mountain landscape", limit=2, min_width=900)
    assert results
    result = results[0]
    assert result.license
    assert "Wikimedia" in result.credit
    info = download_image(result.url, tmp_path / "live.jpg", min_width=900)
    assert Path(info["path"]).is_file()
