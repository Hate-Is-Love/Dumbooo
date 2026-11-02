"""Fakes for the Security Guardian: no real PowerShell, registry, startup folders, services or network."""

from __future__ import annotations

from typing import Any

from guardian.models import GuardianError
from guardian.platforms import WindowsSecurity


class FakePowerShell:
    """Answers DUMBOO's fixed scripts from a table: {script keyword: result or GuardianError}."""

    def __init__(self, answers: dict[str, Any] | None = None, available: bool = True) -> None:
        self.answers = answers or {}
        self.available = available
        self.calls: list[tuple[str, Any]] = []

    def run(self, script: str, data: Any = None, timeout: float = 60.0) -> Any:
        self.calls.append((script, data))
        for key, answer in self.answers.items():
            if key in script:
                if isinstance(answer, Exception):
                    raise answer
                return answer(data) if callable(answer) else answer
        raise GuardianError("no answer configured", "unavailable")


class FakeRegistry:
    def __init__(self, keys: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.keys = keys or {}
        self.writes: list[tuple] = []

    def values(self, hive: str, key: str):
        if (hive, key) not in self.keys:
            raise OSError("no such key")
        return list(self.keys[(hive, key)].items())

    def value(self, hive: str, key: str, name: str):
        try:
            return self.keys[(hive, key)][name]
        except KeyError as exc:
            raise OSError("no such value") from exc

    def delete_value(self, hive: str, key: str, name: str) -> None:
        self.writes.append(("delete", hive, key, name))
        del self.keys[(hive, key)][name]

    def set_string(self, hive: str, key: str, name: str, data: str) -> None:
        self.writes.append(("set", hive, key, name))
        self.keys.setdefault((hive, key), {})[name] = data


class FakeWindows(WindowsSecurity):
    """WindowsSecurity with fake system interfaces and no real services."""

    def __init__(self, powershell, registry, services=None, now=None) -> None:
        kw = {"now": now} if now else {}
        super().__init__(powershell, registry, **kw)
        self._fake_services = services or []

    def _services(self):
        return list(self._fake_services)


def harmless_overrides() -> dict:
    """Default for every test core: the Guardian can't touch the real system."""
    from guardian.platforms import UnsupportedPlatform

    return {"platform": UnsupportedPlatform(), "powershell": FakePowerShell(available=False),
            "winget_run": lambda: "", "advisories": _NoAdvisories()}


class _NoAdvisories:
    def get(self):
        raise GuardianError("advisories are not fetched in tests", "unavailable")
