"""Curriculum Reasoning Agent — what the textbook does not say.

A textbook gives you *"teach: identify objects from different views"* and two
pages of prose. It does not give you what makes that period land:

    what the child must already be able to do before the sentence means anything
    which real objects in a classroom of forty make it concrete
    what the child will wrongly conclude
    what understanding this topic must LEAVE so the next one has a floor

Until now each of those was invented implicitly, inside Planning, while it was
also deciding minutes, emphasis, seams and vocabulary. This node makes them an
explicit artefact — readable, correctable, and checkable — before any plan
exists.

ONE CALL FOR THE WHOLE CHAPTER, and that is a pedagogical decision before it is a
cost one. Prerequisites, anchors and misconceptions are chapter-scale facts, not
topic-scale ones: a Grade 3 shapes chapter has one prerequisite set, one object
pool and a handful of misconceptions. Asked per topic, a model invents forty
slightly different anchor lists for the same forty children — which is exactly
the "locally reasonable, collectively incoherent" failure the planner's docstring
already warns about.

Anchors are the one thing scoped BELOW the chapter, and only as far as the
strand. The first real chapter proved why: a chapter of shapes-then-numbers got
one pool — water bottle, lunchbox, pencil box — and the counting topics quietly
reached for marigolds and beads instead, because you cannot count a pencil box.
Objects follow the thread that uses them; nothing follows the topic.
Only the chain is per topic, and it is two clauses, not two paragraphs:

    T1  gained: a view shows only some faces  ->  bridges: that view has an outline
    T2  gained: outlines are nameable shapes  ->  bridges: shapes recur in objects
    T3  gained: repeated shapes make patterns ->  bridges: (the next chapter)

Written as forty consecutive lines in one response, the chain is something the
model can see whole — the same reason planning writes both ends of the experience
seam in one window instead of hoping two calls agree.

THE KNOWLEDGE SEAM, and why it is checkable. `bridgesTo` for topic N and
`assumes` for topic N+1 describe the same handover from its two sides, written
together, deliberately in the same words. So "did the knowledge actually chain"
is a word-overlap question here, the way "did the Refresher pick up the Explore"
is a word-overlap question in the validator — and for the same reason: both ends
were authored to match. Asked of the generated *prose* instead, it would not be:
the overlap between "a view produces a visible outline" and "identify and trace
2D shapes" is near zero, and scoring that arithmetically would flag every correct
transition in the chapter.

CACHED, because this depends on the textbook, the grade and the subject — not on
the class, the teacher, or the adaptive state. Planning depends on all of those.
Split that way, regenerating a chapter for the second Class 3 section reuses the
reasoning and re-plans only what is class-specific, and iterating on prompts
downstream stops paying for it every time.

Never fatal. A chapter with no reasoning generates exactly as it did before this
node existed: planning falls back to its own judgement, generation omits the
block, and the three validator checks that read it skip. Degradation is recorded
in `state["errors"]`, not raised.
"""
import re

from ..bands import band_block
from ..clauses import roots as clause_roots
from ..llm import call_json
from ..state import ChapterState
from ..tools import cached_reasoning, store_reasoning

# Bumped whenever the prompt below changes what a stored answer would contain.
#
# The cache is keyed on the chapter, so without this a prompt improvement never
# reaches any chapter that has already been derived once — the old answer is
# returned forever and the change looks like it did nothing. v2 added the
# cognitive band, which alters every `gained` clause it touches.
#
# v4 is the standing-still gate below. It is a version bump for a second reason
# as well as the prompt: every chain derived under v3 is in the table already,
# and the EVS family chapter proves they can be degenerate. A bump is the only
# thing that gets those re-derived instead of served from cache forever.
#
# v5: chapter_figures now actually reaches this stage (prep_pipeline_bridge's
# locate_figures wiring) and deep_agents' skills went from off to on for every
# caller. Every chain cached under v4 was derived blind to the chapter's own
# pictures and without reading a single pedagogy skill -- a bump is the only
# thing that retires those instead of serving them forever.
#
# v6: ExperiencePlan gained a `floor` field (deep_agents/schemas.py) -- the
# differentiation skill's own below-grade-level sentence, now a schema field
# instead of prose the agent could skip. Every experience plan cached under v5
# was derived against a schema with no such field, so every one of them has an
# empty floor -- exactly the failure a v7 run surfaced (belowLevelSupport still
# 2/5 despite the wiring) before this was found: the fix was live in code and
# invisible in output because the cache never saw it. A bump forces fresh
# derivation under the new schema instead of serving pre-floor plans forever.
PROMPT_VERSION = 6

MAX_PREREQUISITES = 8
MAX_ANCHORS = 14
MAX_MISCONCEPTIONS = 6

# ── The mastery pass ─────────────────────────────────────────────────────────
#
# Versioned separately from PROMPT_VERSION above, and stored inside the same
# cache row, because the two prompts fail and improve independently. The chain is
# the expensive answer — one call for the whole chapter, gated, retried, and
# refused caching when it is degenerate — and bumping its version to ship a
# change to THIS prompt would throw every good chain in the table away to
# re-derive an answer that was already right.
#
# So a cached row is topped up rather than replaced: a chapter derived before
# this pass existed carries no `_mastery_version`, gets the pass run over it, and
# is written back with its chain untouched.
MASTERY_VERSION = 1

# Six, not the chain's forty-in-one and not the experience node's twelve. This
# pass is the only one that reads the printed page rather than a structured
# extraction of it, and a window is that many excerpts long.
MASTERY_WINDOW = 6

# How much of each topic's excerpt the pass is shown. The judgement it makes is
# "does the page supply this", which cannot be made from a concept list — but it
# also does not need the whole stretch: what a textbook supplies for one idea it
# supplies in the paragraph that introduces it, and the tail of a topic's pages
# is exercises.
MASTERY_EXCERPT = 1100

# The kinds of shortfall a page can have, fixed rather than free text. A model
# asked for "what is missing" writes a different taxonomy every window, and
# nothing downstream can then act on the answer — the generation prompt stages a
# contrast differently from an explanation, and the validator counts them
# differently again.
MASTERY_KINDS = (
    "example",         # the page states the idea and shows no instance of it
    "contrast",        # only the positive case; nothing separates it from its neighbour
    "experience",      # nothing the child does with their hands or eyes
    "misconception",   # the wrong belief is never put where a child can meet it
    "application",     # never used on anything past the instance it was taught with
    "transfer",        # never used on a case the page did not choose
    "explanation",     # the fact is given, the reason for it is not
)

# Three at most, and the number is a judgement about prompts rather than about
# textbooks. A page has as many shortfalls as an adult can name; what a period
# can actually close is one or two. Asked for an inventory, a model returns one,
# and everything downstream then has to pick — which is the same decision made
# later with less information.
MAX_SHORTFALLS = 3


