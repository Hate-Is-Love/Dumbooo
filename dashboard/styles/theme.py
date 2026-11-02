"""Colour palette and Qt style sheet.

Palette sampled from the reference HUD video: near-black teal background,
cyan line work, solid cyan label tags, pale cyan text and warm orange energy
accents inside the core.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

BG = "#070d10"
BG_PANEL = "#0b161b"
BG_PANEL_2 = "#0f1f25"
LINE = "#1c3a42"
LINE_BRIGHT = "#2a6670"
CYAN = "#3fe0e8"
CYAN_DIM = "#20b3bb"
PALE = "#a9dde2"
TEXT = "#cfeef1"
MUTED = "#5f8a92"
ORANGE = "#f08a3c"
AMBER = "#ffc078"
RED = "#e0475b"
GREEN = "#4fd8a0"
VIOLET = "#9b8cff"

MONO = "Cascadia Mono, Consolas, 'Courier New', monospace"
SANS = "Segoe UI, 'Inter', Arial, sans-serif"

# Core colours per assistant state: (primary line colour, energy colour)
STATE_COLORS: dict[str, tuple[str, str]] = {
    "SLEEPING": ("#2a5a62", "#1d3f45"),
    "WAKING": (CYAN, AMBER),
    "IDLE": (CYAN_DIM, ORANGE),
    "LISTENING": (CYAN, "#7ff6ff"),
    "PROCESSING": (VIOLET, CYAN),
    "STREAMING": (CYAN, AMBER),
    "SPEAKING": (CYAN, ORANGE),
    "INTERRUPTED": (AMBER, ORANGE),
    "ERROR": (RED, RED),
    "SHUTTING_DOWN": (MUTED, MUTED),
}


def qcolor(hex_color: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


STYLE_SHEET = f"""
* {{ font-family: {SANS}; color: {TEXT}; }}
QMainWindow, QWidget#Root, QStackedWidget#Root {{ background: {BG}; }}
QWidget {{ background: transparent; font-size: 13px; }}
QToolTip {{ background: {BG_PANEL_2}; color: {TEXT}; border: 1px solid {LINE_BRIGHT}; padding: 4px; }}

QLabel#HudLabel {{ font-family: {MONO}; color: {MUTED}; font-size: 11px; letter-spacing: 2px; }}
QLabel#HudValue {{ font-family: {MONO}; color: {PALE}; font-size: 13px; }}
QLabel#BigValue {{ font-family: {MONO}; color: {CYAN}; font-size: 30px; }}
QLabel#Title {{ font-family: {MONO}; color: {PALE}; font-size: 15px; letter-spacing: 6px; }}
QLabel#Tag {{ font-family: {MONO}; background: {CYAN}; color: {BG}; padding: 2px 8px; font-size: 11px; letter-spacing: 1px; }}
QLabel#TagMuted {{ font-family: {MONO}; background: {LINE}; color: {PALE}; padding: 2px 8px; font-size: 11px; }}
QLabel#Transcript {{ font-family: {MONO}; color: {PALE}; font-size: 14px; }}
QLabel#Banner {{ background: rgba(240,138,60,0.15); border: 1px solid {ORANGE}; color: {AMBER}; padding: 6px 10px; font-family: {MONO}; }}
QLabel#ErrorBanner {{ background: rgba(224,71,91,0.15); border: 1px solid {RED}; color: #ffb3bd; padding: 6px 10px; font-family: {MONO}; }}

QFrame#Panel {{ background: {BG_PANEL}; border: 1px solid {LINE}; }}
QFrame#TopBar {{ background: #050a0c; border-bottom: 1px solid {LINE_BRIGHT}; }}

