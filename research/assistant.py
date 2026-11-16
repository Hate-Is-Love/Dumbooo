"""Connects research to the conversation (voice and typed messages share this path).

``ResearchAssistant`` is a ``ConversationHooks`` in front of the automation and memory
hooks. It only handles messages that need live research or refer to earlier research;
everything else passes through unchanged. Results are shown in full (Markdown with
clickable citations) and spoken as a short summary without URLs.

Opening a source or the official documentation, and exporting a report, go through
the laptop-automation controller (its policy engine and verification) — research
content itself can never trigger an action.
"""

from __future__ import annotations

import logging
import re
import contextvars
import threading
from datetime import datetime
from typing import Any, Callable

from ai.controller import ConversationHooks, LocalReply
from ai.memory_commands import parse_command
from automation.intents import clean
from automation.models import Step
from research.history_repository import ResearchNotFound
from research.intents import ResearchIntent, classify
from research.research_controller import ResearchController, ResearchOutcome
from research.result_formatter import format_speech, references
from services.auth_service import AuthError, Session

log = logging.getLogger(__name__)


_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("research_source", default="")


class ResearchAssistant(ConversationHooks):
    def __init__(self, auth, controller: ResearchController, inner: ConversationHooks, automation=None,
                 recent_turns: Callable[[], list] = lambda: [], config: Callable[[], Any] = lambda: None) -> None:
        self._auth = auth
        self._controller = controller
        self._inner = inner
        self._automation = automation
        self._recent = recent_turns
        self._config = config
        self._last: dict[str, ResearchOutcome] = {}
        self._lock = threading.Lock()

    # -- hooks ----------------------------------------------------------------------------------------
    def prompt_options(self, scope: Any) -> dict:
        return self._inner.prompt_options(scope)

    def extra_context(self, scope: Any, text: str, seq: int) -> str:
        return self._inner.extra_context(scope, text, seq) + (
            "\n\nLive web research is done by DUMBOO's research module, not by you. Never claim you searched the web, "
            "and never present your training knowledge as current news; if the user needs current information, tell "
            "them to ask DUMBOO to search (for example “search for …” or “what's the latest news about …”).")

    def forget_session(self, scope: Session | None) -> None:
        if scope is None:
            return
        with self._lock:
            self._last.pop(scope.token_hash, None)
        self._controller.cancel(session_key=scope.token_hash)
        if hasattr(self._inner, "forget_session"):
            self._inner.forget_session(scope)

    def last(self, session: Session) -> ResearchOutcome | None:
        with self._lock:
            return self._last.get(session.token_hash)

    remote_allowed = staticmethod(lambda scope: False)   # set by the app: may this phone control the laptop?

    def local_reply(self, scope: Any, text: str, source: str) -> LocalReply | None:
        _SOURCE.set(source)
        if not isinstance(scope, Session):
            return self._inner.local_reply(scope, text, source)
        try:
            user = self._auth.require(scope)
        except AuthError:
            return self._inner.local_reply(scope, text, source)
        t = clean(text)
        memory = parse_command(t)
        if memory.kind not in ("none",) and not re.match(r"^(?:remember|save|store|keep) (?:these|this|the) "
                                                         r"(?:findings|research|results|sources|report)", t, re.I):
            return self._inner.local_reply(scope, text, source)
        cfg = self._config()
        intent = classify(t, has_last=self.last(scope) is not None,
                          auto_research=getattr(cfg, "auto_research", True))
        if intent is None:
            return self._inner.local_reply(scope, text, source)
        if not user.can("research.web"):
            return LocalReply("Live web research is turned off for your account. Ask the owner to enable it on the "
                              "ADMIN page (permission “research.web”).", kind="research")
        try:
            return self._handle(scope, intent, t, source)
        except AuthError as exc:
            return LocalReply(exc.user_message, kind="auth")
        except ResearchNotFound as exc:
            return LocalReply(str(exc), kind="research")
        except Exception:
            log.exception("Research request failed")
            return LocalReply("Something went wrong while doing that research. Nothing was changed; details are in "
                              "the log file.", kind="research")

    # -- handling ---------------------------------------------------------------------------------------
    def _remember(self, session: Session, outcome: ResearchOutcome) -> LocalReply:
        with self._lock:
            self._last[session.token_hash] = outcome
        return LocalReply(outcome.markdown, speech=outcome.speech, kind="research")

    def _run(self, session: Session, intent: ResearchIntent, source: str, **kw) -> LocalReply:
        outcome = self._controller.run(session, intent.kind if intent.kind in ("search", "news", "research",
                                                                               "factcheck", "open_docs") else "search",
                                       intent.query, source=source, days=intent.days, official=intent.official,
                                       open_url=lambda url, title: self._open(session, url, title), **kw)
        return self._remember(session, outcome)

    def _resolve_referent(self, session: Session, intent: ResearchIntent) -> str | None:
        """'this course', 'these colleges', 'this topic' … from the last research or saved memories."""
        last = self.last(session)
        ref = intent.referent.lower()
        if ref in ("previous answer", "previous question"):
            return None
        if last is not None and ref in ("this topic", "this event", "this story", "this report", "this news report"):
            r = last.result
            return r.findings[0].headline if (ref != "this topic" and r.findings and r.findings[0].headline) else r.query
        if last is not None and re.search(r"librar|framework|tool|model|product|company", ref):
            return last.result.query
        noun = ref.split()[-1].rstrip("s")
        rows = self._controller.memory_context.lookup(session, f"my {noun}")
        rows = [r for r in rows if noun in (r["memory_key"] + " " + r["memory_value"]).lower()]
        if rows:
            return rows[0]["memory_value"]
        return None

    def _handle(self, session: Session, intent: ResearchIntent, t: str, source: str) -> LocalReply:
        kind = intent.kind
        history = self._controller.history
        if kind == "history":
            rows = history.list(session, intent.query, limit=10)
            if not rows:
                return LocalReply("I don't have any saved research" + (f" about “{intent.query}”" if intent.query
                                                                         else "") + " yet.", kind="research")
            lines = [f"- #{r['id']} {r['created_at'][:10]} · {r['kind']} · “{r['query'][:80]}” · {r['status']}"
                     + (" · saved to memory" if r["memory_id"] else "") for r in rows]
            return LocalReply("Your research history:\n" + "\n".join(lines) + "\n\nSay “open the research report "
                              "about …” or “refresh my previous research about …”. The RESEARCH page shows them all.",
                              speech=f"I found {len(rows)} earlier research sessions. They are listed on screen.",
                              kind="research")
        if kind == "open_saved":
            row = None
            if not intent.query:
                for r in history.list(session, "", limit=30):
                    if r["memory_id"]:
                        row = history.get(session, r["id"])
                        break
            row = row or history.latest(session, intent.query)
            if row is None:
                return LocalReply("I couldn't find saved research" + (f" about “{intent.query}”" if intent.query else
                                                                     "") + ".", kind="research")
            return LocalReply(f"_Saved research #{row['id']} from {row['created_at'][:16].replace('T', ' ')} — these "
                              "sources may be outdated; say “refresh my previous research” to check them again._\n\n"
                              + row["answer"], speech=f"Here is the research about {row['query'][:80]} from "
                                                      f"{row['created_at'][:10]}. Say refresh to update it.",
                              kind="research")
        if kind in ("refresh", "continue", "compare_previous"):
            last = self.last(session)
            if kind == "compare_previous" and last is not None:
                earlier = [r for r in history.list(session, last.result.query, limit=10) if r["id"] != last.result.id
                           and r["status"] in ("completed", "partial")]
                if not earlier:
                    return LocalReply("I don't have earlier research on this topic to compare with.", kind="research")
                previous = history.get(session, earlier[0]["id"])
                query, rkind = last.result.query, last.result.kind
            else:
                previous = history.latest(session, intent.query, statuses=("completed", "partial", "failed",
                                                                          "cancelled"))
                if previous is None:
                    return LocalReply("I don't have earlier research" + (f" about “{intent.query}”" if intent.query
                                                                         else "") + " to "
                                      + ("refresh." if kind == "refresh" else "continue."), kind="research")
                query, rkind = previous["query"], previous["kind"]
                if kind == "continue" and previous["status"] == "completed" and rkind != "news":
                    rkind = "research"  # continuing finished research = going deeper
            prev = {"created_at": previous["created_at"], "summary": previous.get("summary", ""),
                    "summary_short": previous.get("summary", "")[:400], "sources": previous.get("sources", [])}
            outcome = self._controller.run(session, rkind if rkind != "open_docs" else "search", query, source=source,
                                           previous=prev, refreshed_from=previous["id"],
                                           open_url=lambda url, title: self._open(session, url, title))
            return self._remember(session, outcome)
        if kind == "save":
            last = self.last(session)
            if last is None or not last.result.id or last.result.status not in ("completed", "partial"):
                return LocalReply("There are no research findings in this conversation to save yet.", kind="research")
            r = last.result
            project = re.sub(r"\b(?:project|notes?|my)\b", "", intent.query, flags=re.I).strip(" .") if intent.query \
                else ""
            topic = re.sub(r"\s+", " ", r.query)[:50]
            key = f"research: {topic}" if not project else f"{project} research: {topic}"[:80]
            body = (r.summary or "; ".join(f.headline or f.text for f in r.findings[:3]))
            body = re.sub(r"\[\d{1,2}\]", "", body)
            sites = ", ".join(dict.fromkeys(s.domain for s in r.sources if s.id in r.cited_ids())) or \
                ", ".join(dict.fromkeys(s.domain for s in r.sources[:4]))
            value = f"{body[:380]} (sources: {sites[:80]}; researched {datetime.now():%d %b %Y})"
            try:
                memory = self._controller.memory_context.remember_findings(
                    session, key, value, "project" if (project or "project" in intent.query.lower()) else "fact")
            except Exception as exc:
                return LocalReply(getattr(exc, "user_message", "Those findings could not be saved."), kind="research")
            history.link_memory(session, r.id, memory["id"])
            return LocalReply(f"Saved to your memory as “{memory['memory_key']}”: {value}\n\nYou can edit or delete it "
                              "on the MEMORY or RESEARCH page.", speech="Saved these findings to your memory.",
                              kind="research")
        if kind == "use_saved":
            seeds = history.saved_sources(session)
            if not seeds:
                return LocalReply("You haven't saved any research sources yet. Say “remember these findings” after a "
                                  "research answer to keep its sources.", kind="research")
            last = self.last(session)
            query = intent.query or (last.result.query if last else seeds[0].get("title", ""))
            words = {w for w in re.findall(r"[a-z0-9]{4,}", query.lower())}
            relevant = [s for s in seeds if words & set(re.findall(r"[a-z0-9]{4,}", (s.get("title", "") + " " +
                                                                                    s.get("extract", "")).lower()))]
            outcome = self._controller.run(session, "search", query, source=source, seeds=(relevant or seeds)[:6],
                                           open_url=lambda url, title: self._open(session, url, title))
            return self._remember(session, outcome)
        if kind == "export":
            last = self.last(session)
            if last is None:
                return LocalReply("There's no research report in this conversation to export.", kind="research")
            return self._export(session, last)
        if kind == "sources":
            last = self.last(session)
            return LocalReply("**Sources**\n" + references(last.result), speech="The sources are listed on screen.",
                              kind="research")
        if kind == "open_source":
            last = self.last(session)
            n = intent.index or 1
            s = last.result.source(n)
            if s is None:
                return LocalReply(f"There's no source number {n} in the last research.", kind="research")
            return LocalReply(self._open(session, s.url, s.title), kind="research")
        if kind == "followup":
            last = self.last(session)
            r = last.result
            n = intent.index or 1
            if r.findings and n <= len(r.findings):
                f = r.findings[n - 1]
                plain = re.sub(r"\[\d{1,2}\]", "", f.text)[:160]
                query = f"{f.headline or ''} {plain}".strip()
                seeds = [r.source(i).stored() for i in f.source_ids if r.source(i)]
            else:
                query, seeds = r.query, []
            outcome = self._controller.run(session, "search", query, source=source, seeds=seeds,
                                           open_url=lambda url, title: self._open(session, url, title))
            return self._remember(session, outcome)
        # live research kinds ---------------------------------------------------------------------------
        if intent.referent and intent.referent not in ("previous answer", "previous question"):
            resolved = self._resolve_referent(session, intent)
            if resolved is None:
                return LocalReply(f"Which {intent.referent.split()[-1]} do you mean? Please name it (for example "
                                  f"“{t.replace(intent.referent, intent.referent.split()[-1] + ' X')}”).",
                                  kind="research", listen_after=True)
            intent.query = intent.query.replace(intent.referent, resolved) if intent.referent in intent.query else (
                resolved if intent.query in ("", intent.referent) else f"{intent.query} {resolved}")
        if intent.referent == "previous answer":
            claim = self._last_answer(session)
            if not claim:
                return LocalReply("What should I verify? Paste or say the claim, for example “verify: Python 3.11 was "
                                  "released in October 2022”.", kind="research", listen_after=True)
            return self._remember(session, self._controller.run(session, "factcheck", claim[:200], source=source,
                                                                claim_text=claim))
        if intent.referent == "previous question":
            question = self._last_question()
            if not question:
                return LocalReply("Which question should I check against official sources?", kind="research",
                                  listen_after=True)
            intent.query = question
        if intent.kind == "factcheck":
            return self._remember(session, self._controller.run(session, "factcheck", intent.query[:200],
                                                                source=source, claim_text=intent.query))
        return self._run(session, intent, source)

    # -- helpers -----------------------------------------------------------------------------------------------
    def _last_answer(self, session: Session) -> str:
        last = self.last(session)
        turns = self._recent()
        if turns:
            text = turns[-1].assistant
            if last is not None and text.strip() == last.markdown.strip() and last.result.summary:
                text = last.result.summary
            text = re.sub(r"\[\[?\d{1,2}\]?\]?(\([^)]*\))?", "", text)
            return " ".join(text.split())[:1500]
        return last.result.summary if last else ""

    def _last_question(self) -> str:
        turns = self._recent()
        for turn in reversed(turns):
            if not re.match(r"^(?:verify|check)", turn.user, re.I):
                return turn.user
        return ""

    def _remote_blocked(self, session: Session) -> bool:
        return _SOURCE.get() == "phone" and not self.remote_allowed(session)

    def _open(self, session: Session, url: str, title: str) -> str:
        """Open a retrieved source in the user's browser through the automation controller (policy applies)."""
        if self._remote_blocked(session):
            return f"Opening pages on the laptop from your phone is switched off. The link: {url}"
        if self._automation is None:
            return f"Link: {url}"
        try:
            task = self._automation.submit(session, f"open {url}", [Step("web.open", {"url": url, "label": title[:80]},
                                                                         f"Open {title[:60]}")], source="research")
            task = self._automation.wait(task, 20)
        except Exception as exc:
            return f"I couldn't open it ({exc}). Link: {url}"
        step = task.steps[0]
        if step.status.value == "completed":
            return f"Opened it in your browser ({step.verification})."
        if step.status.value == "unverified":
            return f"Asked your browser to open it; {step.verification}."
        return f"I couldn't open it: {step.error or task.error}. Link: {url}"

    def _export_folder(self):
        """Documents when DUMBOO may write there, otherwise the first folder it may write to."""
        from automation.config import known_folder

        guard = self._automation.services.guard
        candidates = [known_folder("documents")] + guard.roots(write=True)
        for folder in candidates:
            if folder.is_dir():
                try:
                    guard.check(folder, write=True, container=True)
                    return folder
                except Exception:
                    continue
        return None

    def _export(self, session: Session, outcome: ResearchOutcome) -> LocalReply:
        if self._remote_blocked(session):
            return LocalReply("Saving files on the laptop from your phone is switched off, so I didn't export it.",
                              kind="research")
        if self._automation is None:
            return LocalReply("Exporting needs the automation module.", kind="research")
        folder = self._export_folder()
        if folder is None:
            return LocalReply("There's no permitted folder I may write the report to.", kind="research")
        slug = re.sub(r"[^a-z0-9]+", "-", outcome.result.query.lower()).strip("-")[:40] or "research"
        name = f"research-{slug}-{datetime.now():%Y%m%d-%H%M}.md"
        content = f"# Research: {outcome.result.query}\n\n{outcome.markdown}\n"
        task = self._automation.submit(session, "export research report", [Step(
            "files.create_file", {"folder": str(folder), "name": name, "content": content},
            f"Save the research report as {name}")], source="research")
        task = self._automation.wait(task, 15)
        step = task.steps[0]
        if step.status.value == "completed":
            return LocalReply(f"Saved the report to {folder / name} ({step.verification}).",
                              speech=f"Saved the research report to your {folder.name} folder.", kind="research")
        return LocalReply(f"I couldn't save the report: {step.error or task.error or step.status.value}.",
                          kind="research")
