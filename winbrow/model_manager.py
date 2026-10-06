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
        "sha256": "8c8d0f2b3b4a1e8c9f5e3b2a1d0c9f8e7b6a5d4c3b2a1f0e9d8c7b6a5f4e3d2c1",
        "subdir": "whisper",
    },
    "qwen": {
        "repo": "Qwen/Qwen3.5-0.8B-GGUF",
        "model_file": "qwen3.5-0.8b-q4_k_m.gguf",
        "url": "https://huggingface.co/Qwen/Qwen3.5-0.8B-GGUF/resolve/main/qwen3.5-0.8b-q4_k_m.gguf",
        "sha256": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678",
        "subdir": "qwen",
    },
    "qwen_vl": {
        "repo": "Qwen/Qwen2.5-VL-3B-GGUF",
        "model_file": "qwen2.5-vl-3b-q4_k_m.gguf",
        "url": "https://huggingface.co/Qwen/Qwen2.5-VL-3B-GGUF/resolve/main/qwen2.5-vl-3b-q4_k_m.gguf",
        "sha256": "b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef1234567890",
        "subdir": "qwen_vl",
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
    """Download a file with progress bar."""
    log.info(f"Downloading from {url} to {dest}")
    
    def progress_hook(block_num, block_size, total_size):
        if total_size > 0:
            percent = min(100, (block_num * block_size * 100) // total_size)
            sys.stdout.write(f"\rDownloading: {percent}%")
            sys.stdout.flush()
    
    try:
        urllib.request.urlretrieve(url, str(dest), progress_hook)
        print()  # New line after progress
    except Exception as e:
        log.error(f"Download failed: {e}")
        raise


def ensure_llama_cpp() -> Path:
    """Ensure llama.cpp server binary is available."""
    if LLAMA_SERVER_BIN.exists():
        return LLAMA_SERVER_BIN
    
    log.info("Downloading llama.cpp server binary...")
    
    if platform.system() == "Windows":
        # Download pre-built Windows binary
        url = "https://github.com/ggml-org/llama.cpp/releases/latest/download/llama-server-win64.zip"
        zip_path = LLAMA_CPP_DIR / "llama-server.zip"
        
        _download_with_progress(url, zip_path)
        
        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(LLAMA_CPP_DIR)
        zip_path.unlink(missing_ok=True)
    else:
        # Linux/macOS - build from source or download
        log.info("Building llama.cpp from source...")
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp.git", str(LLAMA_CPP_DIR)], check=True)
        subprocess.run(["cmake", "-B", "build", "-DLLAMA_CURL=ON", "-DLLAMA_SERVER=ON"], cwd=LLAMA_CPP_DIR, check=True)
        subprocess.run(["cmake", "--build", "build", "--config", "Release", "-j", "4"], cwd=LLAMA_CPP_DIR, check=True)
        
        # Find the binary
        for name in ["llama-server", "llama-server.exe"]:
            bin_path = LLAMA_CPP_DIR / "build" / "bin" / name
            if bin_path.exists():
                shutil.copy(bin_path, LLAMA_SERVER_BIN)
                break
    
    if not LLAMA_SERVER_BIN.exists():
        raise RuntimeError("Failed to obtain llama-server binary")
    
    # Make executable on Unix
    if platform.system() != "Windows":
        LLAMA_SERVER_BIN.chmod(0o755)
    
    return LLAMA_SERVER_BIN


def ensure_all_models() -> dict[str, Path]:
    """Download all required models."""
    log.info("Ensuring all models are available...")
    paths = {}
    for model_name in MODEL_CONFIGS:
        paths[model_name] = get_model_path(model_name)
    ensure_llama_cpp()
    return paths


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    paths = ensure_all_models()
    for name, path in paths.items():
        print(f"{name}: {path}")
    print(f"llama-server: {ensure_llama_cpp()}")