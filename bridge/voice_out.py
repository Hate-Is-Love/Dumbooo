"""DUMBOO's own voice on the phone: the laptop's Piper voice speaks the answer, and the phone plays the audio.

The phone asks with ``{"t":"speak","id":7,"text":"..."}`` and gets ``speech.audio`` messages, one per sentence:
``{"t":"speech.audio","id":7,"seq":0,"rate":22050,"pcm":"<base64 16-bit mono>","last":false}``. If the voice isn't
available the phone gets ``speech.unavailable`` and uses its own voice instead.
"""

from __future__ import annotations

import base64
import logging
import threading
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from audio.text_to_speech import split_sentences

log = logging.getLogger(__name__)

MAX_TEXT = 1500


class PhoneVoice:
    """One Piper voice shared by all phones, loaded the first time a phone needs it (separate from the laptop's
    speaker queue, so a phone's answer never interrupts or waits for the laptop)."""

    def __init__(self, factory: Callable[[Path, float], object], model_path: Callable[[], Path],
                 speed: Callable[[], float]) -> None:
        self._factory = factory
        self._model_path = model_path
        self._speed = speed
        self._synth = None
        self._key: tuple | None = None
        self._lock = threading.Lock()

    def _voice(self):
        key = (str(self._model_path()), float(self._speed()))
        if self._synth is None or key != self._key:
            self._synth = self._factory(Path(key[0]), key[1])
            self._key = key
        return self._synth

    def chunks(self, text: str) -> Iterator[tuple[int, bytes]]:
        """(sample rate, 16-bit PCM) per sentence."""
        with self._lock:                 # Piper isn't thread-safe; phones take turns (sentences are short)
            synth = self._voice()
            rate = int(getattr(synth, "sample_rate", 22050))
            for sentence in split_sentences(text[:MAX_TEXT]):
                audio = [np.asarray(a, dtype=np.float32) for a in synth.synthesize(sentence)]
                if not audio:
                    continue
                pcm = (np.clip(np.concatenate(audio), -1.0, 1.0) * 32767).astype("<i2").tobytes()
                yield rate, pcm

    def speak_to(self, send: Callable[[dict], None], message_id, text: str) -> None:
        try:
            seq = 0
            for rate, pcm in self.chunks(text):
                send({"t": "speech.audio", "id": message_id, "seq": seq, "rate": rate,
                      "pcm": base64.b64encode(pcm).decode(), "last": False})
                seq += 1
            send({"t": "speech.audio", "id": message_id, "seq": seq, "rate": 0, "pcm": "", "last": True})
        except Exception as exc:
            log.warning("Phone voice failed: %s", exc)
            send({"t": "speech.unavailable", "id": message_id})
