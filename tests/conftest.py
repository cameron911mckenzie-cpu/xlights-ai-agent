"""Shared fixtures: synthetic click-track WAVs with known ground truth."""

from __future__ import annotations

import wave

import numpy as np
import pytest


def write_wav(path: str, samples: np.ndarray, sr: int = 44100) -> str:
    clipped = np.clip(samples, -1.0, 1.0)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((clipped * 32767).astype("<i2").tobytes())
    return path


def click_track(bpm: float, dur: float, accent_every: int = 4,
                sr: int = 44100, seed: int = 0) -> tuple:
    """A metronomic click song: returns (samples, beat_times)."""
    iv = 60.0 / bpm
    n = int(sr * dur)
    rng = np.random.default_rng(seed)
    sig = (0.02 * rng.standard_normal(n)).astype(np.float32)
    beats = np.arange(0.0, dur - 0.05, iv)
    for k, b in enumerate(beats):
        i0 = int(b * sr)
        ln = min(int(0.12 * sr), n - i0)
        if ln <= 0:
            break
        t = np.arange(ln) / sr
        burst = np.sin(2 * np.pi * 70 * t) * np.exp(-np.arange(ln) / (0.025 * sr))
        amp = 1.0 if k % accent_every == 0 else 0.55
        sig[i0:i0 + ln] += (amp * burst).astype(np.float32)
        cl = min(200, n - i0)
        sig[i0:i0 + cl] += (0.9 * amp * np.exp(-np.arange(cl) / 40)).astype(np.float32)
    return sig, beats


@pytest.fixture(scope="session")
def song_120(tmp_path_factory) -> dict:
    """16 s at 120 BPM with a loud chorus in the middle."""
    sr = 44100
    sig = np.zeros(int(sr * 24), dtype=np.float32)
    beats = np.arange(0.0, 24 - 0.05, 0.5)
    for k, b in enumerate(beats):
        i0 = int(b * sr)
        ln = min(int(0.12 * sr), sig.size - i0)
        t = np.arange(ln) / sr
        burst = np.sin(2 * np.pi * 70 * t) * np.exp(-np.arange(ln) / (0.025 * sr))
        amp = 0.15 if b < 8 else (1.0 if b < 16 else 0.2)
        amp *= 1.0 if k % 4 == 0 else 0.6   # in-bar accents so phase is observable
        sig[i0:i0 + ln] += (amp * burst).astype(np.float32)
        cl = min(200, sig.size - i0)
        sig[i0:i0 + cl] += (0.8 * amp * np.exp(-np.arange(cl) / 40)).astype(np.float32)
    sig += 0.01 * np.random.default_rng(1).standard_normal(sig.size).astype(np.float32)
    path = str(tmp_path_factory.mktemp("audio") / "song120.wav")
    write_wav(path, sig, sr)
    return {"path": path, "bpm": 120.0, "beats": beats}


@pytest.fixture(scope="session")
def click_174(tmp_path_factory) -> dict:
    """16 s at 174 BPM -- catches tempo-octave regressions."""
    sig, beats = click_track(174.0, 16.0)
    path = str(tmp_path_factory.mktemp("audio") / "click174.wav")
    write_wav(path, sig)
    return {"path": path, "bpm": 174.0, "beats": beats}


@pytest.fixture()
def models() -> list:
    return ["All House", "MegaTree", "Matrix", "Arch1", "RoofOutline",
            "WindowFrame1", "Star", "Bushes", "SingingFace", "PixelStrip"]
