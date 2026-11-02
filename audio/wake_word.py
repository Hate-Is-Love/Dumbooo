"""Wake-phrase detection for "Hey DUMBOO".

No pretrained wake-word model recognises "Hey DUMBOO", so two engines exist:

``openwakeword`` (preferred)
    A custom openWakeWord model trained for the phrase (see
    ``docs/WAKE_WORD_TRAINING.md``). Tiny, constant CPU cost (~1-2 % of one
    core), runs on every 80 ms of audio, works well in noise. Used when the
    model file configured in ``wake_word.model_path`` exists.

``whisper_spotter`` (works out of the box)
    Silero VAD gates the audio, so nothing is transcribed while the room is
    quiet. Only the first 2.5 s of each burst of speech is transcribed by Whisper
    (``base.en`` by default: fast enough for every burst, while commands use the more
    accurate ``small.en``) and matched against the configured phrase
    by spelling and by consonant skeleton. This is not a continuously running
    recogniser, but each speech burst costs roughly 0.5-1 s of CPU on a laptop,
    and accuracy depends on how Whisper hears the name ("Dumbo", "Dombu" ...).
    It is the technically valid fallback until a custom model is trained.

``engine = "auto"`` picks openWakeWord when the custom model exists and the
spotter otherwise. The phrase itself is never substituted.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from audio import SAMPLE_RATE
from audio.speech_to_text import WhisperModelPool, transcribe_array
from audio.vad import SegmenterConfig, SpeechSegmenter

log = logging.getLogger(__name__)

DetectCallback = Callable[[float, str], None]  # (score, follow-up text after the phrase)


# --------------------------------------------------------------------------
# Phrase matching (used by the spotter; pure function, unit-tested)
# --------------------------------------------------------------------------
_GREETINGS = {"hey", "hi", "hay", "hei", "heh", "a", "ok", "okay", "yo", "hello", "eh"}


def normalize(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def skeleton(word: str) -> str:
    """Consonant skeleton: first letter + later consonants, repeats collapsed.

    Whisper spells an unfamiliar name inconsistently ("Dumbo", "Dombu",
    "dumble"), but the consonants stay stable: all become "dmb..".
    """
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return ""
    rest = re.sub(r"[aeiouy]", "", word[1:])
    return re.sub(r"(.)\1+", r"\1", word[0] + rest)


def _ends_in_vowel(word: str) -> bool:
    word = re.sub(r"[^a-z]", "", word.lower())
    return bool(word) and word[-1] in "aeiouy"


def name_similarity(candidate: str, name: str) -> float:
    spelling = difflib.SequenceMatcher(None, candidate, name).ratio()
    sound = difflib.SequenceMatcher(None, skeleton(candidate), skeleton(name)).ratio()
    score = 0.5 * spelling + 0.5 * sound
    # The skeleton ignores vowels, so "dumb" would look like "dumboo": a different final
    # sound (vowel vs consonant) is a different word.
    if _ends_in_vowel(candidate) != _ends_in_vowel(name):
        score *= 0.8
    return score


def name_evidence(words: list[tuple[str, float]], name: str) -> tuple[float, float, str]:
    """Best (similarity, Whisper's word probability, candidate) for ``name`` in the first words."""
    best = (0.0, 0.0, "")
    for i in range(min(len(words), 4)):
        for span in (1, 2):
            group = words[i:i + span]
            if len(group) < span:
                continue
            candidate = "".join(normalize(" ".join(w for w, _ in group)))
            if not candidate:
                continue
            similarity = name_similarity(candidate, name)
            if similarity > best[0]:
                best = (similarity, min(p for _, p in group), candidate)
    return best


@dataclass
class PhraseMatch:
    score: float
    followup: str


def match_phrase(text: str, phrase: str) -> PhraseMatch | None:
    """Fuzzy-match ``phrase`` (e.g. "hey dumboo") at the start of ``text``.

    Returns the best score in 0..1 and any words spoken after the phrase.
    """
    words = normalize(text)
    target = normalize(phrase)
    if not words or not target:
        return None
    name = "".join(target[-1:])
    greeting = target[:-1]
    best: PhraseMatch | None = None
    # The name may appear as one token ("dumbo") or split ("dum boo").
    for i in range(min(len(words), 4)):
        for span in (1, 2):
            if i + span > len(words):
                continue
            candidate = "".join(words[i:i + span])
            similarity = name_similarity(candidate, name)
            before = words[:i]
            if greeting:
                if not before:  # greeting dropped by the recogniser ("Dumbo, ...")
                    score = similarity * 0.85
                elif len(before) <= 2 and (before[-1] in _GREETINGS or
                                           difflib.SequenceMatcher(None, before[-1], greeting[-1]).ratio() >= 0.75):
                    score = similarity
                else:
                    continue  # the phrase must start the utterance
            else:
                if before:
                    continue
                score = similarity
            if best is None or score > best.score:
                best = PhraseMatch(score, " ".join(words[i + span:]))
    return best


def spotter_threshold(sensitivity: float) -> float:
    """Higher sensitivity accepts looser matches (more wakes, more false wakes)."""
    return 0.9 - 0.3 * float(sensitivity)


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------
class OpenWakeWordEngine:
    engine = "openwakeword"
    CHUNK = 1280  # 80 ms at 16 kHz, the size openWakeWord expects

    def __init__(self, model_path: Path, sensitivity: float, on_detect: DetectCallback, model_factory=None) -> None:
        if not model_path.exists():
            raise FileNotFoundError(f"custom wake-word model not found: {model_path}")
        if model_factory is None:
            from openwakeword.model import Model as model_factory
        try:
            self._model = model_factory(wakeword_models=[str(model_path)], inference_framework="onnx")
        except Exception as exc:
            raise RuntimeError(
                f"openWakeWord could not load {model_path.name}: {exc}. "
                "Run 'python scripts/download_models.py' to fetch its feature models."
            ) from exc
        self._on_detect = on_detect
        self.threshold = 1.0 - float(sensitivity)
        self._pending = np.zeros(0, dtype=np.int16)
        self.detail = f"custom model {model_path.name}"

    def process(self, raw: np.ndarray, clean: np.ndarray, speech_prob: float) -> None:
        pcm = (np.clip(raw, -1, 1) * 32767).astype(np.int16)
        self._pending = np.concatenate([self._pending, pcm])
        while len(self._pending) >= self.CHUNK:
            chunk, self._pending = self._pending[:self.CHUNK], self._pending[self.CHUNK:]
            scores = self._model.predict(chunk)
            score = max(scores.values()) if scores else 0.0
            if score >= self.threshold:
                self._model.reset()
                self._on_detect(float(score), "")

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        try:
            self._model.reset()
        except Exception:
            pass

    def close(self) -> None:
        pass


class WhisperSpotterEngine:
    """Checks the start of every speech burst for the wake phrase.

    * An invented name like "Dumboo" is often heard as "dumpling", "Dumbledore", "I Dumbo" ...
      in noise. If the plain transcript doesn't match, Whisper is asked again with the name
      as a hint (``initial_prompt``), and that answer is only trusted when Whisper is
      reasonably sure of the name (see ``_spot``).
    * Only the first ``HEAD_S`` seconds are transcribed to look for the phrase (cheap, even
      when people talk for a long time). A burst that starts with the phrase and goes on
      ("Hey DUMBOO, open Notepad and type ...") is transcribed in full for the command.
    * Whisper hears the original audio; noise suppression only steers the VAD (its
      artefacts confuse Whisper more than the noise does).
    * One transcription at a time; a burst that arrives meanwhile waits (only the newest).
    """

    engine = "whisper_spotter"
    HEAD_S = 2.5          # the phrase must start the burst; "Hey DUMBOO" takes about 1 s
    MAX_BURST_S = 10.0    # wake phrase + a spoken command fit in one burst
    HINT_MIN_PROB = 0.2   # how sure Whisper must be of the name it wrote when given the hint
    VETO_PROB = 0.45      # ... unless, unaided, it clearly heard a different similar word
    VETO_SIMILARITY = 0.6
    SURE_SIMILARITY = 0.8

    def __init__(self, pool: WhisperModelPool, model_name: str, phrase: str, sensitivity: float,
                 vad_threshold: float, on_detect: DetectCallback, preload: bool = True) -> None:
        self._pool = pool
        self._model_name = model_name
        self.phrase = phrase
        self.sensitivity = sensitivity
        self._on_detect = on_detect
        self._prompt = " ".join(w.capitalize() for w in normalize(phrase)) + "."
        self._segmenter = SpeechSegmenter(SegmenterConfig(
            threshold=vad_threshold, end_silence_ms=450, pre_roll_ms=300, min_speech_ms=250,
            max_record_s=self.MAX_BURST_S, listen_timeout_s=10_000,
        ))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WakeSpotter")
        self._lock = threading.Lock()
        self._busy = False
        self._pending = None
        self.detail = f"Whisper {model_name} phrase spotter (VAD-gated)"
        if preload:
            self._executor.submit(self._load)

    def _load(self) -> None:
        try:
            self._pool.get(self._model_name, "cpu", "int8")
        except Exception:
            log.exception("Wake spotter model failed to load")

    def process(self, raw: np.ndarray, clean: np.ndarray, speech_prob: float) -> None:
        for event in self._segmenter.process(raw, speech_prob):
            if event.kind != "end":
                continue
            with self._lock:
                if self._busy:
                    self._pending = event  # keep only the newest burst; never build a backlog
                    continue
                self._busy = True
            self._submit(event)

    def _submit(self, event) -> None:
        try:
            self._executor.submit(self._check, event)
        except RuntimeError:  # engine closed during a settings change
            with self._lock:
                self._busy, self._pending = False, None

    @staticmethod
    def _words(model, audio: np.ndarray, prompt: str | None) -> list[tuple[str, float]]:
        """(word, Whisper's probability for it) for a short burst."""
        if len(audio) < 0.1 * SAMPLE_RATE:
            return []
        # temperature 0 only: faster-whisper's default retries unsure (short, noisy) clips with
        # random sampling, which made the same "Hey DUMBOO" wake on one try and not the next,
        # and could take several seconds. max_new_tokens: 2.5 s holds at most ~10 words; on
        # unclear sound Whisper otherwise loops ("I'm not going to go back to the car" x20).
        segments, _info = model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                           condition_on_previous_text=False, initial_prompt=prompt,
                                           word_timestamps=True, temperature=0.0, max_new_tokens=24)
        words = []
        for seg in segments:
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                continue
            words += [(w.word.strip(), float(w.probability)) for w in (getattr(seg, "words", None) or [])]
        return words

    def _spot(self, model, head: np.ndarray) -> PhraseMatch | None:
        """Look for the phrase: first as Whisper hears it unaided, then with the name as a hint.

        The hint rescues real wake phrases that noise turned into "dumpling" or "Dumbledore",
        but it also makes Whisper write the name for sounds that merely resemble it. Measured
        on synthetic speech: for those, Whisper is almost never sure of the name it wrote
        (word probability ~0.0-0.1, real phrases mostly 0.2-0.85), and when Whisper unaided
        clearly heard a *different* similar word ("Jumbo", "Dumbledore"), that word wins.
        """
        threshold = spotter_threshold(self.sensitivity)
        name = "".join(normalize(self.phrase)[-1:])
        plain = self._words(model, head, None)
        match = match_phrase(" ".join(w for w, _ in plain), self.phrase)
        if match and match.score >= threshold:
            return match
        hinted = self._words(model, head, self._prompt)
        hinted_match = match_phrase(" ".join(w for w, _ in hinted), self.phrase)
        if not hinted_match or hinted_match.score < threshold:
            if hinted_match and hinted_match.score >= threshold - 0.15:
                log.info("Wake phrase near miss (score %.2f, needed %.2f)", hinted_match.score, threshold)
            return None
        _sim, probability, _cand = name_evidence(hinted, name)
        heard_sim, heard_prob, heard = name_evidence(plain, name)
        if probability < self.HINT_MIN_PROB:
            log.info("Wake phrase near miss (name probability %.2f, needed %.2f)", probability, self.HINT_MIN_PROB)
            return None
        if heard and self.VETO_SIMILARITY <= heard_sim < self.SURE_SIMILARITY and heard_prob >= self.VETO_PROB:
            log.info("Wake phrase rejected: a similar but different word was heard clearly")
            return None
        return hinted_match

    def _check(self, event) -> None:
        try:
            model = self._pool.get(self._model_name, "cpu", "int8")
            audio = event.audio
            head = audio[:int(self.HEAD_S * SAMPLE_RATE)]
            match = self._spot(model, head)
            if match is not None:
                followup = match.followup
                if event.truncated:
                    followup = ""  # the command was cut off: DUMBOO wakes and listens for it instead
                elif len(audio) > len(head):
                    full = transcribe_array(model, audio, "en", beam_size=1, initial_prompt=self._prompt)
                    full_match = match_phrase(full.text, self.phrase)
                    followup = full_match.followup if full_match else ""
                log.info("Wake phrase spotted (score %.2f)", match.score)
                self._on_detect(match.score, followup)
        except Exception:
            log.exception("Wake spotter transcription failed")
        finally:
            with self._lock:
                pending, self._pending = self._pending, None
                self._busy = pending is not None
            if pending is not None:
                self._submit(pending)

    def reset(self) -> None:
        self._segmenter.reset()
        with self._lock:
            self._pending = None

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------
# Service: engine selection, de-duplication and status
# --------------------------------------------------------------------------
class WakeWordService:
    """Owns the active wake engine and filters its detections.

    After a detection the service disarms itself until ``arm()`` is called
    (the orchestrator re-arms when DUMBOO goes back to IDLE or SLEEPING), and a
    cooldown period applies as well. Together these guarantee one wake event
    per spoken phrase.
    """

    name = "wake_word"

    def __init__(self, settings: dict, model_path: Path, pool: WhisperModelPool, vad_threshold: float,
                 on_wake: Callable[[str, float, str], None], clock=time.monotonic,
                 engine_factory: Callable[..., object] | None = None) -> None:
        self._settings = settings
        self._model_path = model_path
        self._pool = pool
        self._vad_threshold = vad_threshold
        self._on_wake = on_wake
        self._clock = clock
        self._engine_factory = engine_factory
        self._engine = None
        self._lock = threading.Lock()
        self._armed = True
        self._last_detection = -1e9
        self.error: str | None = None

    # -- engine lifecycle ------------------------------------------------
    def _build_engine(self):
        if self._engine_factory is not None:
            return self._engine_factory(self._handle_detection)
        s = self._settings
        choice = s["engine"]
        if choice == "auto":
            choice = "openwakeword" if self._model_path.exists() else "whisper_spotter"
        if choice == "openwakeword":
            return OpenWakeWordEngine(self._model_path, s["sensitivity"], self._handle_detection)
        return WhisperSpotterEngine(self._pool, s["spotter_model"], s["phrase"], s["sensitivity"],
                                    self._vad_threshold, self._handle_detection)

    def start(self) -> None:
        with self._lock:
            if self._engine is not None or not self._settings["enabled"]:
                return
            try:
                self._engine = self._build_engine()
                self.error = None
                log.info("Wake word engine: %s", self._engine.engine)
            except Exception as exc:
                self.error = str(exc)
                log.error("Wake word engine unavailable: %s", exc)
                raise

    def stop(self) -> None:
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.close()

    def reconfigure(self, settings: dict, vad_threshold: float, model_path: Path | None = None) -> None:
        self.stop()
        self._settings = settings
        self._vad_threshold = vad_threshold
        if model_path is not None:
            self._model_path = model_path
        if settings["enabled"]:
            try:
                self.start()
            except Exception:
                pass

    # -- audio path (audio pipeline thread) ------------------------------
    def process(self, raw: np.ndarray, clean: np.ndarray, speech_prob: float) -> None:
        engine = self._engine
        if engine is None or not self._armed:
            return
        engine.process(raw, clean, speech_prob)

    def _handle_detection(self, score: float, followup: str) -> None:
        now = self._clock()
        with self._lock:
            if not self._armed or now - self._last_detection < float(self._settings["cooldown_s"]):
                log.debug("Ignoring duplicate wake detection")
                return
            self._armed = False
            self._last_detection = now
            engine = self._engine
        if engine is not None:
            engine.reset()
        self._on_wake(engine.engine if engine else "unknown", score, followup)

    def arm(self) -> None:
        with self._lock:
            if not self._armed and self._engine is not None:
                self._engine.reset()
            self._armed = True

    def disarm(self) -> None:
        with self._lock:
            self._armed = False

    # -- status ------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def engine_name(self) -> str:
        engine = self._engine
        return engine.engine if engine else "none"

    @property
    def detail(self) -> str:
        if not self._settings["enabled"]:
            return "disabled in settings"
        if self.error:
            return f"unavailable: {self.error}"
        engine = self._engine
        return getattr(engine, "detail", "") if engine else "not running"
