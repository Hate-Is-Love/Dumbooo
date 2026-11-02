"""Manual microphone / VAD / wake-phrase check (nothing is recorded to disk).

    python scripts/mic_check.py                 # 15 s live level + speech meter
    python scripts/mic_check.py --transcribe    # also transcribe each utterance
    python scripts/mic_check.py --seconds 30 --device "Microphone Array"
    python scripts/mic_check.py --wake --seconds 60   # count real wake-word detections

Speak, pause, speak again. Each detected utterance is listed with its length.
With --transcribe, the text and whether it matches the wake phrase are shown.
With --wake, DUMBOO's real wake engine listens: say "Hey DUMBOO" several times (pause a
couple of seconds between tries) and each detection is counted.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Windows consoles default to a legacy code page; never crash on symbols like "→".
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audio.microphone import Microphone, MicrophoneError  # noqa: E402
from audio.noise_suppression import NoiseSuppressor  # noqa: E402
from audio.vad import SegmenterConfig, SpeechSegmenter, create_vad, rms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--device", default=None)
    parser.add_argument("--transcribe", action="store_true")
    parser.add_argument("--no-ns", action="store_true", help="disable noise suppression")
    parser.add_argument("--wake", action="store_true", help="run the real wake-word engine and count detections")
    parser.add_argument("--sensitivity", type=float, default=0.5)
    args = parser.parse_args()
    if args.wake:
        return wake_check(args)

    vad = create_vad()
    ns = NoiseSuppressor(0.6, enabled=not args.no_ns)
    seg = SpeechSegmenter(SegmenterConfig(listen_timeout_s=10_000))
    model = None
    if args.transcribe:
        from audio.speech_to_text import WhisperModelPool

        print("Loading Whisper base.en…")
        model = WhisperModelPool(ROOT / "models" / "whisper").get("base.en")
    mic = Microphone(args.device)
    try:
        mic.start()
    except MicrophoneError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"VAD engine: {vad.engine}. Noise suppression: {'off' if args.no_ns else 'on'}. Speak now…\n")
    end = time.monotonic() + args.seconds
    count = 0
    try:
        while time.monotonic() < end:
            frame = mic.read(0.5)
            if frame is None:
                print("\nNo audio arriving from the microphone!")
                return 1
            clean = ns.process(frame)
            prob = vad(clean)
            level = min(40, int(rms(clean) * 400))
            print(f"\r level {'#' * level:<40} speech {prob:4.2f} {'SPEECH' if prob >= 0.5 else '      '}", end="")
            for event in seg.process(clean, prob):
                if event.kind == "end":
                    count += 1
                    print(f"\n  utterance {count}: {event.duration:.2f} s", end="")
                    if model is not None:
                        from audio.speech_to_text import transcribe_array
                        from audio.wake_word import match_phrase, spotter_threshold

                        text = transcribe_array(model, event.audio, "en").text
                        match = match_phrase(text, "hey dumboo")
                        wake = match is not None and match.score >= spotter_threshold(0.5)
                        print(f"  “{text}”  wake phrase: {'YES' if wake else 'no'}", end="")
                    print()
                elif event.kind == "rejected":
                    print(f"\n  ignored: {event.reason}")
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
    print(f"\n\nDone. {count} utterance(s) detected. Dropped frames: {mic.dropped_frames}")
    return 0


def wake_check(args) -> int:
    import logging

    from audio.speech_to_text import WhisperModelPool
    from audio.wake_word import WhisperSpotterEngine

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    print("Loading Whisper base.en…")
    pool = WhisperModelPool(ROOT / "models" / "whisper")
    pool.get("base.en", "cpu", "int8")
    vad, ns = create_vad(), NoiseSuppressor(0.6, enabled=not args.no_ns)
    detections = []

    def on_detect(score: float, followup: str) -> None:
        detections.append(score)
        extra = f"  command: “{followup}”" if followup else ""
        print(f"\n  WAKE #{len(detections)}  (score {score:.2f}){extra}")

    engine = WhisperSpotterEngine(pool, "base.en", "hey dumboo", args.sensitivity, 0.5, on_detect, preload=False)
    words_of = engine._words

    def show_words(model, audio, prompt):  # show what Whisper heard (on screen only, never saved)
        words = words_of(model, audio, prompt)
        heard = " ".join(f"{w}({p:.2f})" for w, p in words) or "(nothing)"
        print(f"\n  [{time.strftime('%H:%M:%S')}] {len(audio) / 16000:.1f} s of speech, "
              f"{'with name hint' if prompt else 'heard'}: {heard}")
        return words

    engine._words = show_words
    peak_level = peak_prob = speech_s = 0.0
    mic = Microphone(args.device)
    try:
        mic.start()
    except MicrophoneError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"Listening for {args.seconds:.0f} s. Say “Hey DUMBOO” a few times, with a short pause between tries.\n")
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            frame = mic.read(0.5)
            if frame is None:
                print("\nNo audio arriving from the microphone!")
                return 1
            clean = ns.process(frame)
            prob = vad(clean)
            peak_level, peak_prob = max(peak_level, rms(frame)), max(peak_prob, prob)
            speech_s += len(frame) / 16000 if prob >= 0.5 else 0
            engine.process(frame, clean, prob)
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
    time.sleep(6)  # let the last check finish
    engine.close()
    print(f"\nDone. Wake word detected {len(detections)} time(s). Dropped frames: {mic.dropped_frames}")
    print(f"Loudest moment: level {peak_level:.4f}, speech probability {peak_prob:.2f}; "
          f"{speech_s:.1f} s counted as speech.")
    if speech_s < 1:
        print("Almost no speech reached the microphone: speak closer/louder, or pick the right device "
              "(--device, see 'python main.py --list-devices').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
