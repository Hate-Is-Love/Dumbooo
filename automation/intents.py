"""Turning what the user says into a structured plan (rule-based, no AI needed).

Common commands ("open Chrome", "organise my Downloads", "how much RAM is
available?") are recognised with predictable rules and turned into steps. Only
goals the rules don't cover go to the AI planner (``planner.py``).

The parser may *read* things to build the plan (the user's saved project
paths, which windows are open, which file a name refers to), but it never
changes anything: every step it produces is executed by the controller under
the policy engine. When something is missing or ambiguous it returns an
``Ask`` with a question instead of guessing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from automation.actions.common import norm, resolve_app, windows_matching
from automation.actions.files import walk
from automation.actions.web import resolve_site
from automation.config import known_folder
from automation.models import Step
from automation.personalization import (find_project, installed_editors, project_name, saved_folder, saved_website,
                                        saved_workflow, usual_app)
from automation.registry import ExecContext
from automation.safety import SafetyError

_LEAD = re.compile(r"^\s*(?:(?:hey|hi|ok|okay)\s+)?(?:dumboo|dumbo)\s*[,:]?\s*", re.I)
_POLITE = re.compile(r"^(?:please|kindly|can you|could you|would you|will you|i want you to|i'd like you to|"
                     r"i need you to|go ahead and|now|just|also)\s+", re.I)
_TAIL = re.compile(r"(?:\s+(?:please|for me|now|right now|thanks|thank you))+$|[\s.!?;]+$", re.I)
ORDINALS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3,
            "fourth": 4, "4th": 4, "four": 4, "fifth": 5, "5th": 5, "five": 5, "top": 1}
KNOWN_FOLDERS = {"downloads": "downloads", "download": "downloads", "documents": "documents", "document": "documents",
                 "docs": "documents", "desktop": "desktop", "pictures": "pictures", "photos": "pictures",
                 "music": "music", "videos": "videos"}
EXT_WORDS = {"pdf": ".pdf", "pdfs": ".pdf", "word": ".docx", "docx": ".docx", "excel": ".xlsx", "spreadsheets": ".xlsx",
             "images": None, "photos": None, "python": ".py", "py": ".py", "text": ".txt", "txt": ".txt",
             "powerpoint": ".pptx", "csv": ".csv", "zip": ".zip", "mp3": ".mp3", "mp4": ".mp4", "json": ".json",
             "markdown": ".md", "md": ".md", "jpg": ".jpg", "png": ".png"}
STOP_RE = re.compile(r"^(?:emergency stop|stop everything|stop all(?: tasks| automation| actions)?|abort(?: everything|"
                     r" the task| automation)?|cancel (?:the |this |current |my )?(?:task|automation|everything)|"
                     r"stop (?:the |this )?(?:task|automation|automating))$", re.I)


@dataclass
class Plan:
    steps: list[Step]
    title: str
    origin: str = "rules"


@dataclass
class Ask:
    text: str


@dataclass
class Info:
    text: str


@dataclass
class State:
    """What earlier clauses of the same request established ("open my project and run *its* tests")."""

    project: str | None = None
    app: str | None = None
    last_paths: list[str] = field(default_factory=list)


def clean(text: str) -> str:
    t = _LEAD.sub("", text or "").strip()
    for _ in range(3):
        t = _POLITE.sub("", t).strip()
    if re.match(r"^(?:(?:open|launch|start) .+? and (?:then )?)?(?:type|write)\b", t, re.I):
        return t  # text to type is kept exactly, including its final full stop and words like "now"
    return _TAIL.sub("", t).strip().strip("\"'“”").strip()


def is_stop(text: str) -> bool:
    return bool(STOP_RE.match(clean(text)))


# ------------------------------------------------------------------ resolution helpers
class Resolver:
    def __init__(self, ctx: ExecContext, last_paths: list[str]) -> None:
        self.ctx = ctx
        self.state = State(last_paths=list(last_paths))

    # projects -----------------------------------------------------------------------
    def project(self, name: str | None) -> str | Ask:
        found = find_project(self.ctx, name)
        label = f"your {name} project" if name else "your project"
        if found is None:
            if name:
                return Ask(f"I don't have a folder saved for {label}. Tell me where it is, for example: "
                           f"“remember that my {name} project is in C:\\Users\\you\\Projects\\{name}”.")
            return Ask("Which project? I don't have any project folders saved yet. Say “remember that my <name> "
                       "project is in <folder>”.")
        if found.options:
            names = ", ".join(project_name(m) for m in found.options[:6])
            return Ask(f"You have several saved projects ({names}). Which one do you mean?")
        if found.problem:
            return Ask(f"I can't use {label}: {found.problem}. Is it somewhere else now? Tell me the new location "
                       f"(“remember that my {project_name(found.memory)} project is in …”).")
        self.state.project = found.value
        return found.value

    def project_from_text(self, text: str) -> str | Ask | None:
        """'my DUMBOO project', 'the dumboo project', 'my project', 'its'/'it' (earlier clause)."""
        t = norm(text)
        if t in ("its", "it", "the project", "this project", "project") and self.state.project:
            return self.state.project
        m = re.match(r"^(?:my |the |our )?(?:(?P<name>.+?) )?(?:project|repo|repository|code ?base)(?: folder)?$",
                     text.strip(), re.I)
        if m:
            name = (m.group("name") or "").strip()
            name = re.sub(r"^(?:my|the|our)\s+", "", name, flags=re.I)
            if name.lower() in ("python", "current", "latest", "usual", "main") or not name:
                name = None if name.lower() != "python" else None
            return self.project(name)
        if self.state.project and t in ("it", "its"):
            return self.state.project
        found = find_project(self.ctx, text) if len(t) >= 3 else None
        if found is not None and not found.options:
            if found.problem:
                return Ask(f"I can't use your {text} project: {found.problem}.")
            self.state.project = found.value
            return found.value
        return None

    # apps -----------------------------------------------------------------------------
    def usual(self, purpose: str) -> str | Ask:
        app, found = usual_app(self.ctx, purpose)
        if app is not None:
            return app.key
        if found is not None and found.problem:
            return Ask(f"I can't use it: {found.problem}.")
        if purpose == "coding":
            editors = installed_editors(self.ctx)
            names = " or ".join(e.name for e in editors) or "an editor"
            return Ask(f"I don't know your usual coding application yet. Which one should I use — {names}? "
                       "(Say “remember my usual coding application is …” so I know next time.)")
        return Ask(f"I don't know your usual {purpose} application yet. Which one should I use?")

    def editor_for_project(self) -> str | Ask:
        app, found = usual_app(self.ctx, "coding")
        if app is not None and "folder" in app.opens:
            return app.key
        editors = installed_editors(self.ctx)
        if len(editors) == 1:
            return editors[0].key
        if not editors:
            return Ask("I couldn't find an installed code editor (VS Code, Antigravity …) to open the project with.")
        names = " or ".join(e.name for e in editors)
        return Ask(f"Which editor should I open it with — {names}? (Say “remember my usual coding application is …” "
                   "and I'll use it automatically.)")

    # folders and files ------------------------------------------------------------------
    def folder(self, text: str) -> str | Ask:
        raw = text.strip().strip("\"'")
        t = norm(raw.replace("folder", "").replace("directory", ""))
        if re.match(r"^[A-Za-z]:[\\/]", raw):
            return raw
        if t in KNOWN_FOLDERS:
            return str(known_folder(KNOWN_FOLDERS[t]))
        if t in ("here", "this", "current", "this one", "open", "explorer"):
            return self.this_folder()
        proj = self.project_from_text(raw) if "project" in raw.lower() else None
        if isinstance(proj, (str, Ask)):
            return proj
        saved = saved_folder(self.ctx, t)
        if saved is not None:
            if saved.problem:
                return Ask(f"Your saved {t} folder can't be used: {saved.problem}.")
            return saved.value
        matches = self.find_named(t, kind="folder")
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return Ask(f"I found several folders called “{t}”:\n" + "\n".join(f"- {m}" for m in matches[:6]) +
                       "\nWhich one? (Say “remember that my " + t + " folder is …” to save it.)")
        return Ask(f"I couldn't find a folder called “{t}” in the permitted folders. Tell me its full path.")

    def this_folder(self) -> str | Ask:
        desktop = self.ctx.services.desktop
        last = desktop.last_external()
        folders = desktop.explorer_folders()
        if last is not None:
            for hwnd, path in folders:
                if hwnd == last.hwnd:
                    return path
        if len(folders) == 1:
            return folders[0][1]
        return Ask("Which folder do you mean? Open it in File Explorer and ask again, or tell me its full path.")

    def find_named(self, name: str, kind: str = "file", ext: str | None = None, limit: int = 8) -> list[str]:
        name = name.strip().strip("\"'").lower()
        if not name:
            return []
        exact, partial = [], []
        deadline = time.monotonic() + 8
        for path, entry, _depth in walk(self.ctx, self.ctx.services.guard.roots(), timeout_s=8):
            is_dir = entry.is_dir(follow_symlinks=False)
            if (kind == "folder") != is_dir:
                continue
            fname = path.name.lower()
            if ext and path.suffix.lower() != ext:
                continue
            if fname == name or path.stem.lower() == name:
                exact.append(str(path))
            elif kind == "file" and name in fname:
                partial.append(str(path))
            if len(exact) >= limit or time.monotonic() > deadline:
                break
        return exact or partial[:limit]

    def file(self, text: str, ext: str | None = None) -> str | Ask:
        raw = text.strip().strip("\"'")
        if re.match(r"^[A-Za-z]:[\\/]", raw):
            return raw
        t = raw.lower()
        if re.match(r"^(?:this|that|the|it)(?: (?:file|document|pdf|one))?$", t):
            candidates = [p for p in self.state.last_paths if not ext or p.lower().endswith(ext)]
            if len(candidates) == 1 or (candidates and t in ("it", "that")):
                return candidates[0]
            window_file = self.file_in_window(ext)
            if window_file:
                return window_file
            return Ask("Which file do you mean? Tell me its name or full path.")
        name = re.sub(r"^(?:my|the|this|that)\s+", "", raw, flags=re.I)
        name = re.sub(r"\s+(?:file|document)$", "", name, flags=re.I)
        matches = self.find_named(name, "file", None)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return Ask(f"I found several files matching “{name}”:\n" + "\n".join(f"- {m}" for m in matches[:6]) +
                       "\nWhich one? (You can say the full path.)")
        return Ask(f"I couldn't find a file called “{name}” in the permitted folders.")

    def file_in_window(self, ext: str | None) -> str | None:
        last = self.ctx.services.desktop.last_external()
        if last is None:
            return None
        m = re.search(r"([^\\/:*?\"<>|]+\.(pdf|docx?|txt|md|xlsx?|pptx?|py|csv))", last.title, re.I)
        if not m:
            return None
        name = m.group(1).strip()
        if ext and not name.lower().endswith(ext):
            return None
        matches = self.find_named(name, "file")
        return matches[0] if len(matches) == 1 else None


# ------------------------------------------------------------------ the parser
Handler = Callable[[re.Match, Resolver], "Plan | Ask | Info | None"]


def _plan(*steps: Step, title: str = "") -> Plan:
    return Plan(list(steps), title or steps[0].description or steps[0].capability)


def _s(capability: str, description: str = "", **args) -> Step:
    return Step(capability, {k: v for k, v in args.items() if v is not None}, description)


def _open_target(target: str, r: Resolver, with_app: str | None = None) -> Plan | Ask:
    """'open X': usual app, project, approved app, website, saved website, known folder, file."""
    raw = target.strip()
    t = norm(raw)
    m = re.match(r"^(?:my )?usual (?P<purpose>\w+)(?: application| app| editor| program| browser)?$", raw, re.I)
    if m:
        purpose = m.group("purpose").lower()
        purpose = "coding" if purpose in ("coding", "code", "programming", "development", "dev", "editor", "ide") \
            else purpose
        app = r.usual(purpose)
        if isinstance(app, Ask):
            return app
        r.state.app = app
        return _plan(_s("apps.open", f"Open your usual {purpose} application", app=app))
    if re.search(r"\b(?:website|site|web ?page|link)\b", raw, re.I) and re.search(r"\b(?:i|my|saved|used)\b", raw, re.I):
        sm = re.search(r"(?:for|of) (?:my |the )?(?P<name>.+?)(?: project)?$", raw, re.I)
        name = sm.group("name") if sm else None
        found = saved_website(r.ctx, name) or (saved_website(r.ctx, None) if name else None)
        if found is None:
            return Ask("I don't have a website saved" + (f" for {name}" if name else "") + ". Say “remember that the "
                       f"{name or 'project'} website is https://…” first.")
        if found.options:
            return Ask("Which website? " + "; ".join(f"{m['memory_key']}: {m['memory_value']}" for m in found.options[:5]))
        if found.problem:
            return Ask(f"The saved website can't be opened: {found.problem}")
        return _plan(_s("web.open", f"Open your saved website ({found.value})", url=found.value,
                        label=found.memory["memory_key"]))
    proj = r.project_from_text(raw) if re.search(r"\b(project|repo|repository)\b", raw, re.I) else None
    if proj is None and resolve_app(r.ctx, raw) is None and not resolve_site(r.ctx, raw):
        proj = r.project_from_text(raw)  # e.g. "open DUMBOO" when a DUMBOO project is saved
    if isinstance(proj, Ask):
        return proj
    if isinstance(proj, str):
        app = with_app or r.editor_for_project()
        if isinstance(app, Ask):
            return app
        r.state.app = app
        return _plan(_s("apps.open", f"Open the {Path(proj).name} project in {resolve_app(r.ctx, app).name}",
                        app=app, target=proj))
    app = resolve_app(r.ctx, raw)
    if app is not None:
        r.state.app = app.key
        return _plan(_s("apps.open", f"Open {app.name}", app=app.key))
    url = resolve_site(r.ctx, raw)
    if url:
        return _plan(_s("web.open", f"Open {raw}", url=url, label=raw))
    if t.replace(" folder", "") in KNOWN_FOLDERS or re.match(r"^[A-Za-z]:[\\/]", raw):
        folder = r.folder(raw)
        if isinstance(folder, Ask):
            return folder
        return _plan(_s("files.open", f"Open {Path(folder).name}", path=folder))
    if re.search(r"\.\w{2,5}$", raw) or re.search(r"\b(?:file|document|pdf)\b", raw, re.I):
        path = r.file(raw)
        if isinstance(path, Ask):
            return path
        return _plan(_s("files.open", f"Open {Path(path).name}", path=path, app=with_app))
    if windows_matching(r.ctx, raw):
        return _plan(_s("apps.focus", f"Switch to {raw}", app=raw))
    approved = ", ".join(sorted(a.name for a in r.ctx.services.config.applications.values()))
    return Ask(f"I don't know an approved app, saved project, website or folder called “{raw}”. Approved apps: "
               f"{approved}. (The owner can approve more apps on the AUTOMATION page.)")


def _h_open_and_type(m: re.Match, r: Resolver):
    if re.fullmatch(r"(?:this|the following|the|a|my)?\s*(?:paragraph|text|note|message|this)?", m.group("text").strip(),
                    re.I):
        return Ask("What should I type? Say it again with the text, e.g. “open Notepad and type: Hello world”.")
    opened = _open_target(m.group("app"), r)
    if not isinstance(opened, Plan):
        return opened
    app = opened.steps[0].args.get("app") or m.group("app")
    return _plan(*opened.steps, _s("input.type_text", f"Type the text into {app}", text=m.group("text"), app=app,
                                   new_document=True),
                 title=f"Open {m.group('app')} and type")


def _h_open_with(m: re.Match, r: Resolver):
    app_name, target = m.group("app"), m.group("target")
    app = resolve_app(r.ctx, app_name)
    if app is None:
        usual = re.match(r"^(?:my )?usual (\w+)", app_name, re.I)
        if usual:
            key = r.usual("coding")
            if isinstance(key, Ask):
                return key
            app = resolve_app(r.ctx, key)
    if app is None:
        return None
    proj = r.project_from_text(target)
    if isinstance(proj, Ask):
        return proj
    if isinstance(proj, str):
        r.state.app = app.key
        return _plan(_s("apps.open", f"Open {Path(proj).name} in {app.name}", app=app.key, target=proj))
    path = r.file(target)
    if isinstance(path, Ask):
        return path
    return _plan(_s("apps.open", f"Open {Path(path).name} in {app.name}", app=app.key, target=path))


def _h_open(m: re.Match, r: Resolver):
    result = _open_target(m.group("target"), r)
    verb = (m.groupdict().get("verb") or "open").lower()
    if isinstance(result, Ask) and verb != "open" and result.text.startswith("I don't know an approved app"):
        return None  # "start a story about ..." is conversation
    return result


def _h_open_in(m: re.Match, r: Resolver):
    app = resolve_app(r.ctx, m.group("app"))
    if app is None:
        return None
    target = m.group("target")
    proj = r.project_from_text(target) if re.search(r"\bproject\b", target, re.I) else None
    if isinstance(proj, Ask):
        return proj
    path = proj or (r.folder(target) if re.search(r"\bfolder\b", target, re.I) else r.file(target))
    if isinstance(path, Ask):
        return path
    return _plan(_s("apps.open", f"Open {Path(path).name} in {app.name}", app=app.key, target=path))


def _h_search_files(m: re.Match, r: Resolver):
    return _h_find(m, r) or _plan(_s("files.search", f"Search for “{m.group('what')}”", query=m.group("what")))


def _h_window(m: re.Match, r: Resolver):
    verb = m.group("verb").lower()
    action = {"minimize": "minimize", "minimise": "minimize", "hide": "minimize", "maximize": "maximize",
              "maximise": "maximize", "restore": "restore", "unminimize": "restore"}[verb]
    target = m.group("app")
    if target.lower() in ("it", "that", "this") and r.state.app:
        target = r.state.app
    return _plan(_s("apps.window", f"{action.capitalize()} {target}", app=target, action=action,
                    all_windows=bool(re.search(r"\ball\b", m.group(0), re.I))))


def _h_focus(m: re.Match, r: Resolver):
    target = m.group("app")
    if re.match(r"^(?:my |the )?(?:web )?browser$", target, re.I):
        key = r.usual("browser")
        if isinstance(key, Ask):
            for candidate in ("chrome", "edge", "firefox"):
                app = resolve_app(r.ctx, candidate)
                if app and windows_matching(r.ctx, candidate):
                    key = candidate
                    break
            else:
                return key
        target = key
    if not windows_matching(r.ctx, target) and resolve_site(r.ctx, target) and resolve_app(r.ctx, target) is None:
        return _open_target(target, r)  # "go to github.com"
    if (re.match(r"^(?:go|jump|flip)\b", m.string, re.I) and not windows_matching(r.ctx, target)
            and resolve_app(r.ctx, target) is None):
        return None  # "go to sleep", "go to the next topic": conversation, not a window switch
    r.state.app = target
    return _plan(_s("apps.focus", f"Switch to {target}", app=target))


def _h_close(m: re.Match, r: Resolver):
    target = m.group("app")
    if re.match(r"^(?:the )?(?:this |current )?(?:browser )?tab$", target, re.I):
        return _plan(_s("web.tabs", "Close the browser tab", action="close"))
    if m.group("force"):
        return _plan(_s("apps.force_close", f"Force-close {target}", app=target))
    return _plan(_s("apps.close", f"Close {target}", app=target))


def _h_status(m: re.Match, r: Resolver):
    return _plan(_s("apps.status", f"Check whether {m.group('app')} is running", app=m.group("app")))


def _h_create_folder(m: re.Match, r: Resolver):
    name = m.group("name").strip().strip("\"'“”")
    where = m.group("where")
    parent = r.folder(where) if where else str(known_folder("documents"))
    if isinstance(parent, Ask):
        return parent
    return _plan(_s("files.create_folder", f"Create the folder “{name}” in {Path(parent).name}", parent=parent,
                    name=name))


def _h_create_file(m: re.Match, r: Resolver):
    name = m.group("name").strip().strip("\"'“”")
    where = m.group("where")
    folder = r.folder(where) if where else str(known_folder("documents"))
    if isinstance(folder, Ask):
        return folder
    if "." not in name:
        name += ".txt"
    content = (m.group("content") or "").strip().strip("\"'“”")
    return _plan(_s("files.create_file", f"Create {name} in {Path(folder).name}", folder=folder, name=name,
                    content=content))


def _h_write_code(m: re.Match, r: Resolver):
    lang = (m.group("lang") or "python").lower()
    ext = {"python": ".py", "javascript": ".js", "js": ".js", "java": ".java", "c": ".c", "c++": ".cpp",
           "html": ".html", "typescript": ".ts", "go": ".go", "rust": ".rs"}.get(lang, ".py")
    what = m.group("what").strip()
    name = m.group("name")
    if not name:
        words = [w for w in re.findall(r"[a-z0-9]+", what.lower()) if w not in
                 ("that", "which", "calculates", "calculate", "computes", "compute", "the", "a", "an", "of", "to", "for",
                  "and", "prints", "print", "returns", "finds", "find", "number", "numbers", "given", "it", "is")]
        name = ("_".join(words[:3]) or "script")
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name.strip())
    if not name.lower().endswith(ext):
        name += ext
    where = m.group("where")
    folder = r.folder(where) if where else (r.state.project or str(known_folder("documents")))
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("dev.write_code", f"Write {name}", folder=folder, name=name, request=f"A {lang} file that {what}"),
                 title=f"Create {name}")


def _ext_from(text: str) -> str | None:
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in EXT_WORDS and EXT_WORDS[word]:
            return EXT_WORDS[word]
    m = re.search(r"\.([a-z0-9]{2,5})\b", text.lower())
    return "." + m.group(1) if m else None


def _h_find(m: re.Match, r: Resolver):
    what = m.group("what").strip()
    low = what.lower()
    if not looks_like_files(what) and not re.match(r"^my\b", low):
        return None  # "find the derivative of x" is a question for the AI, not a file search
    pm = re.match(r"^(?:my |the )?(?:latest|newest|most recent|last|recent) (?P<lang>\w+ )?project$", low)
    if pm:
        lang = (pm.group("lang") or "").strip()
        lang = {"python": "python", "node": "node", "javascript": "node", "java": "java", "rust": "rust",
                "go": "go"}.get(lang, "any")
        return _plan(_s("files.latest_project", "Find your latest " + (lang + " " if lang != "any" else "") + "project",
                        language=lang))
    if re.match(r"^(?:my )?(?:recent|latest|last) (?:documents?|files?)$", low):
        return _plan(_s("files.recent", "Find your recent documents", kind="documents"))
    if re.match(r"^(?:all )?duplicates?(?: files?)?(?: in (?P<f>.+))?$", low) or "duplicate" in low:
        fm = re.search(r"\bin (?:my )?(?P<f>.+)$", what, re.I)
        folder = r.folder(fm.group("f")) if fm else None
        if isinstance(folder, Ask):
            return folder
        return _plan(_s("files.duplicates", "Find duplicate files", folder=folder or ""))
    cm = re.match(r"^(?:files?|documents?) (?:containing|that contain|with|mentioning) (?P<text>.+?)"
                  r"(?: in (?P<f>.+))?$", what, re.I)
    if cm:
        folder = r.folder(cm.group("f")) if cm.group("f") else None
        if isinstance(folder, Ask):
            return folder
        return _plan(_s("files.search_content", f"Search documents for “{cm.group('text')}”",
                        text=cm.group("text").strip("\"'“”"), folder=folder or ""))
    folder = None
    fm = re.search(r"\s+(?:in|inside|from|under) (?P<f>[A-Za-z]:[\\/].*|(?:my |the )?[\w .-]+?)$", what, re.I)
    if fm:
        candidate = r.folder(fm.group("f"))
        if isinstance(candidate, Ask) and not re.match(r"^[A-Za-z]:", fm.group("f")):
            candidate = None
        if isinstance(candidate, Ask):
            return candidate
        if candidate:
            folder = candidate
            what = what[:fm.start()].strip()
    words = re.sub(r"^(?:my|the|all|all my|any|a|an)\s+", "", what, flags=re.I).strip()
    # "my DUMBOO README": DUMBOO is a saved project -> search inside that project
    proj_found = None
    tokens = words.split()
    if len(tokens) >= 2:
        for token in tokens:
            if len(token) < 3 or token.lower() in EXT_WORDS or token.lower() in ("file", "files", "folder"):
                continue
            found = find_project(r.ctx, token)
            place = found.value if found is not None and not found.options and not found.problem else None
            if place is None:
                named = r.find_named(token, kind="folder", limit=2)
                place = named[0] if len(named) == 1 else None
            if place:
                proj_found = place
                words = " ".join(w for w in tokens if w.lower() != token.lower())
                break
    folder = folder or proj_found
    ext = None
    lw = words.lower()
    em = re.match(r"^(?:\w+ )?(?P<w>pdfs?|word documents?|excel files?|spreadsheets?|python files?|text files?|"
                  r"images|photos|mp3s?|videos?|zip files?|csv files?|powerpoints?|markdown files?)$", lw)
    if em:
        word = em.group("w").split()[0].rstrip("s") if em.group("w") not in ("images", "photos") else em.group("w")
        ext = EXT_WORDS.get(word) or EXT_WORDS.get(word + "s")
        if em.group("w") in ("images", "photos"):
            return _plan(_s("files.recent", "Find your images", kind="images", folder=folder or ""))
        words = ""
    words = re.sub(r"\b(?:files?|folders?)\b", "", words).strip()
    kind = "folder" if re.search(r"\bfolders?\b", what, re.I) else "any"
    return _plan(_s("files.search", "Search for " + (f"“{words}”" if words else (ext or "files"))
                    + (f" in {Path(folder).name}" if folder else ""), query=words, extension=ext or "",
                    folder=folder or "", kind=kind))


FILE_WORDS = re.compile(r"\b(?:files?|folders?|documents?|docs?|pdfs?|photos?|images?|pictures?|videos?|music|songs?|"
                        r"downloads|desktop|readme|projects?|these|those|them|screenshots?|spreadsheets?|"
                        r"duplicates?|recent|latest)\b|\.[a-z0-9]{2,5}\b|^[a-z]:[\\/]", re.I)


def looks_like_files(text: str) -> bool:
    return bool(FILE_WORDS.search(text or ""))


def _sources_for(text: str, r: Resolver) -> list[str] | Ask:
    t = text.strip()
    if re.match(r"^(?:these|those|them|they|the(?:se)? (?:files|ones)|the (?:results|files found))\b", t, re.I) or \
            re.match(r"^(?:these|those|the) \w+$", t, re.I):
        ext = _ext_from(t)
        paths = [p for p in r.state.last_paths if not ext or p.lower().endswith(ext)]
        if not paths:
            return Ask("Which files do you mean? Ask me to find them first (for example “find my PDFs in "
                       "Downloads”), then say “move these to …”.")
        return paths
    single = r.file(t)
    return single if isinstance(single, Ask) else [single]


def _h_move(m: re.Match, r: Resolver):
    if not looks_like_files(m.group("what")) and not r.find_named(m.group("what").strip(), "file", limit=1):
        return None  # "move on to the next topic" is conversation, not a file operation
    sources = _sources_for(m.group("what"), r)
    if isinstance(sources, Ask):
        return sources
    dest = r.folder(m.group("dest"))
    if isinstance(dest, Ask):
        return dest
    verb = m.group("verb").lower()
    if verb == "copy":
        if len(sources) != 1:
            return Ask("I can copy one file or folder at a time. Which one?")
        return _plan(_s("files.copy", f"Copy {Path(sources[0]).name} to {Path(dest).name}", source=sources[0],
                        destination=dest))
    return _plan(_s("files.move", f"Move {len(sources)} item(s) to {Path(dest).name}", sources=sources,
                    destination=dest))


def _h_rename(m: re.Match, r: Resolver):
    if not looks_like_files(m.group("what")) and not r.find_named(m.group("what").strip(), "file", limit=1):
        return None
    path = r.file(m.group("what")) if not re.search(r"\bfolder\b", m.group("what"), re.I) else r.folder(m.group("what"))
    if isinstance(path, Ask):
        return path
    return _plan(_s("files.rename", f"Rename {Path(path).name} to {m.group('new')}", path=path,
                    new_name=m.group("new").strip().strip("\"'“”")))


def _h_delete(m: re.Match, r: Resolver):
    if not looks_like_files(m.group("what")):
        return None  # "delete my favourite colour" is a memory command (Module 3)
    sources = _sources_for(m.group("what"), r)
    if isinstance(sources, Ask):
        return sources
    return _plan(_s("files.delete", f"Delete {len(sources)} item(s) (to the Recycle Bin)", paths=sources))


def _h_organize(m: re.Match, r: Resolver):
    folder = r.folder(m.group("folder") or "downloads")
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("files.organize", f"Plan how to organise {Path(folder).name}", folder=folder),
                 title=f"Organise {Path(folder).name}")


def _h_summarize_doc(m: re.Match, r: Resolver):
    what = m.group("what")
    ext = _ext_from(what)
    path = r.file(what, ext)
    if isinstance(path, Ask):
        return path
    return _plan(_s("files.summarize", f"Summarise {Path(path).name}", path=path))


def _h_open_folder(m: re.Match, r: Resolver):
    folder = r.folder(m.group("folder"))
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("files.open", f"Open {Path(folder).name}", path=folder))


def _h_undo(m: re.Match, r: Resolver):
    return _plan(_s("files.undo", "Undo the last file operation"))


# input --------------------------------------------------------------------------------
def _h_type(m: re.Match, r: Resolver):
    text = m.group("text").strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("“") and text.endswith("”")):
        text = text[1:-1]
    app = m.group("app") or r.state.app
    return _plan(_s("input.type_text", "Type the text" + (f" into {app}" if app else ""), text=text, app=app or ""))


def _h_save(m: re.Match, r: Resolver):
    return _plan(_s("input.press_keys", "Save the current document (Ctrl+S)", keys="ctrl+s",
                    app=(m.group("app") if "app" in m.groupdict() and m.group("app") else "")))


def _h_copy_selection(m: re.Match, r: Resolver):
    return _plan(_s("input.copy_selection", "Copy the selected text", app=m.group("app") or ""))


def _h_paste(m: re.Match, r: Resolver):
    return _plan(_s("input.paste", "Paste", app=m.group("app") or ""))


def _h_press(m: re.Match, r: Resolver):
    from automation.platform.windows import VK

    keys = m.group("keys").lower().replace(" plus ", "+").replace(" and ", "+").replace("control", "ctrl")
    keys = re.sub(r"\s*\+\s*", "+", keys).replace(" ", "+")
    if not all(k in VK for k in keys.split("+")):
        return None  # "press the save button" is a click
    return _plan(_s("input.press_keys", f"Press {keys.upper()}", keys=keys, app=m.group("app") or ""))


def _h_scroll(m: re.Match, r: Resolver):
    amount = m.group("amount")
    return _plan(_s("input.scroll", f"Scroll {m.group('dir')}", direction=m.group("dir").lower(),
                    amount=int(amount) if amount and amount.isdigit() else 5, app=m.group("app") or ""))


def _h_click(m: re.Match, r: Resolver):
    name = m.group("name").strip().strip("\"'“”")
    return _plan(_s("input.click", f"Click “{name}”", name=name, app=m.group("app") or "",
                    control_type=m.group("type") or ""))


def _h_clipboard(m: re.Match, r: Resolver):
    return _plan(_s("input.read_clipboard", "Read the clipboard"))


# screen -----------------------------------------------------------------------------------
def _h_explain_screen(m: re.Match, r: Resolver):
    return _plan(_s("screen.explain", "Explain what is on screen", question=m.string, app=""))


def _h_describe_screen(m: re.Match, r: Resolver):
    return _plan(_s("screen.describe", "Read the screen"))


def _h_find_control(m: re.Match, r: Resolver):
    return _plan(_s("screen.find", f"Find “{m.group('name')}” on screen", name=m.group("name"),
                    app=m.group("app") or ""))


def _h_screenshot(m: re.Match, r: Resolver):
    return _plan(_s("screen.save_screenshot", "Save a screenshot", app=m.group("app") or ""))


# system -----------------------------------------------------------------------------------
def _h_system(focus: str):
    def handler(m: re.Match, r: Resolver):
        label = {"overview": "Check the laptop's performance", "memory": "Check available memory",
                 "disk": "Check disk space", "battery": "Check the battery", "network": "Check the network",
                 "os": "Get system information", "cpu": "Check CPU load"}[focus]
        return _plan(_s("system.status", label, focus=focus))
    return handler


def _h_slow(m: re.Match, r: Resolver):
    return _plan(_s("system.diagnose", "Check why the laptop is slow"))


def _h_top(m: re.Match, r: Resolver):
    by = "cpu" if re.search(r"\b(cpu|processor)\b", m.group(0), re.I) else "memory"
    return _plan(_s("system.processes", f"List the programs using the most {by}", by=by))


def _h_end_process(m: re.Match, r: Resolver):
    return _plan(_s("system.end_process", f"End {m.group('name')}", name=m.group("name").strip()))


# web -----------------------------------------------------------------------------------------
def _h_search_web(m: re.Match, r: Resolver):
    query = m.group("query").strip().strip("\"'“”")
    if re.search(r"\b(?:in|inside|on) (?:my |the )?(?:computer|laptop|pc|files|folders?|downloads|documents|desktop)\b",
                 query, re.I):
        return _h_find(re.match(r"(?P<what>.+)", query), r)
    where = "browser" if m.group("where") and re.search(r"google|my browser|chrome|edge", m.group("where"), re.I) \
        else "dumboo"
    return _plan(_s("web.search", f"Search the web for “{query}”", query=query, where=where))


def _h_open_dumboo_browser(m: re.Match, r: Resolver):
    target = m.group("target")
    url = resolve_site(r.ctx, target)
    if not url:
        return Ask(f"“{target}” isn't a web address or a site I know.")
    return _plan(_s("web.open", f"Open {target} in DUMBOO's browser", url=url, browser="dumboo", label=target))


def _h_open_result(m: re.Match, r: Resolver):
    raw = (m.group("n") or m.group("n2") or "first").lower()
    n = ORDINALS.get(raw) or (int(raw) if raw.isdigit() else 1)
    return _plan(_s("web.open_result", f"Open search result {n}", number=n))


def _h_tabs(m: re.Match, r: Resolver):
    text = m.group(0).lower()
    if re.search(r"\b(list|show|which)\b", text):
        return _plan(_s("web.tabs", "List browser tabs", action="list"))
    if re.search(r"\b(new)\b", text):
        return _plan(_s("web.tabs", "Open a new tab", action="new"))
    n = re.search(r"\b(\d+|first|second|third|fourth|fifth)\b", text)
    index = (ORDINALS.get(n.group(1)) or int(n.group(1))) if n else None
    if re.search(r"\bclose\b", text):
        return _plan(_s("web.tabs", "Close a browser tab", action="close", index=index))
    return _plan(_s("web.tabs", f"Switch to tab {index or 1}", action="switch", index=index or 1))


def _h_read_page(m: re.Match, r: Resolver):
    return _plan(_s("web.read_page", "Summarise the open web page", question=m.string))


def _h_download(m: re.Match, r: Resolver):
    what = m.group("what") or ""
    url = re.search(r"https?://\S+", what)
    dest = m.group("dest")
    folder = r.folder(dest) if dest else ""
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("web.download", "Download " + (url.group(0)[:60] if url else "the open page's file"),
                    url=url.group(0) if url else "", folder=folder or ""))


def _h_fill(m: re.Match, r: Resolver):
    return _plan(_s("web.fill", f"Fill in “{m.group('field')}”", field=m.group("field").strip(),
                    value=m.group("value").strip().strip("\"'“”")))


# developer ------------------------------------------------------------------------------------
def _project_for(m: re.Match, r: Resolver, group: str = "project") -> str | Ask:
    raw = (m.group(group) if group in m.groupdict() else None) or ""
    raw = raw.strip()
    if not raw or norm(raw) in ("it", "its", "this", "the", "my", "project", "the project", "this project"):
        if r.state.project:
            return r.state.project
        return r.project(None)
    proj = r.project_from_text(raw if "project" in raw.lower() else raw + " project")
    if proj is None:
        return Ask(f"I don't have a project called “{raw}” saved.")
    return proj


def _h_dev(template: str, label: str):
    def handler(m: re.Match, r: Resolver):
        proj = _project_for(m, r)
        if isinstance(proj, Ask):
            return proj
        return _plan(_s("dev.run", f"{label} ({Path(proj).name})", project=proj, template=template))
    return handler


def _h_dev_cap(capability: str, label: str):
    def handler(m: re.Match, r: Resolver):
        proj = _project_for(m, r)
        if isinstance(proj, Ask):
            return proj
        return _plan(_s(capability, f"{label} ({Path(proj).name})", project=proj))
    return handler


def _h_prepare(m: re.Match, r: Resolver):
    proj = _project_for(m, r)
    if isinstance(proj, Ask):
        return proj
    editor = r.editor_for_project()
    if isinstance(editor, Ask):
        return editor
    name = Path(proj).name
    return Plan([_s("apps.open", f"Open {name} in {resolve_app(r.ctx, editor).name}", app=editor, target=proj),
                 _s("dev.inspect_project", f"Inspect {name}'s structure", project=proj),
                 _s("dev.read_setup", f"Read {name}'s setup instructions", project=proj),
                 _s("dev.check_dependencies", f"Check {name}'s dependencies", project=proj)],
                f"Prepare {name} for development")


def _h_explain_error(m: re.Match, r: Resolver):
    from automation.actions.dev import has_last_output

    text = (m.group("text") or m.group("text2") or "").strip()
    command_words = re.search(r"traceback|exception|stack|output|failure", m.group("kind") or "", re.I)
    if not text and not command_words and not has_last_output(r.ctx.user.id):
        # "explain the error" with no pasted text and no recent command output: it's on screen
        return _plan(_s("screen.explain", "Explain what is on screen", question=m.string, app=""))
    return _plan(_s("dev.explain_output", "Explain the error", text=text))


# memory ----------------------------------------------------------------------------------------
def _h_remember_project(m: re.Match, r: Resolver):
    name = re.sub(r"^(?:my|the|our)\s+", "", m.group("name").strip(), flags=re.I)
    where = m.group("where").strip().strip("\"'“”")
    if re.match(r"^(?:this|the current|the open|here|my current)(?: folder| directory| one)?$", where, re.I) or \
            where.lower() == "here":
        folder = r.this_folder()
    elif re.match(r"^[A-Za-z]:[\\/]", where):
        folder = where
    else:
        folder = r.folder(where)
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("memory.remember_project", f"Remember the {name} project folder", name=name, path=folder))


def _h_remember_folder(m: re.Match, r: Resolver):
    name = m.group("name").strip()
    where = m.group("where").strip()
    folder = r.this_folder() if re.match(r"^(?:this|here|the current)", where, re.I) else (
        where if re.match(r"^[A-Za-z]:[\\/]", where) else r.folder(where))
    if isinstance(folder, Ask):
        return folder
    return _plan(_s("memory.remember_folder", f"Remember the {name} folder", name=name, path=folder))


def _h_remember_app(m: re.Match, r: Resolver):
    purpose = m.group("purpose").lower()
    purpose = "coding" if purpose in ("coding", "code", "programming", "development", "dev") else purpose
    return _plan(_s("memory.remember_app", f"Remember {m.group('app')} for {purpose}", purpose=purpose,
                    app=m.group("app").strip()))


def _h_remember_site(m: re.Match, r: Resolver):
    name = re.sub(r"^(?:my|the|our)\s+", "", m.group("name").strip(), flags=re.I)
    return _plan(_s("memory.remember_website", f"Remember the {name} website", name=name, url=m.group("url")))


def _h_save_workflow(m: re.Match, r: Resolver):
    name = (m.group("name") or "").strip().strip("\"'“”")
    name = re.sub(r"^(?:my|the)\s+", "", name, flags=re.I) or "my workflow"
    return _plan(_s("memory.save_workflow", f"Save the workflow “{name}”", name=name))


def _h_permit(m: re.Match, r: Resolver):
    path = m.group("path").strip().strip("\"'“”")
    if not re.match(r"^[A-Za-z]:[\\/]", path):
        return Ask("Please give the folder's full path, for example C:\\final_deskbot_dumbo.")
    write = not re.search(r"\bread[- ]only\b", m.group(0), re.I)
    return _plan(_s("automation.permit_folder", f"Permit access to {path}", path=path, write=write))


def _h_approve_app(m: re.Match, r: Resolver):
    from automation.actions.apps import _start_menu_candidates

    query = m.group("name").strip()
    candidates = [c for c in _start_menu_candidates(query) if c["target"].lower().endswith(".exe")]
    if not candidates:
        return Ask(f"I couldn't find an installed program called “{query}” in the Start menu. The owner can add it "
                   "with its full .exe path on AUTOMATION → APPS & FOLDERS.")
    if len({c["target"].lower() for c in candidates}) > 1:
        return Ask("Which one? " + "; ".join(f"{c['name']} ({c['target']})" for c in candidates[:5]))
    c = candidates[0]
    return _plan(_s("apps.approve", f"Approve {c['name']}", name=c["name"], path=c["target"]))


# ------------------------------------------------------------------ the rules
APP = r"(?P<app>[\w .+&'-]{2,60}?)"
IN_APP = r"(?: (?:in|into|to|on) (?:the )?(?P<app>[\w .+&'-]{2,40}?))?"
RULES: list[tuple[re.Pattern, Handler]] = [(re.compile(p, re.I), h) for p, h in [
    # memory-aware preferences (before generic "remember", which Module 3 handles)
    (r"^remember (?:that )?(?P<name>(?:my |the |our )?[\w .'-]{1,40}?) project (?:is|lives|is located|is stored|is saved)"
     r" (?:in|at|under) (?P<where>.+)$", _h_remember_project),
    (r"^remember (?:that )?(?:my |the )?(?P<name>[\w .'-]{1,30}?) folder is (?:in |at )?(?P<where>.+)$",
     _h_remember_folder),
    (r"^remember (?:that )?my (?:usual|preferred|default) (?P<purpose>\w+) (?:application|app|editor|program|browser) "
     r"is (?P<app>.+)$", _h_remember_app),
    (r"^remember (?:that )?(?P<name>(?:my |the |our )?[\w .'-]{1,40}?) (?:website|site|web ?page|link) is "
     r"(?P<url>https?://\S+|[\w-]+(?:\.[\w-]+)+\S*)$", _h_remember_site),
    (r"^(?:remember|save) (?:this|that|the last) (?:workflow|task|routine|setup|process)(?: (?:as|for|called|named) "
     r"(?P<name>.+?))?(?: for next time)?$", _h_save_workflow),
    (r"^(?:allow|permit|give) (?:dumboo |you )?(?:access to |to access )?(?:the )?(?:folder )?(?P<path>[A-Za-z]:[\\/].*?)"
     r"(?: folder)?(?: (?:read[- ]only|for reading))?$", _h_permit),
    (r"^add (?P<path>[A-Za-z]:[\\/].+?) to (?:the )?permitted folders(?: read[- ]only)?$", _h_permit),
    (r"^approve (?P<name>.+?) as an? (?:application|app)$", _h_approve_app),
    # developer
    (r"^(?:prepare|set ?up|get) (?:my |the )?(?P<project>.*?)\s*(?:python |node )?(?:project|repo|code)?"
     r"(?: ready)? for (?:development|coding|work)$", _h_prepare),
    (r"^(?:run|execute) (?:the |all )?(?:unit )?tests?(?: (?:for|in|of|on) (?P<project>.+?))?$",
     _h_dev("tests", "Run the tests")),
    (r"^(?:run|execute) (?:its|the project'?s|my project'?s) tests$", _h_dev("tests", "Run the tests")),
    (r"^test (?P<project>.+?)$", _h_dev("tests", "Run the tests")),
    (r"^install (?:the |its |all )?(?:project |missing )?(?:dependencies|requirements|packages)(?: (?:for|of|in) "
     r"(?P<project>.+?))?$", _h_dev("install_requirements", "Install the requirements")),
    (r"^(?:create|make|set ?up) (?:a )?(?:virtual environment|venv)(?: (?:for|in) (?P<project>.+?))?$",
     _h_dev("create_venv", "Create a virtual environment")),
    (r"^(?:show |check )?git status(?: (?:for|of|in) (?P<project>.+?))?$", _h_dev("git_status", "Show git status")),
    (r"^(?:check|show|list|verify) (?:the |its |my )?(?:project )?dependencies(?: (?:for|of|in) (?P<project>.+?))?$",
     _h_dev_cap("dev.check_dependencies", "Check the dependencies")),
    (r"^(?:inspect|check|show|read) (?:the |its |my )?(?:project )?logs?(?: (?:for|of|in) (?P<project>.+?))?$",
     _h_dev_cap("dev.inspect_logs", "Inspect the logs")),
    (r"^(?:inspect|analy[sz]e|describe) (?:the |my )?(?:structure of )?(?P<project>.+?project)(?: structure)?$",
     _h_dev_cap("dev.inspect_project", "Inspect the project")),
    (r"^(?:create|write|generate|make) (?:a |the )?readme(?: file)?(?: (?:based on|from|for) (?:the |my )?"
     r"(?:actual )?(?P<project>.+?)(?: (?:project )?files)?)?$", _h_dev_cap("dev.make_readme", "Draft a README")),
    (r"^explain (?:this|the|that|these)? ?(?P<kind>traceback|error|errors|exception|stack ?trace|output|failure)s?"
     r"(?: output)?(?:\s*:\s*(?P<text>[\s\S]+)|\s*\n(?P<text2>[\s\S]+))?$", _h_explain_error),
    (r"^(?:create|write|make|generate) (?:a |an )?(?:new )?(?P<lang>python|javascript|js|java|c\+\+|c|html|typescript|go|"
     r"rust)? ?(?:file|script|program)(?: (?:called|named) (?P<name>[\w.-]+))? (?:that|which|to|for) (?P<what>.+?)"
     r"(?: in (?P<where>.+))?$", _h_write_code),
    # applications and windows
    (r"^(?:open|launch|start) " + APP + r" and (?:then )?(?:type|write)(?: this| the following)?\s*(?:paragraph|text)?"
     r"\s*:?\s*(?P<text>.+)$", _h_open_and_type),
    (r"^(?:open|launch|start) (?P<app>.+?) (?:and|with) (?P<target>(?:my |the |its )?.*?(?:project|repo|\.\w{2,5}))$",
     _h_open_with),
    (r"^(?P<verb>minimi[sz]e|maximi[sz]e|restore|unminimi[sz]e|hide) (?:all )?(?:the )?(?:windows of )?" + APP +
     r"(?: windows?)?$", _h_window),
    (r"^(?:switch|go|change|flip|jump) (?:back )?to (?:the )?(?P<app>.+?)(?: window| app)?$", _h_focus),
    (r"^(?:bring|show) (?:the )?(?P<app>.+?)(?: window)? (?:to the )?(?:front|forward|up)$", _h_focus),
    (r"^focus (?:on )?(?:the )?(?P<app>.+?)$", _h_focus),
    (r"^(?P<force>force[- ](?:close|quit)|kill|terminate) (?P<app>.+?)$", _h_close),
    (r"^(?:close|quit|exit)(?P<force>) (?:the )?(?P<app>.+?)(?: window| app)?$", _h_close),
    (r"^is (?P<app>.+?) (?:running|open|on)$", _h_status),
    (r"^(?:what|which) (?:windows|apps|applications|programs) are (?:open|running)$",
     lambda m, r: _plan(_s("apps.list_windows", "List open windows"))),
    # files
    (r"^(?:create|make|add) (?:a )?(?:new )?folder (?:named |called )?(?P<name>.+?)(?: (?:in|on|inside|under) "
     r"(?P<where>.+))?$", _h_create_folder),
    (r"^(?:create|make) (?:a )?(?:new )?(?:empty )?(?:text )?file (?:named |called )?(?P<name>[\w .()-]+?)"
     r"(?: (?:in|on) (?P<where>.+?))?(?: (?:with|containing) (?:the )?(?:text|content|contents)? ?(?P<content>.+))?$",
     _h_create_file),
    (r"^(?:organi[sz]e|clean ?up|sort|tidy(?: up)?) (?:my |the )?(?P<folder>downloads?|desktop|documents|pictures|"
     r"music|videos|[A-Za-z]:[\\/][^?]*?|[\w .-]+? folder)(?: folder)?(?: by (?:file )?type)?$", _h_organize),
    (r"^(?P<verb>move|copy) (?P<what>.+?) (?:to|into|in) (?P<dest>.+)$", _h_move),
    (r"^rename (?P<what>.+?) (?:to|as) (?P<new>.+)$", _h_rename),
    (r"^(?:delete|remove|trash|erase) (?:the )?(?:file |folder )?(?P<what>.+)$", _h_delete),
    (r"^(?:undo|reverse|revert) (?:the |that |my )?(?:last )?(?:file )?(?:operation|change|move|moves|"
     r"organi[sz]ation|organi[sz]ing|rename)$", _h_undo),
    (r"^(?:summari[sz]e|summary of|give me a summary of|what does) (?P<what>.+?\.(?:pdf|docx?|txt|md)|this (?:pdf|"
     r"document|file)|that (?:pdf|document|file)|the (?:pdf|document|file)|my \w+ (?:pdf|document))(?: say)?$",
     _h_summarize_doc),
    (r"^open (?:my |the )?(?P<folder>downloads?|documents|desktop|pictures|music|videos|[\w .-]+ folder)$",
     _h_open_folder),
    (r"^(?:find|where is|locate|show me) the (?P<name>.+?) (?:button|link|field|menu|option)" + IN_APP + "$",
     _h_find_control),
    (r"^search (?:my |the )?(?:computer|laptop|pc|files|folders?|documents|downloads|desktop) for (?P<what>.+?)$",
     _h_search_files),
    (r"^(?:find|locate|look for|where is|where's|show me) (?P<what>.+?)$", _h_find),
    # keyboard, clipboard, screen
    (r"^type(?: out)?(?: this| the following)?\s*:?\s*(?P<text>\"[^\"]+\"|“[^”]+”|.+?)" + IN_APP + "$", _h_type),
    (r"^write (?P<text>\"[^\"]+\"|“[^”]+”) (?:in|into) (?:the )?(?P<app>[\w .+&'-]{2,40}?)$", _h_type),
    (r"^save (?:the |this |my )?(?:current )?(?:document|file|work|changes|note)?" + IN_APP + "$", _h_save),
    (r"^copy (?:the )?(?:selected|highlighted) (?:text|part)" + IN_APP + "$", _h_copy_selection),
    (r"^copy (?:that|this|the selection|selection)" + IN_APP + "$", _h_copy_selection),
    (r"^paste(?: it| that| the text)?" + IN_APP + "$", _h_paste),
    (r"^(?:press|hit) (?P<keys>[\w +-]{1,30}?)" + IN_APP + "$", _h_press),
    (r"^scroll (?P<dir>up|down)(?: (?P<amount>\d+|a bit|a lot))?" + IN_APP + "$", _h_scroll),
    (r"^(?:click|tap|double[- ]click)(?: on)? (?:the )?(?P<name>.+?)(?: (?P<type>button|link|tab|menu|checkbox))?" +
     r"(?: (?:in|on) (?P<app>[\w .+&'-]{2,40}?))?$", _h_click),
    (r"^(?:press|hit|push) the (?P<name>.+?) (?P<type>button|link|tab|menu|checkbox)" +
     r"(?: (?:in|on) (?P<app>[\w .+&'-]{2,40}?))?$", _h_click),
    (r"^what(?:'s| is) (?:on|in) (?:my |the )?clipboard$", _h_clipboard),
    (r"^(?:explain|what(?:'s| is| does)) (?:the |this |that )?(?:error|problem|issue|warning|message|dialog)"
     r"(?: (?:on|in) (?:my |the )?screen)?(?: mean| say)?$", _h_explain_screen),
    (r"^(?:look at|check|read) (?:the |this |my )?(?:error|screen|problem)(?: on (?:my |the )?screen)?"
     r"(?: and (?:suggest|explain|tell me).*)?$", _h_explain_screen),
    (r"^help me (?:configure|set ?up|use|fix|understand) (?:this|the current|the open) (?:application|app|program|"
     r"window|screen|setting|settings)$", _h_explain_screen),
    (r"^what(?:'s| is) (?:on )?(?:my |the )?screen$|^read (?:my |the )?screen(?: for me)?$", _h_describe_screen),
    (r"^(?:take|grab|capture|save) (?:a )?screen ?shot(?: of (?P<app>.+?))?$", _h_screenshot),
    # system
    (r"^(?:check|show|how is|how's|test)(?: my| the)? (?:laptop|computer|pc|system|machine)(?:'s)?(?: performance| "
     r"status| health)?(?: doing)?$|^system (?:status|check)$|^(?:check|show) (?:my )?(?:laptop|system) performance$",
     _h_system("overview")),
    (r"^how much (?:ram|memory)(?: is)?(?: (?:available|free|left|used|do i have))?$|^(?:check|show) (?:my )?"
     r"(?:ram|memory)(?: usage)?$|^(?:ram|memory) usage$", _h_system("memory")),
    (r"^(?:check|show|how much)(?: my| the)? (?:disk|storage|drive|hard drive|ssd)(?: space)?(?: is)?"
     r"(?: (?:left|free|available|do i have))?$", _h_system("disk")),
    (r"^(?:check|show|what(?:'s| is))?(?: my| the)? battery(?: level| status| percentage)?(?: at)?$",
     _h_system("battery")),
    (r"^(?:am i|are we|is (?:the |my )?(?:laptop|computer|pc)) (?:connected|online)(?: to the internet)?$|"
     r"^(?:check|show)(?: my| the)? (?:network|internet|wifi|wi-fi)(?: connection| status)?$", _h_system("network")),
    (r"^(?:what(?:'s| is) my|show(?: me)? (?:my )?|check (?:my )?)(?:os|operating system|system info(?:rmation)?|"
     r"system specs|specs|computer specs)$", _h_system("os")),
    (r"^(?:check|show|what(?:'s| is))(?: my| the)? cpu(?: usage| load)?$", _h_system("cpu")),
    (r"^why is (?:my |the )?(?:laptop|computer|pc|system|machine) (?:so )?(?:slow|lagging|laggy|hanging|sluggish)"
     r"(?: today| right now)?$|^my (?:laptop|computer|pc) is (?:slow|lagging)$", _h_slow),
    (r"^(?:which|what) (?:apps|applications|programs|processes) (?:are )?(?:using|consuming|taking|eating|hogging) "
     r"(?:the )?(?:most|all the|so much) (?:memory|ram|cpu|processor)(?: right now)?$|^(?:show|list) (?:the )?top "
     r"(?:processes|programs)(?: by (?:memory|ram|cpu))?$", _h_top),
    (r"^(?:end|stop) (?:the )?(?:process|program) (?P<name>[\w .-]+)$", _h_end_process),
    # web
    (r"^(?:search|google|look up|bing)(?: (?P<where>the web|online|google|on google|in my browser|in chrome|"
     r"in edge|the internet))?(?: for)? (?P<query>.+)$", _h_search_web),
    (r"^open (?:the )?(?P<n>first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|top|\d+)(?:st|nd|rd|th)? "
     r"(?:search )?(?:result|link)$|^open (?:search )?result (?:number )?(?P<n2>\d+)$",
     _h_open_result),
    (r"^(?:list|show|which) (?:my |the |all )?(?:browser )?tabs(?: are open)?$|^(?:switch to|go to|close) "
     r"(?:the )?(?:\w+ )?tab(?: \d+)?$|^(?:open )?(?:a )?new tab$", _h_tabs),
    (r"^(?:summari[sz]e|read|what does) (?:this|the|that) (?:web ?)?page(?: say)?$", _h_read_page),
    (r"^download (?P<what>this (?:pdf|file|document)|the (?:pdf|file|document)|it|https?://\S+)"
     r"(?: (?:in|into|to) (?:my |the )?(?P<dest>.+?)(?: folder)?)?$", _h_download),
    (r"^fill (?:in )?(?:the )?(?P<field>[\w .-]{1,40}?)(?: field| box)? with (?P<value>.+)$", _h_fill),
    # generic open (last: everything above is more specific)
    (r"^(?:open|go to|visit) (?P<target>\S+?) in (?:dumboo'?s|the dumboo|your|the automation) browser$",
     _h_open_dumboo_browser),
    (r"^open (?P<target>.+?) (?:with|in|using) (?P<app>[\w .+&'-]{2,40}?)$", _h_open_in),
    (r"^(?P<verb>open|launch|start) (?P<target>.+)$", _h_open),
    (r"^(?:go to|visit|navigate to|browse to) (?P<target>.+)$", _h_open),
]]
SPLIT = re.compile(r",?\s+(?:and then|then|after that|and also|and)\s+|;\s*|,\s+(?=(?:open|close|minimi|maximi|switch|"
                   r"run|create|find|search|type|save|copy|move|organi|check|launch|start)\w*\b)", re.I)
HELP = re.compile(r"^(?:what can you (?:automate|do on my (?:laptop|computer|pc))|automation help|help with automation|"
                  r"what (?:apps|applications) can you open)$", re.I)
HISTORY = re.compile(r"^(?:show|list|what(?:'s| is| are)) (?:me )?(?:my )?(?:automation|task)? ?(?:history|tasks you "
                     r"(?:did|ran|ran today)|recent tasks|last tasks?)$|^what did you do(?: today| for me)?$", re.I)


def parse_one(text: str, r: Resolver) -> Plan | Ask | Info | None:
    for pattern, handler in RULES:
        m = pattern.match(text)
        if not m:
            continue
        try:
            result = handler(m, r)
        except SafetyError as exc:
            return Ask(str(exc))
        if result is not None:
            return result
    return None


def parse(text: str, ctx: ExecContext, last_paths: list[str] | None = None) -> Plan | Ask | Info | None:
    """A plan for ``text``, a question, a local answer, or ``None`` (not an automation request)."""
    from automation.actions.common import resolve_app
    from automation.actions.web import resolve_site
    from automation.slang import normalize

    t = clean(normalize(clean(text), lambda name: bool(name) and bool(resolve_site(ctx, name) or resolve_app(ctx, name))))
    if not t or len(t) > 4000:
        return None
    r = Resolver(ctx, last_paths or [])
    if HELP.match(t):
        apps = ", ".join(sorted(a.name for a in ctx.services.config.applications.values() if a.resolve() or a.uri))
        return Info("I can control your laptop: open/switch/minimise/close apps (" + apps + "), find, create, "
                    "rename, move and organise files in your permitted folders, type and press shortcuts, read and "
                    "explain what's on screen, check CPU/RAM/disk/battery/network, open websites, search the web, "
                    "download files, run your project's tests and installs, and remember your projects, apps, "
                    "websites and workflows. Risky actions always ask first, and “stop everything” stops it all.")
    whole = parse_one(t, r)
    if not _splittable(t) or (isinstance(whole, (Plan, Info)) and not (isinstance(whole, Plan) and _is_generic(whole))):
        return whole
    clauses = [c.strip() for c in SPLIT.split(t) if c and c.strip()]
    if len(clauses) < 2 or len(clauses) > 6:
        return whole
    r2 = Resolver(ctx, last_paths or [])
    steps: list[Step] = []
    for clause in clauses:
        part = parse_one(clean(clause), r2)
        if part is None:
            return whole
        if not isinstance(part, Plan):
            return part
        steps.extend(part.steps)
    return Plan(steps, t[:80])


def _splittable(text: str) -> bool:
    return bool(SPLIT.search(text))


def _is_generic(plan: Plan) -> bool:
    """A whole-sentence match that probably swallowed several commands ("open X and switch to Y")."""
    s = plan.steps[0]
    return len(plan.steps) == 1 and any(isinstance(v, str) and SPLIT.search(v) for v in s.args.values())


def wants_history(text: str) -> bool:
    return bool(HISTORY.match(clean(text)))


def looks_like_automation(text: str) -> bool:
    """Should an unmatched request go to the AI planner? Needs an action verb *and* a laptop-related object."""
    t = clean(text).lower()
    if t.endswith("?") or re.match(r"^(?:what|who|why|how|when|where|is|are|do|does|can|could|should|tell me|explain)\b",
                                   t):
        return False
    verb = re.match(r"^(?:open|launch|start|close|quit|minimi[sz]e|maximi[sz]e|switch|create|make|rename|move|copy|"
                    r"delete|organi[sz]e|clean|sort|find|search|locate|type|save|run|execute|install|set ?up|prepare|"
                    r"configure|fill|click|press|scroll|download|check|show|list|backup|back up|zip|archive|"
                    r"continue|resume|get|go|bring|put|arrange|play)\b", t)
    noun = re.search(r"\b(?:files?|folders?|apps?|applications?|programs?|windows?|browser|tabs?|websites?|site|"
                     r"project|repo|desktop|downloads|documents|screen|laptop|computer|pc|notepad|chrome|edge|vs ?code|"
                     r"editor|terminal|tests?|readme|pdfs?|docs?|dependencies|venv|environment|workflow|task|clipboard|"
                     r"screenshot|disk|ram|cpu|battery|youtube|github|google)\b", t)
    return bool(verb and noun)
