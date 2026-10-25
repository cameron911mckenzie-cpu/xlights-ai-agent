"""Music decoding and beat analysis for the xLights AI auto sequencer.

The pipeline is:

    decode_audio()  ->  analyze_beats()  ->  BeatAnalysis
                          |
                          +-- STFT spectral-flux onset envelope
                          +-- FFT autocorrelation tempo estimate
                          +-- dynamic-programming beat tracking (Ellis style)
                          +-- low-band downbeat/phase detection
                          +-- energy-based section detection (intro/verse/chorus/...)

Beat grids can also be imported from and exported to the ``*_beats.txt`` files
xLights itself uses (one row per beat: ``start<TAB>end<TAB>beat_in_bar``).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np

__all__ = [
    "AudioData",
    "Beat",
    "Section",
    "BeatAnalysis",
    "decode_audio",
    "analyze_beats",
    "load_beats_txt",
    "save_beats_txt",
    "AudioDecodeError",
]


class AudioDecodeError(RuntimeError):
    """Raised when an audio file cannot be decoded."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class AudioData:
    """Decoded mono audio, normalised to float32 in [-1, 1]."""

    path: str
    samples: np.ndarray
    sample_rate: int

    @property
    def duration_s(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return float(len(self.samples)) / float(self.sample_rate)


@dataclass
class Beat:
    """A single detected beat."""

    time_s: float
    index: int                 # 0-based index in the song
    index_in_bar: int          # 1..meter; 1 is the downbeat
    energy: float = 0.0        # normalised loudness around the beat (0..1)

    @property
    def downbeat(self) -> bool:
        return self.index_in_bar == 1

    @property
    def time_ms(self) -> int:
        return int(round(self.time_s * 1000.0))


@dataclass
class Section:
    """A musically homogeneous stretch of the song (intro/verse/chorus/...)."""

    start_s: float
    end_s: float
    label: str                 # "low" | "mid" | "high"
    name: str                  # "intro" | "verse" | "chorus" | "bridge" | "outro"
    energy: float = 0.0

    @property
    def start_ms(self) -> int:
        return int(round(self.start_s * 1000.0))

    @property
    def end_ms(self) -> int:
        return int(round(self.end_s * 1000.0))


@dataclass
class BeatAnalysis:
    """Full result of :func:`analyze_beats`."""

    path: str
    bpm: float
    meter: int
    duration_s: float
    beats: List[Beat] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    confidence: float = 0.0    # 0..1, how strong the beat grid matches the audio

    @property
    def duration_ms(self) -> int:
        return int(round(self.duration_s * 1000.0))

    @property
    def downbeats(self) -> List[Beat]:
        return [b for b in self.beats if b.downbeat]

    @property
    def beat_interval_s(self) -> float:
        if self.bpm <= 0:
            return 0.5
        return 60.0 / self.bpm

    def section_at(self, time_s: float) -> Optional[Section]:
        for s in self.sections:
            if s.start_s <= time_s < s.end_s:
                return s
        return self.sections[-1] if self.sections else None

    def summary(self) -> dict:
        return {
            "path": self.path,
            "bpm": round(self.bpm, 2),
            "meter": self.meter,
            "duration_s": round(self.duration_s, 3),
            "beat_count": len(self.beats),
            "downbeat_count": len(self.downbeats),
            "confidence": round(self.confidence, 3),
            "sections": [
                {
                    "name": s.name,
                    "label": s.label,
                    "start_ms": s.start_ms,
                    "end_ms": s.end_ms,
                    "energy": round(s.energy, 3),
                }
                for s in self.sections
            ],
        }


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

_WAVE_RATES = None  # placeholder for possible future use


def _decode_wav(path: str) -> Optional[AudioData]:
    """Decode PCM WAV files with the stdlib. Returns None for non-PCM WAVs."""
    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError):
        return None
    except OSError:
        raise

    if sampwidth == 1:  # unsigned 8-bit
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        vals = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        data = vals.astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        # Could be 32-bit int PCM or 32-bit IEEE float; wave module rejects
        # float WAVs with a format error before we get here, so treat as int.
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        return None

    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return AudioData(path=path, samples=np.ascontiguousarray(data, dtype=np.float32),
                     sample_rate=framerate)


