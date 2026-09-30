"""What a tool needs to know that the model must not be asked to supply.

EVERY TOOL IN THIS PACKAGE IS A CLOSURE OVER ONE OF THESE. The alternative —
tools that take `chapter_markdown` or `school_id` as arguments — fails in two
specific ways, both of which the pipeline has already been bitten by elsewhere:

  * a 90,000-character chapter cannot be an argument. The model would have to
    hold the text in order to ask a question about the text, which is the exact
    context explosion §7 of the architecture is about.
  * `school_id` as an argument is an authorisation hole. A tool that accepts the
    school whose adaptive state to read is a tool that can be talked into
    reading another school's. Bound at construction, it cannot be.

So the run's identity and its corpus are fixed when the agent is built, and the
model's tool calls carry only what it genuinely chooses: a page number, a
heading, a competency id.

`resource_level`, `grade` and `subject` are here rather than looked up per call
because they change the ANSWER a pedagogy lookup gives, and a tool whose answer
silently depends on an unstated ambient value is not reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RunContext:
    """One chapter, for one class, at one moment."""

    # ── Corpus ───────────────────────────────────────────────────────────────
    chapter_markdown: str = ""
    chapter_title: str = ""
    chapter_number: Optional[int] = None
    # id/page/caption/path, as a classified ingest produced them. The agents get
    # captions and page numbers, never file paths — see tools/book.py.
    chapter_figures: list[dict] = field(default_factory=list)
    page_start: Optional[int] = None
    page_end: Optional[int] = None

    # ── Who it is for ────────────────────────────────────────────────────────
    grade: str = ""
    subject: str = ""
    school_id: Optional[str] = None
    class_id: Optional[str] = None
    teacher_id: Optional[str] = None

    # ── The room, as Node 1 knows it ─────────────────────────────────────────
    resource_level: int = 0
    teacher_settings: dict = field(default_factory=dict)

    # ── Node 1's output, when this context serves Node 2 ─────────────────────
    # The whole contract document. Node 2's tools read it; nothing writes it.
    contract: dict = field(default_factory=dict)
    # The normalised ContextProfile.as_dict() and the per-topic activation
    # summaries, both produced deterministically before the agent is built —
    # architecture §11: inactive factors never reach the prompt.
    profile: dict = field(default_factory=dict)
    activation: dict = field(default_factory=dict)

    # ── Sequencing output, when it exists ────────────────────────────────────
    topics: list[dict] = field(default_factory=list)

    def chapter_key(self) -> str:
        """Stable enough to name an artifact, loose enough to survive a retitle."""
        number = self.chapter_number if self.chapter_number is not None else "x"
        slug = "".join(c if c.isalnum() else "-" for c in self.chapter_title.lower())
        return f"{self.grade}-{self.subject}-{number}-{slug.strip('-')[:40]}"


# Tool answers are read by a model and must fit in a turn. These are the
# ceilings; each tool says in its docstring which one it is bounded by, so a
# truncated answer reads as a limit rather than as missing data.
MAX_TOOL_CHARS = 6_000
MAX_ROWS = 40


def truncated(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    """Cut with a visible marker.

    Silent truncation is the failure `prep_flow/llm.py` documents at length: a
    model shown a cut-off page reasons about the cut as if it were the content.
    Saying so costs 60 characters and prevents that.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated at {limit} characters — narrow the range and ask again]"
