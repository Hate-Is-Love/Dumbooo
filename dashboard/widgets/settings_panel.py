"""Settings page. Values are validated by ``SettingsService`` before saving."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

from audio import audio_devices
from dashboard.dashboard_controller import DashboardController
from dashboard.styles import theme
from ai.config import KEY_HELP, MODEL_SUGGESTIONS, PROVIDER_LABELS, api_keys_for
from services.settings_service import WHISPER_MODELS, SettingsError
from utils.env import mask

DEFAULT_DEVICE = "System default"


class PercentSlider(QWidget):
    """Slider for a 0..1 (or custom range) float with a live value label."""

    def __init__(self, minimum: float = 0.0, maximum: float = 1.0) -> None:
        super().__init__()
        self._min, self._max = minimum, maximum
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.label = QLabel()
        self.label.setFixedWidth(44)
        self.label.setObjectName("HudValue")
        self.slider.valueChanged.connect(lambda _v: self.label.setText(f"{self.value():.2f}"))
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)

    def value(self) -> float:
        return round(self._min + (self._max - self._min) * self.slider.value() / 100, 2)

    def set_value(self, v: float) -> None:
        self.slider.setValue(int(round((v - self._min) / (self._max - self._min) * 100)))
        self.label.setText(f"{self.value():.2f}")


def _spin(minimum: int, maximum: int, suffix: str = "", step: int = 1) -> QSpinBox:
    s = QSpinBox()
    s.setRange(minimum, maximum)
    s.setSingleStep(step)
    s.setSuffix(suffix)
    return s


def _dspin(minimum: float, maximum: float, suffix: str = "", step: float = 0.5) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(minimum, maximum)
    s.setSingleStep(step)
    s.setDecimals(1)
    s.setSuffix(suffix)
    return s


class SettingsPanel(QWidget):
    saved = Signal(list)

    def __init__(self, controller: DashboardController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._c = controller
        self._settings = controller.core.settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # Audio devices
        audio = QGroupBox("AUDIO DEVICES")
        f = QFormLayout(audio)
        self.mic = QComboBox()
        self.speaker = QComboBox()
        refresh = QPushButton("REFRESH DEVICES")
        refresh.clicked.connect(self._load_devices)
        self.ns = QCheckBox("Noise suppression")
        self.ns_strength = PercentSlider()
        f.addRow("Microphone", self.mic)
        f.addRow("Speaker", self.speaker)
        f.addRow("", refresh)
        f.addRow(self.ns)
        f.addRow("Suppression strength", self.ns_strength)
        grid.addWidget(audio, 0, 0)

        # Wake word
        wake = QGroupBox("WAKE WORD")
        f = QFormLayout(wake)
        self.wake_enabled = QCheckBox("Listen for the wake phrase")
        self.wake_phrase = QLineEdit()
        self.wake_engine = QComboBox()
        self.wake_engine.addItems(["auto", "openwakeword", "whisper_spotter"])
        self.wake_model = QLineEdit()
        self.wake_sensitivity = PercentSlider(0.05, 0.95)
        self.wake_cooldown = _dspin(0.5, 10, " s")
        note = QLabel("auto = custom openWakeWord model if the file exists, otherwise the Whisper phrase spotter. "
                      "Higher sensitivity wakes more easily but also falsely more often.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        f.addRow(self.wake_enabled)
        f.addRow("Phrase", self.wake_phrase)
        f.addRow("Engine", self.wake_engine)
        f.addRow("Custom model", self.wake_model)
        f.addRow("Sensitivity", self.wake_sensitivity)
        f.addRow("Cooldown", self.wake_cooldown)
        f.addRow(note)
        grid.addWidget(wake, 0, 1)

        # VAD
        vad = QGroupBox("VOICE ACTIVITY DETECTION")
        f = QFormLayout(vad)
        self.vad_threshold = PercentSlider(0.1, 0.95)
        self.end_silence = _spin(200, 3000, " ms", 50)
        self.pre_roll = _spin(0, 1500, " ms", 50)
        self.min_speech = _spin(100, 2000, " ms", 50)
        self.max_record = _dspin(2, 60, " s")
        self.listen_timeout = _dspin(2, 60, " s")
        f.addRow("Speech threshold", self.vad_threshold)
        f.addRow("End-of-speech silence", self.end_silence)
        f.addRow("Pre-roll", self.pre_roll)
        f.addRow("Minimum speech", self.min_speech)
        f.addRow("Maximum recording", self.max_record)
        f.addRow("Give up listening after", self.listen_timeout)
        grid.addWidget(vad, 1, 0)

        # Interruption
        intr = QGroupBox("INTERRUPTION (BARGE-IN)")
        f = QFormLayout(intr)
        self.intr_enabled = QCheckBox("Stop speaking when I talk over DUMBOO")
        self.intr_threshold = PercentSlider(0.3, 0.98)
        self.intr_min = _spin(100, 2000, " ms", 50)
        self.intr_margin = _dspin(0, 20, " dB")
        f.addRow(self.intr_enabled)
        f.addRow("Speech confidence", self.intr_threshold)
        f.addRow("Must talk for", self.intr_min)
        f.addRow("Louder than echo by", self.intr_margin)
        grid.addWidget(intr, 1, 1)

        # Speech engines
        speech = QGroupBox("SPEECH ENGINES")
        f = QFormLayout(speech)
        self.stt_enabled = QCheckBox("Speech-to-text enabled")
        self.stt_model = QComboBox()
        self.stt_model.addItems(WHISPER_MODELS)
        self.stt_language = QLineEdit()
        self.stt_language.setMaxLength(5)
        self.stt_device = QComboBox()
        self.stt_device.addItems(["cpu", "cuda", "auto"])
        self.stt_compute = QComboBox()
        self.stt_compute.addItems(["int8", "int8_float16", "float16", "float32", "default"])
        self.tts_enabled = QCheckBox("Voice output (TTS) enabled")
        self.tts_voice = QLineEdit()
        browse = QPushButton("BROWSE…")
        browse.clicked.connect(self._browse_voice)
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.tts_voice, 1)
        voice_row.addWidget(browse)
        self.tts_speed = _dspin(0.5, 2.0, "×", 0.1)
        f.addRow(self.stt_enabled)
        f.addRow("Whisper model", self.stt_model)
        f.addRow("Language code", self.stt_language)
        f.addRow("Device", self.stt_device)
        f.addRow("Compute type", self.stt_compute)
        f.addRow(self.tts_enabled)
        f.addRow("Piper voice (.onnx)", voice_row)
        f.addRow("Speaking speed", self.tts_speed)
        grid.addWidget(speech, 2, 0)

        # Dashboard
        dash = QGroupBox("DASHBOARD")
        f = QFormLayout(dash)
        self.start_mode = QComboBox()
        self.start_mode.addItems(["sleep", "visible"])
        self.auto_sleep = QCheckBox("Go to sleep automatically when idle")
        self.sleep_timeout = _spin(10, 3600, " s", 10)
        self.intensity = PercentSlider()
        self.blob_enabled = QCheckBox("Show the 3D voice blob in the core (click it to talk)")
        self.blob_sensitivity = PercentSlider(0.0, 3.0)
        self.blob_sensitivity.setToolTip("How strongly the blob reacts to voices: 0 = calm, 1 = normal, 3 = very lively. "
                                         "Also on the core's right-click menu.")
        self.text_only = QCheckBox("Text-only mode (no microphone; takes effect after restart)")
        f.addRow("On startup", self.start_mode)
        f.addRow(self.auto_sleep)
        f.addRow("Sleep after", self.sleep_timeout)
        f.addRow("Animation intensity", self.intensity)
        f.addRow(self.blob_enabled)
        f.addRow("Blob sensitivity", self.blob_sensitivity)
        f.addRow(self.text_only)
        grid.addWidget(dash, 2, 1)
        grid.addWidget(self._build_ai_group(), 3, 0, 1, 2)

        buttons = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        save = QPushButton("SAVE SETTINGS")
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        revert = QPushButton("REVERT")
        revert.clicked.connect(self.load)
        defaults = QPushButton("RESTORE DEFAULTS")
        defaults.setObjectName("Danger")
        defaults.clicked.connect(self._restore_defaults)
        buttons.addWidget(self.status, 1)
        buttons.addWidget(defaults)
        buttons.addWidget(revert)
        buttons.addWidget(save)
        outer.addLayout(buttons)
        self.load()

    # -- AI ------------------------------------------------------------------------------------
    def _build_ai_group(self) -> QGroupBox:
        box = QGroupBox("AI CONVERSATION")
        outer = QHBoxLayout(box)
        left, right = QFormLayout(), QFormLayout()
        outer.addLayout(left, 1)
        outer.addSpacing(18)
        outer.addLayout(right, 1)

        self.ai_provider = QComboBox()
        for key, label in PROVIDER_LABELS.items():
            self.ai_provider.addItem(label, key)
        self.gemini_model = QComboBox()
        self.gemini_model.setEditable(True)
        self.gemini_model.addItems(MODEL_SUGGESTIONS["gemini"])
        self.groq_model = QComboBox()
        self.groq_model.setEditable(True)
        self.groq_model.addItems(MODEL_SUGGESTIONS["groq"])
        self.groq_model.setToolTip("Groq answers fastest (first words in well under a second). "
                                   "gpt-oss-120b: best quality; gpt-oss-20b: even faster.")
        self.groq_effort = QComboBox()
        self.groq_effort.addItems(["", "low", "medium", "high"])
        self.groq_effort.setToolTip("Reasoning effort for Groq's reasoning models. 'low' keeps conversation quick.")
        self.openai_model = QComboBox()
        self.openai_model.setEditable(True)
        self.openai_model.addItems(MODEL_SUGGESTIONS["openai"])
        self.thinking = QComboBox()
        self.thinking.addItems(["", "minimal", "low", "medium", "high"])
        self.thinking.setToolTip("Gemini thinking level. Empty = the model's default. 'low' answers faster.")
        self.reasoning = QComboBox()
        self.reasoning.addItems(["", "minimal", "low", "medium", "high"])
        self.reasoning.setToolTip("OpenAI reasoning effort for reasoning models. Empty = not sent.")
        self.max_tokens = _spin(32, 16384, " tokens", 64)
        self.temp_default = QCheckBox("Use the model's default temperature")
        self.temperature = _dspin(0.0, 2.0, "", 0.1)
        self.temp_default.toggled.connect(lambda on: self.temperature.setEnabled(not on))
        self.streaming = QCheckBox("Stream answers (show text as it arrives)")
        self.ai_timeout = _dspin(5, 180, " s", 5)
        self.context_turns = _spin(0, 50, " turns")
        self.concise = QCheckBox("Short answers first (recommended)")
        self.auto_switch = QCheckBox("Switch automatically if a provider or key has a problem")
        self.auto_switch.setToolTip("Quota used up, rate limit, rejected key, network trouble or a slow answer: "
                                    "DUMBOO continues with the next provider/key that has a key. "
                                    "Never used to get around a safety block.")
        self.fallback_order = QLineEdit()
        self.fallback_order.setPlaceholderText("groq,gemini,openai")
        self.fallback_order.setToolTip("Order of the backup providers after the chosen one (comma-separated).")
        self.switch_timeout = _dspin(3, 60, " s", 1)
        self.switch_timeout.setToolTip("How long to wait for a provider before trying the next one.")
        note = QLabel("The chosen provider answers first. With automatic switching, every provider and extra key "
                      "you saved takes over when needed. Keys stay in .env on this PC.")
        note.setWordWrap(True)
        note.setObjectName("HudLabel")
        left.addRow("Provider", self.ai_provider)
        left.addRow("Groq model", self.groq_model)
        left.addRow("Groq reasoning", self.groq_effort)
        left.addRow("Gemini model", self.gemini_model)
        left.addRow("Gemini thinking", self.thinking)
        left.addRow("OpenAI model", self.openai_model)
        left.addRow("OpenAI reasoning", self.reasoning)
        left.addRow("Max output tokens", self.max_tokens)
        left.addRow(self.temp_default)
        left.addRow("Temperature", self.temperature)
        left.addRow(self.auto_switch)
        left.addRow("Backup order", self.fallback_order)
        left.addRow("Switch after", self.switch_timeout)
        left.addRow(note)

        right.addRow(self.streaming)
        right.addRow(self.concise)
        right.addRow("Request timeout", self.ai_timeout)
        right.addRow("Remember last", self.context_turns)
        self.key_status: dict[str, QLabel] = {}
        self.key_inputs: dict[str, QLineEdit] = {}
        for provider in ("groq", "gemini", "openai"):
            field = QLineEdit()
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setPlaceholderText("paste a key, then SAVE (replace) or ADD (extra)")
            field.setToolTip(KEY_HELP[provider])
            save = QPushButton("SAVE KEY")
            save.setToolTip("Replace the main key")
            save.clicked.connect(lambda _=False, pr=provider: self._save_key(pr))
            add = QPushButton("ADD KEY")
            add.setToolTip("Keep the current key(s) and add this one as an extra key (used when the others "
                           "hit a limit)")
            add.clicked.connect(lambda _=False, pr=provider: self._save_key(pr, extra=True))
            status = QLabel()
            status.setObjectName("HudLabel")
            row = QHBoxLayout()
            row.addWidget(field, 1)
            row.addWidget(save)
            row.addWidget(add)
            right.addRow(f"{PROVIDER_LABELS[provider]} key", row)
            right.addRow("", status)
            self.key_inputs[provider], self.key_status[provider] = field, status
        test = QPushButton("TEST CONNECTION")
        test.setToolTip("Checks the key and model access without generating text (no generation quota used). "
                        "Save settings first.")
        test.clicked.connect(self._test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        right.addRow(test, self.test_result)
        self.env_note = QLabel("")
        self.env_note.setWordWrap(True)
        self.env_note.setStyleSheet(f"color: {theme.AMBER};")
        right.addRow(self.env_note)
        return box

    def _refresh_key_status(self) -> None:
        environ = self._c.core.environ
        for provider, label in self.key_status.items():
            keys = api_keys_for(provider, environ)
            if not keys:
                label.setText("No key yet — stored in .env when saved, never shown")
            else:
                label.setText(f"{len(keys)} key{'s' if len(keys) > 1 else ''}: " + ", ".join(mask(k) for k in keys)
                              + " — stored in .env, never shown")

    def _save_key(self, provider: str, extra: bool = False) -> None:
        key = self.key_inputs[provider].text().strip()
        if not key:
            self.key_status[provider].setText("Paste a key first.")
            return
        try:
            masked = self._c.save_api_key(provider, key, extra=extra)
        except Exception as exc:
            self.key_status[provider].setText(f"Not saved: {exc}")
            return
        self.key_inputs[provider].clear()  # the key is never kept in the form
        self._refresh_key_status()
        self.key_status[provider].setText(f"{'Extra key added' if extra else 'Key saved'} ({masked}) to .env. "
                                          + self.key_status[provider].text())

    def _test_connection(self) -> None:
        self.test_result.setStyleSheet(f"color: {theme.MUTED};")
        self.test_result.setText("Checking…")

        def show(status) -> None:
            self.test_result.setStyleSheet(f"color: {theme.GREEN if status.ok else theme.RED};")
            latency = f" ({status.latency_s:.1f} s)" if status.latency_s else ""
            self.test_result.setText(status.message + latency)

        self._c.test_ai_connection(show)

    # -- devices -----------------------------------------------------------------------------
    def _load_devices(self) -> None:
        s = self._settings.snapshot()["audio"]
        for combo, labels, current in (
            (self.mic, audio_devices.unique_labels(audio_devices.input_devices()), s["input_device"]),
            (self.speaker, audio_devices.unique_labels(audio_devices.output_devices()), s["output_device"]),
        ):
            combo.clear()
            combo.addItem(DEFAULT_DEVICE)
            combo.addItems(labels)
            if current and current not in labels:
                combo.addItem(f"{current} (not connected)", current)
            combo.setCurrentIndex(max(0, combo.findText(current or DEFAULT_DEVICE, Qt.MatchFlag.MatchStartsWith)))

    @staticmethod
    def _device_value(combo: QComboBox) -> str | None:
        if combo.currentText() == DEFAULT_DEVICE:
            return None
        return combo.currentData() or combo.currentText()

    def _browse_voice(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Piper voice", str(self._c.core.paths.models_dir),
                                              "Piper voice (*.onnx)")
        if path:
            self.tts_voice.setText(path)

    # -- load / save ---------------------------------------------------------------------------
    def load(self) -> None:
        s = self._settings.snapshot()
        self._load_devices()
        self.ns.setChecked(s["audio"]["noise_suppression"])
        self.ns_strength.set_value(s["audio"]["noise_suppression_strength"])
        w = s["wake_word"]
        self.wake_enabled.setChecked(w["enabled"])
        self.wake_phrase.setText(w["phrase"])
        self.wake_engine.setCurrentText(w["engine"])
        self.wake_model.setText(w["model_path"])
        self.wake_sensitivity.set_value(w["sensitivity"])
        self.wake_cooldown.setValue(w["cooldown_s"])
        v = s["vad"]
        self.vad_threshold.set_value(v["threshold"])
        self.end_silence.setValue(v["end_silence_ms"])
        self.pre_roll.setValue(v["pre_roll_ms"])
        self.min_speech.setValue(v["min_speech_ms"])
        self.max_record.setValue(v["max_record_s"])
        self.listen_timeout.setValue(v["listen_timeout_s"])
        i = s["interruption"]
        self.intr_enabled.setChecked(i["enabled"])
        self.intr_threshold.set_value(i["threshold"])
        self.intr_min.setValue(i["min_speech_ms"])
        self.intr_margin.setValue(i["echo_margin_db"])
        st = s["stt"]
        self.stt_enabled.setChecked(st["enabled"])
        self.stt_model.setCurrentText(st["model"])
        self.stt_language.setText(st["language"])
        self.stt_device.setCurrentText(st["device"])
        self.stt_compute.setCurrentText(st["compute_type"])
        t = s["tts"]
        self.tts_enabled.setChecked(t["enabled"])
        self.tts_voice.setText(t["voice_model"])
        self.tts_speed.setValue(t["speed"])
        d = s["dashboard"]
        self.start_mode.setCurrentText(d["start_mode"])
        self.auto_sleep.setChecked(d["auto_sleep"])
        self.sleep_timeout.setValue(d["sleep_timeout_s"])
        self.intensity.set_value(d["animation_intensity"])
        self.blob_enabled.setChecked(d["blob_enabled"])
        self.blob_sensitivity.set_value(d["blob_sensitivity"])
        self.text_only.setChecked(d["text_only"])
        ai = s["ai"]
        self.ai_provider.setCurrentIndex(max(0, self.ai_provider.findData(ai["provider"])))
        self.gemini_model.setCurrentText(ai["gemini_model"])
        self.groq_model.setCurrentText(ai["groq_model"])
        self.groq_effort.setCurrentText(ai["groq_reasoning_effort"])
        self.auto_switch.setChecked(ai["auto_switch"])
        self.fallback_order.setText(ai["fallback_order"])
        self.switch_timeout.setValue(ai["switch_timeout_s"])
        self.openai_model.setCurrentText(ai["openai_model"])
        self.thinking.setCurrentText(ai["gemini_thinking_level"])
        self.reasoning.setCurrentText(ai["openai_reasoning_effort"])
        self.max_tokens.setValue(ai["max_output_tokens"])
        self.temp_default.setChecked(ai["temperature"] is None)
        self.temperature.setValue(ai["temperature"] if ai["temperature"] is not None else 1.0)
        self.temperature.setEnabled(ai["temperature"] is not None)
        self.streaming.setChecked(ai["streaming"])
        self.ai_timeout.setValue(ai["timeout_s"])
        self.context_turns.setValue(ai["context_turns"])
        self.concise.setChecked(ai["concise"])
        self._refresh_key_status()
        overridden = [w for w in getattr(self._c.core, "env_overrides_applied", [])]
        self.env_note.setText(("Set by .env / environment for this run: " + ", ".join(overridden)) if overridden else "")
        self.status.setText("")

    def collect(self) -> dict:
        return {
            "audio": {"input_device": self._device_value(self.mic), "output_device": self._device_value(self.speaker),
                      "noise_suppression": self.ns.isChecked(), "noise_suppression_strength": self.ns_strength.value()},
            "wake_word": {"enabled": self.wake_enabled.isChecked(), "phrase": self.wake_phrase.text().strip().lower(),
                          "engine": self.wake_engine.currentText(), "model_path": self.wake_model.text().strip(),
                          "sensitivity": self.wake_sensitivity.value(), "cooldown_s": self.wake_cooldown.value()},
            "vad": {"threshold": self.vad_threshold.value(), "end_silence_ms": self.end_silence.value(),
                    "pre_roll_ms": self.pre_roll.value(), "min_speech_ms": self.min_speech.value(),
                    "max_record_s": self.max_record.value(), "listen_timeout_s": self.listen_timeout.value()},
            "interruption": {"enabled": self.intr_enabled.isChecked(), "threshold": self.intr_threshold.value(),
                             "min_speech_ms": self.intr_min.value(), "echo_margin_db": self.intr_margin.value()},
            "stt": {"enabled": self.stt_enabled.isChecked(), "model": self.stt_model.currentText(),
                    "language": self.stt_language.text().strip().lower(), "device": self.stt_device.currentText(),
                    "compute_type": self.stt_compute.currentText()},
            "tts": {"enabled": self.tts_enabled.isChecked(), "voice_model": self.tts_voice.text().strip(),
                    "speed": self.tts_speed.value()},
            "ai": {"provider": self.ai_provider.currentData(), "gemini_model": self.gemini_model.currentText().strip(),
                   "groq_model": self.groq_model.currentText().strip(),
                   "groq_reasoning_effort": self.groq_effort.currentText(),
                   "auto_switch": self.auto_switch.isChecked(),
                   "fallback_order": self.fallback_order.text().strip().lower() or "groq,gemini,openai",
                   "switch_timeout_s": self.switch_timeout.value(),
                   "openai_model": self.openai_model.currentText().strip(),
                   "gemini_thinking_level": self.thinking.currentText(),
                   "openai_reasoning_effort": self.reasoning.currentText(),
                   "max_output_tokens": self.max_tokens.value(),
                   "temperature": None if self.temp_default.isChecked() else round(self.temperature.value(), 2),
                   "streaming": self.streaming.isChecked(), "timeout_s": self.ai_timeout.value(),
                   "context_turns": self.context_turns.value(), "concise": self.concise.isChecked()},
            "dashboard": {"start_mode": self.start_mode.currentText(), "auto_sleep": self.auto_sleep.isChecked(),
                          "sleep_timeout_s": self.sleep_timeout.value(),
                          "animation_intensity": self.intensity.value(), "text_only": self.text_only.isChecked(),
                          "blob_enabled": self.blob_enabled.isChecked(),
                          "blob_sensitivity": self.blob_sensitivity.value()},
        }

    def _save(self) -> None:
        changes = self.collect()
        # Settings work (reloading models, reopening devices) can take a moment: run it off the UI thread.
        self.status.setText("Saving…")
        self._c.run_async(lambda: self._c.update_system_settings(changes), self._saved, self._failed)

    def _saved(self, changed: list[str]) -> None:
        if not changed:
            self.status.setStyleSheet(f"color: {theme.MUTED};")
            self.status.setText("No changes.")
            return
        self.status.setStyleSheet(f"color: {theme.GREEN};")
        note = "  Restart DUMBOO for text-only mode to take effect." if "dashboard.text_only" in changed else ""
        self.status.setText(f"Saved {len(changed)} setting(s).{note}")
        self.saved.emit(changed)

    def _failed(self, message: str) -> None:
        self.status.setStyleSheet(f"color: {theme.RED};")
        self.status.setText("Not saved: " + message)

    def _restore_defaults(self) -> None:
        if QMessageBox.question(self, "Restore defaults", "Reset every setting to its default value?") \
                == QMessageBox.StandardButton.Yes:
            self._c.run_async(lambda: self._c.update_system_settings(
                {k: v for k, v in self._settings.defaults().items() if k != "accounts"}),
                lambda ch: (self.load(), self._saved(ch)), self._failed)
