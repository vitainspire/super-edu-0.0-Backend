"""The Tool Gateway, bound for agents.

`prep_flow/tools.py` already carries this package's rule that agents do no I/O
of their own. This subpackage does not replace it — every function here calls
through it — it re-presents it in the one shape a Deep Agent can use: named
tools with docstrings the model reads as instructions.

THE DOCSTRINGS ARE PART OF THE DESIGN, not documentation of it. A tool
description is the only place to say "an empty answer here is a real answer",
and that sentence is what stops an agent inventing a competency id when the
library has none. Treat them as prompt text under review, not as comments.

Four toolsets, one per architecture section 17 category:

    book        the textbook, by page, heading and figure
    knowledge   the canonical library and the reasoning cache
    pedagogy    activity templates, the grade's cognitive ceiling, the room
    room        Node 2 only: the contract, and the context that survived
                deterministic activation
"""
from .runtime import MAX_ROWS, MAX_TOOL_CHARS, RunContext, truncated

from . import book, knowledge, pedagogy, room

__all__ = ["RunContext", "MAX_ROWS", "MAX_TOOL_CHARS", "truncated",
           "book", "knowledge", "pedagogy", "room"]
