"""Prep Material Sequencing Agent — turns one chapter into the ordered spine.

Output is the Chapter Generation Batch: T1 -> T2 -> ... -> T30/40, each anchored
to the pages it teaches from. Everything downstream depends on this order being
decided ONCE and never revisited, because the Refresher of T(n) is defined by
the Explore of T(n-1) — reordering topics after generation would invalidate
every Refresher in the batch.

The split is DETERMINISTIC: no model is asked where the topics are, because the
book has already answered that. A heading starts a new period, a page marker
starts one in a chapter that prints no headings, and the order the stretches
come out in is the order they are printed in. The same chapter therefore always
yields the same spine -- rerun it, regenerate it a month later, diff two runs,
and the topic list is identical.

That is worth more here than a model's judgement. The sequence is decided ONCE
and never revisited (see above), so a run-to-run wobble in it is not a small
difference: it silently changes what every Refresher in the batch refers back
to. And the thing a model was being asked to infer -- where one idea stops and
the next begins -- is something the book states outright with [TOPIC] and
[SUBTOPIC], which `tools.render_classified` turns into the ## and ### headings
this module cuts on.
"""
import re
from typing import Optional

from ..state import ChapterState
from ..tools import (
    TOPIC_EXCERPT_CHARS,
    page_index,
    pages_for_span,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

# A heading is only worth offering as an anchor if it OWNS enough of the chapter
# to teach a period from.
#
# This replaces a filter on the heading's TEXT — anything starting "3. " or
# "(A) " was dropped as a numbered exercise. The intent was right: anchoring a
# topic to "3. Count and write." yields a topic called "5." and an excerpt of one
# fill-in-the-blank. The method was not. On Class 3 Maths chapter 2 it threw away
# "3. Let us see the shapes in objects", "5. Let's play with match-sticks" and
# "3. Count the hundreds" — three of the book's four real section titles — while
# keeping "Example:" and "Do This". The sequencer could not anchor topic-wise
# because the topics were never in the list it was shown.
#
# No string test separates "3. Count and write." from "3. Count the hundreds".
# How much chapter follows does, and it works on any book's numbering
# conventions, including one whose ingest marks exercise items as headings.
MIN_SECTION_CHARS = 250


# The name a topic uses to claim the stretch before the chapter's first heading.
# Not a real heading, so it cannot be matched by text — opening_span() resolves it
# by position instead. It exists because heading-anchoring alone cannot reach
# unheaded text: on Class 3 Maths chapter 2 the first heading sits on page 14, so
# a three-topic run began there and pages 12-13 were never taught by anything.
OPENING_ANCHOR = "(the chapter's opening, before the first heading)"


def opening_span(markdown: str) -> Optional[tuple[int, int]]:
    """(start, end) of the chapter before its first heading, if that is worth teaching."""
    first = _HEADING.search(markdown or "")
    end = first.start() if first else len(markdown or "")
    return (0, end) if end >= MIN_SECTION_CHARS else None


def chapter_outline(markdown: str, limit: int = 120) -> list[dict]:
    """The chapter's own structure, as anchors the sequencing agent can name.

    Giving the model real headings and real page numbers to point at is what
    keeps the resulting page ranges honest. Asked to invent page numbers, it
    invents plausible ones; asked to pick from a list, it picks.
    """
    pages = page_index(markdown)

    def page_at(offset: int) -> Optional[int]:
        for page, start, end in pages:
            if start <= offset < end:
                return page
        return pages[0][0] if pages else None

    marks = [(m, len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(markdown)]

    def owns(i: int, level: int) -> int:
        """Characters between this heading and the next of the same or higher rank."""
        for later, later_level, _ in marks[i + 1:]:
            if later_level <= level:
                return later.start() - marks[i][0].start()
        return len(markdown) - marks[i][0].start()

    out: list[dict] = []
    opening = opening_span(markdown)
    if opening:
        first, last = pages_for_span(markdown, *opening)
        out.append({"heading": OPENING_ANCHOR, "level": 1,
                    "page": first, "spans": (first, last)})

    for i, (match, level, title) in enumerate(marks):
        width = owns(i, level)
        if not title or width < MIN_SECTION_CHARS:
            continue
        first, last = pages_for_span(markdown, match.start(), match.start() + width)
        out.append({
            "heading": title,
            "level": level,
            "page": page_at(match.start()),
            "spans": (first, last),
        })
        if len(out) >= limit:
            break
    return out


# -- Cutting the chapter, deterministically ------------------------------------
#
# Everything below decides the spine without a model. The rule is that a
# boundary has to be something the BOOK printed: a heading, or failing that a
# page break. Where the chapter has to be cut finer than the book marks it, the
# split still lands on a printed boundary -- never at an arbitrary character
# offset that would slice a sentence, or worse an activity, in half.

_PAGE_MARK = re.compile(r"^<!--\s*page\s+(\d+)\s*-->", re.M | re.I)
_LABEL_LINE = re.compile(r"^\[([A-Z][A-Z0-9 _-]*)\][ ]*(.*)$")

# A period title is read off a sheet aloud, so it is a phrase, not a paragraph.
_TITLE_CHARS = 70


def _page_title(start: Optional[int], end: Optional[int]) -> str:
    if start is None:
        return "Untitled section"
    return f"Page {start}" if not end or end == start else f"Pages {start}-{end}"


def _segment_title(markdown: str, start: int, end: int, fallback: str) -> str:
    """A title for a stretch the book gave no heading to, read out of its text.

    The first real sentence it teaches, which is what a teacher scanning a
    contents list needs. Figures and page furniture are skipped: "The image
    shows a cartoon illustration of a child" is a caption, not a period.
    """
    for line in markdown[start:end].splitlines():
        line = line.strip()
        if not line or line.startswith("<!--"):
            continue
        match = _LABEL_LINE.match(line)
        if match:
            name, text = match.group(1), match.group(2).strip()
            if name in ("FIGURE", "FIGURE_DESCRIPTION", "OTHER") or not text:
                continue
            line = text
        elif line.startswith("["):
            continue
        elif line.startswith("#"):
            line = line.lstrip("#").strip()
        if len(line) < 12:
            continue
        # The first sentence, unless the "sentence" is an exercise number: the
        # full stop in "3. Srinivas to Sunil" is not the end of a thought, and
        # splitting on it titles the period "3.".
        sentence = re.split(r"(?<=[.?!])\s", line)[0].strip()
        if len(sentence) < 12:
            sentence = line
        if len(sentence) > _TITLE_CHARS:
            sentence = sentence[:_TITLE_CHARS].rsplit(" ", 1)[0] + "..."
        return sentence
    return fallback


def _join(a: dict, b: dict) -> dict:
    """Two adjacent stretches as one. The earlier one's title wins -- it is
    where the merged period starts, and so what it is about."""
    return {"start": a["start"], "end": b["end"],
            "title": a["title"] or b["title"],
            "level": min(a["level"], b["level"]),
            "heading": a["heading"] or b["heading"]}


def _split(cut: dict, markdown: str) -> list[dict]:
    """One stretch as two, cut at the printed boundary nearest its middle.

    Returns [] when no boundary leaves both halves teachable, which is how a
    chapter with fewer sections than the target asked for stops cleanly rather
    than producing slivers.
    """
    start, end = cut["start"], cut["end"]
    if (end - start) < 2 * MIN_SECTION_CHARS:
        return []

    def usable(offsets):
        return [o for o in offsets
                if o - start >= MIN_SECTION_CHARS and end - o >= MIN_SECTION_CHARS]

    # A page break first: the book printed it, and both halves stay citable as
    # whole pages, which is what every (Page N) on the sheet is measured against.
    candidates = usable(m.start() for m in _PAGE_MARK.finditer(markdown, start, end))
    if not candidates:
        # Otherwise a block boundary -- still a line the ingest drew, so the cut
        # never lands inside a sentence or halfway through an activity.
        candidates = usable(start + m.start()
                            for m in re.finditer(r"^\[", markdown[start:end], re.M))
    if not candidates:
        return []

    middle = (start + end) // 2
    at = min(candidates, key=lambda o: abs(o - middle))
    return [{**cut, "end": at},
            {**cut, "start": at, "title": "", "heading": ""}]


def _merge_thin(cuts: list[dict]) -> list[dict]:
    """Fold any stretch too small to teach a period from into its neighbour."""
    out: list[dict] = []
    for cut in cuts:
        if out and (cut["end"] - cut["start"]) < MIN_SECTION_CHARS:
            out[-1] = _join(out[-1], cut)
        else:
            out.append(dict(cut))
    while len(out) > 1 and (out[0]["end"] - out[0]["start"]) < MIN_SECTION_CHARS:
        out[0] = _join(out[0], out[1])
        del out[1]
    return out


def cut_chapter(markdown: str, *, target: int, maximum: int) -> list[dict]:
    """The chapter as consecutive, non-overlapping stretches, in printed order.

    Boundaries are the book's own: every heading starts one, and a chapter with
    no headings falls back to its page markers. Cutting at EVERY heading
    regardless of level, rather than picking a level to cut at, is what keeps
    the stretches non-overlapping -- a level-2 heading followed by a level-3 one
    yields the parent's own introduction and then the child, which is exactly
    how the two are printed and read.
    """
    total = len(markdown)
    marks = [(m.start(), len(m.group(1)), m.group(2).strip())
             for m in _HEADING.finditer(markdown)]

    cuts: list[dict] = []
    if marks:
        if marks[0][0] >= MIN_SECTION_CHARS:
            cuts.append({"start": 0, "end": marks[0][0], "title": "",
                         "level": 1, "heading": OPENING_ANCHOR})
        for i, (offset, level, title) in enumerate(marks):
            cuts.append({"start": offset,
                         "end": marks[i + 1][0] if i + 1 < len(marks) else total,
                         "title": title, "level": level, "heading": title})
    else:
        spans = page_index(markdown)
        cuts = [{"start": s, "end": e, "title": "", "level": 1, "heading": ""}
                for _, s, e in spans] or [
            {"start": 0, "end": total, "title": "", "level": 1, "heading": ""}]

    cuts = _merge_thin(cuts)

    ceiling = max(1, min(int(target), int(maximum)))

    # More sections than periods asked for: merge the cheapest ADJACENT pair, so
    # the two smallest ideas share a period rather than a large one being cut
    # down. Adjacent only -- merging two stretches that are not neighbours would
    # produce a period teaching two parts of the book with a gap between them.
    while len(cuts) > ceiling:
        i = min(range(len(cuts) - 1),
                key=lambda j: (cuts[j]["end"] - cuts[j]["start"])
                + (cuts[j + 1]["end"] - cuts[j + 1]["start"]))
        cuts[i] = _join(cuts[i], cuts[i + 1])
        del cuts[i + 1]
    # Fewer: split the widest, the one most likely to be holding more than a
    # period's worth. Stops as soon as nothing splits cleanly, which is how a
    # chapter with genuinely fewer sections than the target returns fewer topics
    # instead of slivers.
    while len(cuts) < ceiling:
        i = max(range(len(cuts)), key=lambda j: cuts[j]["end"] - cuts[j]["start"])
        halves = _split(cuts[i], markdown)
        if not halves:
            break
        cuts[i:i + 1] = halves
    return cuts


def derive_topics(markdown: str, *, target: int, maximum: int) -> list[dict]:
    """The ordered spine, straight from the chapter's own structure.

    Holds the INVARIANT the whole pipeline is built on: the page range
    recorded on a topic describes the excerpt that
    topic was actually handed, never the section it was cut out of. Every
    (Page N) the sheet prints and every grounding check the validator runs is
    measured against it.
    """
    topics: list[dict] = []
    seen: dict[str, int] = {}
    for cut in cut_chapter(markdown, target=target, maximum=maximum):
        excerpt = markdown[cut["start"]:cut["end"]][:TOPIC_EXCERPT_CHARS]
        start, end = pages_for_span(markdown, cut["start"],
                                    cut["start"] + len(excerpt))
        title = cut["title"] or _segment_title(
            markdown, cut["start"], cut["start"] + len(excerpt),
            _page_title(start, end))
        key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            title = f"{title} ({seen[key]})"
        topics.append({
            "index": len(topics) + 1,
            "topic": title,
            "subtopic": "",
            "page_start": start,
            "page_end": end,
            # Char offsets into THIS chapter's markdown, always known even when
            # the source carries no <!-- page N --> markers -- the fallback that
            # lets downstream excerpt reconstruction work without pages. See
            # generation/adapt.py::sources_from_chapter and contract.py::_grounding.
            "excerpt_start": cut["start"],
            "excerpt_end": cut["start"] + len(excerpt),
            "anchor_heading": cut.get("heading") or "",
            "anchor_kind": "heading" if cut["title"] else (
                "pages" if start is not None else "proportional"),
            "excerpt": excerpt,
            # The book's order IS the dependency order: a period is
            # prepared for by the one printed before it. No model is needed to
            # assert that, and index 0 falsifies to None for the first topic.
            "prerequisite": len(topics) or None,
            "why": "",
        })
    return topics


async def sequencing_node(state: ChapterState) -> dict:
    config = state.get("config") or {}
    if state.get("skip_sequencing") and state.get("topics"):
        return {"topics": state["topics"],
                "sequencing_note": state.get("sequencing_note") or "",
                "status": "running"}

    markdown = state.get("chapter_markdown") or ""
    if not markdown.strip():
        return {
            "status": "failed",
            "errors": ["sequencing: the chapter has no text to sequence"],
        }

    outline = chapter_outline(markdown)
    minimum = int(config.get("min_topics", 8))
    maximum = int(config.get("max_topics", 40))
    target = max(minimum, min(int(config.get("target_topics", 30)), maximum))

    pages = [p for p, _, _ in page_index(markdown)]
    topics = derive_topics(markdown, target=target, maximum=maximum)

    # Attach the figures printed on each topic's own pages. Done here rather than
    # in the prompt builder because page ranges are settled here and nowhere elss
    for spec in topics:
        lo, hi = spec.get("page_start"), spec.get("page_end")
        spec["figures"] = [f for f in (state.get("chapter_figures") or [])
                           if lo is not None and lo <= (f.get("page") or -1) <= (hi or lo)]

    warnings: list[str] = []
    # Two conditions that make everything downstream quietly weaker, and are
    # invisible in the output: a chapter cut short loses the topics at the end of
    # the syllabus, and one with no page markers gives every topic a guessed
    # slice instead of its own pages — so the Concept grounding check has nothing
    # real to check against and (Page N) citations are impossible.
    declared_end = state.get("page_end")
    if pages and declared_end and pages[-1] < int(declared_end):
        warnings.append(
            f"sequencing: the chapter text stops at page {pages[-1]} but the chapter "
            f"runs to {declared_end} — topics after that page could not be sequenced "
            f"(raise PREP_FLOW_MAX_CHAPTER_CHARS, or finish ingesting the book)")
    if not pages:
        warnings.append(
            "sequencing: this chapter has no <!-- page N --> markers, so per-topic "
            "excerpts fall back to a proportional slice and Concept cannot cite pages")

    if len(topics) < minimum:
        return {
            "status": "failed",
            "errors": [
                f"sequencing: produced {len(topics)} usable topic(s), below the "
                f"minimum of {minimum}. The chapter text may be too thin to teach "
                f"from ({len(markdown)} chars, {len(pages)} page marker(s))."
            ],
            "topics": topics,
        }

    anchored = sum(1 for t in topics if t["anchor_kind"] == "heading")
    if anchored == len(topics):
        note = (f"Cut on the {anchored} section heading(s) the book prints, in "
                f"printed order.")
    elif anchored:
        note = (f"Cut on the book's own boundaries: {anchored} of {len(topics)} "
                f"period(s) start at a printed heading, the rest at a page break.")
    else:
        note = ("This chapter prints no headings, so it is cut on its page "
                "breaks, in printed order.")

    return {
        "topics": topics,
        "sequencing_note": note,
        "status": "running",
        "errors": warnings,
        "metrics": {
            "topics_sequenced": len(topics),
            "outline_headings": len(outline),
            "chapter_pages_available": len(pages),
            "topics_grounded_by_page": sum(
                1 for t in topics if t.get("page_start") in set(pages)),
            "figures_available": sum(len(t.get("figures") or []) for t in topics),
            "figures_with_image": sum(1 for t in topics
                                      for f in (t.get("figures") or []) if f.get("path")),
        },
    }