def _decode_via_ffmpeg(path: str) -> AudioData:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioDecodeError(
            f"Cannot decode '{os.path.basename(path)}': it is not a PCM WAV file and "
            "ffmpeg was not found on PATH. Install ffmpeg to analyse MP3/OGG/FLAC/M4A "
            "audio, or export the song as WAV, or pass a xLights *_beats.txt file "
            "with --beats to skip analysis."
        )
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-i", path,
        "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", "44100", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise AudioDecodeError(f"ffmpeg failed decoding '{path}': {detail}") from exc
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise AudioDecodeError(f"ffmpeg produced no audio for '{path}'")
    return AudioData(path=path, samples=samples, sample_rate=44100)


def decode_audio(path: str) -> AudioData:
    """Decode *path* to mono float32 audio (WAV natively, anything else via ffmpeg)."""
    if not os.path.isfile(path):
        raise AudioDecodeError(f"Audio file not found: {path}")
    audio = None
    if path.lower().endswith(".wav"):
        audio = _decode_wav(path)
    if audio is None:
        audio = _decode_via_ffmpeg(path)
    if audio.samples.size == 0:
        raise AudioDecodeError(f"Audio file is empty: {path}")
    return audio


# ---------------------------------------------------------------------------
# Beat analysis
# ---------------------------------------------------------------------------

def _stft_magnitude(samples: np.ndarray, n_fft: int = 2048, hop: int = 512) -> Tuple[np.ndarray, np.ndarray]:
    """Magnitude spectrogram: returns (frames, freqs_hz)."""
    if samples.size < n_fft:
        samples = np.pad(samples, (0, n_fft - samples.size))
    n_frames = 1 + (len(samples) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    window = np.hanning(n_fft).astype(np.float32)
    frames = samples[idx] * window
    mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, d=1.0)  # normalised; scaled by caller's sr
    return mag, freqs


def _onset_envelope(mag: np.ndarray) -> np.ndarray:
    """Half-wave rectified spectral flux, log-compressed and normalised."""
    flux = np.maximum(np.diff(mag, axis=0), 0.0).sum(axis=1)
    flux = np.concatenate([[0.0], flux]).astype(np.float64)
    # Remove slow trends so quiet intros do not drown in baseline energy.
    if flux.size > 16:
        kernel = np.ones(17) / 17.0
        trend = np.convolve(flux, kernel, mode="same")
        flux = np.maximum(flux - trend, 0.0)
    flux = np.log1p(flux * 50.0)
    scale = flux.std()
    if scale > 1e-9:
        flux = flux / scale
    return flux


def _estimate_period_frames(oe: np.ndarray, fps: float,
                            bpm_min: float = 55.0, bpm_max: float = 190.0) -> Tuple[float, float]:
    """Estimate the beat period in frames from the onset envelope autocorrelation.

    A log-gaussian tempo prior centred on 120 BPM breaks ties between the
    many periodicities a metronomic track contains (60 vs 120 vs 240 ...),
    in the same spirit as librosa's beat tracker.
    """
    n = oe.size
    if n < 8:
        return fps * 60.0 / 120.0, 0.0
    centered = oe - oe.mean()
    nfft = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(centered, nfft)
    ac = np.fft.irfft(spec * np.conj(spec))[:n].real
    if ac[0] <= 0:
        return fps * 60.0 / 120.0, 0.0
    ac = ac / ac[0]

    lag_min = max(2, int(math.floor(fps * 60.0 / bpm_max)))
    lag_max = min(n - 2, int(math.ceil(fps * 60.0 / bpm_min)))
    if lag_max <= lag_min:
        return fps * 60.0 / 120.0, 0.0
    lags = np.arange(lag_min, lag_max + 1)

    score = ac[lags].astype(np.float64).copy()
    # Support only from the lag's double: for a genuine impulse train,
    # ac[k*T] is high at integer multiples, so rewarding ac[2L] favours the
    # TRUE period. Rewarding ac[L/2] (which I tried first) systematically
    # favours half tempo instead.
    lm = np.round(lags * 2.0).astype(int)
    valid = (lm > 0) & (lm < n)
    score[valid] += 0.35 * ac[lm[valid]]
    # Sub-harmonic penalty: when ac at HALF the candidate lag is also strong,
    # the candidate is probably a plain multiple of the true period (periodic
    # impulse trains correlate at every multiple, so T and 2T otherwise tie
    # and the tempo prior alone would pick the musical octave at random).
    hl = np.maximum(np.round(lags * 0.5).astype(int), 0)
    ac_half = ac[hl]
    score -= 0.4 * np.maximum(ac_half - 0.5 * ac[lags], 0.0)
    # Log-gaussian tempo prior ~ N(log2(120), 0.6) in octave space.
    bpm_lags = 60.0 * fps / np.maximum(lags, 1)
    log_oct = np.log2(bpm_lags / 120.0)
    score *= np.exp(-0.5 * (log_oct / 0.6) ** 2) + 0.15

    best = int(np.argmax(score))
    period = float(lags[best])
    # Sub-frame refinement by parabolic interpolation around the peak.
    if 0 < best < len(lags) - 1:
        y0, y1, y2 = score[best - 1], score[best], score[best + 1]
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            delta = 0.5 * (y0 - y2) / denom
            if -1 < delta < 1:
                period += float(delta)
    # Raw (pre-prior) peak strength is a better confidence signal than the
    # prior-shaped value, which is always near 1 at the mode.
    raw = ac[min(int(round(period)), n - 1)]
    confidence = float(np.clip(max(raw, 0.0), 0.0, 1.0))
    return period, confidence


