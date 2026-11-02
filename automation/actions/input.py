"""Feature 3: keyboard, mouse, clipboard and window interaction.

* The target window is always identified first (a named app, else the last
  program the user was using) and brought to the front; typing stops at once
  if another window comes to the front.
* Text that looks like a password, key or token is never typed.
* Only allow-listed keyboard shortcuts are accepted. Keys that can submit or
  destroy something (Enter, Delete, Alt+F4 …) need confirmation; Shift+Delete
  (permanent delete) is high risk.
* Clicking prefers the accessibility API (a named button is "invoked");
  coordinates are used only when that is not possible.
"""

from __future__ import annotations

import re
import time

from automation.actions.common import app_kind_of_window, target_window, wait_until
from automation.models import ActionResult, Risk
from automation.registry import ArgumentError, Capability, CapabilityRegistry, ExecContext, Param
from automation.safety import SafetyError
from security.secrets_filter import contains_secret

LOW_KEYS = {"ctrl+s", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+f", "ctrl+h", "ctrl+t",
            "ctrl+n", "ctrl+o", "ctrl+p", "ctrl+tab", "ctrl+shift+tab", "ctrl+plus", "ctrl+minus", "ctrl+0",
            "ctrl+=", "ctrl+-", "ctrl+shift+t", "ctrl+home", "ctrl+end", "ctrl+b", "ctrl+i", "ctrl+u", "ctrl+l",
            "ctrl+r", "f5", "f11", "f3", "escape", "esc", "home", "end", "pageup", "pagedown", "up", "down", "left",
            "right", "tab", "shift+tab", "alt+left", "alt+right", "win+d", "space", "ctrl+shift+s", "f2"}
MEDIUM_KEYS = {"enter", "return", "ctrl+enter", "shift+enter", "delete", "del", "backspace", "alt+f4", "ctrl+w",
               "ctrl+shift+w", "ctrl+q", "ctrl+d", "ctrl+shift+n", "ctrl+k", "alt+tab"}
HIGH_KEYS = {"shift+delete", "shift+del", "ctrl+shift+delete", "ctrl+shift+del"}
CONSEQUENTIAL = re.compile(r"\b(send|submit|post|publish|pay|buy|purchase|order|checkout|delete|remove|erase|"
                           r"transfer|confirm|sign ?in|log ?in|install|uninstall|format|reset|accept|agree)\b", re.I)


def _norm_combo(combo: str) -> str:
    combo = combo.lower().replace(" ", "").replace("control", "ctrl").replace("escape", "esc")
    return "+".join(combo.split("+"))


def _focus_target(ctx: ExecContext, app: str | None):
    window = target_window(ctx, app)
    desktop = ctx.services.desktop
    if not desktop.focus(window.hwnd):
        raise SafetyError(f"I couldn't bring “{window.title[:50]}” to the front, so I didn't send any input.")
    fg = desktop.foreground()
    if fg is None or fg.hwnd != window.hwnd:
        raise SafetyError("The target window is not in front, so I didn't send any input.")
    return window


def _target_precheck(ctx: ExecContext, args: dict) -> None:
    target_window(ctx, args.get("app") or None)  # raises if there is no identifiable target


# ------------------------------------------------------------------ typing
def _type_risk(ctx: ExecContext, args: dict) -> Risk:
    try:
        window = target_window(ctx, args.get("app") or None)
    except LookupError:
        return Risk.MEDIUM
    kind = app_kind_of_window(ctx, window)
    if kind in ("editor", "office"):
        return Risk.LOW
    if kind == "communication" and "\n" in args.get("text", ""):
        return Risk.HIGH  # a new line in a chat box sends the message
    return Risk.MEDIUM


def _type_precheck(ctx: ExecContext, args: dict) -> None:
    text = args["text"]
    if contains_secret(text):
        raise SafetyError("That text looks like a password, key or other secret, so I won't type it. "
                          "Please type secrets yourself.")
    if len(text) > ctx.services.limit("type_max_chars"):
        raise ArgumentError(f"That's too much text to type ({len(text)} characters; the limit is "
                            f"{ctx.services.limit('type_max_chars')}).")
    _target_precheck(ctx, args)


