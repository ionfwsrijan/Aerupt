"""Multimodal grounding.

The theme wants perception to run *behind* conversational acknowledgments:
  - raw WAV clips -> lightweight acoustic analysis (duration, energy,
    voiced ratio) + optional plug-in ASR transcript
  - PNG frames   -> frame reference for frame-grounded tool calls
                    (manual lookups), plus optional vision plug-in
                    producing a canonical description

The real harness owns ASR/vision; this module keeps the agent honest by
(a) acknowledging quickly and (b) never claiming to "see/hear" more than we
actually know — ambiguous perception triggers a clarification instead.
"""
from __future__ import annotations

import base64
import struct
import zlib
from typing import Any, Dict, Optional


def _wav_info(blob: bytes) -> Optional[Dict[str, Any]]:
    """Parse a minimal RIFF/WAV header to extract shape statistics so the
    agent can acknowledge "heard a clipped phrase" truthfully."""
    if not blob or len(blob) < 44:
        return None
    try:
        if blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
            return None
        nch = struct.unpack("<H", blob[22:24])[0]
        sr = struct.unpack("<I", blob[24:28])[0]
        bits = struct.unpack("<H", blob[34:36])[0]
        data = blob[44:]
        if bits == 8:
            samples = [(b - 128) / 128.0 for b in data]
        elif bits == 16:
            fmt = "<" + ("h" * (len(data) // 2))
            samples = [s / 32768.0 for s in struct.unpack(fmt, data[: len(data) - len(data) % 2])]
        else:
            samples = []
        rms = (sum(s * s for s in samples) / max(len(samples), 1)) ** 0.5
        voiced = sum(1 for s in samples if abs(s) > 0.04) / max(len(samples), 1)
        dur = len(samples) / max(sr, 1)
        return {
            "sample_rate": sr, "channels": nch, "bits": bits,
            "duration_s": round(dur, 3), "rms": round(rms, 4),
            "voiced_ratio": round(voiced, 4),
            "speech_likely": voiced > 0.25 and dur > 0.3,
        }
    except Exception:
        return None


def _png_ref(blob: bytes) -> Optional[str]:
    """Return a stable short reference for a PNG frame (GRANULARITY for
    frame-grounded tool call args)."""
    if not blob:
        return None
    try:
        ihdr = blob.find(b"IHDR")
        if ihdr < 8 or blob[0:4] != b"\x89PNG":
            return None
        w, h = struct.unpack(">II", blob[ihdr + 4: ihdr + 12])
        return f"frame:{zlib.crc32(blob) & 0xFFFFFFFF:08x}:{w}x{h}"
    except Exception:
        return None


class Grounding:
    """Safe multimodal context builder. `vision`/`asr` are optional plug-ins
    with signatures `async def __call__(blob) -> str description`."""

    def __init__(self, asr: Optional[Any] = None, vision: Optional[Any] = None):
        self._asr = asr
        self._vision = vision

    async def ingest_audio(self, blob_or_b64: Any) -> Dict[str, Any]:
        blob = self._to_bytes(blob_or_b64)
        info = _wav_info(blob)
        if info is None:
            return {"ok": False, "reason": "unrecognized_audio"}
        transcript = None
        if self._asr and info.get("speech_likely"):
            try:
                transcript = (await self._asr(blob)) or None
            except Exception:
                transcript = None
        return {"ok": True, "acoustic": info, "transcript": transcript,
                "kind": "speech" if info.get("speech_likely") else "noise"}

    async def ingest_frame(self, blob_or_b64: Any) -> Dict[str, Any]:
        blob = self._to_bytes(blob_or_b64)
        ref = _png_ref(blob)
        if ref is None:
            return {"ok": False, "reason": "unrecognized_frame"}
        description = None
        if self._vision:
            try:
                description = (await self._vision(blob)) or None
            except Exception:
                description = None
        return {"ok": True, "frame_ref": ref, "description": description,
                "kind": "frame"}

    @staticmethod
    def _to_bytes(blob_or_b64: Any) -> bytes:
        if isinstance(blob_or_b64, str):
            s = blob_or_b64.strip()
            if s:
                try:
                    return base64.b64decode(s, validate=True)
                except Exception:
                    pass
                if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
                    try:
                        return bytes.fromhex(s)
                    except Exception:
                        pass
            return blob_or_b64.encode("latin-1", "replace")
        return bytes(blob_or_b64 or b"")