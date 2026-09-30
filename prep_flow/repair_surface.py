"""How much of a sheet one diagnosis is allowed to rewrite.

Targeted repair only works because most of the sheet is off limits. `_merge_sections`
takes every untargeted section from the ORIGINAL regardless of what comes back,
so the sections a diagnosis does NOT name are the safety mechanism — the model is
asked to reproduce them verbatim and mostly does, and "mostly" is not a
guarantee.

A diagnosis that names all six therefore turns targeted repair back into an
unguarded full rewrite, which is the riskiest operation in the pipeline: it can
fix the Concept and break the Challenge, and nothing would catch it except the
gates the sheet has already passed.

MEASURED, NOT ASSUMED. Across one three-topic run:

    learner / application    2 sections   concept, challenge
    learner / forward        1 section    explore
    teacher / struggle       6 sections   all of them
    teacher / struggle       6 sections   all of them

The learner gate localises. The teacher gate's `struggle` facet does not, and it
is easy to see why — "the sheet never says what to do when this fails" is true of
every section at once, so every section is a fair answer to "where would you fix
it". True, and useless: the rewrite still has to happen somewhere in particular.

The prompts already say "a diagnosis listing all six found nothing". Both models
ignore it for this facet. That is the ordinary division of labour in this
codebase — a prompt can ask for a bound, only code can enforce one.

WHAT THE CAP IS NOT. It is not a claim that the sheet is fine everywhere else.
The diagnosis and its root cause are stored in full and reach the review packet
intact; what is bounded is only how much prose one rewrite may touch at a time.
A gap that really is everywhere will fail again on the next round and get another
three sections, which is a slower fix than one big rewrite and a much safer one.
"""
from .sections import SECTION_ORDER

# Three of six. Enough to fix a gap that spans a couple of sections, and few
# enough that half the sheet is still taken from the original and cannot be
# damaged by the rewrite that fixes the other half.
MAX_SECTIONS = 3


def clamp(named: list, *, fallback_all: bool = True) -> list:
    """The sections one repair may rewrite, in teaching order.

    `named` is taken in the order the diagnosis gave it: a model listing sections
    tends to lead with the one it means most, and sorting before truncating would
    throw that away and keep whichever happens to come first in SECTION_ORDER.
    The result is re-sorted for the rewrite, because the repair prompt reads
    better in the order the sheet is taught.

    An empty list means a structural failure rather than a diagnosed one, and
    those genuinely do want the whole sheet — `fallback_all` is how the caller
    says so.
    """
    seen, ordered = set(), []
    for section in named or []:
        if section in SECTION_ORDER and section not in seen:
            seen.add(section)
            ordered.append(section)

    if not ordered:
        return list(SECTION_ORDER) if fallback_all else []

    if len(ordered) > MAX_SECTIONS:
        ordered = ordered[:MAX_SECTIONS]
    return sorted(ordered, key=SECTION_ORDER.index)


def was_clamped(named: list) -> bool:
    """Whether the cap actually bit — for logging and for the run trace.

    Worth recording rather than doing quietly: a diagnosis that keeps naming
    every section is a signal about the EVALUATOR, not about the sheet, and it
    would be invisible if the cap silently absorbed it every time.
    """
    distinct = {s for s in (named or []) if s in SECTION_ORDER}
    return len(distinct) > MAX_SECTIONS