def _type(ctx: ExecContext, args: dict) -> ActionResult:
    window = _focus_target(ctx, args.get("app") or None)
    desktop = ctx.services.desktop
    uia = ctx.services.uia
    before = None
    if uia is not None:
        try:
            before = uia.focused_text(window.hwnd)
        except Exception:
            before = None
    if args.get("new_document") and before:
        # The app reopened an existing document (Windows 11 Notepad restores old tabs): start a new one first,
        # so the text never lands inside one of the user's files.
        desktop.press("ctrl+n")
        # wait until the new blank document really has the keyboard focus (typing earlier garbles the text)
        blank = wait_until(ctx, lambda: (desktop.foreground() is not None
                                         and not uia.focused_text(desktop.foreground().hwnd)), 4)
        window = desktop.foreground() or window
        before = uia.focused_text(window.hwnd) if uia is not None else None
        if not blank or before:
            return ActionResult.fail(f"“{window.title[:50]}” already contains a document and I couldn't open a new "
                                     "blank one, so I didn't type anything.")
    text = args["text"]
    time.sleep(0.25)  # let the window finish taking the keyboard focus
    typed = desktop.type_text(text, window.hwnd, ctx.cancel)
    if typed < len(text):
        return ActionResult.fail(f"Typing stopped after {typed} of {len(text)} characters.")
    time.sleep(0.3)

    def flat(value: str | None) -> str:
        return (value or "").replace("\r", "").replace("\n", "")

    after = uia.focused_text(window.hwnd) if uia is not None else None
    if after is None:
        return ActionResult(True, f"Typed {len(text)} characters into “{window.title[:50]}”.", verified=False,
                            verification="the program does not expose its text, so the result could not be read back",
                            data={"hwnd": window.hwnd, "chars": len(text)})
    expected = flat(text)
    if args.get("new_document"):
        matches = lambda: flat(uia.focused_text(window.hwnd)) == expected  # noqa: E731 - the whole new document
    else:
        matches = lambda: expected in flat(uia.focused_text(window.hwnd))  # noqa: E731
    if wait_until(ctx, matches, 3):
        return ActionResult.done(f"Typed {len(text)} characters into “{window.title[:50]}”.",
                                 "the typed text was read back from the window and matches exactly",
                                 hwnd=window.hwnd, chars=len(text))
    return ActionResult.fail(f"I typed into “{window.title[:50]}”, but the text in the window doesn't match what I "
                             "typed (the program may have changed or dropped characters). Please check it.")


# ------------------------------------------------------------------ keys / scroll / click
def _keys_precheck(ctx: ExecContext, args: dict) -> None:
    combo = _norm_combo(args["keys"])
    if combo not in LOW_KEYS | MEDIUM_KEYS | HIGH_KEYS:
        raise SafetyError(f"'{args['keys']}' is not an approved keyboard shortcut.")
    from automation.platform.windows import parse_combo
    try:
        parse_combo(combo)
    except ValueError as exc:
        raise ArgumentError(str(exc)) from exc
    args["keys"] = combo
    _target_precheck(ctx, args)


def _keys_risk(ctx: ExecContext, args: dict) -> Risk:
    combo = _norm_combo(args["keys"])
    return Risk.HIGH if combo in HIGH_KEYS else Risk.MEDIUM if combo in MEDIUM_KEYS else Risk.LOW


def _keys(ctx: ExecContext, args: dict) -> ActionResult:
    window = _focus_target(ctx, args.get("app") or None)
    desktop = ctx.services.desktop
    combo = args["keys"]
    title_before = window.title
    seq_before = desktop.clipboard_sequence()
    desktop.press(combo)
    time.sleep(0.4)
    after = desktop.window(window.hwnd)
    verification, verified = "the key press was delivered to the active window", True
    if combo == "ctrl+s":
        # Most editors drop the "*" / "●" unsaved marker from the title, or open a Save dialog for new files.
        fg = desktop.foreground()
        if fg is not None and fg.hwnd != window.hwnd:
            verification = f"a dialog opened: “{fg.title[:50]}” — choose a name and location there"
        elif after is not None and after.title != title_before:
            verification = f"title changed to “{after.title[:60]}” (saved)"
        elif after is not None and not re.search(r"(^\*|\*\s|●)", after.title):
            verification = "no unsaved-changes marker in the title"
        else:
            verified, verification = False, "the title still shows unsaved changes"
    elif combo in ("ctrl+c", "ctrl+x"):
        changed = wait_until(ctx, lambda: desktop.clipboard_sequence() != seq_before, 1.5)
        verified = bool(changed)
        verification = "the clipboard changed" if changed else "the clipboard did not change (was anything selected?)"
    elif combo == "alt+f4" or combo in ("ctrl+w", "ctrl+q"):
        gone = wait_until(ctx, lambda: desktop.window(window.hwnd) is None, 2)
        verification = "the window closed" if gone else "the window is still open (it may be asking to save)"
        verified = bool(gone) or combo == "ctrl+w"
    return ActionResult(True, f"Pressed {combo.upper()} in “{window.title[:50]}”.", verified=verified,
                        verification=verification, data={"hwnd": window.hwnd})


