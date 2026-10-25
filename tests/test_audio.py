"""Beat analysis: decoding, tempo, alignment, downbeats, sections, beats.txt."""

from __future__ import annotations


import numpy as np
import pytest

from xlights_ai_agent.audio import (AudioDecodeError, analyze_beats,
                                    decode_audio, load_beats_txt,
                                    save_beats_txt)
from tests.conftest import click_track, write_wav


def test_decode_wav_mono_float(tmp_path):
    p = str(tmp_path / "s.wav")
    sig, _ = click_track(120.0, 2.0)
    write_wav(p, sig)
    audio = decode_audio(p)
    assert audio.sample_rate == 44100
    assert abs(audio.duration_s - 2.0) < 0.01
    assert audio.samples.dtype == np.float32
    assert -1.001 <= float(audio.samples.min()) and float(audio.samples.max()) <= 1.001


def test_decode_missing_file():
    with pytest.raises(AudioDecodeError):
        decode_audio("/nonexistent/song.wav")


def test_tempo_and_alignment_120(song_120):
    a = analyze_beats(decode_audio(song_120["path"]), meter=4)
    assert abs(a.bpm - 120.0) < 4.0
    gt = song_120["beats"]
    errs = []
    for g in gt:
        nearest = min(a.beats, key=lambda b: abs(b.time_s - g))
        errs.append(abs(nearest.time_s - g))
    aligned = np.array(errs) < 0.05
    assert aligned.mean() > 0.9, f"only {aligned.mean():.0%} aligned"
    assert a.confidence > 0.3


def test_tempo_no_octave_error_174(click_174):
    """174 BPM must not fold down to 87 (regression: sub-harmonic penalty)."""
    a = analyze_beats(decode_audio(click_174["path"]), meter=4)
    assert abs(a.bpm - 174.0) < 174.0 * 0.04, f"got {a.bpm}"


@pytest.mark.parametrize("bpm", [90.0, 138.0])
def test_tempo_sweep(tmp_path, bpm):
    sig, _ = click_track(bpm, 12.0)
    p = str(tmp_path / f"s{bpm}.wav")
    write_wav(p, sig)
    a = analyze_beats(decode_audio(p))
    assert abs(a.bpm - bpm) < bpm * 0.03


def test_downbeat_phase_and_meter(song_120):
    a = analyze_beats(decode_audio(song_120["path"]), meter=4)
    # accents are every 4th beat starting at t=0 -> first beat is the downbeat
    assert a.beats[0].index_in_bar == 1
    assert a.beats[1].index_in_bar == 2
    assert len(a.downbeats) >= 4
    pattern = [b.index_in_bar for b in a.beats[:8]]
    assert pattern == [1, 2, 3, 4, 1, 2, 3, 4]


def test_meter_3(tmp_path):
    sig, _ = click_track(96.0, 12.0, accent_every=3)
    p = str(tmp_path / "waltz.wav")
    write_wav(p, sig)
    a = analyze_beats(decode_audio(p), meter=3)
    assert [b.index_in_bar for b in a.beats[:6]] == [1, 2, 3, 1, 2, 3]


def test_sections_detected(song_120):
    a = analyze_beats(decode_audio(song_120["path"]), meter=4)
    names = [s.name for s in a.sections]
    labels = [s.label for s in a.sections]
    # quiet intro (0-8s), loud chorus (8-16s), quiet tail (16-24s)
    assert "intro" in names, names
    assert "high" in labels, labels
    chorus = a.sections[labels.index("high")]
    assert chorus.start_ms <= 9000 <= chorus.end_ms
    assert chorus.end_ms >= 15000
    # sections tile the song
    assert a.sections[0].start_ms == 0
    assert a.sections[-1].end_ms >= a.duration_ms - 1000


def test_beats_txt_roundtrip(song_120, tmp_path):
    a = analyze_beats(decode_audio(song_120["path"]), meter=4)
    out = str(tmp_path / "song_beats.txt")
    save_beats_txt(a, out)
    first = open(out).readline().split()
    assert len(first) == 3            # start<TAB>end<TAB>beat_in_bar
    float(first[0])
    b = load_beats_txt(out, meter=4, duration_s=a.duration_s)
    assert abs(b.bpm - a.bpm) < 1.0
    assert len(b.beats) == len(a.beats)
    assert b.beats[0].index_in_bar == 1


def test_bpm_hint_locks_tempo(song_120):
    a = analyze_beats(decode_audio(song_120["path"]), meter=4, bpm_hint=120.0)
    assert abs(a.bpm - 120.0) < 1.5
