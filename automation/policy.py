"""The central policy engine. Every step passes through ``evaluate`` right before it runs.

Checks, in order:

1. authentication (the session is validated against the database every time);
2. the capability exists (the AI cannot invent operations);
3. the user's permission for that capability;
4. argument validation (types, lengths, allowed values, no unknown arguments);
5. target checks (permitted folders, approved applications, valid web addresses);
6. risk classification: the capability's base risk, raised by the arguments
   (e.g. overwriting) and by the configuration, but never below the
   capability's floor. Nothing the planner writes can lower it;
7. whether a confirmation is required (high: always; medium: unless the
   configuration auto-approves that capability; low: no).

Cancellation, timeouts and verification are enforced by the controller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from automation.config import AutomationConfig
from automation.models import Risk, Step
from automation.registry import ArgumentError, CapabilityRegistry, ExecContext
from automation.safety import MissingPath, SafetyError
from services.auth_service import AuthError, AuthenticatedUser, AuthService, Session

log = logging.getLogger(__name__)


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    risk: Risk = Risk.LOW
    needs_confirmation: bool = False
    args: dict | None = None             # validated arguments (defaults filled in)
    user: AuthenticatedUser | None = None
    missing: bool = False                # refused only because a file/folder doesn't exist (yet)


class PolicyEngine:
    def __init__(self, auth: AuthService, registry: CapabilityRegistry, config: Callable[[], AutomationConfig]) -> None:
        self._auth = auth
        self._registry = registry
        self._config = config

    def classify(self, ctx: ExecContext, name: str, args: dict) -> Risk:
        cap = self._registry.get(name)
        if cap is None:
            return Risk.HIGH
        override = self._config().risk_overrides.get(name)
        base = Risk(override) if override else cap.risk
        dynamic = cap.dynamic_risk(ctx, args) if cap.dynamic_risk else Risk.LOW
        return Risk.max(base, cap.min_risk, dynamic)

    def evaluate(self, session: Session | None, step: Step, ctx: ExecContext) -> Decision:
        try:
            user = self._auth.require(session)
        except AuthError as exc:
            return Decision(False, exc.user_message)
        cap = self._registry.get(step.capability)
        if cap is None:
            log.warning("Refused unknown capability %r", step.capability)
            return Decision(False, f"'{step.capability}' is not something I'm able to do.", Risk.HIGH, user=user)
        if not user.can(cap.permission):
            return Decision(False, "Your account is not allowed to do that "
                                   f"(permission '{cap.permission}'). Ask the owner to enable it.", user=user)
        try:
            args = cap.validate(step.args)
        except ArgumentError as exc:
            return Decision(False, f"Invalid request for {cap.name}: {exc}.", user=user)
        ctx.user = user
        if cap.precheck is not None:
            try:
                cap.precheck(ctx, args)
            except MissingPath as exc:
                return Decision(False, str(exc), user=user, missing=True)
            except (SafetyError, ArgumentError) as exc:
                return Decision(False, str(exc), user=user)
            except LookupError as exc:
                return Decision(False, str(exc).strip("'\""), user=user)
        risk = self.classify(ctx, cap.name, args)
        if risk is Risk.HIGH:
            confirm = True
        elif risk is Risk.MEDIUM:
            confirm = cap.name not in self._config().auto_approve_medium
        else:
            confirm = False
        return Decision(True, "", risk, confirm and not step.approved, args, user)
