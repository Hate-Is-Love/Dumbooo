"""Screen understanding through Windows UI Automation (the accessibility API).

UI Automation exposes what a program shows as a tree of named controls
(buttons, text, edit fields, dialogs …). Reading it is local, fast and exact,
so DUMBOO prefers it over screenshots: error dialogs, button labels and
document text can be read without sending an image anywhere. Password fields
are never read.

Every call initialises COM for the calling thread and is bounded by a time
and item limit, so a program with a huge UI tree cannot stall automation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

try:
    import uiautomation as auto
except Exception:  # pragma: no cover - optional on non-Windows systems
    auto = None

READ_TYPES = {"TextControl", "ButtonControl", "EditControl", "DocumentControl", "MenuItemControl",
              "TabItemControl", "HyperlinkControl", "CheckBoxControl", "RadioButtonControl", "StatusBarControl",
              "WindowControl", "ListItemControl", "TreeItemControl", "HeaderItemControl", "ComboBoxControl",
              "GroupControl", "TitleBarControl", "ToolTipControl", "DataItemControl"}
CLICKABLE = {"ButtonControl", "MenuItemControl", "HyperlinkControl", "TabItemControl", "CheckBoxControl",
             "RadioButtonControl", "ListItemControl", "TreeItemControl", "SplitButtonControl", "ComboBoxControl"}
TYPE_WORDS = {"button": "ButtonControl", "menu": "MenuItemControl", "link": "HyperlinkControl",
              "tab": "TabItemControl", "checkbox": "CheckBoxControl", "check box": "CheckBoxControl",
              "field": "EditControl", "box": "EditControl", "text": "TextControl", "item": "ListItemControl"}


class UIAError(RuntimeError):
    pass


@dataclass
class Element:
    control_type: str
    name: str
    automation_id: str
    rect: tuple[int, int, int, int]
    enabled: bool
    value: str = ""

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.control_type.replace("Control", "").lower(), "name": self.name,
                "automation_id": self.automation_id, "rect": list(self.rect), "enabled": self.enabled,
                **({"value": self.value} if self.value else {})}


def available() -> bool:
    return auto is not None


def _rect(control) -> tuple[int, int, int, int]:
    r = control.BoundingRectangle
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def _is_password(control) -> bool:
    try:
        return bool(control.IsPassword)
    except Exception:
        return False


def _value(control, limit: int) -> str:
    if _is_password(control):
        return ""
    try:
        pattern = control.GetValuePattern()
        if pattern is not None:
            return (pattern.Value or "")[:limit]
    except Exception:
        pass
    try:
        pattern = control.GetTextPattern()
        if pattern is not None:
            return (pattern.DocumentRange.GetText(limit) or "")[:limit]
    except Exception:
        pass
    return ""


def _walk(root, max_items: int, timeout_s: float, max_depth: int = 25):
    deadline = time.monotonic() + timeout_s
    count = 0
    for control, depth in auto.WalkControl(root, includeTop=True, maxDepth=max_depth):
        if time.monotonic() > deadline or count >= max_items:
            break
        count += 1
        yield control, depth


def read_window(hwnd: int, max_items: int = 400, timeout_s: float = 4.0, value_limit: int = 4000) -> dict:
    """Visible labels, messages, fields and document text of one window."""
    if auto is None:
        raise UIAError("UI Automation is not available (the 'uiautomation' package is missing).")
    with auto.UIAutomationInitializerInThread(debug=False):
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise UIAError("That window could not be read (it may have closed).")
        items: list[dict] = []
        seen: set[tuple[str, str]] = set()
        texts: list[str] = []
        for control, _depth in _walk(root, max_items * 3, timeout_s):
            try:
                ctype = control.ControlTypeName
                if ctype not in READ_TYPES:
                    continue
                name = (control.Name or "").strip()
                value = _value(control, value_limit) if ctype in ("EditControl", "DocumentControl") else ""
                if not name and not value:
                    continue
                key = (ctype, name[:200])
                if key in seen and not value:
                    continue
                seen.add(key)
                item = {"type": ctype.replace("Control", "").lower(), "name": name[:300]}
                if value:
                    item["value"] = value
                if _is_password(control):
                    item["password_field"] = True
                items.append(item)
                if ctype in ("TextControl", "DocumentControl", "EditControl", "StatusBarControl", "WindowControl"):
                    texts.append(value or name)
                if len(items) >= max_items:
                    break
            except Exception:
                continue
        return {"title": root.Name or "", "items": items, "text": "\n".join(t for t in texts if t)[:20000]}


def find_elements(hwnd: int, query: str, control_type: str | None = None, limit: int = 10,
                  timeout_s: float = 4.0) -> list[Element]:
    """Controls whose name matches ``query`` (exact match first, then 'contains')."""
    if auto is None:
        raise UIAError("UI Automation is not available.")
    q = " ".join(query.lower().split())
    exact: list[Element] = []
    partial: list[Element] = []
    with auto.UIAutomationInitializerInThread(debug=False):
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise UIAError("That window could not be read.")
        for control, _depth in _walk(root, 3000, timeout_s):
            try:
                ctype = control.ControlTypeName
                if control_type and ctype != control_type:
                    continue
                name = " ".join((control.Name or "").lower().split())
                aid = (control.AutomationId or "").lower()
                if not name and not aid:
                    continue
                if name == q or aid == q or name.rstrip(".…:") == q:
                    target = exact
                elif q in name or (aid and q.replace(" ", "") in aid):
                    target = partial
                else:
                    continue
                target.append(Element(ctype, control.Name or "", control.AutomationId or "", _rect(control),
                                      bool(control.IsEnabled)))
                if len(exact) >= limit:
                    break
            except Exception:
                continue
    ranked = exact + partial
    # Prefer things that can be clicked.
    ranked.sort(key=lambda e: (e not in exact, e.control_type not in CLICKABLE, not e.enabled))
    return ranked[:limit]


def invoke(hwnd: int, element: Element, timeout_s: float = 4.0) -> str:
    """Activate a control through its accessibility pattern. Returns the method used."""
    if auto is None:
        raise UIAError("UI Automation is not available.")
    with auto.UIAutomationInitializerInThread(debug=False):
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            raise UIAError("The window closed.")
        for control, _depth in _walk(root, 3000, timeout_s):
            try:
                if control.ControlTypeName != element.control_type or (control.Name or "") != element.name:
                    continue
                if _rect(control) != element.rect:
                    continue
                if not control.IsEnabled:
                    raise UIAError(f"'{element.name}' is disabled.")
                for getter, action, label in (("GetInvokePattern", "Invoke", "invoke"),
                                              ("GetTogglePattern", "Toggle", "toggle"),
                                              ("GetSelectionItemPattern", "Select", "select"),
                                              ("GetExpandCollapsePattern", "Expand", "expand")):
                    try:
                        pattern = getattr(control, getter)()
                    except Exception:
                        pattern = None
                    if pattern is not None:
                        getattr(pattern, action)()
                        return label
                try:
                    control.GetLegacyIAccessiblePattern().DoDefaultAction()
                    return "default action"
                except Exception:
                    return ""
            except UIAError:
                raise
            except Exception:
                continue
    raise UIAError(f"'{element.name}' is no longer on screen.")


def focused_text(hwnd: int, limit: int = 200000) -> str | None:
    """Text of the main document/edit area of a window (used to verify typing)."""
    if auto is None:
        return None
    with auto.UIAutomationInitializerInThread(debug=False):
        root = auto.ControlFromHandle(hwnd)
        if root is None:
            return None
        best = None
        try:
            focused = auto.GetFocusedControl()
            if focused is not None and focused.ControlTypeName in ("EditControl", "DocumentControl"):
                best = _value(focused, limit)
        except Exception:
            pass
        if best:
            return best
        for control, _depth in _walk(root, 1500, 3.0):
            try:
                if control.ControlTypeName in ("DocumentControl", "EditControl"):
                    text = _value(control, limit)
                    if text:
                        return text
            except Exception:
                continue
    return None
