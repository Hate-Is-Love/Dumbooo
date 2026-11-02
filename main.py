"""DUMBOO desktop assistant — entry point.

    python main.py                 start in standby (dashboard hidden, wake word active)
    python main.py --show          start with the dashboard visible
    python main.py --text-only     no microphone; type to DUMBOO
    python main.py --list-devices  print microphones and speakers, then exit
    python main.py --diagnose      print a diagnostics report, then exit
"""

from __future__ import annotations

import argparse
import logging
import sys

# Windows consoles default to a legacy code page; never crash on symbols like "→".
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

if sys.version_info[:2] != (3, 11):
    print(f"Warning: DUMBOO is developed for Python 3.11; you are running {sys.version.split()[0]}.")

from utils.env import load_dotenv  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402
from utils.paths import default_paths  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DUMBOO personal desktop assistant")
    parser.add_argument("--show", action="store_true", help="show the dashboard at startup")
    parser.add_argument("--text-only", action="store_true", help="disable the microphone for this run")
    parser.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    parser.add_argument("--diagnose", action="store_true", help="print diagnostics and exit")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    paths = default_paths().ensure()
    loaded = load_dotenv(paths.root / ".env")  # API keys and AI_* settings; real env variables win
    log_file = configure_logging(paths.logs_dir, logging.DEBUG if args.debug else logging.INFO)
    if loaded:
        logging.getLogger(__name__).info("Loaded %d setting(s) from .env: %s", len(loaded), ", ".join(loaded))

    if args.list_devices:
        from audio import audio_devices

        for d in audio_devices.list_devices():
            kinds = ("IN " if d.max_input_channels else "   ") + ("OUT" if d.max_output_channels else "   ")
            default = (" (default input)" if d.is_default_input else "") + (" (default output)" if d.is_default_output else "")
            print(f"[{d.index:>2}] {kinds}  {d.label}{default}")
        return 0

    if args.diagnose:
        from services.settings_service import SettingsService
        from utils import diagnostics

        settings = SettingsService(paths.default_settings, paths.user_settings)
        print(diagnostics.format_report(diagnostics.collect(paths, settings.snapshot())))
        return 0

    logging.getLogger(__name__).info("Starting DUMBOO (log file: %s)", log_file)
    from app.application import run_gui

    return run_gui(paths, show=args.show, text_only=True if args.text_only else None)


if __name__ == "__main__":
    sys.exit(main())
