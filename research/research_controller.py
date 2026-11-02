"""The research controller: one place that runs every kind of research, for voice and text alike.

    plan -> search -> choose sources -> read pages -> extract evidence -> group independent
    sources -> AI synthesis from the sources only -> citation check -> format -> history

Each run is a ``ResearchTask`` with a time limit, a search limit and a page limit, and can
be cancelled at any moment (STOP button, RESEARCH page, sign-out). If the search itself
fails, the answer says so — DUMBOO never presents old model knowledge as a live result.
If the AI can't write a summary (quota, network), the sources are shown as they are.
"""

from __future__ import annotations

import copy
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass
from typing import Callable

from app.event_bus import EventBus, Events
from research.ai_json import AIUnavailable, Generate
from research.config import ResearchConfig
from research.history_repository import ResearchHistory
from research.memory_context_service import MemoryContextService
from research.models import ResearchResult, SearchResult, Source, utcnow
from research.news_service import NewsService
from research.quality import assign_groups, canonical_url, quality_label, rank
from research.research_planner import ResearchPlanner
from research.result_formatter import format_extractive, format_markdown, format_speech
from research.search_provider import SearchError
from research.source_retrieval_service import SourceRetrievalService
from research.synthesis_service import SynthesisService
from research.fact_check_service import FactCheckService
from research.task_manager import BudgetExceeded, Cancelled, ResearchTask, ResearchTaskManager
from research.web_search_service import WebSearchService
from services.auth_service import AuthService, Session

log = logging.getLogger(__name__)

TIME_WORDS = re.compile(r"\b(latest|recent|today|current|now|this (?:week|month|year)|20\d\d|new|announced|released)\b",
                        re.I)


# Seconds to wait for pages before answering from what was read (snippets for the rest).
READ_BUDGET_S = {"news": 12.0, "search": 12.0, "open_docs": 12.0, "factcheck": 20.0, "research": 30.0}

class SearchFailed(Exception):
    pass


@dataclass
class ResearchOutcome:
    result: ResearchResult
    markdown: str
    speech: str


