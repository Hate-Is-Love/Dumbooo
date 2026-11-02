"""Download the local models DUMBOO uses (run once after installing requirements).

    python scripts/download_models.py            # everything below
    python scripts/download_models.py --skip-whisper

* Piper voice ``en_US-lessac-medium`` (~63 MB, MIT-licensed voice; from
  huggingface.co/rhasspy/piper-voices) -> models/piper/
* Whisper ``small.en`` (~465 MB): speech-to-text for your commands (understands Indian,
  British and American accents far better than base.en) -> models/whisper/
* Whisper ``base.en`` (~145 MB): the fast wake-phrase spotter, until a custom wake-word
  model is trained -> models/whisper/
* Face unlock: YuNet face detector (~0.2 MB) and SFace face recognizer (~37 MB) from the
  OpenCV model zoo (github.com/opencv/opencv_zoo) -> models/face/
* openWakeWord feature models (melspectrogram + embedding, ~2 MB) into the
  openwakeword package folder. Only needed once you train a custom
  "Hey DUMBOO" model (docs/WAKE_WORD_TRAINING.md).

The Silero VAD model ships inside the faster-whisper package; nothing to download.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

# Windows consoles default to a legacy code page; never crash on symbols like "→".
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FACE_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models/"
FACE_FILES = ("face_detection_yunet/face_detection_yunet_2023mar.onnx",
              "face_recognition_sface/face_recognition_sface_2021dec.onnx")
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/"
PIPER_FILES = ("en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx.json")


def download(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(f"  already present: {target.relative_to(ROOT)}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as fh:  # noqa: S310 - fixed https URLs
        total = int(response.headers.get("Content-Length", 0))
        done = 0
        while chunk := response.read(1 << 16):
            fh.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r    {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    tmp.replace(target)


def piper() -> None:
    print("Piper voice:")
    for name in PIPER_FILES:
        download(PIPER_BASE + name, ROOT / "models" / "piper" / name)


def face_models() -> None:
    print("Face unlock models (OpenCV model zoo):")
    for name in FACE_FILES:
        download(FACE_BASE + name, ROOT / "models" / "face" / name.split("/")[-1])


def whisper(models: list[str]) -> None:
    from faster_whisper import WhisperModel

    for name in models:
        print(f"Whisper {name}: downloading/verifying (first time can take a few minutes)…")
        WhisperModel(name, device="cpu", compute_type="int8", download_root=str(ROOT / "models" / "whisper"))
        print("  ready")


def openwakeword_features() -> None:
    print("openWakeWord feature models:")
    import openwakeword
    from openwakeword.utils import download_models

    # Only the shared feature extractors; no pretrained wake words are needed.
    download_models(model_names=["__none__"])
    folder = Path(openwakeword.__file__).parent / "resources" / "models"
    for name in ("melspectrogram.onnx", "embedding_model.onnx"):
        print(f"  {'found' if (folder / name).exists() else 'MISSING'}: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-whisper", action="store_true")
    parser.add_argument("--skip-piper", action="store_true")
    parser.add_argument("--skip-openwakeword", action="store_true")
    args = parser.parse_args()
    try:
        if not args.skip_piper:
            piper()
        if not args.skip_openwakeword:
            openwakeword_features()
        face_models()
        if not args.skip_whisper:
            whisper(["base.en", "small.en"])
    except Exception as exc:
        print(f"\nDownload failed: {exc}\nCheck your internet connection and run the script again.")
        return 1
    print("\nAll models ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
