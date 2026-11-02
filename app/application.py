"""Composition root: builds every service and wires them together.

``AssistantCore`` contains no Qt code, so it can be started headless in tests.
``run_gui`` adds the PySide6 dashboard on top of it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
import sys
import threading
import time
from pathlib import Path
from typing import Mapping

from app.event_bus import EventBus, Events
from app.lifecycle import Lifecycle
from app.account_manager import AccountManager
from app.orchestrator import Orchestrator
from ai.config import AIConfig, env_overrides
from ai.controller import ConversationController
from ai.memory_assistant import MemoryAssistant
from app.state_machine import AssistantState, StateMachine
from audio import audio_devices
from audio.interruption import InterruptionConfig, InterruptionDetector
from audio.microphone import Microphone
from audio.noise_suppression import NoiseSuppressor
from audio.pipeline import AudioPipeline
from audio.playback import AudioPlayer
from audio.speech_to_text import SpeechToText, WhisperModelPool
from audio.text_to_speech import TextToSpeech
from audio.vad import SegmenterConfig, SpeechSegmenter, create_vad
from audio.wake_word import WakeWordService
from security.token_store import TokenStore
from services.auth_service import AuthService
from services.conversation_recorder import ConversationRecorder
from services.history_service import HistoryService
from services.memory_service import MemoryService
from services.profile_service import ProfileService
from services.schedule_service import ReminderService, ScheduleService, reminder_speech
from services.settings_service import SettingsService
from services.system_monitor import SystemMonitor
from services.task_service import TaskService
from storage.local_database import LocalDatabase
from utils.paths import AppPaths

log = logging.getLogger(__name__)


class Ticker:
    """Publishes ``TICK`` every 250 ms so timeouts run on the bus thread."""

    name = "ticker"

    def __init__(self, bus: EventBus, interval: float = 0.25) -> None:
        self._bus = bus
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Ticker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._bus.publish(Events.TICK, {})

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._thread = None


class AssistantCore:
    def __init__(self, paths: AppPaths, *, text_only: bool | None = None, mic_factory=None,
                 speaker_factory=None, synth_factory=None, whisper_loader=None, vad=None,
                 wake_engine_factory=None, environ: Mapping[str, str] | None = None,
                 ai_provider_factory=None, password_hasher=None, token_store: TokenStore | None = None,
                 desktop=None, browser=None, uia=None, screen_capture=None, research_providers=None,
                 research_client=None, research_resolver=None, research_browser=None, weather_client=None, face_engine=None,
                 camera_factory=None, guardian_overrides: dict | None = None) -> None:
        self.paths = paths.ensure()
        self.camera_factory = camera_factory  # tests inject a fake camera; None = the real webcam
        self._guardian_overrides = guardian_overrides or {}  # tests inject fake OS layers (no real system changes)
        self.environ = os.environ if environ is None else environ
        self.started_at = time.monotonic()
        self.bus = EventBus()
        self.settings = SettingsService(paths.default_settings, paths.user_settings, self._on_settings_changed)
        if text_only is not None:
            self.settings.override_for_session("dashboard", "text_only", text_only)
        self.env_warnings = self._apply_env_overrides()
        s = self.settings.snapshot()
        initial = AssistantState.SLEEPING if s["dashboard"]["start_mode"] == "sleep" else AssistantState.IDLE
        self.state = StateMachine(self.bus, initial)

        # storage, accounts and per-user services (Module 3)
        self.db = LocalDatabase(paths.database)
        policy = lambda: self.settings.section("accounts")  # noqa: E731 - read live, so changes apply at once
        self.auth = AuthService(self.db, policy, hasher=password_hasher)
        self.profile = ProfileService(self.db, self.auth, policy)
        self.history = HistoryService(self.db, self.auth)
        self.memory = MemoryService(self.db, self.auth)
        self.accounts = AccountManager(self.bus, self.auth, self.profile,
                                       token_store if token_store is not None
                                       else TokenStore(paths.data_dir / "session.dat"))
        self.tasks = TaskService(self.db, self.auth)
        self.schedule = ScheduleService(self.db, self.auth)
        self.reminders = ReminderService(self.schedule, self.bus, lambda: self.accounts.session, tasks=self.tasks)
        self.monitor = SystemMonitor(self.service_status, started_at=self.started_at)

        # audio
        self.whisper_pool = WhisperModelPool(paths.models_dir / "whisper", loader=whisper_loader)
        self.player = AudioPlayer(s["audio"]["output_device"], stream_factory=speaker_factory)
        self.mic = Microphone(s["audio"]["input_device"], stream_factory=mic_factory)
        self.vad = vad or create_vad()
        self.suppressor = NoiseSuppressor(s["audio"]["noise_suppression_strength"], s["audio"]["noise_suppression"])
        self.wake = WakeWordService(
            s["wake_word"], paths.resolve(s["wake_word"]["model_path"]), self.whisper_pool,
            s["vad"]["threshold"], self._on_wake, engine_factory=wake_engine_factory,
        )
        self.pipeline = AudioPipeline(
            self.bus, self.mic, self.vad, self.suppressor,
            SpeechSegmenter(SegmenterConfig.from_settings(s["vad"])),
            InterruptionDetector(InterruptionConfig.from_settings(s["interruption"])),
            self.wake, playback_level=lambda: self.player.level,
        )
        self.pipeline.interruption_enabled = s["interruption"]["enabled"]
        self.stt = SpeechToText(self.bus, self.whisper_pool, s["stt"])
        self.tts = TextToSpeech(self.bus, self.player, paths.resolve(s["tts"]["voice_model"]), s["tts"]["speed"],
                                enabled=s["tts"]["enabled"], synth_factory=synth_factory)
        self.recorder = ConversationRecorder(self.bus, self.history)
        self.memory_assistant = MemoryAssistant(self.bus, self.auth, self.profile, self.memory, self.history, policy,
                                                current_conversation=self.recorder.current_conversation)
        self.recorder._memory_assistant = self.memory_assistant
        self.memory_assistant.previous_user_text = lambda: next(
            (turn.user for turn in reversed(self.conversation.context.recent(1))), None)
        from security.face import FaceEngine
        from services.face_service import FaceService

        # Face unlock: local OpenCV models; only encrypted face measurements are stored.
        self.face = FaceService(self.db, self.auth, policy)
        self.face_engine = face_engine or FaceEngine(paths.models_dir / "face")
        self.accounts.face = self.face
        from ai.learning import ConversationLearner

        # Learns lasting preferences/interests from what you say, for future conversations (ai/learning.py).
        self.memory_assistant.learner = ConversationLearner(self._ai_generate)
        self._build_automation(paths, desktop, browser, uia, screen_capture)
        self._build_research(paths, research_providers, research_client, research_resolver, research_browser)
        from ai.everyday_assistant import EverydayAssistant
        from services.weather_service import WeatherService

        self.weather = WeatherService(weather_client)
        # Time, weather, voice reminders, translation and "what did we talk about…" come first in the chain.
        self.everyday_assistant = EverydayAssistant(
            self.research_assistant, self.auth, schedule=self.schedule, weather=self.weather, memory=self.memory,
            history=self.history, generate=self._ai_generate, recent_turns=lambda: self.conversation.context.recent(3))
        from ai.agenda_assistant import AgendaAssistant

        # Tasks, schedule and "what do I have to do" by voice/text (slang too); reminders go to TASKS or SCHEDULE.
        self.agenda_assistant = AgendaAssistant(self.everyday_assistant, self.auth, tasks=self.tasks,
                                                schedule=self.schedule, generate=self._ai_generate)
        self._held_announcements: list[str] = []
        self.bus.subscribe(Events.REMINDER_DUE, self._on_reminder_due)
        self.bus.subscribe(Events.STATE_CHANGED, self._speak_held_announcements)
        self._build_guardian()
        self.bus.subscribe(Events.USER_SIGNED_IN, self._greet)
        controller_kwargs = {"provider_factory": ai_provider_factory} if ai_provider_factory else {}
        self.conversation = ConversationController(
            self.bus, self.ai_config(), paths.root,
            voice_output=lambda: self.tts.available and self._user_voice_on()
            and not self.settings.get("dashboard", "text_only"),
            hooks=self.guardian_assistant, **controller_kwargs,
        )
        self.orchestrator = Orchestrator(
            self.bus, self.state, self.pipeline, self.wake, self.stt, self.tts, self.conversation,
            dashboard_settings=lambda: self.settings.section("dashboard"),
            scope_provider=lambda: self.accounts.session, voice_allowed=self._user_voice_on,
            # Signed out = the sign-in screen is showing: going to sleep would hide it in the tray mid-sign-in.
            can_auto_sleep=lambda: self.accounts.session is not None,
        )
        self.accounts.on_sign_out(self._clear_private_context)
        self.bus.subscribe(Events.CONVERSATION_CLEARED, lambda _n, _p: self.recorder.new_conversation(self.accounts.session))
        self.ticker = Ticker(self.bus)

        self.lifecycle = Lifecycle()
        self.lifecycle.register(self.db)
        self.lifecycle.register(self.orchestrator, required=True)
        self.lifecycle.register(self.conversation)
        self.lifecycle.register(self.recorder)
        self.lifecycle.register(self.accounts)
        self.lifecycle.register(self.reminders)
        if hasattr(self.desktop, "start"):
            self.lifecycle.register(self.desktop)
        self.lifecycle.register(self.automation)
        self.lifecycle.register(self.resource_alerts)
        self.lifecycle.register(self.research)
        self.lifecycle.register(self.research_browser)
        self.lifecycle.register(self.tts)
        self.lifecycle.register(self.stt)
        self._voice_enabled = not s["dashboard"]["text_only"]
        if self._voice_enabled:
            self.lifecycle.register(self.wake)
            self.lifecycle.register(self.pipeline)
        self.lifecycle.register(self.ticker)
        self._shutdown_done = False

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        self.bus.start()
        try:  # housekeeping: old login attempts / sessions, history retention policy
            self.auth.purge_old_attempts()
            self.history.purge_expired(self.settings.get("accounts", "history_retention_days"))
        except Exception:
            log.exception("Database housekeeping failed")
        failures = self.lifecycle.start_all()
        for record in failures:
            self.bus.publish(Events.ERROR, {"source": record.service.name, "recoverable": True,
                                            "message": f"{record.service.name} unavailable: {record.error}"})
        self.publish_wake_status()
        log.info("DUMBOO core started (state %s)", self.state.state.value)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        log.info("Shutting down")
        self.state.transition(AssistantState.SHUTTING_DOWN, "shutdown")
        self.lifecycle.stop_all()
        self.whisper_pool.release()
        self.bus.stop()
        log.info("Shutdown complete")

    # -- accounts ------------------------------------------------------------------
    def _user_voice_on(self) -> bool:
        session = self.accounts.session
        if session is None:
            return True  # e.g. "please sign in" is still spoken
        try:
            return self.profile.get(session)["voice_output"] == "on"
        except Exception:
            return True

    def _clear_private_context(self, session) -> None:
        """Runs whenever the user signs out or changes: nothing of theirs may stay in memory."""
        self.conversation.clear()
        self.tts.stop_speaking()
        self.memory_assistant.forget_session(session)
        self.guardian_assistant.forget_session(session)   # also cancels that user's research and automation
        self.recorder.forget_session(session)

    # -- laptop automation (Module 4) ----------------------------------------------------------
    def _build_automation(self, paths: AppPaths, desktop, browser, uia, screen_capture) -> None:
        from automation.actions import build_registry
        from automation.assistant import AutomationAssistant
        from automation.config import AutomationSettings
        from automation.controller import AutomationController
        from automation.planner import Planner
        from automation.platform.browser import BrowserService
        from automation.platform.processes import ProcessRunner
        from automation.policy import PolicyEngine
        from automation.services import AutomationServices
        from services.automation_history import AutomationHistory
        from services.resource_alerts import ResourceAlerts

        self.automation_settings = AutomationSettings(paths.root / "config" / "automation.json",
                                                      paths.data_dir / "automation.json", paths.root, paths.data_dir)
        if desktop is None:  # the real Windows desktop (tests pass fakes for all four)
            if sys.platform != "win32":  # pragma: no cover - DUMBOO targets Windows
                raise RuntimeError("Laptop automation currently supports Windows only.")
            from automation.platform import screen, uia as uia_module
            from automation.platform.windows import WindowsDesktop
            desktop = WindowsDesktop()
            uia = uia_module if uia_module.available() else None
            screen_capture = screen.capture
        self.desktop = desktop
        self.browser = browser or BrowserService(lambda: self.automation_settings.config.browser,
                                                 paths.data_dir / "browser_profile")
        self.automation_history = AutomationHistory(self.db, self.auth)
        services = AutomationServices(
            settings=self.automation_settings, desktop=desktop, processes=ProcessRunner(), monitor=self.monitor,
            browser=self.browser, memory=self.memory, history=self.automation_history, auth=self.auth, bus=self.bus,
            app_root=paths.root, backup_dir=paths.data_dir / "automation_backups", ai_generate=self._ai_generate,
            uia=uia, capture=screen_capture)
        registry = build_registry()
        policy = PolicyEngine(self.auth, registry, lambda: self.automation_settings.config)
        self.automation = AutomationController(self.bus, self.auth, registry, policy, services)
        self.automation_assistant = AutomationAssistant(self.bus, self.auth, self.automation, Planner(registry),
                                                        self.memory_assistant, speak=self._announce)
        from automation.admin import AutomationAdmin
        self.automation_admin = AutomationAdmin(self.auth, self.automation_settings, self.memory)
        self.resource_alerts = ResourceAlerts(self.bus, self.monitor, lambda: self.automation_settings.config.monitor)
        # The STOP button also stops running automation (immediate cancellation).
        self.bus.subscribe(Events.STOP_SPEAKING, lambda _n, p: self.automation.emergency_stop(p.get("source", "ui"))
                           if p.get("source") == "ui" and self.automation.busy() else None)

    # -- web research (Module 5) ----------------------------------------------------------------
    def research_config(self):
        from research.config import ResearchConfig
        return ResearchConfig.from_settings(self.settings.section("research"), self.environ)

    def _build_research(self, paths: AppPaths, providers, client, resolver, browser) -> None:
        import socket

        from automation.platform.browser import BrowserService
        from research.assistant import ResearchAssistant
        from research.history_repository import ResearchHistory
        from research.http import default_client
        from research.memory_context_service import MemoryContextService
        from research.providers.brave import BraveProvider
        from research.providers.browser_search import BrowserSearchProvider
        from research.providers.gdelt import GdeltProvider
        from research.providers.google_news import GoogleNewsProvider
        from research.providers.tavily import TavilyProvider
        from research.research_controller import ResearchController
        from research.source_retrieval_service import SourceRetrievalService
        from research.web_search_service import WebSearchService

        client = client or default_client
        # A separate, headless browser profile for research (never your normal browser's cookies or logins).
        self.research_browser = browser or BrowserService(
            lambda: {"channel": self.automation_settings.config.browser.get("channel", "msedge"), "headless": True,
                     "normal_user_agent": True, "navigation_timeout_s": 25}, paths.data_dir / "research_browser")
        if providers is None:
            cfg = self.research_config
            providers = {"tavily": TavilyProvider(cfg().tavily_key, client), "brave": BraveProvider(cfg().brave_key, client),
                         "google_news": GoogleNewsProvider(client), "gdelt": GdeltProvider(client),
                         "browser": BrowserSearchProvider(self.research_browser)}
        search = WebSearchService(self.research_config, providers)
        retrieval = SourceRetrievalService(self.research_config, self.research_browser, client,
                                           resolver or socket.getaddrinfo)
        self.research_history = ResearchHistory(self.db, self.auth)
        self.research = ResearchController(self.bus, self.auth, self.research_config, search, retrieval,
                                           self.research_history,
                                           MemoryContextService(self.memory, self.auth, self.bus), self._ai_generate)
        self.research_assistant = ResearchAssistant(self.auth, self.research, self.automation_assistant, self.automation,
                                                    recent_turns=lambda: self.conversation.context.recent(3),
                                                    config=self.research_config)
        # The STOP button also cancels research in progress.
        self.bus.subscribe(Events.STOP_SPEAKING, lambda _n, p: self.research.cancel() if p.get("source") == "ui"
                           else None)

    def _ai_generate(self, system_prompt: str, text: str, images: tuple = (), max_output_tokens: int | None = None) -> str:
        return self.conversation.generate_once(system_prompt, text, images=tuple(images),
                                               max_output_tokens=max_output_tokens)

    # -- security guardian (Module 7) ------------------------------------------------------------
    def guardian_config(self):
        from guardian.config import GuardianConfig

        return GuardianConfig.from_settings(self.settings.section("security"), self.environ)

    def _build_guardian(self) -> None:
        """Self-contained Module 7: one service, one hook in front of the chain, STOP cancels scans."""
        from guardian.assistant import GuardianAssistant
        from guardian.service import GuardianService

        self.guardian = GuardianService(self.db, self.auth, self.guardian_config, self.paths.data_dir,
                                        **self._guardian_overrides)
        self.guardian_assistant = GuardianAssistant(self.agenda_assistant, self.auth, self.guardian, self.profile,
                                                    generate=self._ai_generate)
        self.guardian_assistant.memory = self.memory
        self.bus.subscribe(Events.STOP_SPEAKING, lambda _n, p: self.guardian.cancel() if p.get("source") == "ui"
                           else None)

    def _greet(self, _name: str, payload: dict) -> None:
        """After any sign-in: greet the user (first time: DUMBOO introduces itself). ACCOUNT page can turn it off."""
        from ai.greeting import build_greeting

        session = self.accounts.session
        if session is None:
            return
        try:
            prefs = self.profile.get(session)
            if prefs.get("greeting", "on") != "on":
                return
            now = datetime.now()
            reminders = [e for e in self.schedule.today(session)
                         if e.reminder_minutes is not None and e.reminded_at is None and e.starts_at > now]
            first = prefs.get("introduced") != "yes"
            text, speech = build_greeting(prefs.get("title") or payload.get("display_name", ""), now, first,
                                          payload.get("method", "password"), len(reminders))
            if first:
                self.profile.set(session, "introduced", "yes")
        except Exception:
            log.exception("Greeting failed")
            return
        self.bus.publish(Events.GREETING, {"text": text, "speech": speech, "method": payload.get("method")})
        self._announce(speech)

    def _on_reminder_due(self, _name: str, payload: dict) -> None:
        """Speak a due reminder (the dashboard shows it too) and remember it for "snooze" / "done"."""
        kind = payload.get("kind", "event")
        item_id = payload.get("task_id") if kind == "task" else payload.get("event_id")
        session = self.accounts.session
        if session is not None and item_id is not None:
            try:
                user = self.auth.require(session)
                self.agenda_assistant.reminder_fired(user.id, kind, item_id, payload["title"])
            except Exception:
                pass
        spoken = self._announce(reminder_speech(payload, datetime.now()), hold=True)
        log.info("Reminder due (%s%s): %s", kind, ", missed" if payload.get("missed") else "", spoken)

    def _announce(self, text: str, hold: bool = False) -> None:
        """Speak a background result. ``hold``: if DUMBOO is busy talking, say it as soon as it's free."""
        if self.settings.get("dashboard", "text_only") or not self.tts.available or not self._user_voice_on():
            return "shown only (voice is off)"
        if self.state.state in (AssistantState.IDLE, AssistantState.SLEEPING):
            self.tts.speak(text)
            return "spoken"
        if hold:
            self._held_announcements.append(text)
            return "will be spoken when DUMBOO is free"
        return "not spoken (busy)"

    def _speak_held_announcements(self, _name: str, payload: dict) -> None:
        if self._held_announcements and payload.get("new") in ("IDLE", "SLEEPING"):
            held, self._held_announcements = self._held_announcements, []
            self._announce(" ".join(held))

    # -- wake word ------------------------------------------------------------------
    def _on_wake(self, engine: str, score: float, followup: str) -> None:
        self.bus.publish(Events.WAKE_DETECTED, {"engine": engine, "score": score, "followup": followup,
                                                "phrase": self.settings.get("wake_word", "phrase")})

    def wake_listener_running(self) -> bool:
        """True only if audio is actually flowing into a loaded wake engine."""
        return self.pipeline.running and self.pipeline.mic_ok and self.wake.loaded and not self.mic.muted

    def publish_wake_status(self) -> None:
        self.bus.publish(Events.WAKE_SERVICE_STATUS, {
            "running": self.wake_listener_running(), "engine": self.wake.engine_name, "detail": self.wake.detail,
        })

    # -- AI configuration ------------------------------------------------------------------
    def ai_config(self) -> AIConfig:
        return AIConfig.from_settings(self.settings.section("ai"), self.environ)

    def _apply_env_overrides(self) -> list[str]:
        """AI_PROVIDER, GEMINI_MODEL … from the environment/.env win for this run (not saved)."""
        values, warnings = env_overrides(self.environ)
        self.env_overrides_applied: list[str] = []
        for key, value in values.items():
            try:
                self.settings.override_for_session("ai", key, value)
                self.env_overrides_applied.append(f"ai.{key}")
                log.info("AI setting %s taken from the environment", key)
            except Exception as exc:
                warnings.append(f"environment value for ai.{key} ignored: {exc}")
        for warning in warnings:
            log.warning(warning)
        return warnings

    def reload_ai_keys(self) -> None:
        """Call after an API key was saved from the dashboard."""
        self.conversation.reconfigure(self.ai_config())

    def _ai_ok(self) -> bool:
        try:
            self.ai_config().validate()
        except Exception:
            return False
        return self.conversation.last_error is None or self.conversation.last_error.kind in ("cancelled", "empty")

    # -- status for the System panel and diagnostics -------------------------------------
    def service_status(self) -> dict[str, dict]:
        devices = audio_devices.availability()
        voice = self._voice_enabled
        return {
            "microphone": {"ok": bool(devices["microphone"]) and (self.pipeline.mic_ok or not voice),
                           "detail": ("open" if self.pipeline.mic_ok else (self.mic.last_error or "closed"))
                           if voice else ("available" if devices["microphone"] else "not found") + " (text-only mode)"},
            "speaker": {"ok": bool(devices["speaker"]), "detail": "available" if devices["speaker"] else "not found"},
            "wake_word": {"ok": self.wake_listener_running(),
                          "detail": f"{self.wake.engine_name}: {self.wake.detail}" if voice else "off (text-only mode)"},
            "vad": {"ok": voice and self.pipeline.running, "detail": getattr(self.vad, "engine", "unknown")},
            "stt": {"ok": self.stt.ready, "detail": self.stt.detail},
            "tts": {"ok": self.tts.ready, "detail": self.tts.detail},
            "ai": {"ok": self._ai_ok(), "detail": self.ai_config().describe()
                   + (f" · last error: {self.conversation.last_error.kind}" if self.conversation.last_error else "")},
            "database": {"ok": True, "detail": f"v{self.db.version} · {self.db.size_bytes() / 1048576:.1f} MB · "
                                               f"search {'FTS5' if self.db.fts_enabled else 'LIKE'}"},
            "account": {"ok": self.accounts.signed_in,
                        "detail": (f"signed in: {self.accounts.session.username}" if self.accounts.signed_in
                                   else ("owner setup needed" if self.auth.needs_owner_setup() else "signed out"))},
            "reminders": {"ok": self.reminders.running, "detail": "running" if self.reminders.running else "stopped"},
            "automation": {"ok": self.automation.running, "detail": self._automation_detail()},
            "research": {"ok": bool(self.research.search_service.status()["configured"]),
                         "detail": self.research_config().describe()},
        }

    def _automation_detail(self) -> str:
        config = self.automation_settings.config
        installed = sum(1 for a in config.applications.values() if a.resolve() is not None or a.uri)
        return (f"{'running' if self.automation.running else 'stopped'} · {len(self.automation.registry.names())} "
                f"actions · {installed}/{len(config.applications)} approved apps installed · "
                f"{len(config.permitted)} permitted folders")

    # -- settings ------------------------------------------------------------------------
    def _on_settings_changed(self, s: dict, changed: list[str]) -> None:
        sections = {key.split(".")[0] for key in changed}
        log.info("Settings changed: %s", ", ".join(changed))
        if "audio.input_device" in changed and self.pipeline.running:
            self.pipeline.restart_mic(s["audio"]["input_device"])
        if "audio.output_device" in changed:
            self.player.device_name = s["audio"]["output_device"]
        self.suppressor.enabled = s["audio"]["noise_suppression"]
        self.suppressor.strength = s["audio"]["noise_suppression_strength"]
        if "vad" in sections:
            self.pipeline.update_segmenter(SegmenterConfig.from_settings(s["vad"]))
        if "interruption" in sections:
            self.pipeline.interruption = InterruptionDetector(InterruptionConfig.from_settings(s["interruption"]))
            self.pipeline.interruption_enabled = s["interruption"]["enabled"]
        if "wake_word" in sections or "vad.threshold" in changed:
            if self._voice_enabled:
                self.wake.reconfigure(s["wake_word"], s["vad"]["threshold"],
                                      self.paths.resolve(s["wake_word"]["model_path"]))
            self.publish_wake_status()
        if "stt" in sections:
            self.stt.configure(s["stt"])
            self.stt.reload()
        if "ai" in sections:
            self.conversation.reconfigure(self.ai_config())
        if "tts" in sections:
            self.tts.reconfigure(self.paths.resolve(s["tts"]["voice_model"]), s["tts"]["speed"], s["tts"]["enabled"])
        self.bus.publish(Events.SETTINGS_CHANGED, {"settings": s, "changed": changed})


def run_gui(paths: AppPaths, *, show: bool = False, text_only: bool | None = None) -> int:
    """Start the core and the PySide6 dashboard. Returns the process exit code."""
    from dashboard.main_window import launch

    core = AssistantCore(paths, text_only=text_only)
    return launch(core, show=show)
