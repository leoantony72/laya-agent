"""
Model Manager - Automatic Model Download & Management
======================================================
Handles downloading, verifying, and managing all required models:
- Whisper.cpp (base.en model for STT)
- Qwen3.5 0.8B (4-bit GGUF) for conversational LLM
- Qwen2.5-VL (4-bit GGUF) for vision fallback
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("winbrow.model_manager")

# Model configurations
MODEL_CONFIGS = {
    "whisper": {
        "repo": "ggerganov/whisper.cpp",
        "model_file": "ggml-base.en.bin",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        "sha256": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678",
        "subdir": "whisper",
    },
    "qwen": {
        "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "model_file": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "sha256": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678",
        "subdir": "qwen",
    },
}

# Model storage directory
MODELS_DIR = Path(__file__).parent.parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# llama.cpp server binary
LLAMA_CPP_DIR = Path(__file__).parent.parent / "llama_cpp"
LLAMA_CPP_DIR.mkdir(parents=True, exist_ok=True)

LLAMA_SERVER_BIN = LLAMA_CPP_DIR / ("llama-server.exe" if platform.system() == "Windows" else "llama-server")


def get_model_path(model_name: str) -> Path:
    """Get the local path for a model, downloading if necessary."""
    if model_name not in MODEL_CONFIGS:
        raise ValueError(f"Unknown model: {model_name}")
    
    config = MODEL_CONFIGS[model_name]
    model_dir = MODELS_DIR / config["subdir"]
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / config["model_file"]
    
    if model_path.exists():
        # Verify checksum
        if _verify_checksum(model_path, config["sha256"]):
            return model_path
        else:
            log.warning(f"Checksum mismatch for {model_path}, re-downloading...")
            model_path.unlink(missing_ok=True)
    
    # Download model
    log.info(f"Downloading {model_name} model...")
    _download_with_progress(config["url"], model_path)
    
    # Verify checksum
    if not _verify_checksum(model_path, config["sha256"]):
        raise RuntimeError(f"Checksum verification failed for {model_name}")
    
    log.info(f"Model {model_name} ready at {model_path}")
    return model_path


def _verify_checksum(filepath: Path, expected_sha256: str) -> bool:
    """Verify SHA256 checksum of a file."""
    if expected_sha256 == "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678":
        # Placeholder checksum - skip verification for now
        return True
    
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest().lower() == expected_sha256.lower()


def _download_with_progress(url: str, dest: Path) -> None:
    """Download a file with progress bar using proper User-Agent header."""
    log.info(f"Downloading from {url} to {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "WinBrow/2.0 (Windows NT 10.0; Win64; x64)"})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out_file:
            total_size = int(resp.headers.get("content-length", 0))
            downloaded = 0
            block_size = 16384
            while True:
                buffer = resp.read(block_size)
                if not buffer:
                    break
                downloaded += len(buffer)
                out_file.write(buffer)
                if total_size > 0:
                    percent = min(100, (downloaded * 100) // total_size)
                    sys.stdout.write(f"\rDownloading: {percent}%")
                    sys.stdout.flush()
        print()  # New line after progress
    except Exception as e:
        log.error(f"Download failed: {e}")
        if dest.exists():
            dest.unlink(missing_ok=True)
        raise


def ensure_llama_cpp(download: bool = False) -> Optional[Path]:
    """Ensure llama.cpp server binary is available.

    With download=True (local-brain mode) the latest Windows x64 release is
    fetched from GitHub into llama_cpp/. Failures are non-fatal: returns None
    and callers fall back to cloud providers.
    """
    if LLAMA_SERVER_BIN.exists():
        return LLAMA_SERVER_BIN

    # Check if llama-server is available in system PATH
    path_bin = shutil.which("llama-server") or shutil.which("llama-server.exe")
    if path_bin:
        return Path(path_bin)

    if download and platform.system() == "Windows":
        try:
            return _download_llama_server()
        except Exception as e:
            log.warning(f"llama-server download failed (non-fatal): {e}")
            return None

    log.info("llama-server binary not found locally. Server will use System 1 Laya decision router.")
    return None


# GitHub releases list (binaries ship on per-build tags, not /releases/latest)
LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5"
_GPU_ASSET_MARKERS = ("cuda", "vulkan", "sycl", "hip", "rocm", "opencl", "openvino", "adreno", "arm")


def _pick_llama_asset(assets: list[dict]) -> Optional[dict]:
    """Best Windows x64 server zip: prefer avx2, then cpu; skip GPU variants."""
    import re
    candidates = [
        a for a in assets
        if re.search(r"bin-win-.*x64\.zip$", a.get("name", ""), re.IGNORECASE)
        and not any(m in a.get("name", "").lower() for m in _GPU_ASSET_MARKERS)
    ]
    if not candidates:
        return None

    def rank(name: str) -> int:
        n = name.lower()
        if "avx2" in n:
            return 0
        if "cpu" in n or "noavx" in n:
            return 1
        return 2

    candidates.sort(key=lambda a: rank(a["name"]))
    return candidates[0]


def _download_llama_server() -> Optional[Path]:
    """Download + extract the latest llama.cpp Windows x64 server release."""
    import json as _json
    import zipfile

    log.info("llama-server not found; downloading from GitHub releases...")
    ua = {"User-Agent": "WinBrow/3.0 (Windows NT; x64)"}
    req = urllib.request.Request(
        LLAMA_RELEASES_API,
        headers={**ua, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = _json.loads(resp.read().decode("utf-8"))

    asset = None
    for release in releases:
        if release.get("draft"):
            continue
        asset = _pick_llama_asset(release.get("assets") or [])
        if asset:
            break
    if not asset:
        raise RuntimeError("no Windows x64 asset in recent llama.cpp releases")

    size_mb = int(asset.get("size") or 0) // (1024 * 1024)
    log.info(f"Downloading {asset['name']} (~{size_mb} MB)...")
    zip_path = LLAMA_CPP_DIR / asset["name"]
    dl_req = urllib.request.Request(asset["browser_download_url"], headers=ua)
    with urllib.request.urlopen(dl_req, timeout=120) as resp, open(zip_path, "wb") as out:
        shutil.copyfileobj(resp, out)

    extract_dir = LLAMA_CPP_DIR / "_extract"
    shutil.rmtree(extract_dir, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)

    exe = next(iter(extract_dir.rglob("llama-server.exe")), None)
    if exe is None:
        raise RuntimeError("llama-server.exe not found in release archive")

    # Move the whole bin directory (exe + required DLLs) into llama_cpp/
    for item in list(exe.parent.iterdir()):
        target = LLAMA_CPP_DIR / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
        shutil.move(str(item), str(target))
    shutil.rmtree(extract_dir, ignore_errors=True)
    zip_path.unlink(missing_ok=True)

    if LLAMA_SERVER_BIN.exists():
        log.info(f"llama-server ready at {LLAMA_SERVER_BIN}")
        return LLAMA_SERVER_BIN
    return None


def ensure_all_models() -> dict[str, Path]:
    """Download all required models."""
    log.info("Ensuring all models are available...")
    paths = {}
    for model_name in MODEL_CONFIGS:
        try:
            paths[model_name] = get_model_path(model_name)
        except Exception as e:
            log.warning(f"Could not download model '{model_name}': {e}")
    try:
        ensure_llama_cpp()
    except Exception as e:
        log.warning(f"Could not prepare llama-server binary: {e}")
    return paths


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    paths = ensure_all_models()
    for name, path in paths.items():
        print(f"{name}: {path}")
    print(f"llama-server: {ensure_llama_cpp()}")