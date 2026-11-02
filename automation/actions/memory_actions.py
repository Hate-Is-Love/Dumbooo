"""Feature 8 (writing side): remembering automation preferences the user explicitly asks for.

Each value is checked before it is stored (the folder exists and is permitted,
the app is approved, the URL is valid) and read back afterwards. Only what the
user asked to remember is written; execution logs never become memories.
"""

from __future__ import annotations

from pathlib import Path

from automation.actions.common import check_path, require_app
from automation.models import ActionResult, Risk
from automation.personalization import project_key, remember
from automation.registry import ArgumentError, Capability, CapabilityRegistry, ExecContext, Param
from automation.safety import check_url


def _readback(ctx: ExecContext, key: str, value: str) -> bool:
    return any(m["memory_value"] == value and m["memory_key"].lower() == key.lower()
               for m in ctx.services.memory.list(ctx.session))


def _folder_precheck(ctx: ExecContext, args: dict) -> None:
    path = check_path(ctx, args["path"], must_exist=True)
    if not path.is_dir():
        raise ArgumentError(f"{path} is not a folder.")
    args["path"] = str(path)


def _remember_project(ctx: ExecContext, args: dict) -> ActionResult:
    key = project_key(args["name"])
    remember(ctx, key, args["path"], "project")
    ok = _readback(ctx, key, args["path"])
    return ActionResult(ok, f"I'll remember that your {args['name']} project is in {args['path']}.", verified=ok,
                        verification="saved in your memory and read back" if ok else "could not read it back",
                        data={"path": args["path"]})


def _remember_folder(ctx: ExecContext, args: dict) -> ActionResult:
    key = f"{' '.join(args['name'].lower().split())} folder"
    remember(ctx, key, args["path"], "fact")
    ok = _readback(ctx, key, args["path"])
    return ActionResult(ok, f"I'll remember that your {args['name']} folder is {args['path']}.", verified=ok,
                        verification="saved in your memory and read back" if ok else "could not read it back",
                        data={"path": args["path"]})


def _remember_app(ctx: ExecContext, args: dict) -> ActionResult:
    app = require_app(ctx, args["app"])
    key = f"usual {' '.join(args['purpose'].lower().split())} application"
    remember(ctx, key, app.name, "preference")
    ok = _readback(ctx, key, app.name)
    return ActionResult(ok, f"Got it: your usual {args['purpose']} application is {app.name}.", verified=ok,
                        verification="saved in your memory and read back" if ok else "could not read it back")


def _website_precheck(ctx: ExecContext, args: dict) -> None:
    args["url"] = check_url(args["url"], ctx.services.config.browser.get("blocked_domains"))


def _remember_website(ctx: ExecContext, args: dict) -> ActionResult:
    key = f"{' '.join(args['name'].lower().split())} website"
    remember(ctx, key, args["url"], "fact")
    ok = _readback(ctx, key, args["url"])
    return ActionResult(ok, f"I'll remember {args['url']} as your {args['name']} website.", verified=ok,
                        verification="saved in your memory and read back" if ok else "could not read it back")


def _workflow_precheck(ctx: ExecContext, args: dict) -> None:
    if not args.get("task_id"):
        last = ctx.services.history.last_completed(ctx.session)
        if last is None:
            raise LookupError("There's no completed task to save as a workflow yet.")
        args["task_id"] = last["id"]
    ctx.services.history.get(ctx.session, int(args["task_id"]))  # must be the user's own task


def _save_workflow(ctx: ExecContext, args: dict) -> ActionResult:
    record = ctx.services.history.get(ctx.session, int(args["task_id"]))
    name = " ".join(args["name"].lower().split())
    steps = [s for s in record["steps"] if s.get("status") == "completed"]
    if not steps:
        return ActionResult.fail("That task has no completed steps to save.")
    private = [s for s in steps if any(isinstance(v, str) and v.endswith("not stored]") for v in s["args"].values())]
    ctx.services.history.save_workflow(ctx.session, record["id"], name)
    description = f"task #{record['id']}: " + " → ".join(s["description"] for s in steps)[:440]
    remember(ctx, f"workflow {name}", description, "instruction")
    ok = ctx.services.history.workflow(ctx.session, name) is not None and _readback(ctx, f"workflow {name}",
                                                                                   description)
    note = (f" ({len(private)} step(s) used private text that isn't stored; I'll ask for it when you run the "
            "workflow)" if private else "")
    return ActionResult(ok, f"Saved “{name}” as a workflow with {len(steps)} step(s){note}. Say “run my {name} "
                            "workflow” to use it again.", verified=ok,
                        verification="workflow saved and read back" if ok else "could not read it back")