_REASONING_PROMPT = """You are a curriculum specialist reading one chapter of a Grade {grade} {subject}
textbook before anybody plans a lesson from it. You are NOT planning lessons and
you are NOT writing material for children. You are answering the questions the
textbook itself does not answer.

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
CLASS: about {class_size} children | CLASSROOM RESOURCE LEVEL: {resource_level}
ARC (from sequencing): {arc}
{band_block}

THE TOPICS, in teaching order:
{topics}

THREE THINGS ARE SETTLED ONCE, not per topic — forty slightly different answers
to the same question is worse than one:

  prerequisites  what the class must ALREADY be able to do before topic 1 can
                 begin. Everyday capabilities in a child's terms, not textbook
                 headings and not this chapter's own content.
  anchors        the real objects this chapter is taught with, GROUPED BY THE
                 STRAND that uses them (see `strand` below). Every one must exist
                 in an Indian government primary classroom at resource level
                 {resource_level}: things on a desk, in a bag, on the wall, in the
                 playground, brought from home. No printables, no manipulative
                 kits, no screens, no photocopies.
                 Group them because different threads need different things: a
                 thread about shapes wants objects with faces to look at, and a
                 thread about counting wants things there are HUNDREDS of — bottle
                 caps, seeds, sticks, beads. One pool for both serves neither.
  misconceptions the wrong beliefs children actually form here, each tagged with
                 the topic numbers where it is most likely to appear. Write each
                 one IN THE CHILD'S OWN VOICE, first person, naming a real thing:
                     yes  "the cup changed when I walked round it"
                     no   "a 3D object's view changes its identity"
                 The second is a description of a misconception. The first is one,
                 and it is the only form a teacher can put back in front of the
                 class to see who still believes it.

ONE THING IS PER TOPIC, and it is SHORT — one clause each, never a sentence:

  strand     the thread this topic belongs to, 1-3 words (e.g. "shapes", "number")
  gained     what the child can do or see after this topic that they could not before
  bridgesTo  how that becomes the NEXT topic's starting knowledge — or null
  assumes    what must already be understood for THIS topic to make sense

`bridgesTo` for topic N and `assumes` for topic N+1 are the two sides of the same
handover. Write them naming the SAME thing in the SAME words. If topic 3 bridges
to "an outline can be named as a shape", then topic 4 assumes "an outline can be
named as a shape" — not "basic geometry", not "shape knowledge".

A PRINTED CHAPTER IS OFTEN MORE THAN ONE THREAD. Textbooks bind several strands
into one chapter for reasons of page count, not teaching: four topics on shapes
followed by two on two-digit numbers is one chapter and two threads. Where that
happens, say so — give the topics different `strand` names, set the last topic of
the first thread to `"bridgesTo": null`, and set the first topic of the next
thread to `"assumes": null`.

Do NOT manufacture a link across that seam. "Counting shapes leads to comparing
numbers" is a sentence, not a progression, and a fabricated bridge is worse than
a declared break: the break is something a teacher can see and plan around, and
the fabrication is something they discover mid-period.

`bridgesTo` must follow from that SAME topic's `gained`. If what the topic leaves
behind cannot reach the bridge you wrote, the bridge is invented — set it to null
instead.

EVERY TOPIC MUST GAIN SOMETHING, and this is the rule most often broken. A topic
whose `gained` says the same thing as its own `assumes` has taught nothing: the
class walked in able to do it and walked out able to do it. Read every entry as
three lines and check the middle one moves:

  wrong — the chain has slipped by one, and nothing is ever learned
    T4  assumes: can point to family members they resemble
        gained:  can point to family members they resemble      <- identical
        bridges: can say their family's last name               <- the next TITLE

  right — each line is a step past the one above it
    T4  assumes: family members are connected to each other
        gained:  can point to a feature they share with a relative
        bridges: people who share features often share a name

The wrong version is easy to write by accident, because it agrees with itself at
every seam — `gained` copied from the previous `bridgesTo`, `bridgesTo` copied
from the next topic's title. It reads as a progression and promises nothing. If
you genuinely cannot name a gain for a topic — some review and exercise topics
consolidate rather than teach — write what the consolidation makes newly
possible ("can state, unprompted, which family members live together"), never a
copy of `assumes`.

Topic 1's `assumes` is null: what topic 1 needs is the chapter-level
prerequisites above. The LAST topic of each thread points at what comes next, or
is null if nothing does.

Return ONLY valid JSON, no markdown fences:
{{
  "prerequisites": ["4-8 items, each a capability stated in a child's terms"],
  "anchors": [
    {{"strand": "matches a strand name used in the chain below",
      "objects": ["6-10 objects, 1-3 words each"]}}
  ],
  "misconceptions": [
    {{"belief": "the wrong thing the child concludes, in their own words",
      "topics": [1, 2]}}
  ],
  "chain": [
    {{"index": 1, "strand": "shapes", "gained": "...", "bridgesTo": "...", "assumes": null}}
  ]
}}

Rules:
- One chain entry per topic listed above — all {total} of them, same index, same
  order.
- Most chapters are ONE strand. Use a second only when the topics genuinely stop
  being about the same thing — not because the difficulty rose.
- Every clause under 15 words. This is a chain meant to be read whole, not prose.
- Name concrete things. "Develops spatial reasoning" helps nobody; "which side of
  the bottle you can see" does.
- `gained` is the promise this topic makes about the child, so it is bound by the
  band above. Write what they can DO, in a verb that band can actually perform.
- 3-6 misconceptions for the entire chapter, each tagged with the topic numbers
  above where it bites. A misconception nobody in this grade actually has is
  worse than one fewer.
- Every misconception must name something concrete — an object from the pools
  above, or a thing on the page. One phrased in the abstract cannot be turned
  into a question a seven-year-old will answer honestly.
- Anchors are things, not activities. "water bottle" yes; "bottle sorting game"
  no — choosing the activity is somebody else's job.
- One anchor group per strand you used in the chain, with the same strand name.
  A single-strand chapter gets exactly one group.
"""


_REDERIVE_SUFFIX = """

────────────────────────────────────────────────────────────────────────────────
YOUR PREVIOUS ANSWER TO THIS EXACT PROMPT HAD A BROKEN CHAIN. These entries were
wrong:

  {defects}

Both failures come from the same habit: filling `gained` with the previous
topic's `bridgesTo`, and `bridgesTo` with the next topic's title. That produces a
chain where every seam agrees and no topic teaches anything.

Write the whole thing again. For each topic, before you write its line, answer
this to yourself: what can a child do at the END of this period that they could
NOT do at the start of it? That answer is `gained`. If there is honestly no such
thing — and for a genuine break in the chapter there may not be — then `assumes`
or `bridgesTo` is null, and a declared break is a correct answer. A copied clause
is not.

Return the same JSON shape, complete, for every topic listed above.
"""