class ResearchController:
    name = "research"

    def __init__(self, bus: EventBus, auth: AuthService, config: Callable[[], ResearchConfig],
                 search: WebSearchService, retrieval: SourceRetrievalService, history: ResearchHistory,
                 memory_context: MemoryContextService, generate: Generate | None) -> None:
        self._bus = bus
        self._auth = auth
        self._config = config
        self.search_service = search
        self.news_service = NewsService(search)
        self.retrieval = retrieval
        self.history = history
        self.memory_context = memory_context
        self.planner = ResearchPlanner(generate)
        self.synthesis = SynthesisService(generate)
        self.facts = FactCheckService(generate)
        self.tasks = ResearchTaskManager(bus)

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self) -> None:
        try:
            self.history.recover_interrupted()
        except Exception:
            log.exception("Could not mark interrupted research")

    def stop(self) -> None:
        self.tasks.cancel()

    def cancel(self, user_id: int | None = None, session_key: str | None = None) -> int:
        return self.tasks.cancel(user_id, session_key)

    # -- the public entry point ------------------------------------------------------------------------
    def run(self, session: Session, kind: str, query: str, *, source: str = "typed", days: int | None = None,
            claim_text: str = "", official: bool = False, previous: dict | None = None,
            refreshed_from: int | None = None, seeds: list[dict] | None = None,
            open_url: Callable[[str, str], str] | None = None) -> ResearchOutcome:
        user = self._auth.require(session, "research.web")
        cfg = self._config()
        result = ResearchResult(kind, query)
        result.previous = previous
        task = self.tasks.start(user.id, session.token_hash, kind, query, timeout_s=cfg.total_timeout_s,
                                max_searches=cfg.max_searches, max_pages=cfg.max_pages)
        task.fresh = previous is not None  # refreshing/continuing earlier research: never reuse cached results
        reason = ""
        try:
            result.id = self.history.create(session, result, source, refreshed_from)
            task.research_id = result.id
            rows = self.memory_context.lookup(session, query + " " + claim_text)
            context = self.memory_context.as_context(rows)
            result.memory_used = self.memory_context.labels(rows)
            for seed in seeds or []:
                self._add_seed(result, seed)
            if kind == "news":
                reason = self._news(task, result, context, days or cfg.news_days)
            elif kind == "research":
                reason = self._research(task, result, context)
            elif kind == "factcheck":
                reason = self._factcheck(task, result, claim_text or query)
            elif kind == "open_docs":
                reason = self._open_docs(task, result, session, open_url)
            else:
                reason = self._web(task, result, context, official)
            if result.status == "running":
                result.status = "partial" if reason else "completed"
        except Cancelled:
            result.status, result.error = "cancelled", "Research cancelled."
        except BudgetExceeded:
            result.notes.append(f"The research time limit ({cfg.total_timeout_s:.0f} s) was reached; the answer is "
                                "based on what was found until then.")
            result.status = "partial" if result.sources else "failed"
            result.error = "" if result.sources else "The research time limit was reached before any source was found."
            reason = reason or "time limit reached"
        except SearchFailed as exc:
            result.status, result.error = "failed", str(exc)
        except Exception:
            log.exception("Research failed")
            result.status, result.error = "failed", "An internal error stopped the research. Details are in the log."
        finally:
            result.finished_at = utcnow()
            result.searches, result.pages = task.searches, task.pages
            if previous:
                self._compare_with_previous(result, previous)
            if result.status == "cancelled":
                markdown = "**Research cancelled.** Nothing more will be searched." + (
                    "\n\n**Sources found before stopping:**\n" + "\n".join(
                        f"- [{s.title[:100]}]({s.url}) — {s.domain}" for s in result.sources[:8]) if result.sources else "")
            elif reason and result.status == "partial" and not result.ai_used and result.sources:
                markdown = format_extractive(result, reason)
            else:
                markdown = format_markdown(result)
            speech = format_speech(result)
            try:
                self.history.finish(user.id, result, markdown)
            except Exception:
                log.exception("Could not save research %s", result.id)
            task.finish(result.status)
            self.tasks.done(task)
            self._bus.publish(Events.RESEARCH_DONE, {"user_id": user.id, "research_id": result.id,
                                                     "status": result.status, "kind": kind, "query": query[:200]})
        return ResearchOutcome(result, markdown, speech)

    # -- building blocks ---------------------------------------------------------------------------------
    def _search(self, task: ResearchTask, result: ResearchResult, query: str, kind: str = "web", n: int = 10,
                days: int | None = None, errors: list | None = None) -> list[SearchResult]:
        task.check()
        if not task.can_search():
            result.notes.append(f"The search limit ({task.max_searches}) was reached.")
            return []
        task.searches += 1
        task.note(f"searching {kind}: {query[:70]}")
        outcome = self.search_service.search(query, kind, max_results=n, days=days, cancel=task.cancel,
                                             fresh=task.fresh)
        task.check()
        result.notes.extend(outcome.notes)
        for err in outcome.errors:
            if errors is not None:
                errors.append(err)
        return outcome.results

    @staticmethod
    def _failure(errors: list[SearchError], what: str) -> SearchFailed:
        reasons = "; ".join(dict.fromkeys(e.user_message for e in errors)) or "no provider is available"
        return SearchFailed(f"Live {what} search failed: {reasons}. I won't answer from older knowledge as if it "
                            "were current — please try again later.")

    def _add_sources(self, result: ResearchResult, items: list[SearchResult], query: str) -> list[Source]:
        known = {canonical_url(s.url) for s in result.sources} | {canonical_url(s.original_url) for s in result.sources
                                                                  if s.original_url}
        added = []
        for r in items:
            key = canonical_url(r.url)
            if key in known:
                continue
            known.add(key)
            s = Source(len(result.sources) + 1, r.title or r.url, r.url, r.snippet, r.published, r.date_basis,
                       r.source_name, r.provider, quality_label=quality_label(r.quality_url, query))
            result.sources.append(s)
            added.append(s)
        return added

    def _add_seed(self, result: ResearchResult, seed: dict) -> None:
        url = str(seed.get("url") or "")
        if not url.startswith(("http://", "https://")) or any(canonical_url(s.url) == canonical_url(url)
                                                              for s in result.sources):
            return
        from research.dates import parse_date

        result.sources.append(Source(len(result.sources) + 1, str(seed.get("title") or url), url,
                                     str(seed.get("extract") or seed.get("snippet") or "")[:500],
                                     parse_date(seed.get("published")), str(seed.get("date_basis") or "unknown"),
                                     str(seed.get("source_name") or ""), "saved",
                                     quality_label=str(seed.get("quality_label") or quality_label(url))))

    def _read(self, task: ResearchTask, result: ResearchResult, sources: list[Source], question: str) -> None:
        picks = [s for s in sources if not s.read and not s.read_error]
        room = max(0, task.max_pages - task.pages)
        picks = picks[:room]
        if not picks:
            return
        task.stage(next((s["description"] for s in task.steps if s["description"].startswith("Read")), "Read sources"),
                   f"reading {len(picks)} page(s)")
        done = 0
        # Reading budget: one slow page (or a slow news-link redirect) must not hold up the whole answer.
        # Each page is read into a copy; only pages finished in time are used, the rest keep their search
        # snippet and are marked "snippet only" as before. A late page can't change the answer afterwards.
        budget = READ_BUDGET_S.get(task.kind, 25.0)
        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ResearchRead")
        try:
            futures = {pool.submit(self._retrieve_copy, s, question, task.cancel): s for s in picks}
            try:
                for future in as_completed(futures, timeout=budget):
                    s = futures[future]
                    done += 1
                    task.pages += 1
                    try:
                        s.__dict__.update(future.result().__dict__)
                    except Exception:
                        s.read, s.read_error = False, "the page could not be read"
                    task.note(f"read {done}/{len(picks)}: {s.domain} {'✓' if s.read else '✗'}")
                    if task.cancel.is_set():
                        raise Cancelled()
            except FuturesTimeout:
                late = 0
                for future, s in futures.items():
                    if s.read or s.read_error:
                        continue                       # already handled above
                    if future.done() and not future.cancelled() and future.exception() is None:
                        s.__dict__.update(future.result().__dict__)   # finished right at the deadline: use it
                        done += 1
                    else:
                        s.read_error = f"took longer than {budget:.0f} s"
                        late += 1
                task.pages += late
                log.info("Research reading budget reached after %.1f s: %d of %d pages read", time.monotonic() - started,
                         done, len(picks))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        unread = [s for s in picks if not s.read]
        if unread:
            result.notes.append(f"{len(unread)} page(s) couldn't be read (" + "; ".join(
                dict.fromkeys(f"{s.domain}: {s.read_error}" for s in unread[:3])) + "); their search snippets were "
                "used and are marked 'snippet only'.")
        task.check()

    def _retrieve_copy(self, source: Source, question: str, cancel) -> Source:
        work = copy.copy(source)
        work.passages = list(source.passages)
        self.retrieval.retrieve(work, question, cancel)
        return work

    def _synthesize(self, task: ResearchTask, result: ResearchResult, call: Callable[[], object]) -> str:
        """Run one synthesis call; returns '' on success or the reason the AI couldn't be used."""
        task.stage(next((s["description"] for s in task.steps if s["description"].startswith(("Analyse", "Judge"))),
                        "Analyse evidence"), "writing the answer from the sources")
        try:
            cite = call()
        except AIUnavailable as exc:
            result.notes.append(f"No AI summary: {str(exc).rstrip('.')}. The sources are shown as found.")
            return str(exc)
        task.check()
        task.stage("Check citations", "checking that every citation is a retrieved source")
        result.notes.extend(cite.report.notes())
        return ""

    # -- flows ----------------------------------------------------------------------------------------------
    def _queries(self, query: str, context: str, max_queries: int) -> list[str]:
        if not context:
            return [query]
        plan = self.planner.plan(query, max_searches=max_queries + 1, context=context)
        queries = [q for sq in plan.sub_questions for q in sq.queries][:max_queries]
        if not plan.by_ai:
            queries = [re.sub(r"\b(my|our|related to)\b", "", query, flags=re.I).strip()]
        return queries or [query]

    def _web(self, task: ResearchTask, result: ResearchResult, context: str, official: bool) -> str:
        task.stage("Search the web")
        errors: list = []
        items: list[SearchResult] = []
        for q in self._queries(result.query, context, 2):
            if official and "official" not in q.lower():
                q += " official"
            items += self._search(task, result, q, "web", 10, errors=errors)
        if not items:
            if errors:
                raise self._failure(errors, "web")
            result.summary = f"I searched the web for “{result.query}” but found no results."
            result.gaps.append("Try different or more general words.")
            return ""
        ranked = rank(items, result.query)
        if official:
            ranked.sort(key=lambda r: quality_label(r.quality_url, result.query) != "official/primary")
        sources = self._add_sources(result, ranked[:6], result.query)
        self._read(task, result, sources[:4], result.query)
        assign_groups(result.sources)
        return self._synthesize(task, result, lambda: self.synthesis.answer(result.query, result, context))

    def _news(self, task: ResearchTask, result: ResearchResult, context: str, days: int) -> str:
        task.stage("Search news")
        topic = re.sub(r"^(?:related to|about|on)\s+", "", result.query.strip(), flags=re.I) or "top news"
        queries = self._queries(topic, context, 1) if context else [re.sub(r"\bmy\b", "", topic, flags=re.I).strip()]
        task.check()
        task.searches += 1
        outcome = self.news_service.latest(queries[0] or "top news", days, cancel=task.cancel, fresh=task.fresh)
        task.check()
        result.period = outcome.period
        result.notes.extend(outcome.notes)
        if not outcome.stories:
            if outcome.errors and not outcome.providers:
                raise self._failure(outcome.errors, "news")
            result.summary = f"I found no news about “{topic}” in {outcome.period}."
            result.gaps.append("Try a broader topic or a longer period (for example “this month”).")
            return ""
        task.stage("Check dates", f"{len(outcome.stories)} stories; {outcome.dropped_old} older article(s) left out")
        leads: list[Source] = []
        for n, story in enumerate(outcome.stories, 1):
            added = self._add_sources(result, story.articles[:3], topic)
            for s in added:
                s.group = n
            if added:
                leads.append(added[0])
        self._read(task, result, leads, topic)
        return self._synthesize(task, result, lambda: self.synthesis.news(result.query, result, context))

    def _research(self, task: ResearchTask, result: ResearchResult, context: str) -> str:
        task.stage("Plan research")
        plan = self.planner.plan(result.query, max_searches=task.max_searches, context=context)
        result.plan = [{"question": sq.question, "queries": sq.queries} for sq in plan.sub_questions]
        if not plan.by_ai:
            result.notes.append("The research plan was made without the AI (simple split of the question).")
        task.stage("Search the web", f"{len(plan.sub_questions)} sub-question(s)")
        errors: list = []
        per_question: list[tuple[str, list[Source]]] = []
        for sq in plan.sub_questions:
            items: list[SearchResult] = []
            for q in sq.queries:
                items += self._search(task, result, q, "web", 8, errors=errors)
            if plan.needs_news and sq is plan.sub_questions[0]:
                items += self._search(task, result, sq.queries[0], "news", 8, days=31, errors=errors)
            ranked = rank(items, sq.question)
            per_question.append((sq.question, self._add_sources(result, ranked[:4], sq.question)))
        if not result.sources:
            if errors:
                raise self._failure(errors, "web")
            result.summary = f"I couldn't find sources about “{result.query}”."
            return ""
        for question, sources in per_question:  # read the two best (different sites first) per sub-question
            picks, sites = [], set()
            for s in sorted(sources, key=lambda s: s.domain in sites):
                if s.domain not in sites:
                    picks.append(s)
                    sites.add(s.domain)
                if len(picks) == 2:
                    break
            self._read(task, result, picks, question)
        task.stage("Fill gaps")
        for (question, sources), sq in zip(per_question, plan.sub_questions):
            if any(s.read for s in sources) or not task.can_search():
                continue
            extra = self._add_sources(result, rank(self._search(task, result, f"{sq.queries[0]} official", "web", 6,
                                                                errors=errors), question)[:2], question)
            self._read(task, result, extra, question)
        assign_groups(result.sources)
        plan_text = "\n".join(f"- {sq.question}" for sq in plan.sub_questions)
        return self._synthesize(task, result, lambda: self.synthesis.report(result.query, result, context, plan_text))

    def _factcheck(self, task: ResearchTask, result: ResearchResult, claim_text: str) -> str:
        task.stage("Find claims")
        claims = self.facts.claims(claim_text)
        result.plan = [{"claim": c} for c in claims]
        task.stage("Search evidence", f"{len(claims)} claim(s)")
        errors: list = []
        for claim in claims[:3]:
            items = self._search(task, result, claim[:200], "web", 8, errors=errors)
            if TIME_WORDS.search(claim):
                items += self._search(task, result, claim[:200], "news", 8, days=60, errors=errors)
            sources = self._add_sources(result, rank(items, claim)[:3], claim)
            self._read(task, result, sources, claim)
        if not result.sources:
            if errors:
                raise self._failure(errors, "web")
            result.summary = "I couldn't find any sources about this claim, so I can't verify it."
            return ""
        assign_groups(result.sources)
        return self._synthesize(task, result, lambda: self.facts.judge(claim_text, claims, result))

    def _open_docs(self, task: ResearchTask, result: ResearchResult, session: Session,
                   open_url: Callable[[str, str], str] | None) -> str:
        task.stage("Search the web")
        errors: list = []
        items = self._search(task, result, f"{result.query} official documentation", "web", 10, errors=errors)
        if not items:
            if errors:
                raise self._failure(errors, "web")
            result.summary = f"I couldn't find documentation for “{result.query}”."
            return ""
        ranked = rank(items, result.query)
        official = [r for r in ranked if quality_label(r.quality_url, result.query) == "official/primary"]
        best = (official or ranked)[0]
        self._add_sources(result, [best] + [r for r in ranked if r is not best][:3], result.query)
        if not official:
            result.notes.append("No clearly official site was found; this is the best match.")
        task.stage("Open the documentation", best.url[:80])
        if open_url is None:
            result.summary = f"The official documentation for {result.query} is [{best.title}]({best.url})."
            return ""
        outcome = open_url(best.url, best.title or result.query)
        result.summary = (f"Official documentation for {result.query}: [{best.title}]({best.url}) [1]. {outcome}")
        return ""

    @staticmethod
    def _compare_with_previous(result: ResearchResult, previous: dict) -> None:
        old = {canonical_url(s.get("url", "")) for s in previous.get("sources", [])}
        new = {canonical_url(s.url) for s in result.sources}
        if old or new:
            result.notes.append(f"Compared with the research from {previous.get('created_at', '')[:10]}: "
                                f"{len(new - old)} new source(s), {len(old & new)} still found, "
                                f"{len(old - new)} no longer in the results.")
