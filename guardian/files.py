"""File facts, SHA-256 hashing, digital signatures and path observations."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from guardian.models import NOT_SUPPORTED, UNKNOWN, FileFacts, GuardianError, Signature
from guardian.runner import PowerShell, as_list

CHUNK = 1024 * 1024


def sha256_file(path: str | os.PathLike, max_mb: int = 512, cancel: threading.Event | None = None) -> str:
    """SHA-256 of a file (streamed). Raises GuardianError if too large, unreadable or cancelled."""
    p = Path(path)
    size = p.stat().st_size
    if size > max_mb * 1024 * 1024:
        raise GuardianError(f"the file is larger than {max_mb} MB, so it was not hashed", "invalid")
    digest = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(CHUNK):
            if cancel is not None and cancel.is_set():
                raise GuardianError("cancelled", "cancelled")
            digest.update(chunk)
    return digest.hexdigest()


def file_facts(path: str | None, hash_it: bool = True, max_mb: int = 512,
               cancel: threading.Event | None = None) -> FileFacts:
    if not path:
        return FileFacts(path or "", exists=False, error="the executable path could not be determined")
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return FileFacts(str(p), exists=False, error="the file does not exist")
    except PermissionError:
        return FileFacts(str(p), exists=True, error="access to the file was denied")
    except OSError as exc:
        return FileFacts(str(p), exists=False, error=f"the file could not be read ({exc.__class__.__name__})")
    facts = FileFacts(str(p), True, st.st_size, datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                      datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_ctime)).isoformat(timespec="seconds"))
    if hash_it and p.is_file():
        try:
            facts.sha256 = sha256_file(p, max_mb, cancel)
        except PermissionError:
            facts.error = "access to the file was denied, so it was not hashed"
        except GuardianError as exc:
            if exc.kind == "cancelled":
                raise
            facts.error = exc.user_message
        except OSError as exc:
            facts.error = f"the file could not be hashed ({exc.__class__.__name__})"
    return facts


SIGNATURE_SCRIPT = (
    "$out = foreach ($p in $A.paths) { try { $s = Get-AuthenticodeSignature -LiteralPath $p; "
    "[pscustomobject]@{path=$p; status=[string]$s.Status; "
    "subject=$(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }); "
    "issuer=$(if ($s.SignerCertificate) { $s.SignerCertificate.Issuer } else { '' })} } "
    "catch { [pscustomobject]@{path=$p; status='UnknownError'; subject=''; issuer=''} } }; "
    "$out | ConvertTo-Json -Depth 3 -Compress")


def _cn(subject: str) -> str:
    m = re.search(r"CN=(\"[^\"]+\"|[^,]+)", subject or "")
    return m.group(1).strip('"').strip() if m else (subject or "").strip()


class SignatureChecker:
    """Authenticode signatures through Windows' own ``Get-AuthenticodeSignature`` (batched)."""

    def __init__(self, powershell: PowerShell) -> None:
        self._ps = powershell
        self._cache: dict[str, Signature] = {}

    def check(self, paths: list[str], timeout: float = 90.0) -> dict[str, Signature]:
        wanted = [p for p in dict.fromkeys(paths) if p and p not in self._cache]
        if wanted:
            if not self._ps.available:
                for p in wanted:
                    self._cache[p] = Signature(NOT_SUPPORTED)
            else:
                try:
                    rows = as_list(self._ps.run(SIGNATURE_SCRIPT, {"paths": wanted}, timeout))
                except GuardianError:
                    rows = []
                for row in rows:
                    self._cache[row["path"]] = Signature(row.get("status") or UNKNOWN, _cn(row.get("subject", "")),
                                                         _cn(row.get("issuer", "")))
                for p in wanted:
                    self._cache.setdefault(p, Signature(UNKNOWN))
        return {p: self._cache.get(p, Signature(UNKNOWN)) for p in paths if p}


# -- path observations -------------------------------------------------------------------------------------
SYSTEM_NAMES = {"svchost.exe", "lsass.exe", "csrss.exe", "winlogon.exe", "services.exe", "smss.exe", "explorer.exe",
                "wininit.exe", "spoolsv.exe", "taskhostw.exe", "dwm.exe", "conhost.exe", "rundll32.exe"}
ROOT_SYSTEM_NAMES = {"explorer.exe", "hh.exe", "notepad.exe", "regedit.exe", "write.exe"}  # live in C:\Windows itself


