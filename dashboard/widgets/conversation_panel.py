"""Conversation view: user and assistant messages for the current session.

AI answers are shown progressively while they stream in (with a blinking HUD
cursor), rendered as Markdown (raw HTML from the model is ignored), and kept
until the user clears the conversation. Failed answers get a RETRY button.
"""

from __future__ import annotations

import re
from datetime import datetime

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from dashboard.styles import theme
from dashboard.widgets.hud import HudPanel

MAX_MESSAGES = 200
CURSOR = "▮"

_STYLES = {
    "user":      (theme.LINE_BRIGHT, "#0d2328", theme.TEXT, "YOU"),
    "voice":     (theme.LINE_BRIGHT, "#0d2328", theme.TEXT, "YOU · VOICE"),
    "assistant": (theme.CYAN_DIM, "#08191d", theme.PALE, "DUMBOO"),
    "system":    (theme.ORANGE, "#1d140c", theme.AMBER, "SYSTEM"),
    "error":     (theme.RED, "#1f0d11", "#ffb3bd", "ERROR"),
}


_CODE = re.compile(r"(```[\s\S]*?(?:```|$)|`[^`\n]*`)")


def safe_markdown(text: str) -> str:
    """Markdown with raw HTML from the model neutralised (shown as text, never interpreted).

    Code spans/blocks are left untouched: Markdown never interprets HTML inside them,
    and escaping there would show "&lt;" literally.
    """
    parts = _CODE.split(text)
    return "".join(part if i % 2 else _ITALIC_UNDERSCORE.sub(r"*\1*", part.replace("<", "&lt;"))
                   for i, part in enumerate(parts))


# Qt's Markdown reader turns _text_ into *underline* (it looked like a link); answers mean italics.
# Underscores inside words and addresses (snake_case, a_b.html) are left alone.
_ITALIC_UNDERSCORE = re.compile(r"(?<![\w*\\])_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")


def markdown_html(text: str, font=None) -> str:
    """Markdown -> HTML through Qt's own Markdown reader (raw HTML neutralised first), links in theme cyan."""
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    if font is not None:
        doc.setDefaultFont(font)
    doc.setMarkdown(safe_markdown(text))
    return re.sub(r"color:\s*#0000ff", f"color:{theme.CYAN}", doc.toHtml(), flags=re.I)


