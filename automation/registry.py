"""The capability registry: the complete list of things automation can do.

A capability is a named, typed operation (``files.move``, ``apps.open`` …) with
an argument schema, the permission it needs, a base risk level, an optional
target check (``precheck``) and a handler that performs the action and
verifies the result. The AI planner can only choose from this list; anything
else is rejected before execution.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from automation.models import ActionResult, Risk, Step

if TYPE_CHECKING:
    from automation.services import AutomationServices
    from services.auth_service import AuthenticatedUser, Session


class ArgumentError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    kind: type | tuple[type, ...]
    required: bool = True
    default: Any = None
    choices: tuple | None = None
    max_len: int = 500
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""
    strip: bool = True          # False for content that must be kept exactly (file text, typed text)

    def check(self, name: str, value: Any) -> Any:
        if value is None:
            if self.required:
                raise ArgumentError(f"'{name}' is required")
            return self.default
        kinds = self.kind if isinstance(self.kind, tuple) else (self.kind,)
        if float in kinds and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if isinstance(value, bool) and bool not in kinds:
            raise ArgumentError(f"'{name}' has the wrong type")
        if not isinstance(value, kinds):
            raise ArgumentError(f"'{name}' must be {' or '.join(k.__name__ for k in kinds)}")
        if isinstance(value, str):
            if self.strip:
                value = value.strip()
            if len(value) > self.max_len:
                raise ArgumentError(f"'{name}' is too long (max {self.max_len} characters)")
            if self.required and not value.strip():
                raise ArgumentError(f"'{name}' must not be empty")
            if "\x00" in value:
                raise ArgumentError(f"'{name}' contains a null character")
        if isinstance(value, list):
            if len(value) > self.max_len:
                raise ArgumentError(f"'{name}' has too many items")
            if not all(isinstance(v, (str, int, float, dict)) for v in value):
                raise ArgumentError(f"'{name}' contains unsupported values")
        if self.choices is not None and value not in self.choices:
            raise ArgumentError(f"'{name}' must be one of: {', '.join(map(str, self.choices))}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ArgumentError(f"'{name}' must be at least {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                raise ArgumentError(f"'{name}' must be at most {self.maximum}")
        return value


@dataclass
class ExecContext:
    """Everything a handler may use. ``cancel`` is set by STOP, emergency stop or a timeout."""

    session: "Session"
    user: "AuthenticatedUser"
    services: "AutomationServices"
    cancel: threading.Event
    step: Step | None = None
    task_id: int | None = None
    progress: Callable[[str], None] = lambda _m: None

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()


Handler = Callable[[ExecContext, dict[str, Any]], ActionResult]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    params: dict[str, Param]
    handler: Handler
    permission: str
    risk: Risk = Risk.LOW
    min_risk: Risk = Risk.LOW                       # configuration can never lower the risk below this
    dynamic_risk: Callable[[ExecContext, dict], Risk] | None = None
    precheck: Callable[[ExecContext, dict], None] | None = None   # raises SafetyError/ArgumentError
    describe: Callable[[dict], str] | None = None
    confirm_details: Callable[[ExecContext, dict], str] | None = None
    timeout_s: float | None = None                  # None = limits.step_timeout_s
    private_args: tuple[str, ...] = ()              # never stored in history (typed text, form values)
    planner: bool = True                            # offered to the AI planner
    example: str = ""

    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ArgumentError("arguments must be an object")
        unknown = set(args) - set(self.params)
        if unknown:
            raise ArgumentError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        return {name: spec.check(name, args.get(name)) for name, spec in self.params.items()}

    def text_for(self, args: dict[str, Any]) -> str:
        if self.describe is not None:
            try:
                return self.describe(args)
            except Exception:
                pass
        return self.description

    def planner_signature(self) -> str:
        parts = []
        for name, p in self.params.items():
            kinds = p.kind if isinstance(p.kind, tuple) else (p.kind,)
            t = "|".join(k.__name__ for k in kinds)
            extra = f" one of {list(p.choices)}" if p.choices else ""
            parts.append(f"{name}{'' if p.required else '?'}: {t}{extra}{' - ' + p.help if p.help else ''}")
        return f"- {self.name}({'; '.join(parts)}): {self.description}"


@dataclass
class CapabilityRegistry:
    _items: dict[str, Capability] = field(default_factory=dict)

    def add(self, capability: Capability) -> None:
        if capability.name in self._items:
            raise ValueError(f"duplicate capability {capability.name}")
        self._items[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._items.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)

    def all(self) -> list[Capability]:
        return [self._items[n] for n in self.names()]

    def private_args(self, name: str) -> tuple[str, ...]:
        cap = self._items.get(name)
        return cap.private_args if cap else ()
