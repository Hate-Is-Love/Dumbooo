"""Screenshots, taken only when a capability needs one.

Images are kept in memory and returned as PNG bytes. Nothing is written to disk
unless the user explicitly asks to save a screenshot (``screen.save``).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    pass


def capture(rect: tuple[int, int, int, int] | None = None) -> tuple[bytes, tuple[int, int]]:
    """PNG of ``rect`` (left, top, right, bottom) or of the primary monitor."""
    try:
        import mss
        import mss.tools
    except ImportError as exc:  # pragma: no cover
        raise CaptureError("Screen capture is not available (the 'mss' package is missing).") from exc
    factory = getattr(mss, "MSS", None) or mss.mss
    try:
        with factory() as sct:
            if rect is None:
                region = sct.monitors[1]
            else:
                left, top, right, bottom = rect
                width, height = right - left, bottom - top
                if width < 10 or height < 10:
                    raise CaptureError("That window is too small or minimised.")
                region = {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}
            shot = sct.grab(region)
            png = mss.tools.to_png(shot.rgb, shot.size)
            return png, (shot.size.width, shot.size.height)
    except CaptureError:
        raise
    except Exception as exc:
        log.warning("Screen capture failed: %s", exc.__class__.__name__)
        raise CaptureError(f"The screen could not be captured ({exc.__class__.__name__}).") from exc
