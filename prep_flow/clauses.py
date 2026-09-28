"""Comparing two short derived clauses — the one measure several agents share.

This lived in `validation.py`, which was the right home while the validator was
the only thing asking "are these two clauses about the same thing". It is not any
more: the reasoning node now has to answer it about its OWN output before that
output is cached, and `validation` already imports `reasoning`, so leaving the
helpers there would have meant either a circular import or a second stemmer that
drifts from the first. A chain judged one way at derivation and another way at
validation is worse than no judgement.

Nothing here is a verdict. It is a hint about two short texts, deliberately
crude, and every caller is expected to say so in what it does with the answer.
"""
import re

_WORD = re.compile(r"[a-z][a-z'-]{2,}")


def root(word: str) -> str:
    """A crude stem, for comparing a derived clause against written prose.

    "traced" and "tracing" are the same idea and different tokens, and the
    reasoning chain is written in the infinitive while a lesson is written in the
    past.

    Possessives first, and this is not a nicety. `content_words` keeps
    apostrophes, so "an object's face" and "views of objects" reduced to
    "object'" and "object" and shared nothing — which flagged a correct bridge on
    the first full run. A check whose false positives look exactly like its true
    positives is worse than no check.
    """
    word = word.replace("'s", "").replace("’s", "").strip("'’")
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


# STOPWORDS is tuned for PROSE: "count", "show", "hold" and "look" appear in
# every bullet of every sheet, so stripping them is what makes the continuity
# score mean anything. A derived clause is not prose. "count and compare
# two-digit numbers" has exactly one content word once "count" is stripped, and
# the chain check duly reported a break between two clauses that plainly agree.
# So clause comparisons strip grammar only.
CLAUSE_STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here of in on to for
with without from by at as is are was were be been being do does did doing have
has had having will would shall should can could may might must not no nor so
such very too also its it he she they them their his her our your you we us i me
my one some any each other more most much many few less least own same both all
how what when where which who whom why while about into over under again once out
up down off above below their they students student teacher class
""".split())


def roots(text: str) -> set[str]:
    return {root(w) for w in _WORD.findall((text or "").lower())
            if w not in CLAUSE_STOPWORDS and len(w) > 3}


def mentions(phrase: str, text: str) -> bool:
    """Is this object actually named in this prose?

    A substring test was the obvious implementation and it was wrong in the one
    way that matters: an anchor is written for a shelf ("family photographs") and
    a bullet is written for a child ("hold up a photo of your own family"), so the
    literal string is absent from prose that is plainly about the object. On the
    first EVS chapter that accused seven of twelve sheets of ignoring the anchor
    they were built around, which is how a real signal — T12 genuinely never
    touched its pencil box — got buried under six false ones.

    So: every content word of the phrase has to be present, but a word counts as
    present when it shares a five-letter stem with something in the prose. `root`
    alone does not get there ("photographs" stems to "photograph", "photo" stays
    "photo"), and requiring ALL the words is what stops the stem match from being
    permissive — "water bottle" is not satisfied by "water".
    """
    if not (phrase or "").strip():
        return False
    if phrase.strip().lower() in (text or "").lower():
        return True
    wanted, present = roots(phrase), roots(text)
    if not wanted:
        return False
    return all(any(r.startswith(want[:5]) or want.startswith(r[:5]) for r in present)
               for want in wanted)


# Words that make a clause absolute rather than merely about something. A
# misconception and the understanding that corrects it are ABOUT the same thing
# and therefore share their nouns — "my family name is the only name I have" and
# "I have a family name that is part of my full name" share two content words out
# of three. What separates them is not vocabulary, it is this: the wrong belief
# closes the door ("only"), and the right one does not.
ABSOLUTES = frozenset("""
only just alone sole single all every everything everyone always same identical
none nothing nobody never no not cannot can't don't doesn't isn't aren't won't
without must impossible any anything
""".split())


def polarity(text: str) -> frozenset:
    return frozenset(w for w in _WORD.findall((text or "").lower()) if w in ABSOLUTES)


# ── Activity names ───────────────────────────────────────────────────────────
#
# A SECOND SHARED MEASURE, here for the same reason the clause comparison above
# is. These two lived in `agents/generation.py`, and its docstring said why they
# were exported from there: "Shared with the validator so 'did it use the
# activity we selected' is asked the same way in both places — the alternative is
# generation quietly accepting a name the validator then rejects."
#
# The node split made that arrangement untenable: generation moved out of this
# package (see `_deferred/README.md`) and validation is Node 3, so the two
# callers now live in different packages and NEITHER of them can own the
# definition. A measure two nodes disagree about is worse than no measure — the
# generator would ship a sheet the validator rejects, on a difference of
# punctuation.

def normalise_activity_name(name: str) -> str:
    """An activity name stripped of the decoration models add to it."""
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())
    return re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()


def same_activity(written: str, selected: str) -> bool:
    """Whether `written` is the selected activity, decoration and all.

    Substring either way, because the observed deviations are additive: the
    category appended, or a word dropped from a long name. What this must still
    catch is a genuinely DIFFERENT activity — which is the whole reason the
    Pedagogy Library's selection has an audit trail.
    """
    a, b = normalise_activity_name(written), normalise_activity_name(selected)
    if not a or not b:
        return False
    return a == b or a in b or b in a
