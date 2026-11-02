"""Turn a Markdown answer into clean text for text-to-speech.

The chat panel always shows the full answer. Only the *spoken* copy is
simplified: Markdown symbols are removed, links are read as their text (bare
URLs as just the site name), long code blocks are replaced by a short note,
and list items/table rows become separate sentences so the voice pauses
naturally. Nothing is reworded.

``SpeechChunker`` cuts a streamed answer into whole sentences, so TTS starts
speaking early without receiving every tiny token separately.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

CODE_NOTE = "I've put the code on screen."
LONG_CODE_LINES = 3
LONG_CODE_CHARS = 160

_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|$)", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()\]]+", re.I)
_HTML = re.compile(r"</?[a-zA-Z][^>]*>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_EMPHASIS = re.compile(r"(?<!\w)(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1(?!\w)")
_TRACEBACK = re.compile(r"Traceback \(most recent call last\):.*", re.S)

_SYMBOLS = {"→": ", ", "←": ", ", "≈": " about ", "≠": " not equal to ", "≤": " at most ",
            "≥": " at least ", "×": " times ", "÷": " divided by ", "&": " and ", "…": "...", "•": ""}


def _site_name(url: str) -> str:
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or "a link"


def _code_block(match: re.Match) -> str:
    code = match.group(1).strip()
    if not code:
        return " "
    if code.count("\n") + 1 >= LONG_CODE_LINES or len(code) > LONG_CODE_CHARS:
        return f"\n{CODE_NOTE}\n"
    return f"\n{code}\n"


def _sentence(line: str) -> str:
    line = line.strip()
    if line and line[-1] not in ".!?:;,":
        line += "."
    return line


_LATEX_CMDS = {r"\times": " times ", r"\cdot": " times ", r"\div": " divided by ", r"\pm": " plus or minus ",
               r"\approx": " about ", r"\neq": " not equal to ", r"\leq": " at most ", r"\geq": " at least ",
               r"\le": " at most ", r"\ge": " at least ", r"\pi": " pi ", r"\%": " percent", r"\,": " ",
               r"\left": "", r"\right": ""}
_LATEX_MATH = re.compile(r"\$\$(.+?)\$\$|\$(?=\S)([^$\n]+?)(?<=\S)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]", re.S)
_LATEX_FRAC = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_LATEX_SQRT = re.compile(r"\\sqrt\{([^{}]*)\}")
_LATEX_WRAP = re.compile(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}")


def _latex_to_words(match: re.Match) -> str:
    """Models sometimes answer maths in LaTeX ($15 \\times 8$); read it as words, not symbols."""
    expr = next(g for g in match.groups() if g is not None)
    expr = _LATEX_WRAP.sub(r"\1", expr)
    expr = _LATEX_FRAC.sub(r"\1 over \2", expr)
    expr = _LATEX_SQRT.sub(r"the square root of \1", expr)
    for command, word in sorted(_LATEX_CMDS.items(), key=lambda kv: -len(kv[0])):
        expr = expr.replace(command, word)
    expr = re.sub(r"\^\{?2\}?", " squared", expr)
    expr = re.sub(r"\^\{?([^{}\s]+)\}?", r" to the power of \1", expr)
    return re.sub(r"[{}\\]", "", expr)


def to_speech(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n")
    text = _TRACEBACK.sub(" ", text)          # never read stack traces aloud
    text = _LATEX_MATH.sub(_latex_to_words, text)
    text = _FENCE.sub(_code_block, text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(lambda m: _site_name(m.group(0)), text)
    text = _HTML.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _TABLE_SEP.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    for _ in range(2):  # nested emphasis like ***bold italic***
        text = _EMPHASIS.sub(r"\2", text)

    lines = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.count("|") >= 2:  # table row -> "a, b, c."
            line = ", ".join(c.strip() for c in line.strip("|").split("|") if c.strip())
        line = _BULLET.sub("", line)
        if line:
            lines.append(_sentence(line))  # every line/list item ends as a sentence -> natural pause
    text = " ".join(lines)

    for symbol, spoken in _SYMBOLS.items():
        text = text.replace(symbol, spoken)
    # Drop emoji and other pictographs; keep letters, digits, punctuation and currency.
    text = "".join(ch for ch in text if unicodedata.category(ch) != "So")
    text = re.sub(r"(?<=\w)_(?=\w)", " ", text)  # snake_case -> "snake case"
    text = re.sub(r"(?<=[\w)])\s*=\s*(?=[\w(])", " equals ", text)
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\.{2,}(?!\.)", "...", text)
    text = re.sub(r"([.!?])\.", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n+")


class SpeechChunker:
    """Collects streamed text and releases speakable sentences."""

    def __init__(self, min_chars: int = 24, max_chars: int = 220) -> None:
        self._buf = ""
        self._in_code = False
        self._code = ""
        self._min, self._max = min_chars, max_chars

    def push(self, delta: str) -> list[str]:
        out: list[str] = []
        self._buf += delta
        while True:
            if self._in_code:
                end = self._buf.find("```")
                if end < 0:
                    # Keep the last two characters: they may be the start of a split closing fence.
                    self._code += self._buf[:-2]
                    self._buf = self._buf[-2:]
                    return out
                self._code += self._buf[:end]
                self._buf = self._buf[end + 3:]
                self._in_code = False
                out.extend(self._flush_code())
                continue
            fence = self._buf.find("```")
            head = self._buf if fence < 0 else self._buf[:fence]
            pieces = self._split(head, final=fence >= 0)
            out.extend(pieces[0])
            if fence < 0:
                self._buf = pieces[1]
                return [s for s in out if s]
            # Start of a code block: speak what came before, then hold the code.
            newline = self._buf.find("\n", fence + 3)
            if newline < 0:  # language tag not complete yet; wait for the rest of the line
                self._buf = self._buf[fence:]
                return [s for s in out if s]
            self._buf = self._buf[newline + 1:]
            self._in_code = True
            self._code = ""

    def _flush_code(self) -> list[str]:
        code = self._code.strip()
        self._code = ""
        if not code:
            return []
        long = code.count("\n") + 1 >= LONG_CODE_LINES or len(code) > LONG_CODE_CHARS
        return [CODE_NOTE] if long else [to_speech(code)]

    def _split(self, text: str, final: bool) -> tuple[list[str], str]:
        ready: list[str] = []
        start = 0
        for match in _BOUNDARY.finditer(text):
            piece = text[start:match.end()]
            if len(piece.strip()) >= self._min or "\n" in match.group(0):
                ready.append(piece)
                start = match.end()
        rest = text[start:]
        if len(rest) > self._max:  # very long sentence: cut at the last comma or space
            cut = max(rest.rfind(", ", 0, self._max), rest.rfind(" ", 0, self._max))
            if cut > 0:
                ready.append(rest[:cut + 1])
                rest = rest[cut + 1:]
        if final and rest.strip():
            ready.append(rest)
            rest = ""
        spoken = [to_speech(p) for p in ready]
        return [s for s in spoken if s], rest

    def flush(self) -> list[str]:
        out: list[str] = []
        if self._in_code:
            out.extend(self._flush_code())
            self._in_code = False
        if self._buf.strip():
            spoken = to_speech(self._buf)
            if spoken:
                out.append(spoken)
        self._buf = ""
        return out
