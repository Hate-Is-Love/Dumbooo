"""Forgot your DUMBOO password? Set a new one, on this PC.

    reset_password.bat                 (double-click it in the DUMBOO folder)
    python scripts/reset_password.py   (the same thing)

This works only on this PC, from the Windows account that holds DUMBOO's data: whoever can use that account can
already read DUMBOO's data folder, so it doesn't make anything weaker. It can't be used from the app, the phone or
over the network. Nothing is deleted: your tasks, history and memories stay. All signed-in sessions (including the
phone's) are signed out, the reset is written to the audit log, and you get a new recovery code: keep it somewhere
safe, then next time use "Forgot password?" on the sign-in screen instead.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from security import passwords as pw  # noqa: E402
from services.auth_service import AuthError, AuthService  # noqa: E402
from services.settings_service import SettingsService  # noqa: E402
from storage.local_database import LocalDatabase  # noqa: E402
from utils.paths import default_paths  # noqa: E402


def main() -> int:
    paths = default_paths()
    if not paths.database.exists():
        print("No DUMBOO database found at", paths.database)
        return 1
    settings = SettingsService(paths.default_settings, paths.user_settings)
    db = LocalDatabase(paths.database)
    auth = AuthService(db, lambda: settings.section("accounts"), hasher=pw.make_hasher())
    users = db.query("SELECT username, display_name, role FROM users ORDER BY role = 'owner' DESC, username")
    if not users:
        print("There are no accounts yet. Start DUMBOO to create the owner account.")
        return 1
    print("DUMBOO password reset\n")
    print("Accounts on this PC:")
    for u in users:
        print(f"  {u['username']}  ({u['display_name']}, {u['role']})")
    print()
    username = input("Username to reset: ").strip()
    for _attempt in range(3):
        new = getpass.getpass("New password (8+ characters, hidden while you type): ")
        if new != getpass.getpass("Repeat the new password: "):
            print("The passwords don't match. Try again.\n")
            continue
        try:
            code = auth.reset_password_locally(username, new)
        except AuthError as exc:
            print(f"Not changed: {exc.user_message}\n")
            if "username" in (exc.user_message or ""):
                return 1
            continue
        print("\nDone. Sign in to DUMBOO with the new password.")
        print("Your phone (if paired) reconnects by itself; it was signed out like every other session.\n")
        print("Your NEW RECOVERY CODE (write it down or save it in a password manager):\n")
        print(f"    {code}\n")
        print("If you forget the password again, click “Forgot password?” on the sign-in screen and use this code.")
        return 0
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was changed.")
        sys.exit(1)
