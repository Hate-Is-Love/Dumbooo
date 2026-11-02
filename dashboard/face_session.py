"""Runs a face check (sign-in or setup) with the webcam on a background thread.

The camera is opened only for the check and released as soon as it ends (success, failure,
timeout or cancel). Frames stay in memory: each one is analysed, shown mirrored (like a
mirror/selfie) in the scanner, and dropped. ``progress`` carries the instruction, the step and
a small preview image; ``finished`` the final ``Progress``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)

PREVIEW_WIDTH = 360
FPS = 12


def open_webcam(index: int = 0):
    """The real webcam through OpenCV (Media Foundation on Windows)."""
    import cv2

    camera = cv2.VideoCapture(index)
    if not camera.isOpened():
        camera.release()
        raise OSError("The camera could not be opened. Check that no other app is using it and that camera access "
                      "is allowed (Windows Settings → Privacy & security → Camera → Let desktop apps access your camera).")
    return camera


def preview_image(frame: np.ndarray) -> QImage:
    """BGR frame -> small mirrored RGB QImage for the scanner."""
    h, w = frame.shape[:2]
    step = max(1, w // PREVIEW_WIDTH)
    small = frame[::step, ::-step, ::-1]  # downscale, mirror, BGR -> RGB
    small = np.ascontiguousarray(small)
    image = QImage(small.data, small.shape[1], small.shape[0], small.shape[1] * 3, QImage.Format.Format_RGB888)
    return image.copy()


class FaceSessionWorker(QObject):
    progress = Signal(dict)     # {"message", "stage", "step", "steps", "image": QImage|None, "box": QRect|None}
    finished = Signal(object)   # security.face.Progress (done)

    def __init__(self, engine, machine, camera_factory: Callable[[], Any] | None = None,
                 fps: int = FPS) -> None:
        super().__init__()
        self._engine = engine
        self._machine = machine
        self._open = camera_factory or open_webcam
        self._delay = 1.0 / fps
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="FaceCamera", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        from security.face import Progress

        result = Progress("failed", "The face check stopped.", done=True)
        camera = None
        kind = type(self._machine).__name__
        frames = faces = 0
        log.info("Face check started (%s); camera on", kind)
        try:
            camera = self._open()
            while not self._cancel.is_set():
                started = time.monotonic()
                ok, frame = camera.read()
                if not ok or frame is None:
                    result = Progress("failed", "The camera stopped sending pictures.", done=True)
                    break
                obs = self._engine.observe(frame)
                frames += 1
                faces += obs is not None
                step = self._machine.feed(obs)
                box = None
                if obs is not None:
                    w = frame.shape[1]
                    scale = max(1, w // PREVIEW_WIDTH)
                    x, y, bw, bh = obs.box
                    box = QRect((w - x - bw) // scale, y // scale, bw // scale, bh // scale)  # mirrored like the preview
                self.progress.emit({"message": step.message, "stage": step.stage, "step": step.step,
                                    "steps": step.steps, "image": preview_image(frame), "box": box})
                if step.done:
                    result = step
                    break
                self._cancel.wait(max(0.0, self._delay - (time.monotonic() - started)))
            else:
                result = Progress("failed", "Cancelled.", done=True)
        except OSError as exc:
            result = Progress("failed", str(exc), done=True)
        except Exception:
            log.exception("Face check failed")
            result = Progress("failed", "The face check failed unexpectedly. Use your password.", done=True)
        finally:
            if camera is not None:
                try:
                    camera.release()  # the camera light goes off here
                except Exception:
                    pass
        # Outcome only (stage, reason, how often a face was seen) — never any face data.
        log.info("Face check finished (%s): %s after %d frames, face seen in %d, best match %.2f · %s; camera off",
                 kind, "OK" if result.ok else result.stage, frames, faces, getattr(result, "best", -1.0),
                 result.message)
        self.finished.emit(result)
