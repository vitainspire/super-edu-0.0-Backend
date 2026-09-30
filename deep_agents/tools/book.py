"""Book tools — architecture §17(A). The only way an agent reaches the textbook.

WHY THESE ARE TOOLS AND NOT A PROMPT BLOCK. The existing curriculum call is
handed a fixed excerpt window: `TOPIC_EXCERPT_CHARS` of text chosen before the
model saw anything (`prep_flow/tools.py`). That is the right shape for an
extraction — you know in advance which pages you are extracting from. It is the
wrong shape for the question this stage actually asks, which is *"what does a
child need before this page means anything"*, because the answer is frequently
on a page nobody thought to include: the worked example three pages earlier that
the exercise silently assumes.

Given `get_page_range`, an agent that suspects a missing prerequisite can go and
check instead of inventing one. That is the single reason this layer is worth
its cost, and it is the thing to measure it on.

GROUNDING IS PRESERVED, not relaxed. Every excerpt these return keeps its
`<!-- page N -->` markers, because the validator treats the sliced text as the
only thing a Concept bullet may be grounded in, and a citation the model writes
from a marker it can see is checkable where one it infers is not. See
`validation_flow/checks.py`.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from prep_flow import tools as gateway

from .runtime import MAX_ROWS, RunContext, truncated


def build(ctx: RunContext) -> list[BaseTool]:
    """The book tools, bound to one chapter."""

    @tool
    def get_book_order() -> str:
        """List the pages this chapter occupies and the topics the chapter has
        been cut into, in printed order.

        Call this FIRST. Everything else in this toolset takes a page number or
        a heading, and this is where the legal values come from. The order is
        the book's own and is not negotiable — see the sequencing note in the
        system prompt.
        """
        pages = gateway.page_index(ctx.chapter_markdown)
        lines = []
        if pages:
            lines.append(f"Pages present: {pages[0][0]}–{pages[-1][0]} "
                         f"({len(pages)} page markers)")
        else:
            lines.append("This chapter has no page markers; ask by heading instead.")
        if ctx.topics:
            lines.append("")
            lines.append("Topics, in the order the book teaches them:")
            for spec in ctx.topics[:MAX_ROWS]:
                span = ""
                if spec.get("page_start") is not None:
                    span = f" (pages {spec.get('page_start')}–{spec.get('page_end')})"
                lines.append(f"  T{spec.get('index')}: {spec.get('topic')}"
                             f" / {spec.get('subtopic', '')}{span}")
        return "\n".join(lines)

    def _pages(start: int, end: int) -> str:
        """The shared implementation. Both page tools call this rather than each
        other: reaching through a decorated tool's `.func` couples this module to
        an implementation detail of LangChain's tool wrapper, and it is the kind
        of coupling that survives review and breaks on an upgrade."""
        if start > end:
            start, end = end, start
        text = gateway.excerpt_for_pages(ctx.chapter_markdown, start, end,
                                         limit=6_000)
        if not text.strip():
            return f"No text found for pages {start}-{end}. Call get_book_order first."
        return truncated(text)

    @tool
    def get_page_range(start: int, end: int) -> str:
        """Return the verbatim textbook text for pages `start` to `end` inclusive.

        Page markers are kept in the returned text - cite them as (Page N).
        Ask for the narrowest range that answers your question: the answer is
        capped at 6000 characters and a wide range is truncated rather than
        summarised.
        """
        return _pages(start, end)

    @tool
    def get_book_page(page: int) -> str:
        """Return the verbatim text of a single page. Prefer this when checking
        one specific claim; use get_page_range when following an explanation
        across pages."""
        return _pages(page, page)

    @tool
    def search_book(phrase: str) -> str:
        """Find where a phrase appears in this chapter, with its page number and
        the sentence around it.

        Use it to answer "does the book ever actually say this" — the question
        behind every prerequisite you are about to assert. A prerequisite the
        book already teaches on an earlier page is not a prerequisite; it is
        content, and saying otherwise sends the class backwards.
        """
        needle = (phrase or "").strip().lower()
        if len(needle) < 3:
            return "Give a phrase of at least 3 characters."
        markdown = ctx.chapter_markdown
        hits: list[str] = []
        pages = gateway.page_index(markdown) or [(0, 0, len(markdown))]
        low = markdown.lower()
        start = 0
        while len(hits) < 12:
            found = low.find(needle, start)
            if found < 0:
                break
            page = next((p for p, s, e in pages if s <= found < e), None)
            lo, hi = max(0, found - 160), min(len(markdown), found + 200)
            snippet = " ".join(markdown[lo:hi].split())
            hits.append(f"(Page {page}) …{snippet}…" if page else f"…{snippet}…")
            start = found + len(needle)
        if not hits:
            return (f"'{phrase}' does not appear in this chapter. "
                    "If you were about to rely on the book saying it, do not.")
        return "\n\n".join(hits)

    @tool
    def list_figures() -> str:
        """Every figure printed in this chapter: page, id and caption.

        Figures are what makes a Grade 3 page teachable — the anchor object a
        topic reaches for is often already printed beside it. Prefer an object
        the child can see on the page over one you introduce.
        """
        figures = ctx.chapter_figures or []
        if not figures:
            return "No classified figures were ingested for this chapter."
        rows = [f"  page {f.get('page')}: [{f.get('id')}] {f.get('caption') or '(no caption)'}"
                for f in figures[:MAX_ROWS]]
        more = "" if len(figures) <= MAX_ROWS else f"\n  … and {len(figures) - MAX_ROWS} more"
        return "Figures in this chapter:\n" + "\n".join(rows) + more

    @tool
    def get_figure(figure_id: str) -> str:
        """The caption and page of one figure, by the id `list_figures` gave.

        Returns text only. The image itself is not offered: an anchor object
        chosen from a caption is one a teacher can find on the page, and one
        chosen from pixels is one only this run can see.
        """
        for fig in ctx.chapter_figures or []:
            if str(fig.get("id")) == str(figure_id):
                return (f"Figure {fig.get('id')} — page {fig.get('page')}\n"
                        f"Caption: {fig.get('caption') or '(none)'}")
        return f"No figure with id '{figure_id}'. Call list_figures for the ids."

    return [get_book_order, get_page_range, get_book_page, search_book,
            list_figures, get_figure]
