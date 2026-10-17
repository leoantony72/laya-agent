"""
VAD — speech gating (M4 voice pipeline)
========================================
Frame-level Voice Activity Detector for the mic stream. Two rungs:

  1. silero-vad ONNX (~2MB, <10ms per 100ms frame on 2 cores) via onnxruntime
  2. adaptive energy (RMS) fallback — zero extra deps, works everywhere

The voice loop only advances audio to wake-word/STT when the VAD says speech
— dropping CPU standby use from near-100% (whisper-per-chunk) to ~2–5%.

Model: cached in models/silero/; download is opt-in (WINBROW_SKIP_MODEL_DL).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("winbrow.vad")

SAMPLE_RATE = 16000
_SILERO_WINDOW = 512           # silero consumes 512 samples (~32ms)
_SILERO_URL = ("https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx")
_SILERO_DIR = Path(os.environ.get("WINBROW_MODELS_DIR",
                                  Path(__file__).parent.parent / "models")) / "silero"


def _silero_path() -> Path:
    return _SILERO_DIR / "silero_vad.onnx"


class VADGate:
    """Turn raw 16k mono int16 frames into speech/no-speech booleans.

    Hysteresis (speech-start / end-of-speech) is handled by the CALLER so this
    class stays effectively stateless (internal RNN state aside).
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = float(os.environ.get("WINBROW_VAD_THRESHOLD", str(threshold)))
        self._sess = None
        self._in = None
        self._h = None
        self._c = None
        mode = os.environ.get("WINBROW_VAD", "").lower()
        if mode == "energy":
            self.backend = "energy"
            log.info("VADGate: energy backend (forced)")
            return
        self._try_silero()
        if self._sess is None:
            self.backend = "energy"
            log.info("VADGate: energy backend (silero unavailable)")
        else:
            log.info("VADGate: silero ONNX backend")

    # ---------------------------------------------------------------- silero
    def _try_silero(self) -> None:
        try:
            import numpy as np
            import onnxruntime as ort
            p = _silero_path()
            if not p.exists():
                return
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(
                str(p), sess_options=opts, providers=["CPUExecutionProvider"])
            self._in = self._sess.get_inputs()[0].name
            self._h = np.zeros((2, 1, 128), dtype=np.float32)
            self._c = np.zeros((2, 1, 128), dtype=np.float32)
        except Exception as e:
            self._sess = None
            log.debug(f"silero VAD unavailable ({e})")

    def feed(self, pcm16) -> bool:
        """One 16k/mono/16-bit frame (any length ≥ 32ms). Returns speech?"""
        if self._sess is not None:
            try:
                return self._feed_silero(pcm16)
            except Exception as e:
                log.debug(f"silero feed failed ({e}); energy fallback")
                self.backend = "energy"
        return self._feed_energy(pcm16)

    def reset(self) -> None:
        if self._sess is not None:
            import numpy as np
            self._h = np.zeros((2, 1, 128), dtype=np.float32)
            self._c = np.zeros((2, 1, 128), dtype=np.float32)

    def _feed_silero(self, pcm16) -> bool:
        import numpy as np
        x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size < _SILERO_WINDOW:
            return False
        windows = x[: x.size - (x.size % _SILERO_WINDOW)].reshape(-1, _SILERO_WINDOW)
        probs = []
        for w in windows:
            out = self._sess.run(None, {
                self._in: w.reshape(1, -1),
                "sr": np.array([SAMPLE_RATE], dtype=np.int64),
                "h": self._h, "c": self._c,
            })
            probs.append(float(out[0][0][0]))
            self._h, self._c = out[1], out[2]
        if not probs:
            return False
        return float(sum(probs) / len(probs)) >= self.threshold

    # ---------------------------------------------------------------- energy
    def _feed_energy(self, pcm16) -> bool:
        try:
            import numpy as np
            x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
            if x.size == 0:
                return False
            rms = float(np.sqrt(np.mean(x * x)))
            norm = rms / 3000.0
            return norm >= self.threshold / 4.0
        except Exception:
            return False


def ensure_silero_model() -> bool:
    """Attempt silero model download (skipped per WINBROW_SKIP_MODEL_DL)."""
    if _silero_path().exists():
        return True
    if os.environ.get("WINBROW_SKIP_MODEL_DL", "") not in ("", "0"):
        return False
    try:
        import urllib.request
        _SILERO_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_SILERO_URL, _silero_path())
        log.info(f"silero-vad model cached at {_silero_path()}")
        return True
    except Exception as e:
        log.warning(f"silero model download failed ({e}); energy VAD will be used")
        return False