def _scroll(ctx: ExecContext, args: dict) -> ActionResult:
    window = _focus_target(ctx, args.get("app") or None)
    clicks = int(args.get("amount") or 3) * (1 if args["direction"] == "up" else -1)
    ctx.services.desktop.scroll(window.hwnd, clicks)
    return ActionResult.done(f"Scrolled {args['direction']} in “{window.title[:50]}”.",
                             "scroll input delivered to the window in front", hwnd=window.hwnd)


def _find_element(ctx: ExecContext, args: dict):
    uia = ctx.services.uia
    if uia is None:
        raise LookupError("Reading on-screen controls is not available on this computer.")
    window = target_window(ctx, args.get("app") or None)
    control_type = None
    if args.get("control_type"):
        from automation.platform.uia import TYPE_WORDS
        control_type = TYPE_WORDS.get(args["control_type"].lower())
    matches = uia.find_elements(window.hwnd, args["name"], control_type)
    if not matches:
        raise LookupError(f"I couldn't find “{args['name']}” in “{window.title[:50]}”.")
    return window, matches


def _click_risk(ctx: ExecContext, args: dict) -> Risk:
    return Risk.HIGH if CONSEQUENTIAL.search(args.get("name", "")) else Risk.MEDIUM


def _click(ctx: ExecContext, args: dict) -> ActionResult:
    window, matches = _find_element(ctx, args)
    exact = [m for m in matches if m.name.lower().strip() == args["name"].lower().strip()]
    if len(exact) == 1:
        element = exact[0]
    elif len(matches) == 1:
        element = matches[0]
    else:
        names = "; ".join(f"{m.control_type.replace('Control', '').lower()} “{m.name}”" for m in matches[:5])
        return ActionResult.fail(f"More than one control matches “{args['name']}” ({names}). Please be more specific.")
    if not element.enabled:
        return ActionResult.fail(f"“{element.name}” is disabled (greyed out).")
    desktop = ctx.services.desktop
    _focus_target(ctx, args.get("app") or None)
    title_before = window.title
    method = ""
    try:
        method = ctx.services.uia.invoke(window.hwnd, element)
    except Exception:
        method = ""
    if not method:
        x, y = element.center
        desktop.click(x, y)
        method = f"mouse click at {x},{y}"
    time.sleep(0.6)
    fg = desktop.foreground()
    after = desktop.window(window.hwnd)
    changes = []
    if fg is not None and fg.hwnd != window.hwnd:
        changes.append(f"“{fg.title[:50]}” opened")
    if after is None:
        changes.append("the window closed")
    elif after.title != title_before:
        changes.append(f"title is now “{after.title[:50]}”")
    return ActionResult(True, f"Clicked “{element.name}” ({method}).", verified=True,
                        verification="; ".join(changes) or "the control accepted the action (no visible change "
                                                           "in the window title)",
                        data={"element": element.as_dict()})


# ------------------------------------------------------------------ clipboard
def _copy_selection(ctx: ExecContext, args: dict) -> ActionResult:
    window = _focus_target(ctx, args.get("app") or None)
    desktop = ctx.services.desktop
    before = desktop.clipboard_sequence()
    desktop.press("ctrl+c")
    changed = wait_until(ctx, lambda: desktop.clipboard_sequence() != before, 2)
    if not changed:
        return ActionResult(True, f"Pressed Ctrl+C in “{window.title[:50]}”, but the clipboard did not change.",
                            verified=False, verification="nothing was selected, or the program blocks copying")
    text = desktop.get_clipboard() or ""
    preview = text if len(text) <= 300 else text[:300] + "…"
    return ActionResult.done(f"Copied {len(text)} characters from “{window.title[:50]}”:\n\n{preview}",
                             "the clipboard changed and now holds the selection", chars=len(text), text=text)


def _paste_risk(ctx: ExecContext, args: dict) -> Risk:
    try:
        window = target_window(ctx, args.get("app") or None)
    except LookupError:
        return Risk.MEDIUM
    return Risk.LOW if app_kind_of_window(ctx, window) in ("editor", "office") else Risk.MEDIUM