_MASTERY_PROMPT = """You are reading the printed pages of a Grade {grade} {subject} chapter and
answering ONE question about each topic, the question a textbook never answers
about itself:

    what does a child need in order to MASTER this idea, and how much of that
    does this page actually supply?

You are not planning a lesson, not choosing activities, and not writing anything
for children. You are auditing a page against a standard.

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
{band_block}

TOPICS {first}-{last} OF {total}. Each one gives you what the chain already
settled about it, and then the page itself:

{topics}

────────────────────────────────────────────────────────────────────────────────
THE MASTERY TARGET is not the topic's title and it is not what the page covers.
It is the general thing a child must be able to do to be said to have this idea —
stated so that a child who has it can decide a case NOBODY HAS SHOWN THEM.

That last clause is the whole test. Compare:

    the title says        types of families
    the page teaches      small families, large families, nuclear, extended
    the gain says         can name a family as nuclear or extended
    MASTERY IS            family structure is decided by WHO is in the family,
                          not by how many people it has

A child who has only the gain answers correctly about the two families printed on
the page and guesses at a third. A child who has the mastery target decides the
third. Write the target that separates them.

Write it as the PRINCIPLE, in a child's words, in one clause. Never as an
instruction to the teacher, never as a curriculum outcome, and never a restatement
of `gained` with longer words — if your target and the gain say the same thing,
you have written the gain again and this topic has no target.

────────────────────────────────────────────────────────────────────────────────
WHAT THE PAGE ALREADY SUPPLIES. Read the excerpt and say what a child genuinely
gets from it, in 2-4 clauses. Be generous here: this is the column that stops the
next stage from adding content the book already has, and a page credited with
less than it supplies produces a longer lesson teaching the same thing twice.

Credit what is THERE, not what a good teacher would do with it. "A picture of a
joint family" is supplied. "A discussion about joint families" is not — nothing
on a page discusses anything.

────────────────────────────────────────────────────────────────────────────────
WHAT IS STILL MISSING, and this is the part that must not become a wish list.

Set the mastery target against what the page supplies and name only what a child
cannot get to WITHOUT it. Each one gets a `kind` from this fixed list — use the
word, not a description of it:

  example        the page states the idea and shows no instance of it
  contrast       only the positive case; nothing separates it from what it is
                 commonly confused with
  experience     nothing the child does with their hands or their eyes
  misconception  the wrong belief is never put anywhere a child could meet it
  application    never used on anything past the instance it was taught with
  transfer       never used on a case the page did not choose
  explanation    the fact is given and the reason for it is not

ZERO IS A REAL ANSWER, and on a well-written page it is the right one. A topic
whose page supplies everything its target needs gets `"missing": []`, and that is
a finding, not a failure to find. Manufacturing a shortfall produces a longer
lesson, not a better one — and it produces it at 7am, for a teacher who now has
more to run and no more time.

At most {max_shortfalls} per topic. If you can name five, name the {max_shortfalls}
that stand between this child and the target; the other two are things an adult
knows about the subject.

Each `missing` clause says what is absent, CONCRETELY, naming the thing the page
would have to contain: "no two families of the same size with different members"
— not "insufficient coverage of family structure".

Return ONLY valid JSON, no markdown fences:
{{
  "topics": [
    {{
      "index": {first},
      "masteryTarget": "the principle, in a child's words, one clause",
      "supplied": ["2-4 clauses: what a child genuinely gets from this page"],
      "missing": [
        {{"kind": "one word from the list above",
          "missing": "what is absent, naming what the page would have to contain"}}
      ]
    }}
  ]
}}

Rules:
- One entry per topic listed above, same index, same order — all of them.
- Every clause under 20 words. This is an audit, not prose.
- `masteryTarget` must be decidable: a child either can or cannot use it on an
  unshown case. "Appreciates the diversity of families" is not decidable by
  anybody, including you.
- A target that repeats this topic's `gained` is wrong. The gain is what the
  period leaves; the target is what the idea requires. When they truly coincide —
  and for a drill or revision topic they can — say the target in terms of what the
  child can now DECIDE, not what they can now say.
- `missing` is measured against the target above and against nothing else. Not
  against what the next grade does with the idea, and not against what you would
  have written instead of this page.
- Never name a `kind` that is not in the list. Never invent a kind.
"""

def cache_key(grade: str, subject: str, chapter_title: str, chapter_number) -> str:
    """Identifies a chapter's reasoning for reuse across runs.

    The chapter's identity and the prompt version, and nothing else. Class,
    teacher and adaptive state are deliberately absent — reusing across those is
    the entire point. The prompt version IS in the key, because a cached answer
    is only as good as the prompt that produced it: without it, improving the
    prompt silently changes nothing for every chapter already in the table.

    This key once carried a hash of the topic spine as well, to stop a cached
    per-index chain being read against a different sequencing. It made the cache
    useless: sequencing is an LLM call, so "Identify views of 3D objects" comes
    back as "Identify 2D views of 3D objects" on the next run, the hash changes,
    and every lookup misses while the table fills with near-duplicate rows. The
    safety it was buying is real, so it moved to `_aligned` below — a check at
    READ time, which can tolerate a reworded title while still rejecting a genuine
    re-sequencing.
    """
    chapter = (str(chapter_number) if chapter_number is not None
               else (chapter_title or "").strip().lower())
    return f"v{PROMPT_VERSION}:{grade}:{(subject or '').strip().lower()}:{chapter}"


def _spine(topics: list[dict]) -> list[str]:
    return [(t.get("topic") or "").strip() for t in topics]


def _aligned(cached: list, current: list[str]) -> bool:
    """Is cached reasoning still about THIS sequencing?

    The chain is keyed by topic index, so reusing it across a chapter that has
    been cut into different topics would attach T4's understanding to a T4 that
    teaches something else. Equality is the wrong test though — the titles are
    generated, so they drift in wording between runs without the chapter changing
    at all. Sharing a word is enough to say two titles name the same lesson, and
    a quarter of the chapter may drift before the fixture is treated as stale.
    """
    if not cached or len(cached) != len(current) or not current:
        return False
    def words(text: str) -> set:
        return set(re.findall(r"[a-z][a-z]{3,}", str(text).lower()))
    matched = sum(1 for a, b in zip(cached, current) if words(a) & words(b))
    return matched >= max(1, int(0.75 * len(current)))


