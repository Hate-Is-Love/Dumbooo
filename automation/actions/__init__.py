"""Capability modules. ``build_registry`` registers every automation capability."""

from __future__ import annotations

from automation.registry import CapabilityRegistry


def build_registry() -> CapabilityRegistry:
    from automation.actions import apps, dev, files, input, memory_actions, screen, system, web

    registry = CapabilityRegistry()
    for module in (apps, files, input, screen, system, web, dev, memory_actions):
        module.register(registry)
    return registry
