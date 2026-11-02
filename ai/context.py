"""Short-term conversation context (in memory, for the current session only).

Truncation rule (predictable on purpose):

* Only completed turns (a user message plus DUMBOO's full answer) are stored.
  Cancelled or failed answers are not stored, so the model never sees half an
  answer.
* For each request, the newest stored turns are included, going backwards,
  until either ``max_turns`` turns or ``max_chars`` characters (including the
  new message) would be exceeded. A turn is never split; older turns are
  dropped first.
* A single very long new message is shortened to ``MAX_USER_CHARS`` with a
  visible note, so one paste cannot blow the model's input limit.

Long-term memory and retrieval belong to Module 3.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ai.base import ChatMessage

MAX_USER_CHARS = 8000


def shorten_user_message(text: str, limit: int = MAX_USER_CHARS) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= limit:
        return text, False
    removed = len(text) - limit
    return text[:limit] + f"\n[…message shortened: {removed} characters removed]", True


@dataclass(frozen=True)
class Turn:
    user: str
    assistant: str
    source: str  # "typed" | "voice"


class ConversationContext:
    def __init__(self, max_turns: int = 8, max_chars: int = 12000) -> None:
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._turns: list[Turn] = []
        self._lock = threading.Lock()

    def configure(self, max_turns: int, max_chars: int) -> None:
        with self._lock:
            self.max_turns, self.max_chars = max_turns, max_chars

    def add_turn(self, user: str, assistant: str, source: str) -> None:
        with self._lock:
            self._turns.append(Turn(user, assistant, source))
            # Keep memory bounded even if the window settings are large.
            del self._turns[:-max(50, self.max_turns)]

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    @property
    def turn_count(self) -> int:
        with self._lock:
            return len(self._turns)

    def recent(self, n: int = 3) -> list[Turn]:
        """The last ``n`` exchanges, newest last (used by research for "verify this information")."""
        with self._lock:
            return list(self._turns[-n:])

    def messages_for(self, new_user_message: str) -> list[ChatMessage]:
        """History window + the new message, oldest first."""
        with self._lock:
            budget = self.max_chars - len(new_user_message)
            chosen: list[Turn] = []
            for turn in reversed(self._turns):
                if len(chosen) >= self.max_turns:
                    break
                size = len(turn.user) + len(turn.assistant)
                if size > budget:
                    break
                chosen.append(turn)
                budget -= size
        messages: list[ChatMessage] = []
        for turn in reversed(chosen):
            messages.append(ChatMessage("user", turn.user))
            messages.append(ChatMessage("assistant", turn.assistant))
        messages.append(ChatMessage("user", new_user_message))
        return messages
