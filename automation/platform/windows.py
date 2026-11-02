"""Win32 implementation of the ``Desktop`` interface (ctypes only, no extra packages).

* Windows are found with ``EnumWindows`` (the same visible, titled, un-cloaked
  windows Alt+Tab shows). For Store apps hosted in ``ApplicationFrameHost``
  the real app process (e.g. ``CalculatorApp.exe``) is reported.
* Typing uses ``SendInput`` with Unicode key events. Before every small chunk
  the foreground window is checked again: if the user (or anything else)
  switches windows, typing stops instead of going to the wrong program.
* A background thread remembers the last foreground window that is *not*
  DUMBOO, so "copy the selected text" or "explain the error on my screen"
  refer to the program the user was actually using.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from automation.platform.desktop import DesktopError, WindowInfo, clean_environment

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:  # pragma: no cover
    dwmapi = None

ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_sig = [
    (user32.EnumWindows, (WNDENUMPROC, wintypes.LPARAM), wintypes.BOOL),
    (user32.EnumChildWindows, (wintypes.HWND, WNDENUMPROC, wintypes.LPARAM), wintypes.BOOL),
    (user32.GetWindowTextLengthW, (wintypes.HWND,), ctypes.c_int),
    (user32.GetWindowTextW, (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
    (user32.GetClassNameW, (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int),
    (user32.IsWindowVisible, (wintypes.HWND,), wintypes.BOOL),
    (user32.IsWindow, (wintypes.HWND,), wintypes.BOOL),
    (user32.IsIconic, (wintypes.HWND,), wintypes.BOOL),
    (user32.IsZoomed, (wintypes.HWND,), wintypes.BOOL),
    (user32.GetWindowThreadProcessId, (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD),
    (user32.GetForegroundWindow, (), wintypes.HWND),
    (user32.SetForegroundWindow, (wintypes.HWND,), wintypes.BOOL),
    (user32.BringWindowToTop, (wintypes.HWND,), wintypes.BOOL),
    (user32.ShowWindow, (wintypes.HWND, ctypes.c_int), wintypes.BOOL),
    (user32.PostMessageW, (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM), wintypes.BOOL),
    (user32.GetWindow, (wintypes.HWND, wintypes.UINT), wintypes.HWND),
    (user32.GetWindowLongPtrW, (wintypes.HWND, ctypes.c_int), LONG_PTR),
    (user32.GetWindowRect, (wintypes.HWND, ctypes.POINTER(wintypes.RECT)), wintypes.BOOL),
    (user32.AttachThreadInput, (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL), wintypes.BOOL),
    (user32.SendInput, (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int), wintypes.UINT),
    (user32.SetCursorPos, (ctypes.c_int, ctypes.c_int), wintypes.BOOL),
    (user32.OpenClipboard, (wintypes.HWND,), wintypes.BOOL),
    (user32.CloseClipboard, (), wintypes.BOOL),
    (user32.EmptyClipboard, (), wintypes.BOOL),
    (user32.GetClipboardData, (wintypes.UINT,), wintypes.HANDLE),
    (user32.SetClipboardData, (wintypes.UINT, wintypes.HANDLE), wintypes.HANDLE),
    (user32.IsClipboardFormatAvailable, (wintypes.UINT,), wintypes.BOOL),
    (user32.GetClipboardSequenceNumber, (), wintypes.DWORD),
    (kernel32.GlobalAlloc, (wintypes.UINT, ctypes.c_size_t), wintypes.HGLOBAL),
    (kernel32.GlobalLock, (wintypes.HGLOBAL,), wintypes.LPVOID),
    (kernel32.GlobalUnlock, (wintypes.HGLOBAL,), wintypes.BOOL),
    (kernel32.GlobalFree, (wintypes.HGLOBAL,), wintypes.HGLOBAL),
    (kernel32.GetCurrentThreadId, (), wintypes.DWORD),
]
for fn, args, res in _sig:
    fn.argtypes = args
    fn.restype = res

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSE_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
MOUSEEVENTF_WHEEL = 0x800
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
DWMWA_CLOAKED = 14
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "plus": 0xBB, "=": 0xBB, "minus": 0xBD, "-": 0xBD,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "printscreen": 0x2C,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(c): c for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
}
VK = {k: (v - 32 if len(k) == 1 and "a" <= k <= "z" else v) for k, v in VK.items()}  # letters use upper-case codes
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}
SHELL_CLASSES = {"Shell_TrayWnd", "Progman", "WorkerW", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"}


def _text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _cloaked(hwnd) -> bool:
    if dwmapi is None:
        return False
    value = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        return False
    return value.value != 0


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(inputs: list[INPUT]) -> int:
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise DesktopError("Windows blocked the simulated input (another program may have focus protection, "
                           "or DUMBOO is not allowed to control a program running as administrator).")
    return sent


def parse_combo(combo: str) -> list[int]:
    keys = [k.strip().lower() for k in combo.replace(" ", "").split("+") if k.strip()]
    if not keys:
        raise ValueError("empty key combination")
    codes = []
    for k in keys:
        if k not in VK:
            raise ValueError(f"unknown key '{k}'")
        codes.append(VK[k])
    return codes


class WindowsDesktop:
    name = "desktop"
    available = True

    def __init__(self) -> None:
        self._own_pid = os.getpid()
        self._names: dict[int, str] = {}
        self._last_external: WindowInfo | None = None
        self._stop = threading.Event()
        self._tracker: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- foreground tracking ----------------------------------------------------------
    def start(self) -> None:
        if self._tracker is None:
            self._stop.clear()
            self._tracker = threading.Thread(target=self._track, name="ForegroundTracker", daemon=True)
            self._tracker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._tracker is not None:
            self._tracker.join(timeout=1)
        self._tracker = None

    def _track(self) -> None:
        while not self._stop.wait(0.4):
            try:
                info = self.foreground()
            except Exception:
                continue
            if info is not None and info.pid != self._own_pid and info.title and info.cls not in SHELL_CLASSES:
                with self._lock:
                    self._last_external = info

    def last_external(self) -> WindowInfo | None:
        with self._lock:
            info = self._last_external
        if info is not None and not user32.IsWindow(info.hwnd):
            return None
        return self.window(info.hwnd) if info is not None else None

    # -- windows -----------------------------------------------------------------------
    def _process_name(self, pid: int) -> str:
        name = self._names.get(pid)
        if name is None:
            try:
                import psutil

                name = psutil.Process(pid).name()
            except Exception:
                name = ""
            if len(self._names) > 512:
                self._names.clear()
            self._names[pid] = name
        return name

    def _real_process(self, hwnd: int, pid: int, name: str) -> tuple[int, str]:
        """Store apps run inside ApplicationFrameHost; report the hosted app's process instead."""
        if name.lower() != "applicationframehost.exe":
            return pid, name
        found: list[int] = []

        def child(h, _l):
            p = _pid(h)
            if p != pid:
                found.append(p)
                return False
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(child), 0)
        if found:
            return found[0], self._process_name(found[0])
        return pid, name

    def _info(self, hwnd: int) -> WindowInfo | None:
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        pid = _pid(hwnd)
        pid, name = self._real_process(hwnd, pid, self._process_name(pid))
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return WindowInfo(int(hwnd), _text(hwnd), pid, name, _class(hwnd), bool(user32.IsIconic(hwnd)),
                          bool(user32.IsZoomed(hwnd)), (rect.left, rect.top, rect.right, rect.bottom))

    def windows(self) -> list[WindowInfo]:
        handles: list[int] = []

        def collect(hwnd, _l):
            if (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0
                    and not user32.GetWindow(hwnd, GW_OWNER)
                    and not (user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
                    and not _cloaked(hwnd)):
                handles.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(collect), 0)
        out = []
        for h in handles:
            info = self._info(h)
            if info is not None and info.cls not in SHELL_CLASSES:
                out.append(info)
        return out

    def window(self, hwnd: int) -> WindowInfo | None:
        return self._info(hwnd)

    def foreground(self) -> WindowInfo | None:
        return self._info(user32.GetForegroundWindow())

    def focus(self, hwnd: int) -> bool:
        if not user32.IsWindow(hwnd):
            return False
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # Windows only lets the program that received the last input change the foreground
        # window. Tapping ALT (no visible effect) satisfies that rule.
        try:
            _send([_key_input(VK["alt"]), _key_input(VK["alt"], flags=KEYEVENTF_KEYUP)])
        except DesktopError:
            pass
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None)
            me = kernel32.GetCurrentThreadId()
            if fg_thread and fg_thread != me:
                user32.AttachThreadInput(me, fg_thread, True)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
                user32.AttachThreadInput(me, fg_thread, False)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.05)
        return False

    def set_state(self, hwnd: int, state: str) -> bool:
        if not user32.IsWindow(hwnd):
            return False
        cmd = {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE}[state]
        user32.ShowWindow(hwnd, cmd)
        return True

    def close(self, hwnd: int) -> bool:
        """Ask the window to close, exactly like clicking X (the app can still ask to save)."""
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))

    # -- keyboard and mouse ------------------------------------------------------------------
    def type_text(self, text: str, guard_hwnd: int, cancel: threading.Event) -> int:
        # One character per SendInput call with a short pause: batching several characters made Windows 11
        # Notepad's editor drop and repeat keys (measured on a real desktop; one-by-one at 15 ms was exact).
        typed = 0
        for ch in text:
            if cancel.is_set():
                break
            if typed % 8 == 0 and user32.GetForegroundWindow() != guard_hwnd:
                raise DesktopError("Typing stopped: the target window is no longer in front.")
            if ch == "\r":
                typed += 1
                continue
            if ch == "\n":
                inputs = [_key_input(VK["enter"]), _key_input(VK["enter"], flags=KEYEVENTF_KEYUP)]
            elif ch == "\t":
                inputs = [_key_input(VK["tab"]), _key_input(VK["tab"], flags=KEYEVENTF_KEYUP)]
            else:
                inputs = []
                data = ch.encode("utf-16-le")  # characters outside the BMP become two code units
                for i in range(0, len(data), 2):
                    code = int.from_bytes(data[i:i + 2], "little")
                    inputs += [_key_input(0, code, KEYEVENTF_UNICODE),
                               _key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
            _send(inputs)
            typed += 1
            time.sleep(0.015)
        return typed

    def press(self, combo: str) -> None:
        codes = parse_combo(combo)
        down = [_key_input(c, flags=KEYEVENTF_EXTENDEDKEY if c in EXTENDED else 0) for c in codes]
        up = [_key_input(c, flags=KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if c in EXTENDED else 0))
              for c in reversed(codes)]
        _send(down + up)
        time.sleep(0.05)

    def scroll(self, hwnd: int, clicks: int) -> None:
        info = self._info(hwnd)
        if info is not None:
            left, top, right, bottom = info.rect
            user32.SetCursorPos((left + right) // 2, (top + bottom) // 2)
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(int(clicks) * 120).value, MOUSEEVENTF_WHEEL, 0, 0)
        _send([inp])

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        down, up = MOUSE_FLAGS[button]
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        events = []
        for _ in range(2 if double else 1):
            for flag in (down, up):
                inp = INPUT(type=INPUT_MOUSE)
                inp.mi = MOUSEINPUT(0, 0, 0, flag, 0, 0)
                events.append(inp)
        _send(events)

    # -- clipboard ----------------------------------------------------------------------------
    def _open_clipboard(self) -> bool:
        for _ in range(20):
            if user32.OpenClipboard(None):
                return True
            time.sleep(0.03)
        return False

    def clipboard_sequence(self) -> int:
        return int(user32.GetClipboardSequenceNumber())

    def get_clipboard(self) -> str | None:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        if not self._open_clipboard():
            raise DesktopError("The clipboard is in use by another program.")
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def set_clipboard(self, text: str) -> bool:
        data = (text + "\0").encode("utf-16-le")
        if not self._open_clipboard():
            raise DesktopError("The clipboard is in use by another program.")
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()

    # -- processes ---------------------------------------------------------------------------------
    def launch(self, exe: Path, args: list[str]) -> int:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008  # DETACHED_PROCESS
        proc = subprocess.Popen([str(exe), *args], env=clean_environment(), close_fds=True, creationflags=flags,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.pid

    def shell_open(self, target: str) -> None:
        os.startfile(target)  # noqa: S606 - targets are validated (no executables) before this is called

    def process_running(self, name: str) -> list[int]:
        import psutil

        name = name.lower()
        pids = []
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == name:
                pids.append(p.pid)
        return pids

    def recycle(self, paths: list[Path]) -> bool:
        """Move files/folders to the Recycle Bin (restorable). Windows asks before anything would be
        deleted permanently (e.g. too large for the bin), instead of silently destroying it."""

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]

        if not paths:
            return True
        buffer = ctypes.create_unicode_buffer("\0".join(str(p) for p in paths) + "\0\0")
        op = SHFILEOPSTRUCTW(hwnd=None, wFunc=3, pFrom=ctypes.cast(buffer, wintypes.LPCWSTR), pTo=None,
                             fFlags=0x40 | 0x10 | 0x4 | 0x400 | 0x4000)  # ALLOWUNDO|NOCONFIRMATION|SILENT|NOERRORUI|WANTNUKEWARNING
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
        shell32.SHFileOperationW.restype = ctypes.c_int
        result = shell32.SHFileOperationW(ctypes.byref(op))
        return result == 0 and not op.fAnyOperationsAborted

    def explorer_folders(self) -> list[tuple[int, str]]:
        """Open File Explorer windows and the folder each one shows."""
        try:
            import comtypes
            import comtypes.client
        except ImportError:
            return []
        out: list[tuple[int, str]] = []
        comtypes.CoInitialize()
        try:
            shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
            windows = shell.Windows()
            for i in range(windows.Count):
                try:
                    w = windows.Item(i)
                    path = w.Document.Folder.Self.Path
                    if path and Path(path).is_absolute():
                        out.append((int(w.HWND), str(path)))
                except Exception:
                    continue
        except Exception:
            log.debug("Explorer windows could not be listed", exc_info=True)
        finally:
            comtypes.CoUninitialize()
        return out
