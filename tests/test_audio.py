"""Bell audio synthesis and playback fallback tests."""

from __future__ import annotations

import wave

from sacs import audio


def test_synthesise_wav_writes_valid_pcm(tmp_path):
    path = tmp_path / "bell.wav"
    audio.synthesise_wav(path, duration=0.2)
    assert path.exists() and path.stat().st_size > 0
    with __import__("wave").open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 44100
        assert handle.getnframes() > 0


def test_resolve_sound_path_generates_missing_file(tmp_path, monkeypatch):
    from sacs.config import override_settings, reset_settings
    from sacs import audio as audio_mod

    override_settings(home=tmp_path, database_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    try:
        path = audio_mod.resolve_sound_path()
        assert path.exists()
        assert path.name == "bell.wav"
    finally:
        reset_settings()


def test_play_missing_file_returns_false(tmp_path):
    missing = tmp_path / "nope.wav"
    assert audio.play(missing) is False


def test_is_available_is_bool():
    assert isinstance(audio.is_available(), bool)