def _paste_precheck(ctx: ExecContext, args: dict) -> None:
    if args.get("text") and contains_secret(args["text"]):
        raise SafetyError("That text looks like a secret, so I won't paste it.")
    _target_precheck(ctx, args)


def _paste(ctx: ExecContext, args: dict) -> ActionResult:
    desktop = ctx.services.desktop
    if args.get("text"):
        desktop.set_clipboard(args["text"])
    content = desktop.get_clipboard()
    if not content:
        return ActionResult.fail("The clipboard has no text to paste.")
    if contains_secret(content):
        return ActionResult.fail("The clipboard holds something that looks like a password or key; I won't paste it.")
    window = _focus_target(ctx, args.get("app") or None)
    desktop.press("ctrl+v")
    return ActionResult(True, f"Pasted {len(content)} characters into “{window.title[:50]}”.", verified=True,
                        verification="Ctrl+V delivered to the window in front", data={"chars": len(content)})


def _clipboard(ctx: ExecContext, args: dict) -> ActionResult:
    text = ctx.services.desktop.get_clipboard()
    if text is None:
        return ActionResult.done("The clipboard doesn't contain text.", "clipboard read", text="")
    preview = text if len(text) <= 500 else text[:500] + "…"
    return ActionResult.done(f"The clipboard holds {len(text)} characters:\n\n{preview}", "clipboard read",
                             text=text)


def register(reg: CapabilityRegistry) -> None:
    app = Param(str, required=False, default="", max_len=120, help="target app; default = the app you last used")
    reg.add(Capability("input.type_text", "Type text into an application window (never secrets)",
                       {"text": Param(str, max_len=50000, strip=False), "app": app,
                        "new_document": Param(bool, required=False, default=False,
                                              help="type into a new blank document, never an existing one")},
                       _type, "automation.input", Risk.LOW,
                       dynamic_risk=_type_risk, precheck=_type_precheck, private_args=("text",), timeout_s=180,
                       describe=lambda a: f"Type {len(a.get('text', ''))} characters"
                       + (f" into {a['app']}" if a.get("app") else "")))
    reg.add(Capability("input.press_keys", "Press an approved keyboard shortcut in an application",
                       {"keys": Param(str, max_len=40, help="e.g. ctrl+s, ctrl+c, f5, enter"), "app": app},
                       _keys, "automation.input", Risk.LOW, dynamic_risk=_keys_risk, precheck=_keys_precheck,
                       describe=lambda a: f"Press {a['keys'].upper()}" + (f" in {a['app']}" if a.get("app") else "")))
    reg.add(Capability("input.scroll", "Scroll an application window up or down",
                       {"direction": Param(str, choices=("up", "down")),
                        "amount": Param(int, required=False, default=3, minimum=1, maximum=30), "app": app},
                       _scroll, "automation.input", Risk.LOW, precheck=_target_precheck,
                       describe=lambda a: f"Scroll {a['direction']}" + (f" in {a['app']}" if a.get("app") else "")))
    reg.add(Capability("input.click", "Click a named button, link, tab or menu item in an application",
                       {"name": Param(str, max_len=120), "app": app,
                        "control_type": Param(str, required=False, default="", max_len=20)},
                       _click, "automation.input", Risk.MEDIUM, min_risk=Risk.MEDIUM, dynamic_risk=_click_risk,
                       precheck=lambda c, a: _find_element(c, a) and None,
                       describe=lambda a: f"Click “{a['name']}”" + (f" in {a['app']}" if a.get("app") else "")))
    reg.add(Capability("input.copy_selection", "Copy the currently selected text in an application",
                       {"app": app}, _copy_selection, "automation.input", Risk.LOW, precheck=_target_precheck,
                       describe=lambda a: "Copy the selected text"))
    reg.add(Capability("input.paste", "Paste (optionally given text) into an application",
                       {"app": app, "text": Param(str, required=False, default="", max_len=50000, strip=False)},
                       _paste, "automation.input", Risk.LOW, dynamic_risk=_paste_risk, precheck=_paste_precheck,
                       private_args=("text",), describe=lambda a: "Paste" + (f" into {a['app']}" if a.get("app") else "")))
    reg.add(Capability("input.read_clipboard", "Read the text currently on the clipboard", {}, _clipboard,
                       "automation.input", Risk.LOW, describe=lambda a: "Read the clipboard"))
