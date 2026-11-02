"""GuardianService: runs the checks for a signed-in user, records results, handles safe actions.

Permissions: reading needs ``security.view``; changing anything needs ``security.manage`` (owner
by default) plus an explicit confirmation of that specific action. Only one scan runs at a time;
every scan has a timeout and can be cancelled ("stop the current scan" or the STOP button).

Action levels (``ACTION_LEVELS``):
* 0 read-only (all checks), done directly;
* 1 low risk (save a report, add a folder to integrity monitoring), done directly and audited;
* 2 needs confirmation: disabling a *per-user* startup entry (Run key value or Startup-folder item),
  with a backup kept for **restore**. Machine-wide entries, tasks and services need administrator
  rights: DUMBOO explains how to do it instead of trying;
* 3 high impact (ending processes, deleting files, removing services): not done by the Guardian.
  Ending a process is Automation's "end process …", which has its own confirmation.
The AI never runs commands here: actions are fixed functions with validated targets.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from guardian import integrity as integ
from guardian.config import GuardianConfig
from guardian.files import SignatureChecker, command_observations, path_observations
from guardian.intel import provider_for, why_not
from guardian.investigate import investigate_entry, investigate_file
from guardian.models import (FAIL, NOT_SUPPORTED, PASS, UNKNOWN, WARNING, CheckResult, GuardianError, Indicator,
                             Investigation, StartupEntry)
from guardian.processes import find_processes, list_processes, network_overview, process_details
from guardian.runner import PowerShell
from guardian.updates import AdvisoryCache, kev_matches, winget_upgrades
from guardian.urlcheck import analyse_message, analyse_url, judge, trace_redirects

log = logging.getLogger(__name__)

ACTION_LEVELS = {"save_report": 1, "monitor_folder": 1, "disable_startup": 2, "restore_startup": 2}
HKCU_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


class Busy(GuardianError):
    def __init__(self) -> None:
        super().__init__("A security scan is already running. Say “stop the current scan” to cancel it.", "busy")


class GuardianService:
    name = "guardian"

    def __init__(self, db, auth, config: Callable[[], GuardianConfig], data_dir: Path, platform=None,
                 powershell: PowerShell | None = None, intel_client_factory=None, advisories: AdvisoryCache | None = None,
                 winget_run=None, clock: Callable[[], datetime] = datetime.now) -> None:
        from guardian.platforms import current

        self._db, self._auth, self._config = db, auth, config
        self._data = Path(data_dir)
        self._ps = powershell or PowerShell()
        self.platform = platform or current()
        self._signatures = SignatureChecker(self._ps)
        self._intel_factory = intel_client_factory
        self._advisories = advisories or AdvisoryCache()
        self._winget = winget_run
        self._clock = clock
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._running: str | None = None
        self._startup: dict[int, list[StartupEntry]] = {}       # per user: last startup listing
        self._last: dict[int, Investigation] = {}               # per user: last investigation (for "save report")

    # -- plumbing --------------------------------------------------------------------------------------------
    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _user(self, session, permission: str = "security.view"):
        if not self._config().monitor_enabled:
            raise GuardianError("The security guardian is switched off (SETTINGS → Security).", "disabled")
        return self._auth.require(session, permission)

    @property
    def busy(self) -> str | None:
        return self._running

    def cancel(self) -> bool:
        if self._running:
            self._cancel.set()
            return True
        return False

    def _job(self, kind: str):
        service = self

        class Job:
            def __enter__(self_inner):
                with service._lock:
                    if service._running:
                        raise Busy()
                    service._running = kind
                    service._cancel.clear()
                self_inner.deadline = time.monotonic() + service._config().scan_timeout_s
                return self_inner

            def __exit__(self_inner, *exc):
                with service._lock:
                    service._running = None
                return False

            def check(self_inner) -> None:
                if service._cancel.is_set():
                    raise GuardianError("The scan was cancelled.", "cancelled")
                if time.monotonic() > self_inner.deadline:
                    raise GuardianError("The scan took too long and was stopped.", "timeout")

        return Job()

    def _record_scan(self, user_id: int, kind: str, status: str, summary: str, result: Any, error: str = "",
                     started: str | None = None) -> int:
        return self._db.execute(
            "INSERT INTO security_scans (user_id, kind, status, summary, result_json, error, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, kind, status, summary[:500], json.dumps(result, default=str)[:400_000], error[:500],
             started or self._now(), self._now()))

    def _record_finding(self, user_id: int, scan_id: int, category: str, subject: str, status: str,
                        inv: Investigation | None = None, evidence: Any = None, source: str = "") -> None:
        self._db.execute(
            "INSERT INTO security_findings (scan_id, user_id, category, subject, status, assessment, confidence, "
            "evidence_json, source, limitations, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, user_id, category, subject[:200], status, inv.assessment if inv else "",
             inv.confidence if inv else "", json.dumps(inv.to_dict() if inv else evidence, default=str)[:200_000],
             source[:200], "\n".join(inv.limitations) if inv else "", self._now()))

    def _intel_lookup(self):
        cfg = self._config()
        provider = provider_for(cfg, self._intel_factory)
        return (provider.lookup if provider else None), why_not(cfg)

    # -- 1. overview ------------------------------------------------------------------------------------------
    def overview(self, session) -> dict:
        user = self._user(session)
        started = self._now()
        with self._job("security check") as job:
            try:
                checks = self.platform.overview()
                job.check()
                startup = self._list_startup(user.id, job)
                job.check()
                flagged = [e for e in startup if e.flags]
                checks.append(CheckResult("startup", "Startup programs", WARNING if flagged else PASS,
                                          f"{len(startup)} entries; {len(flagged)} with something to look at"
                                          if flagged else f"{len(startup)} entries; nothing unusual at a glance",
                                          {"flagged": [{"name": e.name, "flags": e.flags} for e in flagged[:10]]},
                                          "registry, Startup folders, Task Scheduler, services"))
                updates = self._updates_summary(job)
                if updates is not None:
                    checks.append(updates)
            except GuardianError as exc:
                self._record_scan(user.id, "overview", "cancelled" if exc.kind == "cancelled" else "failed", "", {},
                                  exc.user_message, started)
                raise
        counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARNING, FAIL, UNKNOWN, NOT_SUPPORTED)}
        result = {"checks": [c.to_dict() for c in checks], "counts": counts, "time": self._now(),
                  "flagged_startup": [e.name for e in flagged]}
        scan_id = self._record_scan(user.id, "overview", "completed", _counts_text(counts), result, started=started)
        for c in checks:
            if c.status in (WARNING, FAIL):
                self._record_finding(user.id, scan_id, "overview", c.title, c.status, evidence=c.to_dict(),
                                     source=c.source)
        return result

    def _updates_summary(self, job) -> CheckResult | None:
        try:
            ups = winget_upgrades(self._winget)
        except GuardianError as exc:
            return CheckResult("software_updates", "App updates", UNKNOWN, exc.user_message, source="winget")
        job.check()
        return CheckResult("software_updates", "App updates", WARNING if ups else PASS,
                           f"{len(ups)} app(s) have newer versions" if ups else "All apps winget knows are up to date",
                           {"updates": ups[:30]}, "winget upgrade (an update is not proof of a vulnerability)",
                           "Say “check for security updates” for details." if ups else "")

    # -- 2. startup -------------------------------------------------------------------------------------------
    def _list_startup(self, user_id: int, job=None) -> list[StartupEntry]:
        entries = self.platform.startup_entries(self._config().scan_timeout_s)
        sigs = self._signatures.check([e.executable for e in entries if e.executable])
        for e in entries:
            e.signature = sigs.get(e.executable) if e.executable else None
            flags = [t for t, w in path_observations(e.executable) if w >= 2]
            flags += [t for t, w in command_observations(e.command) if w >= 2]
            if e.signature is not None and e.signature.status in ("NotSigned", "HashMismatch", "NotTrusted"):
                flags.append({"NotSigned": "Not digitally signed", "HashMismatch": "Broken signature",
                              "NotTrusted": "Untrusted signature"}[e.signature.status])
            if e.executable and e.signature is not None and e.signature.status == "UnknownError" and \
                    not Path(e.executable).exists():
                flags.append("Points to a file that doesn't exist")
            e.flags = flags
            if job is not None:
                job.check()
        self._startup[user_id] = entries
        return entries

    def startup(self, session) -> list[StartupEntry]:
        user = self._user(session)
        with self._job("startup listing") as job:
            return self._list_startup(user.id, job)

    def find_entry(self, session, query: str | None) -> StartupEntry | list[StartupEntry] | None:
        """One entry by name/id, or the flagged ones when no name is given."""
        user = self._user(session)
        entries = self._startup.get(user.id) or self.startup(session)
        if not query:
            flagged = [e for e in entries if e.flags]
            return flagged[0] if len(flagged) == 1 else flagged
        q = query.strip().lower()
        exact = [e for e in entries if e.id == q or e.name.lower() == q]
        if exact:
            return exact[0]
        partial = [e for e in entries if q in e.name.lower() or (e.executable and q in Path(e.executable).name.lower())]
        return partial[0] if len(partial) == 1 else (partial or None)

    def investigate(self, session, entry: StartupEntry) -> Investigation:
        user = self._user(session)
        lookup, reason = self._intel_lookup()
        with self._job("startup investigation") as job:
            sig = self._signatures.check([entry.executable]).get(entry.executable) if entry.executable else None
            job.check()
            inv = investigate_entry(entry, sig, lookup, reason, self._config().max_hash_mb, self._cancel)
        scan_id = self._record_scan(user.id, "investigation", "completed", f"{entry.name}: {inv.assessment}",
                                    inv.to_dict())
        self._record_finding(user.id, scan_id, "startup", entry.name, inv.assessment, inv, source="local checks")
        self._last[user.id] = inv
        return inv

    # -- 3. processes and network --------------------------------------------------------------------------------
    def processes(self, session, sort: str = "memory") -> list[dict]:
        self._user(session)
        return list_processes(40, sort)

    def inspect_process(self, session, name_or_pid: str) -> Investigation | list[dict] | None:
        user = self._user(session)
        procs = find_processes(name_or_pid)  # by PID, or every process with that program name
        if not procs:
            return None
        proc = procs[0]  # instances of one program share its file; the count is reported
        lookup, reason = self._intel_lookup()
        with self._job("process inspection") as job:
            facts, indicators, limitations = process_details(proc)
            job.check()
            exe = facts.get("Executable")
            exe = exe if exe and exe not in ("access denied", "unavailable") else None
            sig = self._signatures.check([exe]).get(exe) if exe else None
            if len(procs) > 1:
                facts["Instances"] = len(procs)
            inv = investigate_file(facts.get("Name", name_or_pid), "process", exe, facts.get("Command", ""), sig,
                                   lookup, {k: v for k, v in facts.items() if k not in ("Executable", "Command")},
                                   indicators, self._config().max_hash_mb, self._cancel, why_no_intel=reason)
            inv.limitations = limitations + inv.limitations
        scan_id = self._record_scan(user.id, "process", "completed", f"{inv.subject}: {inv.assessment}", inv.to_dict())
        self._record_finding(user.id, scan_id, "process", inv.subject, inv.assessment, inv, source="psutil")
        self._last[user.id] = inv
        return inv

    def signature_of(self, session, target: str):
        """Signature of a startup entry, process or file path. Returns (subject, path, Signature) or None."""
        self._user(session)
        path = None
        if Path(target).exists():
            path = target
        else:
            entry = self.find_entry(session, target)
            if isinstance(entry, StartupEntry):
                path = entry.executable
            else:
                procs = find_processes(target)
                if procs:
                    try:
                        path = procs[0].exe()
                    except Exception:
                        path = None
        if not path:
            return None
        return target, path, self._signatures.check([path])[path]

    def network(self, session) -> dict:
        user = self._user(session)
        result = network_overview()
        self._record_scan(user.id, "network", "completed",
                          f"{result['total_connections']} connections, {len(result['exposed'])} open ports",
                          {k: result[k] for k in ("total_connections", "programs", "exposed", "notes")})
        return result

    # -- 4. updates and advisories --------------------------------------------------------------------------------
    def updates(self, session) -> dict:
        user = self._user(session)
        cfg = self._config()
        out: dict = {"updates": [], "advisories": [], "notes": [], "checked": self._now()}
        with self._job("update check") as job:
            try:
                out["updates"] = winget_upgrades(self._winget)
            except GuardianError as exc:
                out["notes"].append(f"Available updates could not be listed: {exc.user_message}")
            job.check()
            if cfg.advisory_feeds and out["updates"]:
                try:
                    kev = self._advisories.get()
                    out["advisories"] = kev_matches(out["updates"], kev)
                    out["catalogue"] = {"name": "CISA Known Exploited Vulnerabilities",
                                        "version": kev.get("catalogVersion"), "fetched": kev.get("_fetched")}
                except GuardianError as exc:
                    out["notes"].append(f"Security advisories could not be checked: {exc.user_message}")
            elif not cfg.advisory_feeds:
                out["notes"].append("Advisory feeds are switched off (SECURITY → SETTINGS).")
        self._record_scan(user.id, "updates", "completed",
                          f"{len(out['updates'])} updates, {len(out['advisories'])} advisories to review", out)
        return out

    # -- 5. URLs and messages -------------------------------------------------------------------------------------
    def check_url(self, session, url: str, trace: bool = False) -> dict:
        user = self._user(session)
        indicators, facts = analyse_url(url)
        hops = []
        if trace:
            try:
                hops = trace_redirects(url)
            except GuardianError as exc:
                facts["Redirect check"] = exc.user_message
            if len(hops) > 1:
                final_host = hops[-1]["url"]
                facts["Ends at"] = final_host
                end_ind, _ = analyse_url(final_host)
                indicators += [Indicator("After redirects: " + i.text, i.weight) for i in end_ind if i.weight > 0]
        verdict = judge(indicators)
        result = {"url": url, "facts": facts, "indicators": [asdict(i) for i in indicators], "assessment": verdict,
                  "hops": hops, "opened": bool(trace)}
        scan_id = self._record_scan(user.id, "url", "completed", f"{facts.get('Host')}: {verdict}", result)
        self._record_finding(user.id, scan_id, "url", facts.get("Host", url), verdict, evidence=result,
                             source="local URL analysis" + (" + safe redirect check" if trace else ""))
        return result

    def check_message(self, session, text: str) -> dict:
        user = self._user(session)
        indicators, links, facts = analyse_message(text)
        link_results = []
        for link in links[:5]:
            ind, f = analyse_url(link)
            link_results.append({"url": link, "assessment": judge(ind), "indicators": [i.text for i in ind if i.weight > 0]})
            indicators += [Indicator(f"Link {f.get('Host')}: {i.text}", i.weight) for i in ind if i.weight > 0]
        verdict = judge(indicators)
        result = {"facts": facts, "indicators": [asdict(i) for i in indicators], "links": link_results,
                  "assessment": verdict}
        self._record_scan(user.id, "message", "completed", f"message: {verdict}", result)  # text itself not stored
        return result

    # -- 6. file integrity -----------------------------------------------------------------------------------------
    def add_scope(self, session, path: str, label: str = "") -> dict:
        user = self._user(session)
        if not self._config().file_integrity_enabled:
            raise GuardianError("File integrity monitoring is switched off (SECURITY → SETTINGS).", "disabled")
        p = Path(path).expanduser()
        if not p.exists():
            raise GuardianError(f"“{path}” doesn't exist.", "invalid")
        reason = integ.refused_reason(p, self._data)
        if reason:
            raise GuardianError(f"Not monitored: {reason}.", "invalid")
        resolved = str(p.resolve())
        existing = self._db.row("SELECT * FROM integrity_scopes WHERE user_id = ? AND path = ?", (user.id, resolved))
        if existing is None:
            self._db.execute("INSERT INTO integrity_scopes (user_id, path, label, created_at) VALUES (?, ?, ?, ?)",
                             (user.id, resolved, label or Path(resolved).name, self._now()))
            self._audit(user.id, "monitor_folder", resolved, "completed", {"label": label})
        return self.baseline(session, resolved)

    def scopes(self, session) -> list[dict]:
        user = self._user(session)
        return self._db.query("SELECT s.*, (SELECT COUNT(*) FROM integrity_baselines b WHERE b.scope_id = s.id) AS files "
                              "FROM integrity_scopes s WHERE s.user_id = ? ORDER BY s.created_at", (user.id,))

    def _scope(self, user_id: int, path_or_label: str | None) -> dict:
        rows = self._db.query("SELECT * FROM integrity_scopes WHERE user_id = ?", (user_id,))
        if not rows:
            raise GuardianError("No folders are monitored yet. Say “monitor the integrity of <folder>”.", "invalid")
        if not path_or_label:
            if len(rows) == 1:
                return rows[0]
            raise GuardianError("Which folder? " + ", ".join(r["label"] for r in rows), "invalid")
        q = path_or_label.strip().lower()
        try:  # scopes are stored resolved; a short (8.3) or relative spelling must still match
            wanted = str(Path(path_or_label.strip()).expanduser().resolve()).lower()
        except (OSError, ValueError):
            wanted = q
        for r in rows:
            if r["path"].lower() == wanted or r["label"].lower() == q \
                    or q in r["label"].lower():
                return r
        raise GuardianError(f"“{path_or_label}” isn't monitored. Monitored: " + ", ".join(r["label"] for r in rows),
                            "invalid")

    def baseline(self, session, path_or_label: str | None = None) -> dict:
        user = self._user(session)
        scope = self._scope(user.id, path_or_label)
        cfg = self._config()
        with self._job("integrity baseline"):
            snap, skipped = integ.snapshot(Path(scope["path"]), cfg.max_integrity_files, cfg.max_hash_mb, self._cancel)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM integrity_baselines WHERE scope_id = ?", (scope["id"],))
            conn.executemany("INSERT INTO integrity_baselines (scope_id, rel_path, sha256, size, mtime) VALUES (?, ?, ?, ?, ?)",
                             [(scope["id"], rel, sha, size, mtime) for rel, (sha, size, mtime) in snap.items()])
            conn.execute("UPDATE integrity_scopes SET baseline_at = ? WHERE id = ?", (self._now(), scope["id"]))
        return {"scope": scope["label"], "path": scope["path"], "files": len(snap), "skipped": skipped}

    def check_integrity(self, session, path_or_label: str | None = None) -> dict:
        user = self._user(session)
        scope = self._scope(user.id, path_or_label)
        cfg = self._config()
        base = {r["rel_path"]: (r["sha256"], r["size"], r["mtime"])
                for r in self._db.query("SELECT * FROM integrity_baselines WHERE scope_id = ?", (scope["id"],))}
        with self._job("integrity check"):
            snap, skipped = integ.snapshot(Path(scope["path"]), cfg.max_integrity_files, cfg.max_hash_mb, self._cancel)
        events = integ.compare(base, snap)
        now = self._now()
        with self._db.transaction() as conn:
            conn.executemany("INSERT INTO integrity_events (scope_id, user_id, kind, rel_path, old_sha256, new_sha256, "
                             "detected_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [(scope["id"], user.id, e["kind"], e["rel_path"], e["old"], e["new"], now) for e in events])
            conn.execute("UPDATE integrity_scopes SET checked_at = ? WHERE id = ?", (now, scope["id"]))
        counts = {k: sum(1 for e in events if e["kind"] == k) for k in ("modified", "missing", "new")}
        self._record_scan(user.id, "integrity", "completed", f"{scope['label']}: " + ", ".join(f"{n} {k}" for k, n in counts.items()),
                          {"scope": scope["label"], "counts": counts, "events": events[:200]})
        return {"scope": scope["label"], "path": scope["path"], "files": len(snap), "baseline_at": scope["baseline_at"],
                "events": events, "counts": counts, "skipped": skipped}

    def integrity_events(self, session, limit: int = 100) -> list[dict]:
        user = self._user(session)
        return self._db.query("SELECT e.*, s.label FROM integrity_events e JOIN integrity_scopes s ON s.id = e.scope_id "
                              "WHERE e.user_id = ? ORDER BY e.id DESC LIMIT ?", (user.id, int(limit)))

    # -- 7. history ---------------------------------------------------------------------------------------------
    def latest(self, session, limit: int = 20) -> dict:
        user = self._user(session)
        return {"scans": self._db.query("SELECT id, kind, status, summary, started_at, finished_at FROM security_scans "
                                        "WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user.id, int(limit))),
                "findings": self._db.query("SELECT id, category, subject, status, assessment, confidence, created_at "
                                           "FROM security_findings WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                           (user.id, int(limit))),
                "actions": self._db.query("SELECT id, action, level, target, status, created_at, finished_at "
                                          "FROM security_actions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                          (user.id, int(limit)))}

    def last_overview(self, session) -> dict | None:
        user = self._user(session)
        row = self._db.row("SELECT result_json, finished_at FROM security_scans WHERE user_id = ? AND kind = 'overview' "
                           "AND status = 'completed' ORDER BY id DESC LIMIT 1", (user.id,))
        return json.loads(row["result_json"]) if row else None

    def last_investigation(self, session) -> Investigation | None:
        user = self._user(session)
        return self._last.get(user.id)

    # -- 8. actions (audited) -----------------------------------------------------------------------------------
    def _audit(self, user_id: int, action: str, target: str, status: str, details: Any = None,
               recovery: Any = None, action_id: int | None = None) -> int:
        if action_id is None:
            return self._db.execute(
                "INSERT INTO security_actions (user_id, action, level, target, status, details_json, recovery_json, "
                "created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, action, ACTION_LEVELS.get(action, 0), target[:300], status, json.dumps(details or {}),
                 json.dumps(recovery or {}), self._now(), None if status == "proposed" else self._now()))
        self._db.execute("UPDATE security_actions SET status = ?, details_json = ?, recovery_json = "
                         "CASE WHEN ? = '{}' THEN recovery_json ELSE ? END, finished_at = ? WHERE id = ? AND user_id = ?",
                         (status, json.dumps(details or {}), json.dumps(recovery or {}), json.dumps(recovery or {}),
                          self._now(), action_id, user_id))
        return action_id

    def save_report(self, session, markdown: str, title: str) -> Path:
        user = self._user(session)
        folder = self._data / "security_reports"
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in title)[:60] or "report"
        path = folder / f"{self._clock():%Y%m%d-%H%M%S}-{safe}.md"
        path.write_text(markdown, encoding="utf-8")
        self._audit(user.id, "save_report", str(path), "completed")
        return path

    def propose_disable(self, session, entry: StartupEntry) -> dict:
        """Level 2: describe what disabling would do. Nothing changes until ``confirm``."""
        user = self._user(session, "security.manage")
        supported = entry.mechanism in ("registry_run", "startup_folder") and entry.scope == "user" \
            and (entry.mechanism != "registry_run" or entry.location == f"HKCU\\{HKCU_RUN}")
        if not supported:
            how = {"registry_run": "Task Manager → Startup apps (or Settings → Apps → Startup)",
                   "startup_folder": "Task Manager → Startup apps",
                   "scheduled_task": "Task Scheduler → right-click the task → Disable",
                   "service": "Services (services.msc) → the service → Startup type: Manual"}.get(entry.mechanism, "")
            self._audit(user.id, "disable_startup", entry.name, "not_supported", {"mechanism": entry.mechanism,
                                                                                  "scope": entry.scope})
            return {"supported": False, "message": f"I can only disable startup entries of your own account (Run key "
                                                   f"or Startup folder) safely. “{entry.name}” is a "
                                                   f"{entry.mechanism.replace('_', ' ')} for all users, which needs "
                                                   f"administrator rights. You can do it in {how}."}
        action_id = self._audit(user.id, "disable_startup", entry.name, "proposed",
                                {"mechanism": entry.mechanism, "location": entry.location, "entry_id": entry.id})
        return {"supported": True, "action_id": action_id, "entry": entry,
                "message": (f"Disable “{entry.name}” from starting with Windows? What changes: its "
                            f"{'Run key value' if entry.mechanism == 'registry_run' else 'Startup-folder item'} is "
                            "removed, and DUMBOO keeps a copy so you can say “restore startup entry …” to undo it. "
                            "The program itself is not deleted. Say “yes” to disable it or “no” to keep it.")}

    def decline(self, session, action_id: int) -> None:
        user = self._user(session)
        self._audit(user.id, "disable_startup", "", "declined", {"by": "user"}, action_id=action_id)

    def confirm_disable(self, session, action_id: int, entry: StartupEntry, registry=None) -> str:
        user = self._user(session, "security.manage")
        row = self._db.row("SELECT * FROM security_actions WHERE id = ? AND user_id = ? AND status = 'proposed'",
                           (action_id, user.id))
        if row is None:
            raise GuardianError("That action is no longer waiting for confirmation.", "invalid")
        self._audit(user.id, "disable_startup", entry.name, "confirmed", {"entry_id": entry.id}, action_id=action_id)
        reg = registry or getattr(self.platform, "_reg", None)
        try:
            if entry.mechanism == "registry_run":
                data = dict(reg.values("HKCU", HKCU_RUN)).get(entry.name)
                if data is None:
                    raise GuardianError("The entry is already gone.", "invalid")
                recovery = {"kind": "registry_run", "hive": "HKCU", "key": HKCU_RUN, "name": entry.name, "data": data}
                reg.delete_value("HKCU", HKCU_RUN, entry.name)
                if entry.name in dict(reg.values("HKCU", HKCU_RUN)):
                    raise GuardianError("Windows didn't remove the value.", "unavailable")
            else:
                source = Path(entry.location) / next(
                    (f.name for f in Path(entry.location).iterdir() if f.stem == entry.name), entry.name)
                backup_dir = self._data / "security_backup" / "startup_folder"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = backup_dir / f"{self._clock():%Y%m%d%H%M%S}-{source.name}"
                shutil.move(str(source), str(target))
                if source.exists() or not target.exists():
                    raise GuardianError("The item could not be moved.", "unavailable")
                recovery = {"kind": "startup_folder", "original": str(source), "backup": str(target)}
        except GuardianError as exc:
            self._audit(user.id, "disable_startup", entry.name, "failed", {"error": exc.user_message}, action_id=action_id)
            raise
        except OSError as exc:
            self._audit(user.id, "disable_startup", entry.name, "failed", {"error": exc.__class__.__name__},
                        action_id=action_id)
            raise GuardianError(f"Disabling failed ({exc.__class__.__name__}). Nothing else was changed.") from exc
        self._audit(user.id, "disable_startup", entry.name, "completed", {"verified": True}, recovery, action_id)
        self._startup.pop(user.id, None)
        return (f"Done — “{entry.name}” will no longer start with Windows (verified). Say “restore startup entry "
                f"{entry.name}” to undo it.")

    def restore(self, session, name: str, registry=None) -> str:
        user = self._user(session, "security.manage")
        row = self._db.row("SELECT * FROM security_actions WHERE user_id = ? AND action = 'disable_startup' AND "
                           "status = 'completed' AND lower(target) = lower(?) ORDER BY id DESC LIMIT 1", (user.id, name))
        if row is None:
            raise GuardianError(f"I have no disabled startup entry called “{name}” to restore.", "invalid")
        rec = json.loads(row["recovery_json"] or "{}")
        reg = registry or getattr(self.platform, "_reg", None)
        try:
            if rec.get("kind") == "registry_run":
                reg.set_string(rec["hive"], rec["key"], rec["name"], rec["data"])
                ok = rec["name"] in dict(reg.values(rec["hive"], rec["key"]))
            else:
                shutil.move(rec["backup"], rec["original"])
                ok = Path(rec["original"]).exists()
        except (OSError, KeyError) as exc:
            self._audit(user.id, "restore_startup", name, "failed", {"error": exc.__class__.__name__})
            raise GuardianError(f"Restoring failed ({exc.__class__.__name__}).") from exc
        if not ok:
            self._audit(user.id, "restore_startup", name, "failed", {"error": "not verified"})
            raise GuardianError("Restoring could not be verified.")
        self._db.execute("UPDATE security_actions SET status = 'restored', finished_at = ? WHERE id = ?",
                         (self._now(), row["id"]))
        self._audit(user.id, "restore_startup", name, "completed", {"verified": True})
        self._startup.pop(user.id, None)
        return f"Restored — “{name}” will start with Windows again (verified)."


def _counts_text(counts: dict) -> str:
    return ", ".join(f"{n} {k.lower().replace('_', ' ')}" for k, n in counts.items() if n)
