"""Protected storage for the optional "Stay signed in on this PC" session token.

On Windows the token is encrypted with DPAPI (``CryptProtectData``), which
ties it to the current Windows user account: another Windows user, or a copy
of the file on another PC, cannot decrypt it. The database only ever stores a
SHA-256 hash of the token. On other systems "stay signed in" is unavailable.

Limit (documented in the README): anyone who can log in to *your* Windows
account can also use the remembered session until it expires or you sign out.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def available() -> bool:
    return sys.platform == "win32"


def protect(data: bytes, purpose: str) -> bytes:
    """Encrypt ``data`` for the current Windows user (DPAPI), bound to ``purpose`` (e.g. "face")."""
    return _dpapi(data, True, b"DUMBOO-" + purpose.encode())


def unprotect(data: bytes, purpose: str) -> bytes:
    return _dpapi(data, False, b"DUMBOO-" + purpose.encode())


def _dpapi(data: bytes, protect: bool, entropy_bytes: bytes = b"DUMBOO-session-v1") -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), buffer)
    blob_out = Blob()
    entropy_buf = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    entropy = Blob(len(entropy_bytes), entropy_buf)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, token: str) -> bool:
        if not available():
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(_dpapi(token.encode("utf-8"), protect=True))
            return True
        except OSError:
            log.warning("Could not store the remembered session securely; you will need to sign in next time")
            return False

    def load(self) -> str | None:
        if not available() or not self.path.exists():
            return None
        try:
            return _dpapi(self.path.read_bytes(), protect=False).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            log.warning("Remembered session could not be read; it was discarded")
            self.clear()
            return None

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