def _permit_precheck(ctx: ExecContext, args: dict) -> None:
    path = Path(args["path"])
    if not path.is_absolute() or not path.is_dir():
        raise ArgumentError(f"{args['path']} is not an existing folder.")
    resolved = path.resolve()
    if ctx.services.config.is_protected(resolved):
        raise ArgumentError("That location is protected (system, programs, app data or DUMBOO's own data).")
    if resolved.anchor == str(resolved):
        raise ArgumentError("A whole drive cannot be a permitted folder.")
    args["path"] = str(resolved)


def _permit(ctx: ExecContext, args: dict) -> ActionResult:
    entries = ctx.services.settings.permitted_entries()
    existing = [e for e in entries if Path(e["resolved"]) == Path(args["path"])]
    if existing and existing[0]["write"] == args["write"]:
        return ActionResult.done(f"{args['path']} is already permitted.", "already in the configuration")
    entries = [e for e in entries if Path(e["resolved"]) != Path(args["path"])]
    entries.append({"path": args["path"], "write": args["write"]})
    ctx.services.settings.set_permitted_directories([{"path": e["path"], "write": e["write"]} for e in entries])
    ok = ctx.services.guard.readable(args["path"])
    return ActionResult(ok, f"DUMBOO may now {'read and change files in' if args['write'] else 'read'} {args['path']}.",
                        verified=ok, verification="saved and checked against the new configuration")


def register(reg: CapabilityRegistry) -> None:
    reg.add(Capability("memory.remember_project", "Remember where one of the user's projects is",
                       {"name": Param(str, max_len=60), "path": Param(str, max_len=1000)}, _remember_project,
                       "memory.use", Risk.LOW, precheck=_folder_precheck,
                       describe=lambda a: f"Remember the {a['name']} project folder"))
    reg.add(Capability("memory.remember_folder", "Remember a named folder (e.g. 'college folder')",
                       {"name": Param(str, max_len=60), "path": Param(str, max_len=1000)}, _remember_folder,
                       "memory.use", Risk.LOW, precheck=_folder_precheck,
                       describe=lambda a: f"Remember the {a['name']} folder"))
    reg.add(Capability("memory.remember_app", "Remember the user's usual application for a purpose",
                       {"purpose": Param(str, max_len=40), "app": Param(str, max_len=80)}, _remember_app,
                       "memory.use", Risk.LOW, precheck=lambda c, a: require_app(c, a["app"]) and None,
                       describe=lambda a: f"Remember {a['app']} as the usual {a['purpose']} application"))
    reg.add(Capability("memory.remember_website", "Remember a website for later",
                       {"name": Param(str, max_len=60), "url": Param(str, max_len=2048)}, _remember_website,
                       "memory.use", Risk.LOW, precheck=_website_precheck,
                       describe=lambda a: f"Remember the {a['name']} website"))
    reg.add(Capability("memory.save_workflow", "Save a completed task as a named, repeatable workflow",
                       {"name": Param(str, max_len=60), "task_id": Param(int, required=False, minimum=1)},
                       _save_workflow, "memory.use", Risk.LOW, precheck=_workflow_precheck, planner=False,
                       describe=lambda a: f"Save the workflow “{a['name']}”"))
    reg.add(Capability("automation.permit_folder", "Allow DUMBOO to access a folder (owner only)",
                       {"path": Param(str, max_len=1000), "write": Param(bool, required=False, default=True)},
                       _permit, "automation.admin", Risk.HIGH, min_risk=Risk.HIGH, precheck=_permit_precheck,
                       planner=False, describe=lambda a: f"Permit DUMBOO to {'change' if a.get('write') else 'read'} "
                                                         f"files in {a['path']}",
                       confirm_details=lambda c, a: f"DUMBOO will be able to {'read, create, move and change' if a['write'] else 'read'} "
                                                    f"files in:\n{a['path']}"))
