"""Local bell audio: synthesis and best-effort playback.

SACS rings a real bell without shipping binary assets or adding dependencies.
A short multi-tone chime is synthesised as a PCM WAV file using the standard
library (``wave`` + ``struct``) the first time it is needed, then played
through the platform's native facility:

* Windows  -> ``winsound`` (asynchronous when not blocking)
* macOS    -> ``afplay``
* Linux    -> ``paplay`` / ``aplay`` / ``ffplay`` when present
* fallback -> the terminal bell character

Playback is deliberately best-effort: a missing or broken audio device must
never crash the bell scheduler or an API request. Failures are logged and
reported to callers as ``False`` so they can be surfaced as a notification.

The generated sound is written under the configured SACS home directory and is
regenerated if it is deleted. A custom WAV can be supplied with
``SACS_BELL_SOUND``.
"""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

logger = logging.getLogger("sacs.audio")

_DEFAULT_NAME = "bell.wav"
_SAMPLE_RATE = 44_100
_DURATION_SECONDS = 1.6
# A bright, bell-like triad. Distinct partials give a metallic strike.
_PARTIALS = (880.0, 1174.66, 1567.98)


def synthesise_wav(
    path: Path,
    *,
    sample_rate: int = _SAMPLE_RATE,
    duration: float = _DURATION_SECONDS,
) -> Path:
    """Write a decaying bell chime to ``path`` as 16-bit mono PCM WAV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    total = int(sample_rate * duration)
    frames = bytearray()
    for index in range(total):
        t = index / sample_rate
        envelope = math.exp(-3.5 * t)
        strike = math.exp(-45.0 * t)
        tone = sum(math.sin(2.0 * math.pi * freq * t) for freq in _PARTIALS) / len(_PARTIALS)
        sample = (tone * 0.7 + strike * 0.3) * envelope
        sample = max(-1.0, min(1.0, sample))
        frames += struct.pack("<h", int(sample * 32767 * 0.8))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(frames))
    return path


def default_sound_path() -> Path:
    """Location of the built-in bell sound within the SACS home directory."""
    from .config import get_settings

    settings = get_settings()
    return settings.home / _DEFAULT_NAME


def resolve_sound_path() -> Path:
    """Return the sound to play: the custom one from settings or the built-in."""
    from .config import get_settings

    settings = get_settings()
    custom = (settings.bell_sound or "").strip()
    path = Path(custom).expanduser() if custom else default_sound_path()
    if not path.exists():
        synthesise_wav(path)
    return path


def is_available() -> bool:
    """Whether a real playback backend exists on this platform."""
    if sys.platform.startswith("win"):
        return True
    if sys.platform == "darwin":
        return shutil.which("afplay") is not None
    return any(shutil.which(prog) for prog in ("paplay", "aplay", "ffplay"))


def _play_windows(path: Path, blocking: bool) -> bool:
    import winsound  # type: ignore[import-not-found]

    flags = winsound.SND_FILENAME
    flags |= winsound.SND_SYNC if blocking else winsound.SND_ASYNC
    winsound.PlaySound(str(path), flags)
    return True


def _play_posix(path: Path, blocking: bool) -> bool:
    if sys.platform == "darwin":
        candidates = [["afplay", str(path)]]
    else:
        candidates = [
            ["paplay", str(path)],
            ["aplay", "-q", str(path)],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
        ]
    for argv in candidates:
        if shutil.which(argv[0]) is None:
            continue
        if blocking:
            subprocess.run(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


def play(path: Path | None = None, *, blocking: bool = False) -> bool:
    """Play ``path`` (or the built-in bell). Returns whether playback started."""
    try:
        target = Path(path) if path is not None else resolve_sound_path()
    except Exception:  # noqa: BLE001 - synthesis failure must not propagate
        logger.exception("could not prepare bell sound")
        return False
    if not target.exists():
        logger.warning("bell sound missing: %s", target)
        return False
    try:
        if sys.platform.startswith("win"):
            return _play_windows(target, blocking)
        if _play_posix(target, blocking):
            return True
    except Exception:  # noqa: BLE001 - never crash the caller
        logger.exception("bell playback failed")
        return False
    _terminal_bell()
    return False


def _terminal_bell() -> None:
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def ring(*, blocking: bool = False, background: bool = False) -> bool:
    """Ring the configured bell.

    ``background=True`` runs playback on a daemon thread so callers (such as
    the scheduler) never block. Returns whether playback was initiated.
    """
    if background:
        threading.Thread(target=play, kwargs={"blocking": blocking}, daemon=True).start()
        return True
    return play(blocking=blocking)
