"""Enumerate and resolve microphones and speakers (PortAudio via sounddevice)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
except Exception as exc:  # PortAudio missing or broken
    sd = None
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = None


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} [{self.hostapi}]"


def backend_error() -> str | None:
    return _IMPORT_ERROR


def list_devices() -> list[AudioDevice]:
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        default_in, default_out = sd.default.device
    except Exception:
        log.exception("Could not query audio devices")
        return []
    result = []
    for index, d in enumerate(devices):
        result.append(AudioDevice(
            index=index,
            name=d["name"],
            hostapi=hostapis[d["hostapi"]]["name"],
            max_input_channels=d["max_input_channels"],
            max_output_channels=d["max_output_channels"],
            default_samplerate=d["default_samplerate"],
            is_default_input=index == default_in,
            is_default_output=index == default_out,
        ))
    return result


def _preferred_hostapi_order(dev: AudioDevice) -> int:
    # On Windows the same physical device appears once per host API. Prefer
    # WASAPI (low latency, resamples for us), then MME. WDM-KS is avoided.
    order = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2}
    return order.get(dev.hostapi, 3 if "WDM" not in dev.hostapi else 9)


def input_devices() -> list[AudioDevice]:
    return [d for d in list_devices() if d.max_input_channels > 0]


def output_devices() -> list[AudioDevice]:
    return [d for d in list_devices() if d.max_output_channels > 0]


def resolve(name: str | None, kind: str) -> int | None:
    """Map a saved device label/name to a PortAudio index.

    ``None`` means "system default". Raises ``LookupError`` if a named device
    is not connected, so the caller can report it instead of silently using a
    different microphone.
    """
    if name is None:
        return None
    candidates = input_devices() if kind == "input" else output_devices()
    exact = [d for d in candidates if d.label == name or d.name == name]
    if not exact:
        exact = [d for d in candidates if name.lower() in d.label.lower()]
    if not exact:
        raise LookupError(f"{kind} device '{name}' is not connected")
    return sorted(exact, key=_preferred_hostapi_order)[0].index


def availability() -> dict[str, bool | str | None]:
    if sd is None:
        return {"microphone": False, "speaker": False, "error": _IMPORT_ERROR}
    devices = list_devices()
    return {
        "microphone": any(d.max_input_channels > 0 for d in devices),
        "speaker": any(d.max_output_channels > 0 for d in devices),
        "error": None,
    }


_VIRTUAL = ("Sound Mapper", "Primary Sound")


def unique_labels(devices: list[AudioDevice]) -> list[str]:
    """One entry per physical device for the settings pickers.

    Windows lists every device once per host API (MME truncates names to 31
    characters, WDM-KS exposes raw driver endpoints). Show the WASAPI entries
    when there are any, otherwise MME, and hide the virtual "mapper" devices.
    """
    real = [d for d in devices if not any(v in d.name for v in _VIRTUAL)]
    for api in ("Windows WASAPI", "MME"):
        chosen = [d for d in real if d.hostapi == api]
        if chosen:
            return [d.label for d in chosen]
    best: dict[str, AudioDevice] = {}  # non-Windows: one entry per name
    for d in sorted(real, key=_preferred_hostapi_order):
        best.setdefault(d.name, d)
    return [d.label for d in best.values()]