def _track_beats(oe: np.ndarray, period: float, alpha: float = 80.0) -> np.ndarray:
    """Dynamic-programming beat tracking (Ellis-style), returns beat frame indices."""
    n = oe.size
    if n == 0:
        return np.zeros(0, dtype=int)
    period = max(2.0, period)
    min_lag = max(1, int(round(period * 0.5)))
    max_lag = max(min_lag + 1, int(round(period * 1.5)))
    lags = np.arange(min_lag, max_lag + 1)
    lag_pen = alpha * ((lags - period) / period) ** 2

    score = np.full(n, -1e18, dtype=np.float64)
    back = np.full(n, -1, dtype=int)
    score[0] = oe[0]
    for f in range(1, n):
        lo = max(1, f - max_lag)
        hi = f - min_lag
        if hi < lo:
            # Frames within one max_lag of the start: a beat may begin here.
            score[f] = oe[f]
            back[f] = -1
            continue
        best_val = -1e18
        best_prev = -1
        # Explicit loop over a tiny window (at most ~1.5 beat periods).
        k = lo
        while k <= hi:
            cand = score[k] - lag_pen[f - k - min_lag]
            if cand > best_val:
                best_val = cand
                best_prev = k
            k += 1
        score[f] = oe[f] + best_val
        back[f] = best_prev

    end = int(np.argmax(score))
    beats: List[int] = []
    cur = end
    while cur >= 0:
        beats.append(cur)
        cur = back[cur]
    beats.reverse()
    return np.asarray(beats, dtype=int)


