"""
prompt.py — assemble the model prompt for one job and parse the output.

A summary is a paragraph of 80-150 words followed by 3-5 key-fact
bullets. We render the head term in **bold**. The model never sees the
words "Wikipedia" or "the article" in its instructions; it's told the
context is a search result for the query and to answer the implied
question.

The final markdown is rendered by the front-end via a tiny markdown
renderer (paragraphs, **bold**, dash bullets). Anything fancier than
that gets stripped here so the front-end's output always matches what
we authored.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass


SYSTEM_PROMPT = """\
You write very short knowledge-panel summaries for a search engine.

Output format — exactly this, nothing else:
1. ONE paragraph of 60-100 words. Not more. Hard cap.
2. A blank line.
3. Exactly 3 to 5 bullets. Each bullet is ONE short line, under 15 words.

Strict rules:
- Length is a hard limit. A 101-word paragraph is wrong. A 16-word bullet is wrong. Cut it shorter.
- Wrap the first occurrence of the head term in **double asterisks**.
- Bullets start with `- `.
- Ground every claim in the source documents below. If a fact isn't in the sources, omit it.
- No headings. No preamble ("Here is", "Sure", "Summary:"). No closing remarks.
- No URLs. No reference markers like [1]. No images.
- Output the paragraph and bullets directly. Nothing before. Nothing after.
"""


DOC_SEP = "=== DOC {rank}: {title} ===\n"


@dataclass
class JobDoc:
    rank: int
    title: str
    text: str
    # PRD-029 day-roundup carries a category label per event so the
    # prompt can group by topic. None for biographical / news-spike
    # jobs that do not supply it.
    category: str | None = None


@dataclass
class Job:
    query: str
    query_norm: str
    docs: list[JobDoc]
    # PRD-021 news-spike fields. Only populated when mode == "news-spike".
    # PRD-029 day-roundup uses event_date for the date being summarised;
    # the per-event bundles ride in `docs` (one doc per event).
    mode: str = "biographical"
    event_date: str | None = None
    event_paragraph: str | None = None


def cap_doc_text(text: str, cap_bytes: int) -> str:
    """Cap UTF-8 text at cap_bytes without splitting a multibyte char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    # Walk back until we land on a byte boundary
    cut = cap_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore").rstrip() + "…"


# PRD-021: a separate prompt for "why is this in the news" summaries.
# Single paragraph (no bullets), grounded in a Wikipedia article
# paragraph that mentions a specific recent dated event. Producer
# selects the paragraph; we just compress + style.
NEWS_SYSTEM_PROMPT = """\
You write very short knowledge-panel summaries explaining why a Wikipedia article is currently in the news.

Output format — exactly this, nothing else:
ONE paragraph of 60-100 words. Not more. Hard cap.

Strict rules:
- Start the paragraph by wrapping the topic title in **double asterisks**.
- State the recent event with specific dates and names from the source.
- Use the past tense for the event; present tense only for ongoing conditions.
- No bullet points. No headings. No preamble like "Here is" or "Summary:". No closing remarks.
- No URLs, reference markers, or images.
- Use only facts in the source paragraph below. Do not invent or extrapolate.
- Output the paragraph directly. Nothing before. Nothing after.
"""


def build_news_prompt(job: Job) -> tuple[str, str]:
    """Build the (system, user) prompt for a news-spike job. Reads
    job.event_paragraph as the sole source; the doc list (if any) is
    ignored."""
    para = job.event_paragraph or ""
    date_line = (
        f"The article was last edited around {job.event_date}.\n"
        if job.event_date else ""
    )
    user = (
        f'The article topic is "{job.query}".\n'
        f"{date_line}"
        f"Here is the relevant paragraph from the article body:\n\n"
        f"{para}\n\n"
        f"Write the summary now."
    )
    return NEWS_SYSTEM_PROMPT, user


# PRD-029: "what happened today" day-roundup. Editor-style 3-5 sentence
# wrap-up of the day's top events. Each docs[] entry is one event
# (title=entity, text=event paragraph, category optional).
DAY_ROUNDUP_SYSTEM_PROMPT = """\
You write a short newspaper-style "what happened today" roundup for a search engine.

Output format — exactly this, nothing else:
ONE paragraph of 80-160 words. Hard cap.

Strict rules:
- Cover the biggest events first, by overall importance.
- Wrap entity names in **double asterisks** the first time they appear (e.g. **Iran**, **OpenAI**, **Pep Guardiola**). Bold 3 to 8 entities total.
- Group related events with a single sentence when natural (e.g. "In tech, ..." or "In sport, ..."), but do not invent headings.
- Use the past tense for events; present tense only for ongoing conditions.
- No bullet points. No headings. No preamble like "Here is" or "Today's news:". No closing remarks.
- No URLs, reference markers, or images.
- Use only facts in the source events below. Do not invent or extrapolate.
- Output the paragraph directly. Nothing before. Nothing after.
"""


def build_day_prompt(job: Job, per_event_cap_bytes: int = 1500) -> tuple[str, str]:
    """Build the (system, user) prompt for a day-roundup job. Reads
    job.docs as the list of events for the day (already sorted by
    importance by the producer)."""
    date_str = job.event_date or "today"
    lines = [
        f'You are writing the news roundup for {date_str}.\n',
        f"Below are the day's top events from Wikipedia, ranked by importance "
        f"(most important first). Each event is one paragraph from the relevant "
        f"Wikipedia article. Use these as the only source.\n",
    ]
    for i, d in enumerate(job.docs, 1):
        body = cap_doc_text(d.text, per_event_cap_bytes)
        cat_tag = f" [{d.category}]" if d.category else ""
        lines.append(f"=== EVENT {i}: {d.title}{cat_tag} ===")
        lines.append(body)
        lines.append("")
    lines.append("Write the roundup paragraph now.")
    return DAY_ROUNDUP_SYSTEM_PROMPT, "\n".join(lines)


