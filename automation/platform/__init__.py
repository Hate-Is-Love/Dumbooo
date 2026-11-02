"""Operating-system access for automation, isolated behind small interfaces.

``desktop.Desktop`` is the interface the capabilities use; ``windows.WindowsDesktop``
implements it with the Win32 API (ctypes, no extra dependency). Tests use a fake
implementation, so most automation logic is tested without touching the real desktop.
"""