def _topic_lines(topics: list[dict]) -> str:
    lines = []
    for spec in topics:
        knowledge = spec.get("knowledge") or {}
        names = spec.get("canonical_names") or {}
        concepts = (names.get("concepts") or knowledge.get("concepts") or [])[:6]
        contexts = (knowledge.get("contexts") or [])[:5]
        lines.append(
            f"T{spec['index']}. {spec['topic']}"
            + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
            + f"\n    pages {spec.get('page_start')}-{spec.get('page_end')}"
            + f" | difficulty: {knowledge.get('difficulty') or '?'}"
            + f"\n    concepts: {', '.join(concepts) or '(none extracted)'}"
            + f"\n    things the book itself names: {', '.join(contexts) or '(none)'}"
        )
    return "\n".join(lines)


def _clause(value, limit: int = 140) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def anchor_groups(reasoning: dict) -> list[dict]:
    """The object pools, as [{strand, objects}].

    Tolerates the flat list of strings this field used to be. Cached reasoning
    written before anchors were grouped is still perfectly good — the objects are
    right, they simply were not attributed to a thread — and re-deriving a whole
    chapter to change a shape would spend a call to learn nothing.
    """
    raw = (reasoning or {}).get("anchors") or []
    if raw and isinstance(raw[0], str):
        return [{"strand": "main",
                 "objects": [a for a in raw if isinstance(a, str) and a.strip()]}]
    return [g for g in raw
            if isinstance(g, dict) and isinstance(g.get("objects"), list) and g["objects"]]


def anchors_for(reasoning: dict, strand: str) -> list[str]:
    """The objects this strand is taught with.

    Falls back to every object when the strand is unknown, deliberately. A topic
    whose thread was never named is not evidence that its objects are wrong, and
    the check that reads this must not manufacture a violation out of a gap in
    the reasoning.
    """
    groups = anchor_groups(reasoning)
    for group in groups:
        if (group.get("strand") or "main") == (strand or "main"):
            return group["objects"]
    return [o for group in groups for o in group["objects"]]


def all_anchors(reasoning: dict) -> list[str]:
    return [o for group in anchor_groups(reasoning) for o in group["objects"]]