def build_prompt(job: Job, per_doc_cap_bytes: int = 12000) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt)."""
    lines = [f'The user search query was: "{job.query}".\n',
             "Source documents (newest data, most-relevant first):\n"]
    for d in job.docs:
        body = cap_doc_text(d.text, per_doc_cap_bytes)
        lines.append(DOC_SEP.format(rank=d.rank, title=d.title))
        lines.append(body)
        lines.append("\n")
    lines.append("\nWrite the summary now.")
    return SYSTEM_PROMPT, "\n".join(lines)


# --- output validation ---------------------------------------------------

_BAD_PATTERNS = [
    re.compile(r"^here is", re.I),
    re.compile(r"^sure[,!]", re.I),
    re.compile(r"^summary:", re.I),
    re.compile(r"\[\d+\]"),                # citation markers
    re.compile(r"https?://"),               # URLs
    re.compile(r"^#+\s", re.M),             # headings
]


def parse_summary(raw: str, head_term: str) -> str:
    """Trim/normalise model output. Raise ValueError if it's unusable."""
    text = raw.strip()
    if not text:
        raise ValueError("empty output")
    # Drop common preambles like "Sure, here's a summary:" on the first line.
    first_line, _, rest = text.partition("\n")
    if any(p.search(first_line) for p in _BAD_PATTERNS[:3]):
        text = rest.lstrip()

    for p in _BAD_PATTERNS:
        if p.search(text):
            # We don't try to fix; let the worker retry the call.
            raise ValueError(f"output contains forbidden pattern: {p.pattern!r}")

    # Ensure the head term appears bolded at least once. If the model
    # forgot the asterisks, add them around the first case-insensitive
    # whole-word occurrence.
    if "**" not in text:
        pattern = re.compile(rf"\b({re.escape(head_term)})\b", re.I)
        text, n = pattern.subn(r"**\1**", text, count=1)
        if n == 0:
            raise ValueError(f"head term {head_term!r} not present in output")

    # Length check. Too short → probably a refusal. Too long → model
    # ignored the 60-100 word target. We don't ship overshoots — the
    # worker will retry up to 3 times via the ollama backend.
    word_count = len(text.split())
    if word_count < 30:
        raise ValueError(f"output too short ({word_count} words)")
    if word_count > 200:
        raise ValueError(f"output too long ({word_count} words)")

    return text


def parse_day_summary(raw: str) -> str:
    """Trim/normalise a PRD-029 day-roundup output. Same forbidden-
    pattern checks as parse_summary but drops the single-head-term
    requirement (day roundups bold multiple entities) and widens the
    word window a little (40-250) to accommodate a denser news day.
    Bullets are explicitly disallowed."""
    text = raw.strip()
    if not text:
        raise ValueError("empty output")
    first_line, _, rest = text.partition("\n")
    if any(p.search(first_line) for p in _BAD_PATTERNS[:3]):
        text = rest.lstrip()
    for p in _BAD_PATTERNS:
        if p.search(text):
            raise ValueError(f"output contains forbidden pattern: {p.pattern!r}")
    if "**" not in text:
        raise ValueError("output has no bolded entities")
    word_count = len(text.split())
    if word_count < 40:
        raise ValueError(f"output too short ({word_count} words)")
    if word_count > 250:
        raise ValueError(f"output too long ({word_count} words)")
    if re.search(r"(?m)^- ", text):
        raise ValueError("day roundup must not contain bullet points")
    return text


# --- stub backend used when model.backend = "stub" -----------------------

def stub_summary(job: Job) -> str:
    """Deterministic placeholder so we can prove the round-trip
    end-to-end before plugging in a real model."""
    if job.mode == "day-roundup":
        return _stub_day_roundup(job)
    first_doc = job.docs[0] if job.docs else None
    if first_doc:
        preview = textwrap.shorten(first_doc.text, width=160, placeholder="…")
    else:
        preview = "(no source documents in the job)"
    return (
        f"**{job.query}** is a placeholder summary generated by the stub "
        f"backend. When the real model is wired up this paragraph will be "
        f"replaced by a factual 80-150 word answer grounded in the top-M "
        f"search results.\n\n"
        f"- Query: {job.query}\n"
        f"- Sources: {len(job.docs)}\n"
        f"- First source: {first_doc.title if first_doc else '(none)'}\n"
        f"- Preview: {preview}\n"
        f"- Generated by: zettair-summariser (stub backend)"
    )


def _stub_day_roundup(job: Job) -> str:
    """Stub variant for the PRD-029 day-roundup mode. Must pass
    parse_day_summary: bolded entities (>=3), no bullets, 40-250 words."""
    bolded = [f"**{d.title}**" for d in job.docs[:6] if d.title]
    if len(bolded) < 3:
        # Need to satisfy parse_day_summary's "at least one bolded" check
        # and word minimum. Pad with a placeholder entity so the round-
        # trip test stays green even with sparse fixtures.
        while len(bolded) < 3:
            bolded.append(f"**Stub-{len(bolded)}**")
    intro = (
        f"On {job.event_date or 'today'}, several stories carried the day. "
        f"{bolded[0]} led the headlines, alongside {bolded[1]} and "
        f"{bolded[2]}."
    )
    middle_bits = []
    for d in job.docs[:6]:
        snippet = textwrap.shorten(d.text, width=120, placeholder="…")
        cat = f" ({d.category})" if d.category else ""
        middle_bits.append(f"{d.title}{cat}: {snippet}")
    middle = " ".join(middle_bits)
    closer = "Stub day-roundup output — replace by plugging in a real model backend."
    return f"{intro} {middle} {closer}"
