"""Formatting research results: detailed Markdown for the screen, a short summary for speech.

Citations ``[n]`` become clickable links to the source. Every answer ends with the
numbered references (site, publication date and how it is known, whether the page was
read or only a search snippet was available) and a line saying how the search was
done. Speech never contains URLs, citation markers or Markdown.
"""

from __future__ import annotations

import re

from research.models import Finding, ResearchResult, Source

VERDICT_LABEL = {"supported": "✅ Supported", "contradicted": "❌ Contradicted", "partly_supported": "◐ Partly supported",
                 "insufficient": "❔ Not enough evidence", "outdated": "⌛ Outdated"}
KIND_LABEL = {"claim": "reported claim", "analysis": "analysis", "uncertain": "uncertain"}


def _link(text: str, result: ResearchResult) -> str:
    def repl(m: re.Match) -> str:
        s = result.source(int(m.group(1)))
        return f"[[{m.group(1)}]]({s.url})" if s else ""
    return re.sub(r"\[(\d{1,2})\](?!\()", repl, text or "")


def _cites(ids: list[int], result: ResearchResult) -> str:
    return "".join(f"[[{i}]]({result.source(i).url})" for i in ids if result.source(i))


def _escape(text: str) -> str:
    return (text or "").replace("[", "(").replace("]", ")").replace("*", "").replace("<", "‹").replace(">", "›")


def references(result: ResearchResult, only_cited: bool = False) -> str:
    cited = result.cited_ids() | {int(n) for n in re.findall(r"\[(\d{1,2})\]", result.summary or "")}
    lines = []
    for s in result.sources:
        if only_cited and cited and s.id not in cited:
            continue
        date = f"published {s.published_text()}" + (f" ({s.date_basis})" if s.date_basis not in ("publisher",
                                                                                                   "unknown") else "")
        if s.published is None:
            date = "date unknown"
        how = "page read" if s.read else ("snippet only — " + s.read_error if s.read_error else "snippet only")
        lines.append(f"{s.id}. [{_escape(s.title)[:110] or s.domain}]({s.url}) — {s.domain}"
                     f"{' · ' + _escape(s.source_name) if s.source_name and s.source_name not in s.domain else ''} · "
                     f"{date} · {s.quality_label} · {how}")
    return "\n".join(lines)


def footer(result: ResearchResult) -> str:
    read = sum(1 for s in result.sources if s.read)
    when = result.started_at.astimezone().strftime("%d %b %Y %H:%M")
    parts = [f"Searched {when}", f"{result.searches} search(es)", f"{read} of {len(result.sources)} page(s) read"]
    if not result.ai_used:
        parts.append("no AI summary")
    text = " · ".join(parts)
    notes = [n for n in dict.fromkeys(result.notes)]
    return f"_{text}_" + ("\n\n" + "\n".join(f"_ℹ {n}_" for n in notes) if notes else "")


def _finding_line(f: Finding, result: ResearchResult) -> str:
    label = KIND_LABEL.get(f.kind)
    return f"- {_link(f.text, result)} {_cites([i for i in f.source_ids if f'[{i}]' not in f.text], result)}" + (
        f" _({label})_" if label else "")


