"""Module 7 — Security Guardian. Everything runs against fakes: no real registry, startup folder,
firewall, process, PowerShell, network or paid API is touched."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.event_bus import Events
from guardian import intel, report
from guardian.assess import assess
from guardian.config import GuardianConfig
from guardian.files import SignatureChecker, command_observations, file_facts, path_observations, redact, sha256_file
from guardian.investigate import investigate_entry, investigate_file
from guardian.models import (FAIL, HIGH_CONCERN, INCONCLUSIVE, NO_CONCERN, NOT_SUPPORTED, PASS, SUSPICIOUS, UNKNOWN,
                             GuardianError, Indicator, Signature, StartupEntry)
from guardian.platforms import RUN_KEYS, UnsupportedPlatform, resolve_executable
from guardian.processes import network_overview
from guardian.service import HKCU_RUN, GuardianService
from guardian.updates import kev_matches, parse_winget_table
from guardian.urlcheck import analyse_message, analyse_url, judge, trace_redirects
from services.auth_service import PermissionDenied
from tests.guardian_fakes import FakePowerShell, FakeRegistry, FakeWindows, harmless_overrides
from tests.test_history_memory import Rig

NOW = datetime(2026, 10, 23, 12, 0)
DEFENDER_OK = {"AMServiceEnabled": True, "AntivirusEnabled": True, "RealTimeProtectionEnabled": True,
               "IsTamperProtected": True, "AntivirusSignatureLastUpdated": "2026-10-22T05:00:00",
               "QuickScanEndTime": "2026-10-20T10:00:00"}


def fake_windows(tmp_path, ps_answers=None, run_values=None, services=None):
    reg = FakeRegistry({("HKCU", RUN_KEYS[0][1]): dict(run_values or {}),
                        ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"): {"EnableLUA": 1}})
    answers = {"Get-MpComputerStatus": DEFENDER_OK,
               "SecurityCenter2": [{"displayName": "Windows Defender", "productState": 397568}],
               "Get-NetFirewallProfile": [{"Name": "Domain", "Enabled": True}, {"Name": "Private", "Enabled": True},
                                          {"Name": "Public", "Enabled": True}],
               "Get-HotFix": [{"HotFixID": "KB1", "InstalledOn": "2026-10-14T00:00:00"}],
               "Windows Defender/Operational": [], "Get-ScheduledTask": [],
               "Get-AuthenticodeSignature": lambda data: [
                   {"path": p, "status": "Valid" if "signed" in p.lower() else "NotSigned",
                    "subject": "CN=Good Corp, O=Good Corp" if "signed" in p.lower() else "", "issuer": "CN=DigiCert"}
                   for p in data["paths"]]}
    answers.update(ps_answers or {})
    ps = FakePowerShell(answers)
    return FakeWindows(ps, reg, services, now=lambda: NOW), ps, reg


@pytest.fixture
def startup_dirs(tmp_path, monkeypatch):
    appdata, programdata = tmp_path / "appdata", tmp_path / "programdata"
    (appdata / r"Microsoft\Windows\Start Menu\Programs\Startup").mkdir(parents=True)
    (programdata / r"Microsoft\Windows\Start Menu\Programs\StartUp").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("PROGRAMDATA", str(programdata))
    return appdata / r"Microsoft\Windows\Start Menu\Programs\Startup"


@pytest.fixture
def rig(paths, tmp_path, startup_dirs):
    signed = tmp_path / "apps" / "SignedApp.exe"
    unsigned = tmp_path / "temp" / "updater.exe"
    signed.parent.mkdir()
    unsigned.parent.mkdir()
    signed.write_bytes(b"signed program")
    unsigned.write_bytes(b"mystery program")
    win, ps, reg = fake_windows(tmp_path, run_values={
        "GoodApp": f'"{signed}" --minimized', "UnknownUpdater": f'"{unsigned}" --token SECRET123',
        "Leftover": r'"C:\Gone\old.exe"'})
    r = Rig(paths, guardian_overrides={"platform": win, "powershell": ps, "winget_run": lambda: WINGET,
                                       "advisories": SimpleNamespace(get=lambda: KEV)})
    r.win, r.reg, r.ps, r.unsigned = win, reg, ps, unsigned
    yield r
    r.close()


WINGET = """Name                Id               Version   Available Source
-----------------------------------------------------------------------
Git                 Git.Git          2.51.2    2.55.0    winget
VLC media player    VideoLAN.VLC     3.0.16    3.0.23    winget
2 upgrades available.
"""
KEV = {"catalogVersion": "2026.10.21", "_fetched": "2026-10-23T10:00:00", "vulnerabilities": [
    {"cveID": "CVE-2025-48384", "vendorProject": "Git", "product": "Git", "vulnerabilityName": "Git Link Following",
     "dateAdded": "2025-08-25", "requiredAction": "Apply updates"},
    {"cveID": "CVE-2099-0001", "vendorProject": "Oracle", "product": "Java", "vulnerabilityName": "x",
     "dateAdded": "2025-01-01"}]}


# 1. initialisation and settings ----------------------------------------------------------------------------
def test_guardian_initialises_and_can_be_switched_off(rig):
    assert rig.core.guardian is not None
    cfg = GuardianConfig.from_settings({"monitor_enabled": True}, {"SECURITY_EXTERNAL_LOOKUPS_ENABLED": "true",
                                                                    "VIRUSTOTAL_API_KEY": "k"})
    assert cfg.external_lookups and cfg.threat_intel_provider == "none" and not cfg.intel_ready
    rig.core.guardian._config = lambda: GuardianConfig(monitor_enabled=False)
    with pytest.raises(GuardianError):
        rig.core.guardian.overview(rig.session)


# 2. process enumeration (read-only, real psutil on the test process itself) -----------------------------------
def test_processes_are_listed_and_this_process_can_be_inspected(rig):
    rows = rig.core.guardian.processes(rig.session)
    assert rows and {"pid", "name", "memory_mb"} <= set(rows[0])
    inv = rig.core.guardian.inspect_process(rig.session, str(os.getpid()))
    assert inv.facts["PID"] == os.getpid() and inv.kind == "process"
    assert "high cpu" not in " ".join(i.text.lower() for i in inv.indicators)  # usage is never a verdict


# 3. missing executable ----------------------------------------------------------------------------------------
def test_missing_executable_is_inconclusive_not_a_threat():
    entry = StartupEntry("x", "Leftover", "registry_run", "HKCU\\Run", r'"C:\Gone\old.exe"', r"C:\Gone\old.exe")
    inv = investigate_entry(entry, Signature("UnknownError"), None, "external lookups are off")
    assert inv.assessment == INCONCLUSIVE and inv.facts["File exists"] == "no"
    assert inv.intel == {"status": "NOT_CHECKED", "reason": "no hash (file missing or unreadable)"}


# 4. signatures and unsupported platforms -------------------------------------------------------------------------
def test_signature_statuses_come_from_windows_and_are_never_assumed(tmp_path):
    ps = FakePowerShell({"Get-AuthenticodeSignature": [
        {"path": "a.exe", "status": "Valid", "subject": "CN=Microsoft Corporation, O=Microsoft", "issuer": "CN=MS CA"},
        {"path": "b.exe", "status": "HashMismatch", "subject": "CN=Evil", "issuer": ""}]})
    sigs = SignatureChecker(ps).check(["a.exe", "b.exe", "c.exe"])
    assert sigs["a.exe"].valid and sigs["a.exe"].publisher == "Microsoft Corporation"
    assert sigs["b.exe"].status == "HashMismatch" and sigs["b.exe"].publisher == "UNKNOWN"  # not trusted
    assert sigs["c.exe"].status == UNKNOWN                                                  # no answer: unknown
    assert SignatureChecker(FakePowerShell(available=False)).check(["a.exe"])["a.exe"].status == NOT_SUPPORTED


# 5. hashing ----------------------------------------------------------------------------------------------------
def test_sha256_is_correct_bounded_and_cancellable(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 3_000_000)
    assert sha256_file(f) == hashlib.sha256(b"x" * 3_000_000).hexdigest()
    with pytest.raises(GuardianError):
        sha256_file(f, max_mb=1)
    stop = threading.Event()
    stop.set()
    with pytest.raises(GuardianError) as info:
        sha256_file(f, cancel=stop)
    assert info.value.kind == "cancelled"
    facts = file_facts(str(f))
    assert facts.exists and facts.size == 3_000_000 and facts.sha256


# 6. startup enumeration ----------------------------------------------------------------------------------------
def test_startup_entries_are_enumerated_from_every_mechanism(tmp_path, startup_dirs):
    (startup_dirs / "Notes.lnk").write_bytes(b"lnk")
    win, _ps, _reg = fake_windows(tmp_path, run_values={"GoodApp": r'"C:\Apps\Good.exe" -x'}, ps_answers={
        "CreateShortcut": [{"lnk": str(startup_dirs / "Notes.lnk"), "target": r"C:\Apps\Notes.exe", "args": ""}],
        "Get-ScheduledTask": [{"name": "Updater", "path": "\\", "state": "Ready", "exe": r"C:\Apps\upd.exe",
                               "args": "--password hunter2"}]},
        services=[])
    entries = {e.name: e for e in win.startup_entries()}
    assert entries["GoodApp"].executable == r"C:\Apps\Good.exe" and entries["GoodApp"].mechanism == "registry_run"
    assert entries["Notes"].mechanism == "startup_folder" and entries["Notes"].executable == r"C:\Apps\Notes.exe"
    assert entries["Updater"].mechanism == "scheduled_task" and "hunter2" not in entries["Updater"].command
    assert resolve_executable(r'C:\Windows\System32\rundll32.exe "C:\Users\x\AppData\Temp\evil.dll",Run') == \
        r"C:\Users\x\AppData\Temp\evil.dll"


def test_investigating_a_startup_entry_reports_real_evidence(rig):
    inv_text = rig.reply("investigate the startup entry UnknownUpdater")
    assert "**Executable:**" in inv_text and str(rig.unsigned) in inv_text
    assert hashlib.sha256(b"mystery program").hexdigest() in inv_text      # the real hash
    assert "NotSigned" in inv_text and "Publisher:** UNKNOWN" in inv_text   # never an invented publisher
    assert "NOT_CHECKED" in inv_text                                       # no lookup was configured
    assert "SECRET123" not in inv_text                                     # secrets in the command line hidden
    assert "Assessment:**" in inv_text and "Confidence:**" in inv_text and "Limitations" in inv_text
    assert "malicious" not in inv_text.lower().replace("never claim", "")


# 7. network ----------------------------------------------------------------------------------------------------
def test_network_overview_marks_unusual_ports_without_calling_them_malicious(monkeypatch):
    import psutil

    addr = SimpleNamespace
    conns = [SimpleNamespace(laddr=addr(ip="10.0.0.5", port=50000), raddr=addr(ip="203.0.113.9", port=4444),
                             status="ESTABLISHED", pid=None),
             SimpleNamespace(laddr=addr(ip="0.0.0.0", port=8080), raddr=None, status="LISTEN", pid=None)]
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": conns)
    n = network_overview()
    assert n["total_connections"] == 1 and n["exposed"] == [("unknown (no permission)", 8080)]
    assert "4444" in n["notes"][0] and "not proof" in n["notes"][0]
    assert "203.0.113.9" not in report.network_speech(n)  # no addresses in speech


# 8. file integrity -------------------------------------------------------------------------------------------
def test_integrity_baseline_detects_modified_missing_and_new_files(rig, tmp_path):
    folder = Path.home() / ".dumboo_guardian_test" / tmp_path.name
    folder.mkdir(parents=True)
    try:
        (folder / "a.txt").write_text("one")
        (folder / "b.txt").write_text("two")
        base = rig.core.guardian.add_scope(rig.session, str(folder))
        assert base["files"] == 2
        (folder / "a.txt").write_text("changed")
        (folder / "b.txt").unlink()
        (folder / "c.txt").write_text("new")
        result = rig.core.guardian.check_integrity(rig.session, str(folder))
        assert result["counts"] == {"modified": 1, "missing": 1, "new": 1}
        assert (folder / "c.txt").exists()  # nothing is deleted or changed
        assert len(rig.core.guardian.integrity_events(rig.session)) == 3
    finally:
        import shutil
        shutil.rmtree(folder.parent, ignore_errors=True)


def test_integrity_refuses_system_and_private_locations(rig):
    for path in (os.environ.get("WINDIR", r"C:\Windows"), "C:\\", str(rig.core.paths.data_dir)):
        with pytest.raises(GuardianError):
            rig.core.guardian.add_scope(rig.session, path)


# 9. URLs with mocked network ------------------------------------------------------------------------------------
def test_url_analysis_flags_lookalikes_and_never_guarantees_safety():
    ind, _facts = analyse_url("http://paypa1.com/login")
    assert judge(ind) == HIGH_CONCERN and any("lookalike" in i.text for i in ind)
    ind, _ = analyse_url("https://paypal.com.secure-login.xyz/verify")
    assert any("subdomain" in i.text for i in ind)
    ind, _ = analyse_url("https://www.paypal.com/signin")
    assert judge(ind) == NO_CONCERN
    ind, links, _ = analyse_message('From: "PayPal" <help@paypa1-mail.com>\nURGENT: verify your account and enter your '
                                    'password <a href="http://paypa1.com/x">www.paypal.com</a>')
    assert judge(ind) == HIGH_CONCERN and links == ["http://paypa1.com/x"]
    assert "No check can prove a link is safe" in report.url_markdown(
        {"assessment": NO_CONCERN, "facts": {}, "indicators": [], "hops": [], "opened": False})


def test_redirect_tracing_is_safe_and_blocks_private_addresses():
    def handler(request):
        if request.url.host == "short.example":
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})
        return httpx.Response(200, text="<script>alert(1)</script>")
    hops = trace_redirects("http://short.example/x", client_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False),
        is_public=lambda host: not host.startswith("192.168."))
    assert hops[0]["status"] == 302 and hops[-1]["status"] == "blocked"


# 10. external providers: timeouts and rate limits ---------------------------------------------------------------------
@pytest.mark.parametrize("response,status", [
    (httpx.Response(429), "ERROR"), (httpx.Response(404), "NOT_FOUND"), (httpx.Response(401), "ERROR"),
    (httpx.Response(200, json={"data": {"attributes": {"last_analysis_stats": {"malicious": 40, "undetected": 30}}}}),
     "MALICIOUS")])
def test_virustotal_results_are_mapped_honestly(response, status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: response))
    assert intel.VirusTotal("key", client).lookup("ab" * 32)["status"] == status


def test_provider_timeout_is_an_error_not_a_verdict():
    def boom(_request):
        raise httpx.ConnectTimeout("slow")
    result = intel.VirusTotal("key", httpx.Client(transport=httpx.MockTransport(boom))).lookup("ab" * 32)
    assert result["status"] == "ERROR"
    inv = investigate_file("x", "file", __file__, "", Signature("Valid", "Good"), lambda h: result)
    assert inv.assessment != HIGH_CONCERN and any("Threat intelligence not checked" in l for l in inv.limitations)


def test_only_threat_intelligence_can_say_high_concern_for_a_clean_looking_file():
    inv = investigate_file("x", "file", __file__, "", Signature("Valid", "Good"),
                           lambda h: {"provider": "VirusTotal", "status": "MALICIOUS", "malicious": 30, "engines": 70})
    assert inv.assessment == HIGH_CONCERN and inv.confidence == "High"
    assert any(i.kind == "external" for i in inv.indicators)


# 11. unsupported platform -------------------------------------------------------------------------------------------
def test_unsupported_platform_reports_not_supported_instead_of_simulating():
    checks = UnsupportedPlatform().overview()
    assert {c.id: c.status for c in checks}["firewall"] == NOT_SUPPORTED
    with pytest.raises(GuardianError):
        UnsupportedPlatform().startup_entries()


# 12. missing permissions ----------------------------------------------------------------------------------------------
def test_denied_or_failed_checks_are_unknown_never_fail(tmp_path):
    denied = GuardianError("Access is denied", "permission")
    win, _ps, _reg = fake_windows(tmp_path, ps_answers={"Get-MpComputerStatus": denied, "Get-NetFirewallProfile": denied,
                                                         "SecurityCenter2": denied})
    checks = {c.id: c for c in win.overview()}
    for key in ("defender", "firewall", "av_products"):
        assert checks[key].status == UNKNOWN
    assert "does not mean it is off" in checks["defender"].detail
    assert checks["bitlocker"].status == UNKNOWN and "administrator" in checks["bitlocker"].detail


# 13. migrations and persistence ------------------------------------------------------------------------------------
def test_overview_is_persisted_with_findings(rig):
    text = rig.reply("Hey DUMBOO, check my laptop security")
    assert "Microsoft Defender antivirus" in text and "Windows Firewall" in text
    scans = rig.core.db.query("SELECT kind, status FROM security_scans")
    assert ("overview", "completed") in [(s["kind"], s["status"]) for s in scans]
    findings = rig.core.db.query("SELECT subject, status FROM security_findings")
    assert any(f["subject"] == "Startup programs" and f["status"] == "WARNING" for f in findings)
    assert rig.core.db.scalar("PRAGMA user_version") >= 6


# 14. user isolation and permissions -------------------------------------------------------------------------------------
def test_other_users_see_nothing_and_need_permission(rig):
    rig.reply("check my laptop security")
    rig.core.auth.create_user(rig.session, "guest", "Guest", "Plum-Violet-Harbor-91")
    rig.core.accounts.logout()
    rig.core.accounts.login("guest", "Plum-Violet-Harbor-91")
    with pytest.raises(PermissionDenied):
        rig.core.guardian.latest(rig.session)  # normal users don't get security.view by default
    reply = rig.reply("run a security check").lower()  # other wording: repeats within 3 s are ignored
    assert "permission" in reply
    assert not rig.core.db.query("SELECT 1 FROM security_scans s JOIN users u ON u.id = s.user_id "
                                 "WHERE u.username = 'guest'")


# 15. cancellation and timeouts ----------------------------------------------------------------------------------------
def test_scans_can_be_cancelled_and_time_out(rig):
    svc = rig.core.guardian
    with svc._job("test") as job:
        assert svc.busy == "test"
        assert svc.cancel()
        with pytest.raises(GuardianError) as info:
            job.check()
        assert info.value.kind == "cancelled"
        with pytest.raises(GuardianError):
            with svc._job("second"):
                pass  # only one scan at a time
    assert svc.busy is None
    svc._config = lambda: GuardianConfig(scan_timeout_s=-1)
    with svc._job("slow") as job:
        with pytest.raises(GuardianError) as info:
            job.check()
        assert info.value.kind == "timeout"


def test_stop_the_current_scan_by_voice(rig):
    assert "No security scan is running" in rig.reply("stop the current scan")


# 16-17. confirmation, authorisation, recovery and audit ----------------------------------------------------------------
def test_disabling_needs_confirmation_is_verified_audited_and_restorable(rig):
    rig.reply("show my startup applications")
    assert "Say “yes”" in rig.reply("disable startup entry UnknownUpdater")
    assert "UnknownUpdater" in dict(rig.reg.values("HKCU", HKCU_RUN))       # nothing changed before "yes"
    assert "verified" in rig.reply("yes")
    assert "UnknownUpdater" not in dict(rig.reg.values("HKCU", HKCU_RUN))
    actions = rig.core.db.query("SELECT action, level, status, recovery_json FROM security_actions ORDER BY id")
    assert [(a["action"], a["level"], a["status"]) for a in actions][-1] == ("disable_startup", 2, "completed")
    assert "SECRET123" in json.loads(actions[-1]["recovery_json"])["data"]   # exact backup for restore (local DB)
    assert "verified" in rig.reply("restore startup entry UnknownUpdater")
    assert "UnknownUpdater" in dict(rig.reg.values("HKCU", HKCU_RUN))


def test_declining_changes_nothing_and_is_audited(rig):
    rig.reply("disable startup entry GoodApp")
    assert "left" in rig.reply("no")
    assert "GoodApp" in dict(rig.reg.values("HKCU", HKCU_RUN))
    assert rig.core.db.query("SELECT status FROM security_actions")[-1]["status"] == "declined"


def test_machine_wide_entries_are_not_changed_without_admin(rig):
    entry = StartupEntry("m", "Vendor", "service", "Service vendor", "x", r"C:\Program Files\v.exe", "machine")
    proposal = rig.core.guardian.propose_disable(rig.session, entry)
    assert not proposal["supported"] and "administrator" in proposal["message"]
    assert rig.core.db.query("SELECT status FROM security_actions")[-1]["status"] == "not_supported"


def test_normal_users_cannot_change_startup_entries(rig):
    guest_id, _ = rig.core.auth.create_user(rig.session, "guest", "Guest", "Plum-Violet-Harbor-91")
    rig.core.auth.set_permission(rig.session, guest_id, "security.view", True)  # may look, may not change
    rig.core.accounts.logout()
    rig.core.accounts.login("guest", "Plum-Violet-Harbor-91")
    entry = StartupEntry("u", "X", "registry_run", f"HKCU\\{HKCU_RUN}", "x", None)
    with pytest.raises(PermissionDenied):
        rig.core.guardian.propose_disable(rig.session, entry)


# 18. AI explanations only from structured findings ------------------------------------------------------------------------
def test_ai_explanation_uses_evidence_and_rejects_invented_numbers(rig):
    rig.reply("investigate UnknownUpdater")
    rig.provider.answer = "It is not signed and lives in a temp folder. Confidence is low."
    assert "explained" in rig.reply("explain this finding")
    assert "Use ONLY the JSON evidence" in rig.provider.requests[-1].system_prompt
    rig.provider.answer = "37 antivirus engines flag this file as malware."
    # (different wording: the same message twice within 3 s is ignored as a duplicate)
    assert "**Investigation:" in rig.reply("explain the finding")  # invented number -> structured report instead


# 19. nothing is fabricated when checks fail -------------------------------------------------------------------------------
def test_failed_checks_never_produce_findings_or_verdicts(tmp_path):
    failing = GuardianError("PowerShell failed", "unavailable")
    win, _ps, _reg = fake_windows(tmp_path, ps_answers={k: failing for k in (
        "Get-MpComputerStatus", "SecurityCenter2", "Get-NetFirewallProfile", "Get-HotFix", "Windows Defender/")})
    statuses = {c.id: c.status for c in win.overview()}
    assert FAIL not in statuses.values() and PASS in statuses.values()  # only the OS line (read locally) passes
    inv = investigate_file("x", "file", __file__, "", None, None)
    assert inv.facts["Publisher"] == "UNKNOWN" and inv.facts["Signature"] == "not checked"
    assert "The digital signature could not be checked" in inv.limitations


def test_assessment_rules_are_evidence_based():
    assert assess([], True, True, True, False) == (NO_CONCERN, "Medium")
    assert assess([Indicator("unsigned", 1)], True, True, True, False)[0] == NO_CONCERN
    assert assess([Indicator("encoded powershell", 3), Indicator("temp", 2)], True, True, True, False)[0] == HIGH_CONCERN
    assert assess([Indicator("x", 3)], True, True, True, False)[0] == SUSPICIOUS
    assert command_observations("powershell -w hidden -enc AAA") and not command_observations("app.exe --minimized")
    assert path_observations(r"C:\Windows\explorer.exe") == [("Located in the Windows system folder", -1)]
    assert redact("x --password p4ss --token=abc") == "x --password [hidden] --token=[hidden]"


# 5 (updates). available updates vs advisories ------------------------------------------------------------------------------
def test_updates_and_advisories_are_kept_apart(rig):
    text = rig.reply("check for security updates")
    assert "| Git | 2.51.2 | 2.55.0 | winget |" in text
    assert "CVE-2025-48384" in text and "not verified against the affected range" in text
    assert "CVE-2099-0001" not in text  # Java isn't installed
    rows = parse_winget_table(WINGET)
    assert [r["id"] for r in rows] == ["Git.Git", "VideoLAN.VLC"]
    assert kev_matches([{"name": "Unrelated Tool", "installed": "1"}], KEV) == []


# 20. regression: other modules keep their commands ------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["kill notepad", "investigate the history of the Roman empire", "open youtube",
                                  "what's the weather in Pune", "remind me at 5 to call mom", "tell me a joke"])
def test_non_security_requests_pass_through_unchanged(rig, text):
    assistant = rig.core.guardian_assistant
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(assistant._inner, "local_reply", lambda scope, t, source: "PASSED THROUGH")
        assert assistant.local_reply(rig.session, text, "typed") == "PASSED THROUGH"


def test_harmless_defaults_keep_other_tests_off_the_real_system():
    overrides = harmless_overrides()
    assert isinstance(overrides["platform"], UnsupportedPlatform) and not overrides["powershell"].available


def test_already_quoted_task_paths_are_resolved(tmp_path):
    # Regression (found live): Task Scheduler stores some paths with quotes; quoting again broke resolution and
    # every such task was falsely reported as "points to a file that doesn't exist".
    real = tmp_path / "Brave Update.exe"
    real.write_bytes(b"x")
    win, _ps, _reg = fake_windows(tmp_path, ps_answers={"Get-ScheduledTask": [
        {"name": "BraveUpdateTask", "path": "\\", "state": "Ready", "exe": f'"{real}"', "args": "/c"}]})
    [task] = [e for e in win.startup_entries() if e.mechanism == "scheduled_task"]
    assert task.executable == str(real)


def test_reports_list_each_observation_once_with_the_real_intel_reason():
    inv = investigate_file("explorer.exe", "process", r"C:\Windows\explorer.exe", "", Signature("Valid", "Microsoft"),
                           None, extra_indicators=[Indicator("Located in the Windows system folder", -1)],
                           why_no_intel="external lookups are off")
    assert [i.text for i in inv.indicators].count("Located in the Windows system folder") == 1
    assert inv.intel == {"status": "NOT_CHECKED", "reason": "external lookups are off"}


def test_lookups_send_only_the_hash_and_malwarebazaar_is_mapped_honestly():
    sent = []

    def handler(request):
        sent.append(request)
        if "virustotal" in request.url.host:
            return httpx.Response(404)
        found = b"hash=" + b"ab" * 32 in request.content
        return httpx.Response(200, json={"query_status": "ok", "data": [{"signature": "AgentTesla"}]} if found
                              else {"query_status": "hash_not_found"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert intel.VirusTotal("key", client).lookup("ab" * 32)["status"] == "NOT_FOUND"
    assert intel.MalwareBazaar("key", client).lookup("ab" * 32)["status"] == "MALICIOUS"
    assert intel.MalwareBazaar("key", client).lookup("cd" * 32)["status"] == "NOT_FOUND"
    for request in sent:   # nothing but the hash (no file content, path or user name) leaves the PC
        body = request.content.decode()
        assert len(body) < 100 and "\\" not in body and "Users" not in str(request.url)


def test_regional_domains_are_not_lookalikes_but_disguised_brands_are():
    ind, _ = analyse_url("https://www.amazon.in/orders")
    assert judge(ind) == NO_CONCERN
    ind, _ = analyse_url("https://amaz0n-support.com/refund")
    assert judge(ind) != NO_CONCERN


def test_redirect_tracing_stops_after_the_hop_limit_and_reads_at_most_64_kb():
    sent = {"bytes": 0}

    def endless(request):
        return httpx.Response(302, headers={"location": f"/next{len(request.url.path)}"})
    hops = trace_redirects("http://loop.example/", client_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(endless)), is_public=lambda host: True, max_hops=4)
    assert len(hops) == 5 and hops[-1]["status"] == "stopped"

    def body():
        for _ in range(100):          # 100 × 16 KB would be 1.6 MB
            sent["bytes"] += 16384
            yield b"x" * 16384

    hops = trace_redirects("http://big.example/", client_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body()))), is_public=lambda host: True)
    assert hops == [{"url": "http://big.example/", "status": 200, "type": ""}] and sent["bytes"] <= 65536 + 16384


def test_a_monitored_folder_is_found_under_another_spelling_of_its_path(rig, tmp_path):
    folder = Path.home() / ".dumboo_guardian_test" / tmp_path.name
    (folder / "sub").mkdir(parents=True)
    try:
        (folder / "a.txt").write_text("one")
        rig.core.guardian.add_scope(rig.session, str(folder))
        other = str(folder / "sub" / "..")    # like a short C:\Users\NAME~1\… path, it resolves to the same folder
        assert rig.core.guardian.check_integrity(rig.session, other)["counts"] == {"modified": 0, "missing": 0, "new": 0}
        assert "1 modified" not in rig.core.guardian.latest(rig.session, 5)["scans"][0]["summary"]
        assert "{" not in rig.core.guardian.latest(rig.session, 5)["scans"][0]["summary"]   # readable, not a dict
    finally:
        import shutil
        shutil.rmtree(folder.parent, ignore_errors=True)


def test_connections_between_programs_on_this_pc_are_listed_last(monkeypatch):
    import psutil

    addr = SimpleNamespace
    conns = [SimpleNamespace(laddr=addr(ip="127.0.0.1", port=5000 + i), raddr=addr(ip="127.0.0.1", port=6000 + i),
                             status="ESTABLISHED", pid=None) for i in range(3)]
    conns.append(SimpleNamespace(laddr=addr(ip="10.0.0.5", port=50000), raddr=addr(ip="203.0.113.9", port=443),
                                 status="ESTABLISHED", pid=None))
    monkeypatch.setattr(psutil, "net_connections", lambda kind="inet": conns)
    n = network_overview()
    assert n["connections"][0]["remote"] == "203.0.113.9" and n["loopback"] == 3
    assert "3 of them are between programs on this PC" in report.network_markdown(n)