def path_observations(path: str | None) -> list[tuple[str, int]]:
    """(observation, weight) for where a program lives. Weights: + concern, - reassurance."""
    if not path:
        return []
    p = path.replace("/", "\\")
    low = p.lower()
    name = low.rsplit("\\", 1)[-1]
    out: list[tuple[str, int]] = []
    windir = os.environ.get("WINDIR", r"C:\Windows").lower()
    in_windows_root = low == f"{windir}\\{name}"  # e.g. C:\Windows\explorer.exe is where Explorer really lives
    if low.startswith(windir + "\\system32\\") or low.startswith(windir + "\\syswow64\\") or (
            in_windows_root and name in ROOT_SYSTEM_NAMES):
        out.append(("Located in the Windows system folder", -1))
    elif name in SYSTEM_NAMES:
        out.append((f"Uses the name of a Windows system program ({name}) but is not in the system folder", 3))
    if re.search(r"\\(?:temp|tmp)\\", low) or "\\appdata\\local\\temp\\" in low:
        out.append(("Runs from a temporary folder", 2))
    if "\\downloads\\" in low:
        out.append(("Runs from the Downloads folder", 2))
    if "\\users\\public\\" in low:
        out.append(("Runs from the shared Public folder", 2))
    if "\\appdata\\roaming\\" in low and not re.search(r"\\appdata\\roaming\\(?:microsoft|zoom|spotify|discord|"
                                                       r"telegram desktop|whatsapp)\\", low):
        out.append(("Runs from AppData\\Roaming (normal for some apps, also used by malware)", 1))
    if re.match(r"^[a-z]:\\programdata\\[^\\]+\.exe$", low):
        out.append(("Runs directly from the ProgramData root folder", 2))
    if re.search(r"\.(?:pdf|docx?|xlsx?|jpg|png|txt)\.(?:exe|scr|com|bat|cmd|vbs|js)$", low):
        out.append(("Has a double file extension (a common disguise)", 3))
    if low.endswith((".scr", ".pif")):
        out.append(("Is a screensaver/PIF executable, unusual for startup", 2))
    if low.startswith(("c:\\program files\\", "c:\\program files (x86)\\")):
        out.append(("Installed under Program Files (needs administrator rights to place there)", -1))
    return out


SUSPICIOUS_COMMANDS = [
    (r"powershell(?:\.exe)?\b.*\s-(?:e|en|enc|enco|encodedcommand)\s", "Starts PowerShell with an encoded (hidden) command", 3),
    (r"powershell(?:\.exe)?\b.*-w(?:indowstyle)?\s+hidden", "Starts PowerShell in a hidden window", 2),
    (r"\bmshta(?:\.exe)?\b", "Uses mshta (runs HTML applications; often abused)", 3),
    (r"\b(?:wscript|cscript)(?:\.exe)?\b.*\.(?:js|jse|vbs|vbe|wsf)\b", "Runs a Windows script file", 2),
    (r"\bregsvr32(?:\.exe)?\b.*\s/i:https?:", "Uses regsvr32 to load code from the internet", 3),
    (r"\bcertutil(?:\.exe)?\b.*-urlcache", "Uses certutil to download files", 3),
    (r"\bbitsadmin(?:\.exe)?\b.*/transfer", "Uses bitsadmin to download files", 2),
    (r"\brundll32(?:\.exe)?\b\s+(?:javascript:|.*\\(?:temp|downloads|users\\public)\\)",
     "Uses rundll32 with a script or a file in an unusual folder", 3),
    (r"https?://\S+\.(?:exe|ps1|dll|bat|vbs)\b", "Downloads a program or script from the internet", 3),
]


def command_observations(command: str) -> list[tuple[str, int]]:
    low = (command or "").lower()
    return [(text, w) for pattern, text, w in SUSPICIOUS_COMMANDS if re.search(pattern, low)]


_SECRET = re.compile(r"(?i)((?:password|passwd|pwd|token|secret|apikey|api_key|key)\s*[=:]\s*|"
                     r"(?:--?(?:password|token|secret|key|apikey))\s+)(\S+)")


def redact(command: str) -> str:
    """Command lines can contain secrets: hide values after password/token/key style options."""
    text = _SECRET.sub(lambda m: m.group(1) + "[hidden]", command or "")
    text = re.sub(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[hidden]@", text)  # user:password@host
    return text[:1000]