class MessageBubble(QFrame):
    def __init__(self, kind: str, text: str, markdown: bool = False, header_extra: str = "") -> None:
        super().__init__()
        border, bg, fg, who = _STYLES[kind]
        self.kind = kind
        self._fg = fg
        self.setStyleSheet(
            f"MessageBubble {{ background: {bg}; border: 1px solid {border}; border-left: 3px solid {border}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(3)
        self.header = QLabel(f"{who}  ·  {datetime.now():%H:%M:%S}" + (f"  ·  {header_extra}" if header_extra else ""))
        self.header.setStyleSheet(f"color: {border}; font-family: Consolas; font-size: 10px; letter-spacing: 2px;")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet(f"color: {fg}; font-size: 13px;")
        palette = self.body.palette()  # citations/links readable on the dark theme
        palette.setColor(QPalette.ColorRole.Link, theme.qcolor(theme.CYAN))
        palette.setColor(QPalette.ColorRole.LinkVisited, theme.qcolor(theme.VIOLET))
        self.body.setPalette(palette)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.footer = QLabel()
        self.footer.setStyleSheet(f"color: {theme.MUTED}; font-family: Consolas; font-size: 10px;")
        self.footer.hide()
        # One-line labels must not force the chat wider than its panel (long model names, letter spacing).
        for label in (self.header, self.footer):
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(0)
        layout.addWidget(self.header)
        layout.addWidget(self.body)
        layout.addWidget(self.footer)
        self.set_text(text, markdown)

    def set_text(self, text: str, markdown: bool = False) -> None:
        if markdown:
            self.body.setTextFormat(Qt.TextFormat.RichText)
            self.body.setText(markdown_html(text, self.body.font()))
        else:
            self.body.setTextFormat(Qt.TextFormat.PlainText)
            self.body.setText(text)

    def set_footer(self, text: str) -> None:
        self.footer.setText(text)
        self.footer.setVisible(bool(text))


class AnswerBubble(MessageBubble):
    """An AI answer that grows while it streams in."""

    def __init__(self, seq: int, label: str) -> None:
        super().__init__("assistant", "", header_extra=label)
        self.seq = seq
        self.text = ""
        self.done = False
        self._blink = True
        self._dots = 0
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(120)  # also throttles re-rendering to ~8 per second
        self._render()

    def update_text(self, text: str) -> None:
        self.text = text
        self._dirty = True

    def _tick(self) -> None:
        self._dots = (self._dots + 1) % 4
        if self._dots % 2 == 0:
            self._blink = not self._blink
        self._render()

    def _render(self) -> None:
        if self.done:
            return
        if not self.text:
            self.body.setTextFormat(Qt.TextFormat.PlainText)
            self.body.setText("analysing" + "." * self._dots)
            self.body.setStyleSheet(f"color: {theme.MUTED}; font-style: italic;")
            return
        self.body.setStyleSheet(f"color: {self._fg}; font-size: 13px;")
        self.set_text(self.text + (" " + CURSOR if self._blink else ""), markdown=True)
        self._dirty = False

    def finish(self, text: str, footer: str = "") -> None:
        self.done = True
        self._timer.stop()
        self.text = text
        self.body.setStyleSheet(f"color: {self._fg}; font-size: 13px;")
        self.set_text(text, markdown=True)
        self.set_footer(footer)

    def stop(self, note: str) -> None:
        self.done = True
        self._timer.stop()
        if self.text:
            self.body.setStyleSheet(f"color: {self._fg}; font-size: 13px;")
            self.set_text(self.text, markdown=True)
        else:
            self.body.setStyleSheet(f"color: {theme.MUTED}; font-style: italic;")
            self.set_text("(no answer)")
        self.set_footer(note)


class ConversationPanel(HudPanel):
    send_requested = Signal(str)
    retry_requested = Signal()
    cleared = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Conversation // session", parent, tag="AI")
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self._list = QVBoxLayout(container)
        self._list.setContentsMargins(0, 0, 6, 0)
        self._list.setSpacing(8)
        self._list.addStretch(1)
        self._scroll.setWidget(container)
        self.body.addWidget(self._scroll, 1)

        self._answer: AnswerBubble | None = None
        self._last_submit: tuple[str, float] | None = None

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a message and press Enter…")
        self.input.setMaxLength(8000)
        self.input.returnPressed.connect(self._submit)
        self.send_button = QPushButton("SEND")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self._submit)
        self.clear_button = QPushButton("NEW CHAT")
        self.clear_button.setToolTip("Start a new conversation (the current one stays saved in HISTORY)")
        self.clear_button.clicked.connect(self._confirm_clear)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        row.addWidget(self.clear_button)
        self.body.addLayout(row)

    # -- header tag ------------------------------------------------------------------------
    def set_provider_tag(self, text: str, ok: bool = True) -> None:
        for label in self.findChildren(QLabel):
            if label.objectName() in ("TagMuted", "Tag"):
                label.setText(text)
                label.setObjectName("TagMuted" if ok else "ErrorTag")
                label.setStyleSheet("" if ok else f"background: {theme.RED}; color: white; padding: 2px 8px;"
                                                  " font-family: Consolas; font-size: 11px;")
                break

    # -- input --------------------------------------------------------------------------------
    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        now = datetime.now().timestamp()
        if self._last_submit and self._last_submit[0] == text and now - self._last_submit[1] < 1.0:
            return  # double Enter / double click
        self._last_submit = (text, now)
        self.send_requested.emit(text)

    def accept_submission(self) -> None:
        self.input.clear()

    # -- messages -----------------------------------------------------------------------------
    def _insert(self, widget: QWidget) -> None:
        self._list.insertWidget(self._list.count() - 1, widget)
        self._fade_in(widget)
        while self._list.count() - 1 > MAX_MESSAGES:
            old = self._list.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        QTimer.singleShot(30, self._scroll_to_bottom)

    def add_message(self, kind: str, text: str, markdown: bool = False) -> MessageBubble:
        bubble = MessageBubble(kind, text, markdown)
        self._insert(bubble)
        return bubble

    def start_answer(self, seq: int, label: str) -> AnswerBubble:
        if self._answer is not None and not self._answer.done:
            self._answer.stop("stopped — replaced by your new message")
        self._answer = AnswerBubble(seq, label)
        self._insert(self._answer)
        return self._answer

    def _current(self, seq: int) -> AnswerBubble | None:
        answer = self._answer
        return answer if answer is not None and answer.seq == seq and not answer.done else None

    def update_answer(self, seq: int, text: str) -> None:
        answer = self._current(seq)
        if answer is not None:
            at_bottom = self._at_bottom()
            answer.update_text(text)
            if at_bottom:
                QTimer.singleShot(130, self._scroll_to_bottom)

    def finish_answer(self, seq: int, text: str, footer: str) -> None:
        answer = self._current(seq)
        if answer is None:
            answer = self.start_answer(seq, "")
        answer.finish(text, footer)
        QTimer.singleShot(30, self._scroll_to_bottom)

    def stop_answer(self, seq: int | None, note: str) -> None:
        answer = self._answer
        if answer is not None and not answer.done and (seq is None or answer.seq == seq):
            answer.stop(note)

    def fail_answer(self, seq: int, message: str) -> None:
        answer = self._current(seq)
        if answer is not None:
            answer.stop("answer interrupted" if answer.text else "")
            if not answer.text:
                answer.hide()
        bubble = MessageBubble("error", message)
        retry = QPushButton("↻  RETRY")
        retry.setFixedWidth(110)
        retry.clicked.connect(lambda: (retry.setEnabled(False), self.retry_requested.emit()))
        bubble.layout().addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
        self._insert(bubble)

    def _fade_in(self, widget: QWidget) -> None:
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", widget)
        anim.setDuration(260)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # Remove the effect afterwards; idle graphics effects cost repaint time.
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def _at_bottom(self) -> bool:
        bar = self._scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 40

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def message_count(self) -> int:
        return sum(1 for i in range(self._list.count())
                   if isinstance(self._list.itemAt(i).widget(), MessageBubble))

    def answers(self) -> list[AnswerBubble]:
        return [self._list.itemAt(i).widget() for i in range(self._list.count())
                if isinstance(self._list.itemAt(i).widget(), AnswerBubble)]

    def _confirm_clear(self) -> None:
        if self.message_count() == 0:
            return
        answer = QMessageBox.question(self, "New conversation",
                                      "Start a new conversation? The current one stays saved in HISTORY, and "
                                      "long-term memories are kept.")
        if answer == QMessageBox.StandardButton.Yes:
            self.clear()

    def clear(self, emit: bool = True) -> None:
        self._answer = None
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        if emit:
            self.cleared.emit()