def _normalise_anchors(raw, chain: dict) -> list[dict]:
    """Group the object pools by strand, bounded, deduplicated.

    Groups naming a strand no topic uses are folded into `main` rather than
    dropped: the objects are still real and still in the room, and discarding
    them over a label would leave a thread with no pool at all.
    """
    known = {(entry.get("strand") or "main") for entry in chain.values()} or {"main"}
    grouped: dict[str, list[str]] = {}

    def add(strand: str, objects) -> None:
        pool = grouped.setdefault(strand if strand in known else "main", [])
        for item in objects or []:
            name = _clause(item, 40)
            if name and name.lower() not in {p.lower() for p in pool}:
                pool.append(name)

    if isinstance(raw, list) and raw and isinstance(raw[0], str):
        add("main", raw)                       # the old flat shape, still accepted
    else:
        for group in (raw or []):
            if isinstance(group, dict):
                add(_clause(group.get("strand"), 40).lower() or "main",
                    group.get("objects"))

    per_group = max(4, MAX_ANCHORS // max(1, len(known)))
    return [{"strand": strand, "objects": objects[:per_group]}
            for strand, objects in grouped.items() if objects]


def _normalise(data: dict, topics: list[dict]) -> dict:
    """Bound everything the model returned, and close the chain's two ends.

    Bounded rather than validated for the same reason `_clamp_state` bounds the
    feedback optimizer: these values are read by every downstream prompt, and a
    list that grows without a ceiling becomes the prompt.
    """
    indices = [t["index"] for t in topics]
    valid = set(indices)
    first_index = indices[0] if indices else None

    def _strings(key: str, limit: int, clause_limit: int = 60) -> list[str]:
        seen: list[str] = []
        for item in (data.get(key) or []):
            text = _clause(item, clause_limit)
            if text and text.lower() not in {s.lower() for s in seen}:
                seen.append(text)
        return seen[:limit]

    misconceptions: list[dict] = []
    for entry in (data.get("misconceptions") or []):
        if not isinstance(entry, dict):
            continue
        belief = _clause(entry.get("belief"), 160)
        if not belief:
            continue
        tagged = []
        for raw in (entry.get("topics") or []):
            try:
                index = int(raw)
            except (TypeError, ValueError):
                continue
            if index in valid and index not in tagged:
                tagged.append(index)
        # An untagged misconception is kept, not dropped: it is still true of the
        # chapter, it just does not steer any one topic's Level Set.
        misconceptions.append({"belief": belief, "topics": sorted(tagged)})
    misconceptions = misconceptions[:MAX_MISCONCEPTIONS]

    by_index: dict[int, dict] = {}
    for entry in (data.get("chain") or []):
        if isinstance(entry, dict):
            try:
                by_index[int(entry.get("index"))] = entry
            except (TypeError, ValueError):
                continue
    # Positional fallback, the same rescue planning uses: a model that renumbered
    # the chain still returned it in teaching order.
    ordered = [e for e in (data.get("chain") or []) if isinstance(e, dict)]

    chain: dict[int, dict] = {}
    for position, spec in enumerate(topics):
        index = spec["index"]
        entry = by_index.get(index) or (ordered[position] if position < len(ordered) else {})
        chain[index] = {
            "index": index,
            # Defaulted rather than required. A single-strand chapter is the
            # common case and a model that omits the field has told us something
            # true about it; forcing a name would only invent distinctions.
            "strand": _clause(entry.get("strand"), 40).lower() or "main",
            "gained": _clause(entry.get("gained")),
            "bridgesTo": _clause(entry.get("bridgesTo")),
            # The first topic's floor is the chapter's prerequisites, not a
            # previous topic's gain. Left populated, it would be an instruction to
            # build on a lesson the class has not had — the same defect the
            # planner's `refresherBridge` guards against on the experience side.
            "assumes": None if index == first_index else _clause(entry.get("assumes")),
        }

    _seal_threads(chain, [spec["index"] for spec in topics])

    return {
        "prerequisites": _strings("prerequisites", MAX_PREREQUISITES, 90),
        "anchors": _normalise_anchors(data.get("anchors"), chain),
        "misconceptions": misconceptions,
        "chain": chain,
    }


def _seal_threads(chain: dict, ordered: list[int]) -> None:
    """Null `assumes` at the start of every thread, not just the chapter's.

    The prompt has always said a thread boundary is declared by nulling both
    sides of it, and the normaliser only ever honoured one: `assumes` was cleared
    for the chapter's first topic and left alone everywhere else. So a chapter
    that correctly reported a seam still shipped an assumption written across it
    — on the EVS family chapter T10 opened a new thread while claiming to stand
    on "can list things learned about families", which no topic before it left.

    Done here rather than in the prompt because it is not a judgement. Once the
    strands are named, which topics start one is arithmetic, and asking a model
    to be consistent about something arithmetic is how it becomes inconsistent.
    """
    for thread in strands({"chain": chain}, ordered):
        entry = chain.get(thread["topics"][0])
        if entry:
            entry["assumes"] = None


def chain_defects(chain: dict, ordered: list[int]) -> list[str]:
    """The two ways a chain can be wrong that the chain itself can show you.

    Both are checked again in `validation.check_knowledge_chain`, and that is not
    duplication — it is the same question asked at the two places it can be acted
    on. Here it decides whether to spend a second call and whether the answer is
    fit to cache. There it decides what the teacher is told about the chapter that
    shipped. A chain served from cache never passes through this function at all,
    which is exactly why the validator still has to ask.
    """
    defects: list[str] = []
    for index in ordered:
        entry = chain.get(index) or {}
        gained = entry.get("gained") or ""
        assumes = entry.get("assumes") or ""
        bridges = entry.get("bridgesTo") or ""
        if gained and assumes and clause_roots(gained) == clause_roots(assumes):
            defects.append(
                f"T{index} gains nothing: `gained` says the same thing as its own "
                f"`assumes` (\"{gained[:60]}\")")
        elif gained and bridges and not (clause_roots(gained) & clause_roots(bridges)):
            defects.append(
                f"T{index} bridges to something it does not reach: leaves "
                f"\"{gained[:44]}\" but claims to lead to \"{bridges[:44]}\"")
    return defects


def _supplied_structurally(spec: dict) -> str:
    """What context assembly already read off this topic's pages.

    Given to the mastery pass ALONGSIDE the excerpt rather than instead of it.
    The excerpt is what the audit is actually made against — "does the page supply
    this" is not answerable from a concept list — but the extraction says which of
    those words the pipeline is already treating as this topic's content, and a
    pass that credits the page with a concept nobody downstream knows about has
    credited it with nothing usable.
    """
    knowledge = spec.get("knowledge") or {}
    names = spec.get("canonical_names") or {}
    parts = []
    for label, key in (("concepts", "concepts"), ("words", "vocabulary")):
        items = (names.get(key) or knowledge.get(key) or [])[:6]
        if items:
            parts.append(f"{label}: {', '.join(str(i) for i in items)}")
    outcomes = (knowledge.get("learning_outcomes") or [])[:3]
    if outcomes:
        parts.append("it asks them to: " + "; ".join(str(o) for o in outcomes))
    return " | ".join(parts) or "(nothing structured came off these pages)"


def _mastery_topic_lines(topics: list[dict], chain: dict) -> str:
    """One block per topic: what the chain settled, then the printed page.

    The chain comes first deliberately. The pass has to write a target that is
    MORE than `gained`, and the only way to hold it to that is to put the gain in
    front of it before it writes.
    """
    lines = []
    for spec in topics:
        index = spec["index"]
        entry = chain.get(index) or {}
        excerpt = " ".join((spec.get("excerpt") or "").split())[:MASTERY_EXCERPT]
        beliefs = spec.get("_misconceptions") or []
        lines.append(
            f"--- T{index}. {spec['topic']}"
            + (f" - {spec['subtopic']}" if spec.get("subtopic") else "")
            + f"  (pages {spec.get('page_start')}-{spec.get('page_end')})"
            + f"\n    the gain it was given:   {entry.get('gained') or '(not derived)'}"
            + f"\n    they arrive able to:     {entry.get('assumes') or '(the chapter prerequisites)'}"
            + f"\n    flagged wrong beliefs:   {'; '.join(beliefs) or '(none for this topic)'}"
            + f"\n    extracted from the page: {_supplied_structurally(spec)}"
            + "\n    THE PAGE ITSELF:\n      "
            + (excerpt or "(no textbook text was ingested for these pages)")
        )
    return "\n".join(lines)


def _normalise_mastery(raw: dict, topics: list[dict]) -> dict:
    """Bound the audit, and drop the two answers that are worse than no answer.

    A shortfall with no `kind` from the fixed list is discarded rather than
    relabelled: the kind is what the generation prompt stages against and what the
    validator counts, so an invented one is a shortfall nothing can act on, and
    keeping it would put it in front of a teacher as work with no instructions.

    A `masteryTarget` that merely restates the topic's `gained` is also dropped.
    That is the failure this whole pass exists to avoid — the target is supposed
    to be the thing the gain is not — and a restatement passed downstream would
    make every consumer believe a target was derived when the answer is that one
    was not. Empty is honest, and every reader of this field already treats empty
    as "not derived".
    """
    by_index: dict[int, dict] = {}
    for entry in (raw.get("topics") or []):
        if isinstance(entry, dict):
            try:
                by_index[int(entry.get("index"))] = entry
            except (TypeError, ValueError):
                continue
    ordered = [e for e in (raw.get("topics") or []) if isinstance(e, dict)]

    out: dict[int, dict] = {}
    for position, spec in enumerate(topics):
        index = spec["index"]
        entry = by_index.get(index) or (ordered[position] if position < len(ordered) else {})

        target = _clause(entry.get("masteryTarget"), 160)
        gained = _clause(spec.get("_gained"), 160)
        if target and gained and clause_roots(target) == clause_roots(gained):
            target = ""

        missing: list[dict] = []
        seen_kinds: set[str] = set()
        for item in (entry.get("missing") or []):
            if not isinstance(item, dict):
                continue
            kind = _clause(item.get("kind"), 20).lower()
            detail = _clause(item.get("missing"), 160)
            # One per kind. Two `contrast` shortfalls on one topic are one
            # shortfall described twice, and the second only dilutes what the
            # period is actually asked to close.
            if kind not in MASTERY_KINDS or not detail or kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            missing.append({"kind": kind, "missing": detail})

        supplied: list[str] = []
        for item in (entry.get("supplied") or []):
            text = _clause(item, 120)
            if text and text.lower() not in {s.lower() for s in supplied}:
                supplied.append(text)

        out[index] = {
            "index": index,
            "masteryTarget": target,
            "supplied": supplied[:4],
            "missing": missing[:MAX_SHORTFALLS],
        }
    return out


async def mastery_pass(topics: list[dict], reasoning: dict, *, grade: str,
                       subject: str, chapter_title: str) -> tuple[dict, list[str]]:
    """Audit every topic's page against what mastering its idea requires.

    Windowed and never fatal, like every other per-topic pass in this package: a
    window that fails leaves those topics with an empty audit, every consumer
    downstream treats an empty audit as "not derived" and behaves as it did before
    this pass existed, and a chapter is not lost to one failed call.

    Returns (index -> audit, errors).
    """
    audit: dict[int, dict] = {}
    errors: list[str] = []
    total = len(topics)
    chain = (reasoning or {}).get("chain") or {}
    # Tagged per topic at the chapter level, so resolved here rather than read off
    # a chain entry: the beliefs are the one input that makes a `misconception`
    # shortfall answerable, and a pass that cannot see them reports every page as
    # supplying its own misconception handling.
    beliefs_by_index: dict[int, list[str]] = {}
    for entry in ((reasoning or {}).get("misconceptions") or []):
        for index in (entry.get("topics") or []):
            beliefs_by_index.setdefault(index, []).append(entry.get("belief") or "")

    for offset in range(0, total, MASTERY_WINDOW):
        window = topics[offset:offset + MASTERY_WINDOW]
        first_index, last_index = window[0]["index"], window[-1]["index"]
        # The gain and the beliefs are copied onto private keys rather than
        # re-derived, so the audit is measured against the same chain the prompt
        # was written from. `_gained` is what `_normalise_mastery` compares the
        # returned target against; drop either and the pass can quietly return the
        # gain as the target and nothing notices.
        annotated = []
        for spec in window:
            entry = chain.get(spec["index"]) or {}
            annotated.append({**spec,
                              "_gained": entry.get("gained") or "",
                              "_misconceptions": beliefs_by_index.get(spec["index"]) or []})
        prompt = _MASTERY_PROMPT.format(
            grade=grade, subject=subject,
            chapter_title=chapter_title or "(untitled)",
            band_block=band_block(grade),
            first=first_index, last=last_index, total=total,
            max_shortfalls=MAX_SHORTFALLS,
            topics=_mastery_topic_lines(annotated, chain),
        )
        try:
            data = await call_json(
                prompt, label=f"mastery[{first_index}-{last_index}]",
                required=("topics",), temperature=0.3,
                # The audit is ~120 tokens a topic; sized to roughly twice that,
                # because a truncated window loses its last topics entirely and
                # those are the ones a short chapter can least afford.
                max_tokens=600 + 280 * len(window),
            )
        except Exception as exc:
            errors.append(f"mastery: topics {first_index}-{last_index}: {exc}")
            for spec in window:
                audit[spec["index"]] = {"index": spec["index"], "masteryTarget": "",
                                        "supplied": [], "missing": []}
            continue
        audit.update(_normalise_mastery(data, annotated))

    return audit, errors


def mastery_for(reasoning: dict, index: int) -> dict:
    """One topic's audit, tolerant of a cache row written before it existed."""
    raw = (reasoning or {}).get("mastery") or {}
    entry = raw.get(index)
    if entry is None:
        # Keys come back from JSON as strings, the same way the chain's do.
        entry = raw.get(str(index))
    if not isinstance(entry, dict):
        return {"masteryTarget": "", "supplied": [], "missing": []}
    return {
        "masteryTarget": entry.get("masteryTarget") or "",
        "supplied": entry.get("supplied") or [],
        "missing": [m for m in (entry.get("missing") or [])
                    if isinstance(m, dict) and m.get("kind") in MASTERY_KINDS],
    }


def topic_reasoning(reasoning: dict, index: int) -> dict:
    """The slice of the chapter's reasoning that belongs to one topic.

    Attached to the topic spec so generation's per-topic block can read it
    without being handed the whole chapter, and so it persists on the topic row
    beside the plan it shaped.
    """
    chain = (reasoning.get("chain") or {}).get(index) or {}
    strand = chain.get("strand") or "main"
    audit = mastery_for(reasoning, index)
    return {
        "strand": strand,
        # Resolved here, once, so every consumer downstream — the prompt, the
        # validator, the CLI — is looking at the same pool for this topic.
        "anchors": anchors_for(reasoning, strand),
        "gained": chain.get("gained") or "",
        "bridgesTo": chain.get("bridgesTo") or "",
        "assumes": chain.get("assumes"),
        "misconceptions": [m["belief"] for m in (reasoning.get("misconceptions") or [])
                           if index in (m.get("topics") or [])],
        # The audit, flattened onto the same slice everything downstream already
        # reads. `gained` is what this period leaves; `masteryTarget` is what the
        # idea requires, and the two are only the same thing on a topic where the
        # pass found nothing to add. Empty throughout on a chapter derived before
        # the pass existed, which every reader treats as "not derived".
        "masteryTarget": audit["masteryTarget"],
        "supplied": audit["supplied"],
        "missing": audit["missing"],
    }


def strands(reasoning: dict, ordered: list[int]) -> list[dict]:
    """The chapter's threads, as runs of consecutive topics sharing a strand.

    Runs rather than groups: a chapter that returns to shapes after two topics of
    number has two shape *sections*, and treating them as one would hide the
    interruption, which is the thing worth knowing.
    """
    chain = (reasoning or {}).get("chain") or {}
    runs: list[dict] = []
    for index in ordered:
        name = (chain.get(index) or {}).get("strand") or "main"
        if runs and runs[-1]["strand"] == name:
            runs[-1]["topics"].append(index)
        else:
            runs.append({"strand": name, "topics": [index]})
    return runs


def _empty(topics: list[dict]) -> dict:
    return {"prerequisites": [], "anchors": [], "misconceptions": [],
            "chain": {t["index"]: {"index": t["index"], "strand": "main", "gained": "",
                                   "bridgesTo": "", "assumes": None} for t in topics}}


def _attach(topics: list[dict], reasoning: dict) -> list[dict]:
    return [{**spec, "reasoning": topic_reasoning(reasoning, spec["index"])}
            for spec in topics]


async def reasoning_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["reasoning: no topics to reason about"]}

    settings = state.get("teacher_settings") or {}
    grade, subject = state.get("grade", ""), state.get("subject", "")
    key = cache_key(grade, subject, state.get("chapter_title") or "",
                    state.get("chapter_number"))
    spine = _spine(topics)

    cached = await cached_reasoning(key)
    if cached and not _aligned(cached.get("_spine") or [], spine):
        print(f"[prep_flow:reasoning] cached reasoning for {key} describes a "
              f"different sequencing of this chapter; deriving again")
        cached = {}
    if cached:
        # Keys come back from JSON as strings; the chain is indexed by int
        # everywhere else, and a silently string-keyed chain would make every
        # downstream lookup miss without erroring.
        chain = {}
        for raw, entry in (cached.get("chain") or {}).items():
            try:
                chain[int(raw)] = entry
            except (TypeError, ValueError):
                continue
        mastery = {}
        for raw, entry in (cached.get("mastery") or {}).items():
            try:
                mastery[int(raw)] = entry
            except (TypeError, ValueError):
                continue
        reasoning = {**cached, "chain": chain, "mastery": mastery}
        print(f"[prep_flow:reasoning] reusing cached reasoning for {key}")

        # TOPPED UP, NOT REPLACED. The chain in this row is the expensive answer —
        # one call for the whole chapter, gated, retried, and refused caching when
        # it came back degenerate — and the mastery audit is versioned separately
        # precisely so a change to its prompt does not throw a good chain away.
        #
        # So a chapter derived before this pass existed, or under an older version
        # of it, keeps its chain and pays only for the audit. Which is also the
        # answer to "what happens to every chapter already in the table": they get
        # the new layer on their next run, for the cost of the pass alone.
        errors: list[str] = []
        topped_up = False
        if int(cached.get("_mastery_version") or 0) != MASTERY_VERSION:
            print(f"[prep_flow:reasoning] cached reasoning for {key} has no mastery "
                  f"audit at v{MASTERY_VERSION}; auditing its pages against it")
            mastery, errors = await mastery_pass(
                topics, reasoning, grade=grade, subject=subject,
                chapter_title=state.get("chapter_title") or "")
            reasoning = {**reasoning, "mastery": mastery,
                         "_mastery_version": MASTERY_VERSION}
            topped_up = True
            # Written back under the same key, carrying the same chain. A failed
            # write costs the next run one pass, which is the trade every cache
            # write in this package makes.
            if not reasoning.get("_uncacheable"):
                await store_reasoning(
                    key, grade=grade, subject=subject,
                    chapter_title=state.get("chapter_title") or "",
                    chapter_number=state.get("chapter_number"),
                    reasoning=reasoning)

        return {
            "reasoning": reasoning,
            "topics": _attach(topics, reasoning),
            "errors": errors,
            "metrics": {"reasoning_cached": True,
                        "reasoning_anchors": len(all_anchors(reasoning)),
                        "reasoning_misconceptions": len(reasoning.get("misconceptions") or []),
                        "mastery_topped_up": topped_up,
                        **_mastery_metrics(mastery, topics)},
        }

    prompt = _REASONING_PROMPT.format(
        grade=grade, subject=subject,
        chapter_title=state.get("chapter_title") or "(untitled)",
        class_size=settings.get("classSize", 40),
        resource_level=settings.get("resourceLevel", 0),
        arc=state.get("sequencing_note") or "(none recorded)",
        band_block=band_block(grade),
        topics=_topic_lines(topics),
        total=len(topics),
    )

    try:
        # THE ONE SEAM WHERE A DEEP AGENT MAY STAND IN. `config["deep_agents"]`
        # routes this single call through `deep_agents/bridge.py`, which returns
        # the same raw dict this prompt asks for — so the gate below, the
        # re-derive, the mastery pass, the caching refusal and every metric in
        # this function run unchanged either way. That is what makes the two
        # implementations comparable rather than merely alternative.
        #
        # It replaces the CALL and not the prompt's job: an agent here can go and
        # read the page that decides whether something is a prerequisite, which
        # is the one thing a fixed excerpt window cannot do.
        if (state.get("config") or {}).get("deep_agents"):
            from deep_agents.bridge import derive_curriculum_reasoning
            data = await derive_curriculum_reasoning(state)
        else:
            data = await call_json(
                prompt, label="reasoning", required=("chain",),
                temperature=0.4,
                # The chain is ~40 tokens a topic and the chapter block is ~400, so a
                # 40-topic chapter lands near 2000. Sized to roughly twice that: this
                # is one call for the whole run, and a truncated chain would lose the
                # end of the chapter the way a truncated plan does.
                max_tokens=2000 + 90 * len(topics),
            )
    except Exception as exc:
        # Everything downstream treats empty reasoning as "not available" and
        # behaves as it did before this node existed.
        #
        # The mastery pass is skipped rather than run on the empty chain, and that
        # is not thrift. The audit's whole job is to say what mastery requires
        # BEYOND the gain the chain settled, and with no gain to exceed it has no
        # standard to write against — it would return the topic titles restated as
        # targets, which is the one answer worse than none.
        reasoning = _empty(topics)
        return {
            "reasoning": reasoning,
            "topics": _attach(topics, reasoning),
            "errors": [f"reasoning: {exc}"],
            "metrics": {"reasoning_available": False},
        }

    reasoning = {**_normalise(data, topics), "_spine": spine}

    # THE GATE. Everything downstream is built on this chain and none of it can
    # repair it: the experience plan derives a path to the gain it is given, the
    # sheet is written to deliver that path, and a sheet repaired against a chain
    # that promises nothing comes back promising nothing. So the chain is checked
    # once, here, where a bad one costs a single call to fix instead of forty.
    #
    # One retry, not a loop. The failure is a model copying a column, and naming
    # the copied entries back to it fixes it or does not; a second retry has never
    # been the difference between a good chain and a bad one, and this is the call
    # every chapter pays for.
    ordered = [spec["index"] for spec in topics]
    defects = chain_defects(reasoning["chain"], ordered)
    if defects:
        print(f"[prep_flow:reasoning] chain has {len(defects)} defect(s); asking again")
        try:
            retry = await call_json(
                prompt + _REDERIVE_SUFFIX.format(defects="\n  ".join(defects)),
                label="reasoning-rederive", required=("chain",),
                temperature=0.2, max_tokens=2000 + 90 * len(topics))
            candidate = {**_normalise(retry, topics), "_spine": spine}
            # Kept only if it is actually better. A retry that trades three
            # standing-still topics for four is a worse chain arrived at more
            # expensively, and the first answer is still on the table.
            if len(chain_defects(candidate["chain"], ordered)) < len(defects):
                reasoning = candidate
                defects = chain_defects(reasoning["chain"], ordered)
        except Exception as exc:
            print(f"[prep_flow:reasoning] re-derive failed, keeping first chain: {exc}")

    # A chain still standing still on a quarter of the chapter is not cached.
    #
    # This is the part that makes it a gate rather than a warning. The run
    # continues — refusing to generate would cost a chapter over a defect a
    # teacher can read past — but a defective chain never becomes the permanent
    # answer for this book. Cached, it would be served to every future run of
    # every class studying it, and the prompt fix above would never reach them.
    # AFTER the gate and BEFORE the write, in that order and for two reasons.
    # The audit is measured against `gained`, so it must see the chain that
    # survived the re-derive rather than the one that provoked it; and the row is
    # written once, below, so an audit computed after the write would be correct in
    # this run and absent from every future one.
    mastery, mastery_errors = await mastery_pass(
        topics, reasoning, grade=grade, subject=subject,
        chapter_title=state.get("chapter_title") or "")
    reasoning["mastery"] = mastery
    reasoning["_mastery_version"] = MASTERY_VERSION

    standing_still = [d for d in defects if "gains nothing" in d]
    if len(standing_still) > max(1, len(topics) // 4):
        print(f"[prep_flow:reasoning] {len(standing_still)}/{len(topics)} topics gain "
              f"nothing — not caching this chain, so the next run derives it again")
        # Travels with the chain, because this node is not the only writer of that
        # cache row: the experience node writes its plans back into the same row
        # and would have re-cached the chain along with them, quietly undoing the
        # refusal above. Private, like `_spine`, and dropped by both readers.
        reasoning["_uncacheable"] = True
    else:
        await store_reasoning(
            key, grade=grade, subject=subject,
            chapter_title=state.get("chapter_title") or "",
            chapter_number=state.get("chapter_number"),
            reasoning=reasoning,
        )

    chained = sum(1 for entry in reasoning["chain"].values() if entry["gained"])
    threads = strands(reasoning, [t["index"] for t in topics])
    if len(threads) > 1:
        # Printed, because it changes how the chapter should be read: two threads
        # bound into one chapter is a fact about the book, and the alternative to
        # noticing it is a fabricated bridge between them.
        print("[prep_flow:reasoning] this chapter contains "
              f"{len(threads)} separate threads: "
              + "; ".join(f"{t['strand']} (T{t['topics'][0]}-T{t['topics'][-1]})"
                          for t in threads))
    return {
        "reasoning": reasoning,
        "topics": _attach(topics, reasoning),
        "errors": mastery_errors,
        "metrics": {
            "reasoning_available": True,
            "reasoning_cached": False,
            "reasoning_prerequisites": len(reasoning["prerequisites"]),
            "reasoning_anchors": len(all_anchors(reasoning)),
            "reasoning_misconceptions": len(reasoning["misconceptions"]),
            "reasoning_chain_written": chained,
            "reasoning_chain_defects": len(defects),
            "reasoning_chain_standing_still": len(standing_still),
            "reasoning_strands": len(threads),
            "mastery_topped_up": False,
            **_mastery_metrics(mastery, topics),
        },
    }


def _mastery_metrics(mastery: dict, topics: list[dict]) -> dict:
    """What the audit found, reported and never floored.

    `mastery_shortfalls` in particular is a fact about the textbook, not a score.
    A chapter whose pages genuinely supply what their ideas require should come
    back near zero, and a target on this number would teach the pass to invent
    deficiencies to hit it — which costs a teacher real minutes at 7am closing
    gaps that were never open. The one number worth watching is
    `mastery_targeted`: a chapter where few topics got a target is a chapter where
    this layer did nothing, and that is a prompt problem rather than a good book.
    """
    audits = [mastery.get(spec["index"]) or {} for spec in topics]
    kinds: dict[str, int] = {}
    for audit in audits:
        for item in (audit.get("missing") or []):
            kind = item.get("kind")
            if kind:
                kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "mastery_targeted": sum(1 for a in audits if a.get("masteryTarget")),
        "mastery_shortfalls": sum(len(a.get("missing") or []) for a in audits),
        "mastery_topics_complete": sum(1 for a in audits
                                       if a.get("masteryTarget") and not a.get("missing")),
        "mastery_kinds": kinds,
    }


