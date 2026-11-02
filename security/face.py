"""Face detection, recognition and a simple liveness check for face unlock.

Models (OpenCV model zoo, run locally with OpenCV's DNN, no internet):
* YuNet (``face_detection_yunet_2023mar.onnx``): finds faces and 5 landmarks (eyes, nose,
  mouth corners).
* SFace (``face_recognition_sface_2021dec.onnx``): turns an aligned face into a 128-number
  feature vector. Two vectors of the same person have a high cosine similarity (OpenCV's
  reference threshold is 0.363; DUMBOO uses a stricter 0.42 and needs several frames).

Frames are processed in memory and never saved. Only feature vectors are kept (encrypted,
see ``services/face_service.py``).

Liveness: a photo can be recognised, but it cannot turn its head. Sign-in therefore asks the
user to turn their head slightly and look back, and checks that the head angle (nose position
between the eyes) really changes while the face stays the same person. Limit (README): a video
of the user turning their head could still pass; Windows Hello's infrared camera is stronger.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
MODEL_NAME = "sface_2021dec"

MATCH = 0.42           # same person, confidently (one frame)
MATCH_TURNED = 0.30    # same person while the head is turned (features are less stable at an angle)
MIN_FACE_PX = 70       # smaller faces are too far away to recognise reliably
MIN_SCORE = 0.80       # detector confidence
ENROLL_SAMPLES = 8     # what face setup stores; these originals are never replaced
LEARNED_MAX = 12       # extra samples kept from confirmed sign-ins (different light, glasses …)


@dataclass
class FaceObservation:
    box: tuple[int, int, int, int]       # x, y, w, h in the frame
    score: float
    yaw: float                           # nose offset between the eyes / eye distance (0 = facing the camera)
    embedding: np.ndarray                # unit-length SFace vector
    faces: int = 1                       # how many faces were in the frame

    @property
    def usable(self) -> bool:
        return self.faces == 1 and self.score >= MIN_SCORE and min(self.box[2], self.box[3]) >= MIN_FACE_PX


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


class FaceEngine:
    """Wraps YuNet + SFace. Loading is lazy; ``available`` / ``error`` say if face unlock can work."""

    def __init__(self, models_dir: Path) -> None:
        self._dir = models_dir
        self._detector = None
        self._recognizer = None
        self.error: str | None = None

    @property
    def available(self) -> bool:
        if not ((self._dir / DETECTOR_FILE).exists() and (self._dir / RECOGNIZER_FILE).exists()):
            self.error = "the face models are missing (run python scripts/download_models.py)"
            return False
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.error = "OpenCV is not installed (pip install -r requirements.txt)"
            return False
        return True

    def _load(self) -> None:
        if self._detector is None:
            import cv2

            self._detector = cv2.FaceDetectorYN.create(str(self._dir / DETECTOR_FILE), "", (320, 320), 0.7, 0.3, 5)
            self._recognizer = cv2.FaceRecognizerSF.create(str(self._dir / RECOGNIZER_FILE), "")

    def observe(self, frame: np.ndarray) -> FaceObservation | None:
        """The largest face in a BGR frame, or None if there is no face."""
        self._load()
        h, w = frame.shape[:2]
        self._detector.setInputSize((w, h))
        _ok, faces = self._detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        face = max(faces, key=lambda f: f[2] * f[3])
        aligned = self._recognizer.alignCrop(frame, face)
        feature = self._recognizer.feature(aligned).flatten().astype(np.float32)
        feature /= np.linalg.norm(feature) + 1e-9
        return FaceObservation(tuple(int(v) for v in face[:4]), float(face[14]), yaw_of(face), feature, len(faces))


def yaw_of(face) -> float:
    """Head turn from YuNet landmarks: 0 facing the camera, about ±0.3 when turned noticeably."""
    rx, ry, lx, ly, nx = face[4], face[5], face[6], face[7], face[8]
    eye_distance = max(1.0, float(np.hypot(lx - rx, ly - ry)))
    return float((nx - (rx + lx) / 2) / eye_distance)


# -- step-by-step checks (pure: fed with observations, so they are testable without a camera) --------------
@dataclass
class Progress:
    stage: str                 # look | turn | back | done | failed  (enrolment: front | side | other | done | failed)
    message: str
    done: bool = False
    ok: bool = False
    user_id: int | None = None
    step: int = 0              # for a progress bar
    steps: int = 1
    best: float = -1.0         # best match score seen (diagnostics: why a sign-in failed)
    samples: list = field(default_factory=list)   # confirmed frontal features of a successful sign-in


@dataclass
class FaceSignIn:
    """look (recognised in 3 frames) -> turn (head angle changes, same person) -> back (facing again)."""

    identify: Callable[[np.ndarray], tuple[int | None, float]]
    timeout_s: float = 20.0
    clock: Callable[[], float] = time.monotonic
    stage: str = "look"
    _start: float | None = None
    _user: int | None = None
    _hits: int = 0
    _base_yaw: float = 0.0
    _unknown_since: float | None = None
    _reference: np.ndarray | None = None
    _final: Progress | None = None
    _best: float = -1.0
    _confirmed: list = field(default_factory=list)

    def feed(self, obs: FaceObservation | None) -> Progress:
        now = self.clock()
        if self._start is None:
            self._start = now
        if self._final is not None:  # finished checks stay finished
            return self._final
        result = self._feed(obs, now)
        result.best = round(self._best, 3)
        if result.done:
            self._final = result
        return result

    def _feed(self, obs: FaceObservation | None, now: float) -> Progress:
        if now - self._start > self.timeout_s:
            self.stage = "failed"
            return Progress("failed", "Face sign-in timed out. Try again in better light, or use your password.",
                            done=True)
        if obs is None:
            return Progress(self.stage, "Look at the camera.", step=self._step(), steps=3)
        if obs.faces > 1:
            return Progress(self.stage, "Only one face, please.", step=self._step(), steps=3)
        if not obs.usable:
            return Progress(self.stage, "Come a little closer to the camera.", step=self._step(), steps=3)
        if self.stage == "look":
            user, score = self.identify(obs.embedding)
            self._best = max(self._best, score)
            if user is None or score < MATCH:
                self._unknown_since = self._unknown_since or now
                if now - self._unknown_since > 6.0:
                    self.stage = "failed"
                    return Progress("failed", "Face not recognised. Use your password, or set up face unlock on "
                                    "the ACCOUNT page.", done=True)
                self._hits = 0
                return Progress("look", "Hold still… checking your face.", step=0, steps=3)
            self._unknown_since = None
            if self._user not in (None, user):
                self._hits = 0
            self._user, self._hits = user, self._hits + 1
            self._base_yaw = obs.yaw if self._hits == 1 else (self._base_yaw + obs.yaw) / 2
            self._reference = obs.embedding
            self._confirmed.append(obs.embedding)
            if self._hits >= 3:
                self.stage = "turn"
                return Progress("turn", "Recognised. Now turn your head slightly to one side.", step=1, steps=3)
            return Progress("look", "Hold still… checking your face.", step=0, steps=3)
        if cosine(obs.embedding, self._reference) < MATCH_TURNED:
            return Progress(self.stage, "Keep your face in view.", step=self._step(), steps=3)
        if self.stage == "turn":
            if abs(obs.yaw - self._base_yaw) >= 0.22:
                self.stage = "back"
                return Progress("back", "Good. Now look straight at the camera again.", step=2, steps=3)
            return Progress("turn", "Turn your head slightly to one side.", step=1, steps=3)
        if self.stage == "back":
            user, score = self.identify(obs.embedding)
            if abs(obs.yaw - self._base_yaw) <= 0.12 and user == self._user and score >= MATCH:
                self.stage = "done"
                return Progress("done", "Face confirmed.", done=True, ok=True, user_id=user, step=3, steps=3,
                                samples=[self._confirmed[0], obs.embedding] if self._confirmed else [obs.embedding])
            return Progress("back", "Now look straight at the camera again.", step=2, steps=3)
        return Progress(self.stage, "", done=self.stage in ("done", "failed"))

    def _step(self) -> int:
        return {"look": 0, "turn": 1, "back": 2}.get(self.stage, 3)


@dataclass
class FaceEnrollment:
    """Collects 8 samples: 4 facing the camera, 2 turned to one side, 2 to the other; all the same person."""

    timeout_s: float = 45.0
    clock: Callable[[], float] = time.monotonic
    samples: list = field(default_factory=list)
    stage: str = "front"
    _start: float | None = None
    _side: float = 0.0
    _last_take: float = -1e9

    PLAN = (("front", 4, "Look straight at the camera."), ("side", 2, "Turn your head slightly to one side."),
            ("other", 2, "Now turn slightly to the other side."))

    def _needed(self) -> int:
        return sum(n for _s, n, _m in self.PLAN)

    def _message(self) -> str:
        return next(m for s, _n, m in self.PLAN if s == self.stage)

    def feed(self, obs: FaceObservation | None) -> Progress:
        now = self.clock()
        if self._start is None:
            self._start = now
        total = self._needed()
        if self.stage in ("done", "failed"):  # finished checks stay finished
            return Progress(self.stage, "Face unlock is ready." if self.stage == "done" else "Face setup stopped.",
                            done=True, ok=self.stage == "done", step=len(self.samples), steps=total)
        if now - self._start > self.timeout_s:
            self.stage = "failed"
            return Progress("failed", "Face setup timed out. Try again with your face well lit.", done=True)
        if obs is None:
            return Progress(self.stage, "Look at the camera.", step=len(self.samples), steps=total)
        if obs.faces > 1:
            return Progress(self.stage, "Only your face should be in view.", step=len(self.samples), steps=total)
        if not obs.usable:
            return Progress(self.stage, "Come a little closer to the camera.", step=len(self.samples), steps=total)
        if self.samples and cosine(obs.embedding, self.samples[0]) < MATCH_TURNED:
            return Progress(self.stage, "That doesn't look like the same face. Only you should be in view.",
                            step=len(self.samples), steps=total)
        if now - self._last_take < 0.35:  # spread the samples over time (different frames, expressions)
            return Progress(self.stage, self._message(), step=len(self.samples), steps=total)
        take = False
        if self.stage == "front" and abs(obs.yaw) <= 0.12:
            take = True
        elif self.stage == "side" and abs(obs.yaw) >= 0.15:
            self._side = self._side or float(np.sign(obs.yaw))
            take = np.sign(obs.yaw) == self._side
        elif self.stage == "other" and abs(obs.yaw) >= 0.15 and np.sign(obs.yaw) == -self._side:
            take = True
        if take:
            self.samples.append(obs.embedding)
            self._last_take = now
            done = {"front": 4, "side": 6, "other": 8}[self.stage]
            if len(self.samples) >= done:
                self.stage = {"front": "side", "side": "other", "other": "done"}[self.stage]
        if self.stage == "done":
            return Progress("done", "Face unlock is ready.", done=True, ok=True, step=total, steps=total)
        return Progress(self.stage, self._message(), step=len(self.samples), steps=total)

    def embeddings(self) -> np.ndarray:
        return np.stack(self.samples).astype(np.float32)