def format_markdown(result: ResearchResult) -> str:
    out: list[str] = []
    if result.status == "failed":
        out.append(f"**I couldn't complete the research:** {result.error}")
        if result.sources:
            out += ["", "**What I found before it stopped:**", references(result)]
        if result.notes:
            out += ["", footer(result)]
        return "\n".join(out)
    if result.previous:
        out.append(f"_Earlier research (saved {result.previous.get('created_at', '')[:10]}, shown for comparison — not "
                   "current evidence):_ " + _escape(result.previous.get("summary_short", ""))[:400])
        out.append("")
    if result.kind == "news":
        out.append(f"**News · {result.period}**")
        if result.summary:
            out += ["", _link(result.summary, result)]
        for n, f in enumerate(result.findings, 1):
            out += ["", f"**{n}. {_escape(f.headline) or 'Development'}**",
                    f"{_link(f.text, result)} {_cites([i for i in f.source_ids if f'[{i}]' not in f.text], result)}",
                    f"_Published: {f.published}_"]
            if f.why:
                out.append(f"**Why it matters:** {_link(f.why, result)}")
            if f.uncertainty:
                out.append(f"**Uncertain / disputed:** {_link(f.uncertainty, result)}")
        if result.comparison:
            out += ["", f"**How sources differ:** {_link(result.comparison, result)}"]
    elif result.kind == "factcheck":
        out.append("**Fact-check**")
        for f in result.findings:
            out += ["", f"**{VERDICT_LABEL.get(f.verdict, f.verdict)}** — “{_escape(f.headline)}”",
                    _link(f.text, result)]
            if f.source_ids:
                out.append(f"Supporting: {_cites(f.source_ids, result)} "
                           f"({f.independent_sources} independent source{'s' if f.independent_sources != 1 else ''})")
            if f.contradicting_ids:
                out.append(f"Contradicting: {_cites(f.contradicting_ids, result)}")
            if f.uncertainty:
                out.append(f"_Note: {_link(f.uncertainty, result)}_")
    elif result.kind == "research":
        out += ["**Executive summary**", _link(result.summary, result)]
        if result.findings:
            out += ["", "**Main findings**"] + [_finding_line(f, result) for f in result.findings]
        if result.comparison:
            out += ["", "**Source comparison**", _link(result.comparison, result)]
        if result.uncertainty or result.gaps:
            out += ["", "**Uncertainty and limitations**"]
            if result.uncertainty:
                out.append(_link(result.uncertainty, result))
            out += [f"- Unanswered: {_escape(g)}" for g in result.gaps]
    else:  # search / open_docs
        if result.summary:
            out.append(_link(result.summary, result))
        if result.findings:
            out += [""] + [_finding_line(f, result) for f in result.findings]
        if result.uncertainty:
            out += ["", f"_Uncertain: {_link(result.uncertainty, result)}_"]
        for g in result.gaps:
            out += ["", f"_{_escape(g)}_"]
    if result.memory_used:
        out += ["", "_Used your saved context: " + "; ".join(_escape(m) for m in result.memory_used[:4]) + "_"]
    out += ["", "**Sources**", references(result, only_cited=result.ai_used), "", footer(result)]
    # Every line is its own paragraph (single line breaks are ignored by Markdown renderers).
    return "\n\n".join(line for line in out if line != "").strip()


def format_extractive(result: ResearchResult, reason: str) -> str:
    """No AI summary available: show what the sources themselves say, clearly labelled."""
    out = [f"_I couldn't write a summary ({reason}), so here is what the sources themselves say:_"]
    if result.kind == "news":
        out.insert(0, f"**News · {result.period}**")
    for s in result.sources[:8]:
        extract = (s.passages[0] if s.passages else s.snippet)[:280]
        out += ["", f"**[{s.id}] {_escape(s.title)[:120]}** — {s.domain} · {s.published_text()}",
                f"{_escape(extract)}{'…' if extract else '(no text available)'}"]
    out += ["", "**Sources**", references(result), "", footer(result)]
    return "\n\n".join(line for line in out if line != "")


def _plain(text: str) -> str:
    text = re.sub(r"\[\[?\d{1,2}\]?\]?(\([^)]*\))?", "", text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    text = re.sub(r"\s+([.,;:!?])", lambda m: m.group(1), " ".join(text.split()))
    return text


def first_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", _plain(text))
    return " ".join(parts[:n]).strip()


def format_speech(result: ResearchResult) -> str:
    if result.status == "failed":
        return f"I couldn't complete the research. {first_sentences(result.error, 1)}"
    if result.status == "cancelled":
        return "Research cancelled."
    if not result.ai_used:
        if result.kind == "news" and result.sources:
            heads = "; ".join(_plain(s.title) for s in result.sources[:3])
            return f"I couldn't write a summary, but the top headlines are: {heads}. Sources are on screen."
        return "I found sources but couldn't write a summary. They are listed on screen."
    if result.kind == "news":
        heads = [f"{['First', 'Second', 'Third'][i]}, {_plain(f.headline)}." for i, f in enumerate(result.findings[:3])]
        intro = first_sentences(result.summary, 1) if result.summary else "Here are the main developments."
        return f"{intro} {' '.join(heads)} Details and sources are on screen.".strip()
    if result.kind == "factcheck":
        parts = [f"{VERDICT_LABEL.get(f.verdict, '').split(' ', 1)[-1]}: {_plain(f.headline)[:120]}."
                 for f in result.findings[:3]]
        return " ".join(parts) + " The evidence is on screen."
    summary = first_sentences(result.summary, 3 if result.kind == "research" else 2)
    return (summary or "I found some sources; they are on screen.") + " Sources are on screen."