def mastery_block(entry: dict, *, missing: bool = True) -> str:
    """One topic's mastery target and page audit, rendered for a prompt.

    Reads the slice `topic_reasoning` built, not the chapter's audit, so a caller
    holding a topic spec needs nothing else.

    THE ORDER OF THE THREE PARTS IS THE ARGUMENT. What mastery requires, then what
    the page already supplies, then what is left — because the last of those is the
    only one anybody is asked to act on, and it means nothing without the two above
    it. A prompt handed the shortfalls alone reads them as a licence to add
    content; handed all three, it reads them as the short list of things the page
    does not already do, which is what they are.

    `missing=False` withholds that third part, and the generation prompt passes it
    whenever an experience plan has already CHOSEN which shortfall this period
    closes. Printing both would put a list of three in front of a model one line
    above an instruction to stage one of them, and the sheet that comes back stages
    all three — thirty minutes spent starting three things. The audit's own text
    says "close these and add nothing else", which is exactly the wrong sentence
    once the choice has been made downstream of it.
    """
    if not entry:
        return ""
    target = entry.get("masteryTarget") or ""
    supplied = entry.get("supplied") or []
    shortfalls = (entry.get("missing") or []) if missing else []
    if not target and not shortfalls:
        return ""

    lines = []
    if target:
        lines += [
            "",
            "  WHAT MASTERY OF THIS IDEA ACTUALLY REQUIRES. Not what the period",
            "  leaves — that is the gain above. This is the principle a child needs",
            "  in order to decide a case nobody has shown them, and it is what the",
            "  Level Set is ultimately checking for:",
            f"    {target}",
        ]
    if supplied:
        lines += [
            "",
            "  WHAT THE PAGE ALREADY SUPPLIES. Teach FROM these rather than around",
            "  them, and do not build a second version of anything listed here — the",
            "  book is the source of this chapter's content and re-teaching what it",
            "  already does well makes the sheet longer without making it better:",
        ] + [f"    - {item}" for item in supplied]
    if shortfalls:
        lines += [
            "",
            "  WHAT THE PAGE DOES NOT SUPPLY, audited against that target. This is a",
            "  CLOSED list: close these and add nothing else. Each one is staged",
            "  inside the six sections that already exist — none of them is a new",
            "  section and none of them may make this sheet longer:",
        ] + [f"    - [{item['kind']}] {item['missing']}" for item in shortfalls]
    return "\n".join(lines)


def chapter_block(reasoning: dict) -> str:
    """What the class brings INTO the chapter, rendered for a prompt.

    The object pools are deliberately not here. They are per strand now, so they
    belong beside the topic that uses them - printing every thread's pool in the
    chapter header would hand a counting lesson a list of things to look at the
    faces of, which is how the pools got mixed in the first place.
    """
    prerequisites = (reasoning or {}).get("prerequisites") or []
    if not prerequisites:
        return ""
    return (
        "\nWHAT THE CLASS ALREADY HAS coming into this chapter - build on these\n"
        "and do not re-teach them:\n  " + "; ".join(prerequisites) + "\n"
    )


def topic_anchors_block(objects: list) -> str:
    """One topic's object pool, for its block in the generation prompt.

    Per strand rather than per chapter, and the difference is not cosmetic: the
    first real chapter handed its counting topics a pool of things to look at the
    faces of, and they quietly went and found marigolds instead.
    """
    if not objects:
        return ""
    return (
        "OBJECTS FOR THIS TOPIC. Chosen for the thread this period belongs to,\n"
        "because they exist in THIS classroom. Reach for them in Real Life and\n"
        "Explore before inventing anything, and never ask for something the room\n"
        "does not have:\n  " + ", ".join(objects)
    )