def _frame_rms_envelope(samples: np.ndarray, sr: int, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    if samples.size < n_fft:
        samples = np.pad(samples, (0, n_fft - samples.size))
    n_frames = 1 + (len(samples) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = samples[idx]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    peak = rms.max()
    if peak > 1e-9:
        rms = rms / peak
    return rms


def _detect_phase(beat_frames: np.ndarray, low_mag: np.ndarray, meter: int,
                  window: int = 12) -> int:
    """Pick which beat index is the downbeat.

    Uses the low-frequency *energy* right after each beat (not the trend-
    removed flux): bar starts (kick + bass together) carry markedly more
    low-end than weak beats, and compression would flatten that contrast in
    a flux signal. Falls back to phase 0 when everything is equal.
    """
    if meter <= 1 or beat_frames.size < meter:
        return 0
    n_low = low_mag.shape[0]
    energies: List[float] = []
    for f in beat_frames:
        f0 = int(f)
        f1 = min(f0 + window, n_low)
        if f0 >= f1:
            energies.append(0.0)
        else:
            energies.append(float(low_mag[f0:f1].sum()))
    best_phase, best_score = 0, -1.0
    for phase in range(meter):
        starts = energies[phase::meter]
        if not starts:
            continue
        score = sum(starts) / len(starts)
        if score > best_score:
            best_score, best_phase = score, phase
    return best_phase


def _label_sections(beat_times: np.ndarray, energies: np.ndarray, duration_s: float) -> List[Section]:
    """Classify beats into low/mid/high energy runs and name them musically."""
    if beat_times.size == 0:
        return []
    if energies.size >= 9:
        kernel = np.ones(9) / 9.0
        smoothed = np.convolve(energies, kernel, mode="same")
    else:
        smoothed = energies
    lo, hi = np.percentile(smoothed, [35, 70])
    # Near-constant energy: treat the whole song as one flat section rather
    # than splitting on noise between the percentiles.
    spread_gate = max(0.02, 0.05 * float(np.mean(smoothed)))
    if hi - lo < spread_gate:
        lo, hi = -1e9, 1e9

    labels = np.where(smoothed < lo, "low", np.where(smoothed > hi, "high", "mid"))

    runs: List[Tuple[str, int, int]] = []  # label, start_idx, end_idx (exclusive)
    start = 0
    for i in range(1, len(labels) + 1):
        if i == len(labels) or labels[i] != labels[start]:
            runs.append((labels[start], start, i))
            start = i
    # Merge runs shorter than 4 beats into the previous run.
    merged: List[Tuple[str, int, int]] = []
    for label, s, e in runs:
        if merged and (e - s) < 4:
            pl, ps, pe = merged[-1]
            merged[-1] = (pl, ps, e)
        else:
            merged.append((label, s, e))

    sections: List[Section] = []
    seen_high = False
    for idx, (label, s, e) in enumerate(merged):
        if len(merged) == 1:
            name = "verse"
        elif idx == 0 and label == "low":
            name = "intro"
        elif label == "high":
            name = "chorus" if not seen_high else "chorus"
            seen_high = True
        elif label == "low" and seen_high:
            name = "bridge"
        elif idx == len(merged) - 1 and label != "high":
            name = "outro"
        else:
            name = "verse"
        start_s = float(beat_times[s])
        if e < beat_times.size:
            end_s = float(beat_times[e])
        else:
            end_s = duration_s
        energy = float(smoothed[s:e].mean()) if e > s else 0.0
        sections.append(Section(start_s=start_s, end_s=end_s, label=label,
                                name=name, energy=energy))
    if sections:
        # The lead-in before the first beat belongs to the first section.
        sections[0].start_s = 0.0
        if sections[-1].end_s < duration_s:
            sections[-1].end_s = duration_s
    return sections


def analyze_beats(audio: AudioData, meter: int = 4,
                  bpm_hint: Optional[float] = None) -> BeatAnalysis:
    """Detect the beat grid, downbeats, tempo and sections of *audio*.

    :param meter: beats per bar (4 for 4/4, 3 for 3/4, ...).
    :param bpm_hint: if set, the beat period is locked to this tempo instead of
        being estimated from the audio (useful for heavily rubato songs).
    """
    if meter < 2:
        raise ValueError("meter must be >= 2")
    samples = audio.samples.astype(np.float32)
    sr = audio.sample_rate
    hop, n_fft = 512, 2048
    fps = sr / hop

    mag, norm_freqs = _stft_magnitude(samples, n_fft=n_fft, hop=hop)
    freqs = norm_freqs * sr
    oe = _onset_envelope(mag)

    low_mask = freqs <= 1600.0
    low_mag = mag[:, low_mask] if low_mask.any() else mag

    if bpm_hint and bpm_hint > 0:
        period = fps * 60.0 / bpm_hint
        confidence = 1.0
    else:
        period, confidence = _estimate_period_frames(oe, fps)

    beat_frames = _track_beats(oe, period)
    if beat_frames.size < 4:
        # Fall back to a uniform metronome grid at the estimated tempo.
        beat_times = np.arange(0.0, audio.duration_s, period / fps)
        beat_frames = np.clip((beat_times * fps).astype(int), 0, oe.size - 1)
    else:
        beat_times = beat_frames.astype(np.float64) / fps

    # Re-estimate the tempo from the tracked intervals for accuracy, then fold
    # it into a musical 55..190 BPM window (picks the octave that feels right).
    if beat_times.size >= 4:
        median_iv = float(np.median(np.diff(beat_times)))
        if median_iv > 1e-3:
            period = median_iv * fps
    bpm = 60.0 * fps / period if period > 0 else 120.0
    while bpm < 55.0:
        bpm *= 2.0
    while bpm > 190.0:
        bpm /= 2.0

    # Beat energies from the frame RMS envelope.
    rms = _frame_rms_envelope(samples, sr, n_fft=n_fft, hop=hop)
    energies = np.array([rms[min(int(t * fps), rms.size - 1)] for t in beat_times])
    emax = float(energies.max()) if energies.size else 0.0
    energies_n = energies / emax if emax > 1e-9 else energies

    phase = _detect_phase(beat_frames, low_mag, meter)

    beats: List[Beat] = []
    for i, t in enumerate(beat_times):
        beats.append(Beat(
            time_s=float(t),
            index=i,
            index_in_bar=((i - phase) % meter) + 1,
            energy=float(energies_n[i]) if i < energies_n.size else 0.0,
        ))

    sections = _label_sections(beat_times, energies_n, audio.duration_s)

    # Confidence: onset strength at beats relative to the whole envelope.
    if oe.size and beat_frames.size:
        at_beats = float(np.mean([oe[min(f, oe.size - 1)] for f in beat_frames]))
        base = float(np.mean(oe)) + 1e-9
        match = float(np.clip(at_beats / (base * 2.0), 0.0, 1.0))
        confidence = float(np.clip(0.5 * confidence + 0.5 * match, 0.0, 1.0))

    return BeatAnalysis(
        path=audio.path,
        bpm=float(bpm),
        meter=meter,
        duration_s=audio.duration_s,
        beats=beats,
        sections=sections,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# xLights *_beats.txt import/export
# ---------------------------------------------------------------------------

def load_beats_txt(path: str, meter: int = 4, duration_s: Optional[float] = None) -> BeatAnalysis:
    """Load a xLights/VAMP ``*_beats.txt`` file as a :class:`BeatAnalysis`.

    Row format (tab or whitespace separated): ``start_seconds  end_seconds  beat_in_bar``.
    The third column is optional; when missing a plain 4/4 bar numbering is assumed.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    times: List[float] = []
    in_bars: List[Optional[int]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", "\t").split()
            try:
                t = float(parts[0])
            except ValueError:
                continue
            bar: Optional[int] = None
            if len(parts) >= 3:
                try:
                    bar = int(float(parts[2]))
                except ValueError:
                    bar = None
            times.append(t)
            in_bars.append(bar)

    if not times:
        raise ValueError(f"No beats found in {path}")
    times_a = np.asarray(times, dtype=float)
    if duration_s is None:
        duration_s = float(times_a[-1]) + 0.5

    # Derive BPM from the median interval.
    if len(times_a) >= 2:
        iv = float(np.median(np.diff(times_a)))
        bpm = 60.0 / iv if iv > 1e-3 else 120.0
    else:
        bpm = 120.0
    while bpm < 55.0:
        bpm *= 2.0
    while bpm > 190.0:
        bpm /= 2.0

    # Estimate section energy from inter-beat spacing stability fallback = flat.
    beats: List[Beat] = []
    detected_meter = meter
    if in_bars and all(b is not None for b in in_bars):
        vals = [b for b in in_bars if b is not None]
        detected_meter = max(vals)
        if detected_meter < 2:
            detected_meter = meter
    for i, t in enumerate(times):
        if in_bars[i] is not None and in_bars[i] >= 1:
            in_bar = ((in_bars[i] - 1) % detected_meter) + 1
        else:
            in_bar = (i % detected_meter) + 1
        beats.append(Beat(time_s=float(t), index=i, index_in_bar=in_bar, energy=0.5))

    sections: List[Section] = []
    if beats:
        sections.append(Section(start_s=0.0, end_s=float(duration_s),
                                label="mid", name="verse", energy=0.5))
    return BeatAnalysis(path=path, bpm=float(bpm), meter=detected_meter,
                        duration_s=float(duration_s), beats=beats,
                        sections=sections, confidence=1.0)


def save_beats_txt(analysis: BeatAnalysis, path: str) -> str:
    """Write *analysis* to *path* in the xLights ``*_beats.txt`` format."""
    with open(path, "w", encoding="utf-8") as fh:
        for b in analysis.beats:
            fh.write(f"{b.time_s:.6f}\t{b.time_s:.6f}\t{b.index_in_bar}\n")
    return path