QPushButton {{
    font-family: {MONO}; background: {BG_PANEL_2}; border: 1px solid {LINE_BRIGHT}; color: {PALE};
    padding: 7px 14px; letter-spacing: 1px;
}}
QPushButton:hover {{ border-color: {CYAN}; color: {CYAN}; }}
QPushButton:pressed {{ background: {LINE}; }}
QPushButton:disabled {{ color: #34555b; border-color: {LINE}; }}
QPushButton#Primary {{ background: {CYAN}; color: {BG}; border: 1px solid {CYAN}; }}
QPushButton#Primary:hover {{ background: #7ff6ff; }}
QPushButton#Danger {{ border-color: #6a2a33; color: #ff9aa7; }}
QPushButton#Danger:hover {{ border-color: {RED}; color: {RED}; }}
QPushButton#Emergency {{ background: rgba(224,71,91,0.22); border: 1px solid {RED}; color: #ffd0d6; font-weight: bold; }}
QPushButton#Emergency:hover {{ background: rgba(224,71,91,0.45); color: #ffffff; }}
QPushButton#Nav {{ background: transparent; border: none; border-bottom: 2px solid transparent; color: {MUTED};
    padding: 12px 4px; font-size: 11px; letter-spacing: 0.5px; }}
QPushButton#Nav:hover {{ color: {PALE}; }}
QPushButton#Nav:checked {{ color: {CYAN}; border-bottom: 2px solid {CYAN}; }}
QPushButton#IconButton {{ padding: 6px 10px; }}

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QTimeEdit {{
    background: #071114; border: 1px solid {LINE}; padding: 6px 8px; color: {TEXT};
    selection-background-color: {CYAN_DIM}; selection-color: {BG}; font-family: {MONO};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QDateEdit:focus, QTimeEdit:focus {{ border-color: {CYAN}; }}
QComboBox QAbstractItemView {{ background: {BG_PANEL_2}; border: 1px solid {LINE_BRIGHT}; selection-background-color: {LINE_BRIGHT}; }}
QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 18px; }}

QCheckBox {{ spacing: 8px; font-family: {MONO}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {LINE_BRIGHT}; background: #071114; }}
QCheckBox::indicator:checked {{ background: {CYAN}; border-color: {CYAN}; }}

QSlider::groove:horizontal {{ height: 4px; background: {LINE}; }}
QSlider::sub-page:horizontal {{ background: {CYAN_DIM}; }}
QSlider::handle:horizontal {{ width: 12px; margin: -6px 0; background: {CYAN}; }}

QListWidget, QTableWidget, QTreeWidget {{
    background: #071114; border: 1px solid {LINE}; alternate-background-color: #0a171b;
    gridline-color: {LINE}; font-family: {MONO}; outline: none;
}}
QListWidget::item, QTreeWidget::item {{ padding: 6px; border-bottom: 1px solid #0f2328; }}
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected {{ background: {LINE}; color: {CYAN}; }}
QHeaderView::section {{ background: {BG_PANEL_2}; color: {MUTED}; border: none; border-bottom: 1px solid {LINE_BRIGHT};
    padding: 6px; font-family: {MONO}; letter-spacing: 1px; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {LINE_BRIGHT}; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; }}
QScrollBar::handle:horizontal {{ background: {LINE_BRIGHT}; min-width: 30px; }}

QTabWidget::pane {{ border: 1px solid {LINE}; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 14px; font-family: {MONO}; letter-spacing: 2px; }}
QTabBar::tab:selected {{ color: {CYAN}; border-bottom: 2px solid {CYAN}; }}

QGroupBox {{ border: 1px solid {LINE}; margin-top: 18px; padding: 12px 10px 10px 10px; font-family: {MONO}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 6px; color: {CYAN}; letter-spacing: 3px; }}

QMessageBox {{ background: {BG_PANEL}; }}
QDialog {{ background: {BG_PANEL}; }}
QMenu {{ background: {BG_PANEL_2}; border: 1px solid {LINE_BRIGHT}; }}
QMenu::item {{ padding: 6px 18px; }}
QMenu::item:selected {{ background: {LINE}; color: {CYAN}; }}
QProgressBar {{ background: #071114; border: 1px solid {LINE}; height: 8px; text-align: center; font-size: 1px; }}
QProgressBar::chunk {{ background: {CYAN_DIM}; }}
"""
