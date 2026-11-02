"""Operating-system specific, read-only security information.

``WindowsSecurity`` uses Windows' own interfaces: Microsoft Defender (``Get-MpComputerStatus``),
Security Center (``root/SecurityCenter2``), firewall profiles, the UAC policy value, installed
updates (``Get-HotFix``), Defender's threat-detection log, the registry Run keys (``winreg``),
Startup folders, scheduled tasks and services. None of these need administrator rights;
checks that do (BitLocker, Secure Boot) are reported as UNKNOWN with the reason.

``UnsupportedPlatform`` is used on Linux/macOS for now: every OS-specific check is NOT_SUPPORTED
(processes, network and file integrity still work there through psutil/hashing).
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from guardian.files import redact
from guardian.models import (FAIL, NOT_SUPPORTED, PASS, UNKNOWN, WARNING, CheckResult, GuardianError,
                             StartupEntry)
from guardian.runner import PowerShell, as_list

DEFENDER = ("Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,"
            "IsTamperProtected,AntivirusSignatureLastUpdated,QuickScanEndTime,FullScanEndTime | "
            "ConvertTo-Json -Compress")
AV_PRODUCTS = ("Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | "
               "Select-Object displayName,productState | ConvertTo-Json -Compress")
FIREWALL = "Get-NetFirewallProfile | Select-Object Name,Enabled | ConvertTo-Json -Compress"
HOTFIX = ("Get-HotFix | Where-Object { $_.InstalledOn } | Sort-Object InstalledOn -Descending | "
          "Select-Object -First 3 HotFixID,Description,@{n='InstalledOn';e={$_.InstalledOn.ToString('s')}} | "
          "ConvertTo-Json -Compress")
THREATS = ("$e = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Windows Defender/Operational'; "
           "Id=1116,1117; StartTime=(Get-Date).AddDays(-30)} -ErrorAction SilentlyContinue | Select-Object -First 20 "
           "@{n='time';e={$_.TimeCreated.ToString('s')}},Id,@{n='msg';e={($_.Message -split \"`n\")[0..6] -join ' | '}}; "
           "if ($e) { $e | ConvertTo-Json -Compress } else { '[]' }")
TASKS = ("Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\\Microsoft\\*' } | ForEach-Object { $t=$_; "
         "foreach ($a in $t.Actions) { if ($a.Execute) { [pscustomobject]@{name=$t.TaskName; path=$t.TaskPath; "
         "state=[string]$t.State; exe=$a.Execute; args=$a.Arguments} } } } | ConvertTo-Json -Compress")
SHORTCUTS = ("$sh = New-Object -ComObject WScript.Shell; $out = foreach ($p in $A.paths) { $l = $sh.CreateShortcut($p); "
             "[pscustomobject]@{lnk=$p; target=$l.TargetPath; args=$l.Arguments} }; $out | ConvertTo-Json -Compress")

RUN_KEYS = [  # (hive name, key, scope)
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run", "user"),
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "user"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\Run", "machine"),
    ("HKLM", r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "machine"),
    ("HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "machine"),
]
INTERPRETERS = {"rundll32.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe", "mshta.exe",
                "regsvr32.exe", "javaw.exe", "java.exe", "python.exe", "pythonw.exe", "node.exe"}


def entry_id(mechanism: str, location: str, name: str) -> str:
    return hashlib.sha1(f"{mechanism}|{location}|{name}".lower().encode()).hexdigest()[:12]


def resolve_executable(command: str) -> str | None:
    """The file a startup command runs: quoted or first path; for rundll32/scripts, the file they load."""
    cmd = os.path.expandvars((command or "").strip())
    if not cmd:
        return None
    m = re.match(r'^"([^"]+)"', cmd)
    exe = m.group(1) if m else None
    rest = cmd[m.end():] if m else ""
    if exe is None:
        m2 = re.match(r"^(.+?\.(?:exe|com|bat|cmd|scr|vbs|js|ps1|lnk))(?=\s|$)", cmd, re.I)
        exe = m2.group(1) if m2 else cmd.split()[0]
        rest = cmd[len(exe):]
    exe = exe.strip()
    if not re.search(r"[\\/]", exe):  # bare program name: look in the system folders
        for folder in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32"),
                       os.environ.get("WINDIR", r"C:\Windows")):
            candidate = os.path.join(folder, exe if exe.lower().endswith(".exe") else exe + ".exe")
            if os.path.exists(candidate):
                exe = candidate
                break
    name = os.path.basename(exe).lower()
    if name in INTERPRETERS:  # the interesting file is the script/DLL it loads
        m3 = re.search(r'"?([A-Za-z]:\\[^",]+?\.(?:dll|vbs|vbe|js|jse|ps1|bat|cmd|hta|jar|py|wsf))"?', rest, re.I)
        if m3:
            return m3.group(1)
    return exe


class UnsupportedPlatform:
    name = sys.platform

    def overview(self) -> list[CheckResult]:
        return [CheckResult("os", "Operating system", PASS, f"{platform.system()} {platform.release()}",
                            {"version": platform.version()}, "platform module"),
                CheckResult("antivirus", "Antivirus / endpoint protection", NOT_SUPPORTED,
                            "Not checked on this operating system yet."),
                CheckResult("firewall", "Firewall", NOT_SUPPORTED, "Not checked on this operating system yet."),
                CheckResult("updates", "Security updates", NOT_SUPPORTED, "Not checked on this operating system yet.")]

    def startup_entries(self, timeout: float = 60.0) -> list[StartupEntry]:
        raise GuardianError("startup inspection is not supported on this operating system yet", "unavailable")


class WindowsSecurity:
    name = "windows"

    def __init__(self, powershell: PowerShell | None = None, registry=None, now=datetime.now) -> None:
        self._ps = powershell or PowerShell()
        self._reg = registry or _WinRegistry()
        self._now = now

    # -- overview -------------------------------------------------------------------------------------
    def _query(self, script: str, timeout: float = 30.0) -> Any:
        return self._ps.run(script, None, timeout)

    def overview(self) -> list[CheckResult]:
        checks = [self._os(), *self._defender(), self._other_av(), *self._firewall(), self._uac(), self._updates(),
                  self._threat_history()]
        checks += [CheckResult("bitlocker", "Disk encryption (BitLocker)", UNKNOWN,
                               "Reading BitLocker status needs administrator rights, so it was not checked.",
                               source="needs administrator",
                               advice="To check it yourself: Settings → Privacy & security → Device encryption."),
                   CheckResult("secure_boot", "Secure Boot", UNKNOWN,
                               "Reading Secure Boot status needs administrator rights, so it was not checked.",
                               source="needs administrator", advice="Check it in System Information (msinfo32).")]
        return checks

    def _os(self) -> CheckResult:
        return CheckResult("os", "Operating system", PASS,
                           f"{platform.system()} {platform.release()} (build {platform.version()})",
                           {"platform": platform.platform()}, "platform module")

    def _defender(self) -> list[CheckResult]:
        try:
            d = self._query(DEFENDER)
        except GuardianError as exc:
            return [CheckResult("defender", "Microsoft Defender", UNKNOWN, f"Could not read Defender's status: "
                                f"{exc.user_message}. This does not mean it is off.", source="Get-MpComputerStatus")]
        out = []
        on = bool(d.get("AMServiceEnabled")) and bool(d.get("AntivirusEnabled"))
        rtp = bool(d.get("RealTimeProtectionEnabled"))
        out.append(CheckResult("defender", "Microsoft Defender antivirus", PASS if on else FAIL,
                               "Running" if on else "The antivirus engine is not enabled",
                               {"service": d.get("AMServiceEnabled"), "antivirus": d.get("AntivirusEnabled")},
                               "Get-MpComputerStatus", "" if on else "Open Windows Security → Virus & threat "
                               "protection, unless another antivirus product protects this PC."))
        out.append(CheckResult("realtime", "Real-time protection", PASS if rtp else FAIL,
                               "On" if rtp else "Off", {"enabled": rtp}, "Get-MpComputerStatus",
                               "" if rtp else "Turn on real-time protection in Windows Security."))
        sig = _parse_time(d.get("AntivirusSignatureLastUpdated"))
        if sig is None:
            out.append(CheckResult("signatures", "Antivirus definitions", UNKNOWN, "Last update time not available",
                                   source="Get-MpComputerStatus"))
        else:
            age = (self._now() - sig).days
            out.append(CheckResult("signatures", "Antivirus definitions", PASS if age <= 3 else WARNING,
                                   f"Updated {sig:%d %b %Y %H:%M} ({age} day{'s' if age != 1 else ''} ago)",
                                   {"updated": sig.isoformat(timespec="seconds"), "age_days": age}, "Get-MpComputerStatus",
                                   "" if age <= 3 else "Check for updates in Windows Security."))
        tamper = d.get("IsTamperProtected")
        out.append(CheckResult("tamper", "Tamper protection", PASS if tamper else (UNKNOWN if tamper is None else WARNING),
                               "On" if tamper else ("Not reported" if tamper is None else "Off"),
                               {"enabled": tamper}, "Get-MpComputerStatus",
                               "" if tamper else "Tamper protection stops malware from switching Defender off."))
        scan = _parse_time(d.get("QuickScanEndTime"))
        if scan is not None:
            days = (self._now() - scan).days
            out.append(CheckResult("last_scan", "Last antivirus scan", PASS if days <= 14 else WARNING,
                                   f"Quick scan finished {scan:%d %b %Y} ({days} days ago)",
                                   {"quick_scan": scan.isoformat(timespec="seconds")}, "Get-MpComputerStatus",
                                   "" if days <= 14 else "Run a quick scan in Windows Security."))
        return out

    def _other_av(self) -> CheckResult:
        try:
            rows = as_list(self._query(AV_PRODUCTS))
        except GuardianError as exc:
            return CheckResult("av_products", "Registered antivirus products", UNKNOWN,
                               f"Could not read Security Center: {exc.user_message}", source="SecurityCenter2")
        products = []
        for row in rows:
            state = int(row.get("productState") or 0)
            hexs = f"{state:06x}"
            products.append({"name": row.get("displayName"), "enabled": bool(int(hexs[2:4], 16) & 0x10),
                             "up_to_date": hexs[4:6] == "00"})
        active = [p for p in products if p["enabled"]]
        status = PASS if active and all(p["up_to_date"] for p in active) else (WARNING if active else FAIL)
        detail = ", ".join(f"{p['name']} ({'on' if p['enabled'] else 'off'}, "
                           f"{'up to date' if p['up_to_date'] else 'out of date'})" for p in products) or "none registered"
        return CheckResult("av_products", "Registered antivirus products", status, detail, {"products": products},
                           "Windows Security Center (root/SecurityCenter2)")

    def _firewall(self) -> list[CheckResult]:
        try:
            rows = as_list(self._query(FIREWALL))
        except GuardianError as exc:
            return [CheckResult("firewall", "Firewall", UNKNOWN, f"Could not read the firewall: {exc.user_message}. "
                                "This does not mean it is off.", source="Get-NetFirewallProfile")]
        off = [r["Name"] for r in rows if not _truthy(r.get("Enabled"))]
        return [CheckResult("firewall", "Windows Firewall", PASS if not off else FAIL,
                            "On for all profiles (" + ", ".join(r["Name"] for r in rows) + ")" if not off
                            else "Off for: " + ", ".join(off),
                            {r["Name"]: _truthy(r.get("Enabled")) for r in rows}, "Get-NetFirewallProfile",
                            "" if not off else "Turn the firewall on in Windows Security → Firewall & network protection.")]

    def _uac(self) -> CheckResult:
        try:
            value = self._reg.value("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "EnableLUA")
        except OSError:
            return CheckResult("uac", "User Account Control", UNKNOWN, "The UAC setting could not be read",
                               source="registry")
        on = value == 1
        return CheckResult("uac", "User Account Control (UAC)", PASS if on else FAIL, "On" if on else "Off",
                           {"EnableLUA": value}, r"HKLM\...\Policies\System\EnableLUA",
                           "" if on else "UAC asks before programs make system changes; turn it back on.")

    def _updates(self) -> CheckResult:
        try:
            rows = as_list(self._query(HOTFIX, 45))
        except GuardianError as exc:
            return CheckResult("updates", "Windows updates", UNKNOWN, f"Installed updates could not be read: "
                               f"{exc.user_message}", source="Get-HotFix")
        if not rows:
            return CheckResult("updates", "Windows updates", UNKNOWN, "No installed updates with a date were found",
                               source="Get-HotFix")
        latest = _parse_time(rows[0].get("InstalledOn"))
        days = (self._now() - latest).days if latest else None
        status = UNKNOWN if days is None else (PASS if days <= 45 else WARNING)
        return CheckResult("updates", "Windows updates", status,
                           f"Latest installed update {rows[0].get('HotFixID')} on {latest:%d %b %Y} ({days} days ago)"
                           if latest else "Installed update dates unavailable",
                           {"latest": rows[0]}, "Get-HotFix (installed updates; pending updates are not listed)",
                           "" if status != WARNING else "Open Settings → Windows Update and install pending updates.")

    def _threat_history(self) -> CheckResult:
        try:
            rows = as_list(self._query(THREATS, 45))
        except GuardianError as exc:
            return CheckResult("threats", "Defender detections (30 days)", UNKNOWN,
                               f"Defender's detection log could not be read: {exc.user_message}",
                               source="Windows Defender/Operational log")
        if not rows:
            return CheckResult("threats", "Defender detections (30 days)", PASS, "No detections recorded",
                               {"count": 0}, "Windows Defender/Operational log (events 1116, 1117)")
        return CheckResult("threats", "Defender detections (30 days)", WARNING,
                           f"{len(rows)} detection event(s) recorded; latest {rows[0].get('time', '')[:16]}",
                           {"count": len(rows), "events": rows[:5]}, "Windows Defender/Operational log (1116, 1117)",
                           "Open Windows Security → Protection history to see what was found and what was done.")

    # -- startup entries --------------------------------------------------------------------------------------
    def startup_entries(self, timeout: float = 60.0) -> list[StartupEntry]:
        entries: list[StartupEntry] = []
        for hive, key, scope in RUN_KEYS:
            try:
                values = self._reg.values(hive, key)
            except OSError:
                continue
            for name, data in values:
                command = str(data)
                entries.append(StartupEntry(entry_id("registry_run", f"{hive}\\{key}", name), name, "registry_run",
                                            f"{hive}\\{key}", redact(command), resolve_executable(command), scope))
        entries += self._startup_folders()
        entries += self._tasks(timeout)
        entries += self._services()
        return entries

    def _startup_folders(self) -> list[StartupEntry]:
        folders = [(Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup", "user"),
                   (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) /
                    r"Microsoft\Windows\Start Menu\Programs\StartUp", "machine")]
        found, links = [], []
        for folder, scope in folders:
            if not folder.is_dir():
                continue
            for item in folder.iterdir():
                if item.name.lower() == "desktop.ini" or not item.is_file():
                    continue
                found.append((item, scope))
                if item.suffix.lower() == ".lnk":
                    links.append(str(item))
        targets = {}
        if links and self._ps.available:
            try:
                for row in as_list(self._ps.run(SHORTCUTS, {"paths": links}, 30)):
                    targets[row["lnk"]] = (row.get("target") or "", row.get("args") or "")
            except GuardianError:
                pass
        out = []
        for item, scope in found:
            target, args = targets.get(str(item), (str(item) if item.suffix.lower() != ".lnk" else "", ""))
            command = (f'"{target}" {args}'.strip() if target else str(item))
            out.append(StartupEntry(entry_id("startup_folder", str(item.parent), item.name), item.stem,
                                    "startup_folder", str(item.parent), redact(command), target or None, scope))
        return out

    def _tasks(self, timeout: float) -> list[StartupEntry]:
        if not self._ps.available:
            return []
        try:
            rows = as_list(self._ps.run(TASKS, None, timeout))
        except GuardianError:
            return []
        out = []
        for row in rows:
            exe = (row.get("exe") or "").strip().strip('"')  # Task Scheduler may store the path already quoted
            command = f'"{exe}" {row.get("args") or ""}'.strip()
            location = f"Task Scheduler {row.get('path', '')}"
            out.append(StartupEntry(entry_id("scheduled_task", location, row.get("name", "")), row.get("name", ""),
                                    "scheduled_task", location, redact(command),
                                    resolve_executable(command), "machine", row.get("state") != "Disabled"))
        return out

    def _services(self) -> list[StartupEntry]:
        try:
            import psutil
        except ImportError:
            return []
        windir = os.environ.get("WINDIR", r"C:\Windows").lower()
        out = []
        try:
            services = list(psutil.win_service_iter())
        except Exception:
            return []
        for svc in services:
            try:
                info = svc.as_dict()
            except Exception:
                continue
            if info.get("start_type") != "automatic":
                continue
            binpath = info.get("binpath") or ""
            exe = resolve_executable(binpath)
            if exe and exe.lower().startswith(windir):
                continue  # Windows' own services: listing ~200 of them would hide what matters
            out.append(StartupEntry(entry_id("service", "services", info.get("name", "")),
                                    info.get("display_name") or info.get("name", ""), "service",
                                    f"Service {info.get('name', '')}", redact(binpath), exe, "machine"))
        return out


class _WinRegistry:
    """Small read/write wrapper over winreg (writes only for confirmed, reversible actions)."""

    HIVES = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}

    def _hive(self, hive: str):
        import winreg

        return getattr(winreg, self.HIVES[hive])

    def values(self, hive: str, key: str) -> list[tuple[str, str]]:
        import winreg

        out = []
        with winreg.OpenKey(self._hive(hive), key) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, data, _type = winreg.EnumValue(k, i)
                out.append((name, data))
        return out

    def value(self, hive: str, key: str, name: str):
        import winreg

        with winreg.OpenKey(self._hive(hive), key) as k:
            return winreg.QueryValueEx(k, name)[0]

    def delete_value(self, hive: str, key: str, name: str) -> None:
        import winreg

        with winreg.OpenKey(self._hive(hive), key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)

    def set_string(self, hive: str, key: str, name: str, data: str) -> None:
        import winreg

        with winreg.OpenKey(self._hive(hive), key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, data)


def _truthy(value) -> bool:
    return value in (True, 1, "True", "true", "1")


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, str):
        m = re.match(r"/Date\((\d+)\)/", value)
        if m:
            return datetime.fromtimestamp(int(m.group(1)) / 1000)
        try:
            return datetime.fromisoformat(value.replace("Z", "")[:19])
        except ValueError:
            return None
    return None


def current() -> WindowsSecurity | UnsupportedPlatform:
    return WindowsSecurity() if sys.platform == "win32" else UnsupportedPlatform()


__all__ = ["WindowsSecurity", "UnsupportedPlatform", "current", "resolve_executable", "entry_id", "timedelta"]
