"""Experience Plan Agent — the join, and the boundary generation may not cross.

Reasoning settled what must be learned. This settles the two questions that stand
between a learning goal and a lesson:

    COGNITIVE   what sequence of mental operations gets a child of this age from
                where they are to that understanding?
    CONTEXTUAL  which real object in this room makes that sequence happen?

and joins them with the knowledge into one artefact per topic. Nothing downstream
writes a bullet without one.

WHY THE TRAJECTORY IS DERIVED, NOT LOOKED UP. `bands.py` can tell you a grade 2
child works from things they can hold and cannot yet justify a generalisation.
It cannot tell you that *this* concept needs
`observe -> move -> observe again -> compare -> infer`, while the next one needs
`recall -> discriminate -> match -> name -> trace`. Those are properties of the
concept, not of the age, and a table indexed by grade will give the same answer
to both. So the path is derived per topic, and the band becomes the guardrail it
is checked against rather than the source it is read from.

WHY PER TOPIC BUT NOT PER CALL. Windows of 12, like planning, and for the same
reason: a trajectory written while the model can still see the neighbouring
topics does not repeat the previous topic's path with the nouns swapped, and
forty of them in one response truncates. Reasoning stays one call for the whole
chapter because its content is chapter-scale; this is genuinely per topic, so it
is windowed instead.

THE ANCHOR IS CHOSEN HERE, NOT IN REASONING. Reasoning produced a pool of objects
this classroom has. Which one a topic uses is a different question — it depends on
the trajectory. A cup is the right object for `observe -> move -> compare` because
a child can walk round it; it is the wrong object for a counting thread, where the
answer is bottle caps. Picking from a pool with the trajectory in hand is the
whole reason the two stages are separate.

THE GAP IS FOUND IN REASONING AND STAGED HERE, and that split is newer than the
rest of this file. The mastery pass reads the printed page with the mastery target
in hand and returns the shortfalls it audited, each tagged with a kind. This node
is not asked to find them again — it is asked which ONE of them this period can
actually close, and what closes it in a room of forty with the anchor already in
hand.

Written the other way round it does not work, and it did not: a node holding a
trajectory and a concept list was asked "what does the book not supply", which is
a question about the book, and answered it from the only thing in front of it. The
audit is made where the excerpt is, and the staging where the trajectory is.

When the audit is empty — a chapter cached before the pass existed, or a page that
genuinely supplies what its idea needs — this node falls back to finding the gap
itself, exactly as it did before. That fallback is why the prompt below still
carries the question.

A PLAN ALWAYS EXISTS. When the call fails, a plan is still assembled from what
reasoning already knows — the strand's first anchor, the topic's misconception,
its forward bridge — and marked `derived: false`. Generation is never left
without one, and the run is never lost to one failed window: the same trade the
rest of this package makes everywhere.
"""
import json

from ..bands import band_block
from ..clauses import roots as clause_roots
from ..llm import call_json
from ..state import ChapterState
from .reasoning import MASTERY_KINDS, anchors_for, cache_key, topic_reasoning
from ..tools import store_reasoning

PLAN_WINDOW = 12

_EXPERIENCE_PROMPT = """You are designing the LEARNING EXPERIENCE for a chapter of Grade {grade} {subject}.
You are not writing lessons and not choosing activities. You are answering, for
each topic, two questions that stand between a learning goal and a lesson:

  1. What sequence of mental operations must a child actually go through to reach
     this understanding?
  2. Which real object in this room makes that sequence happen?

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
CLASS: about {class_size} children | RESOURCE LEVEL: {resource_level}
{band_block}
WHAT THE CLASS ARRIVES WITH: {prerequisites}

TOPICS {first}-{last} OF {total}. Each carries what reasoning already settled —
that part is given, and you design the experience that delivers it:

{topics}

THE COGNITIVE TRAJECTORY is the heart of this, and it is a property of the
CONCEPT, not of the grade. Two topics in the same chapter for the same children
need different paths:

    a topic about viewpoints   observe -> move -> observe again -> compare ->
                               notice the difference -> infer what caused it
    a topic about naming shapes  recall -> look at the outline -> tell two apart ->
                               match to a name -> name it unprompted -> represent it

Write the ACTUAL path for the actual concept. Six to eight steps, each 2-6 words,
each one thing the child does or notices in their head. If two topics in this
window get the same trajectory with the nouns swapped, at least one of them is
wrong.

Then name the single hardest move in that path — the `conceptualJump`. It is
almost never the first step or the last. It is the one place a child who is
following along stops following, and it is where the teaching has to be thickest.

THE ANCHOR is chosen from the pool below, and chosen BECAUSE it supports the
trajectory — not because it is familiar. A cup works for observe-move-compare
because a child can walk round it and the far side really is hidden. For a
trajectory that needs hundreds of things to count, a cup is useless and bottle
caps are right. Say which and say why in one clause.

THE GAP is the last question, and the one that decides whether this period
teaches the concept or reproduces the page.

YOU ARE NOT LOOKING FOR IT. Each topic above already carries THE PAGE DOES NOT
GIVE — a list audited against that topic's mastery target by somebody who read the
printed page. Your job is to pick the ONE entry from it this period can actually
close, and to say what closes it. Copy its `kind` into `gapKind` and its wording
into `gap`, then write the `gapCloser`.

Pick by what the trajectory needs, which is the thing the audit could not see. A
shortfall that sits on the step where your `conceptualJump` falls is worth ten
that sit somewhere the class was never going to struggle. Where two look equally
important, take the one your anchor can stage — a closer that needs an object the
room does not have closes nothing.

Family types is the clean example:

    the page gives    a small family; a large family; the words nuclear and extended
    mastery needs     who is in the family decides its type, not how many there are
    the audit found   [contrast] no two families of the same size with different members
    you choose it     because the jump is "size is not what decides it"
    what closes it    one contrasting pair — four people including grandparents,
                      five people who are only parents and children

ONE, not an inventory, and this is a real constraint rather than a stylistic one.
Three closers staged in one period is a period that runs out of minutes and
delivers none of them, and the sheet is thirty minutes long whatever this list
says. Leave the other two: the chapter is a term long and the next topic in this
thread will meet the same idea again.

Where the audit found nothing, decide for yourself against the trajectory — and
`"gap": ""` is then a real and common answer. A page that supplies what its idea
needs is a good page, and inventing a deficiency to fill produces a longer lesson,
not a better one.

TWO STANDARDS, AND ONLY ONE OF THEM IS WHAT THIS PERIOD LANDS. Every topic above
carries both, and confusing them designs the wrong lesson:

    must leave them able to   what THIS period lands. The trajectory ends here and
                              `inference` IS this, in a child's words. The next
                              topic stands on it, so a period that lands something
                              else breaks the chapter underneath it.
    MASTERY REQUIRES          the principle the whole idea needs, which usually
                              takes several periods. It is the standard the GAP and
                              the TRANSFER TASK are measured against — never the
                              thing this one period aims at.

So: aim the trajectory at the gain, and measure the gap and the transfer against
mastery. A plan whose `inference` restates MASTERY REQUIRES has quietly promoted a
chapter-long principle into a thirty-minute period, and both the period and the
seam after it are then wrong.

`transferTask` is the other half, and it is measured against MASTERY REQUIRES
rather than against the gain. One situation the child has NOT been shown, where
they must apply what they worked out and say why. "A family has a mother, two
children and a grandmother — what would you call it, and what made you decide?"
That question is only worth asking because the target is about who is in the
family; asked against the gain, "name a family as nuclear or extended", the
page's own two families would have done. It belongs in Level Set, where the class
is already checking what they believe. Keep it to something answerable aloud in
under a minute.

Return ONLY valid JSON, no markdown fences:
{{
  "plans": [
    {{
      "index": {first},
      "entryPoint": "where a child who knows nothing yet can start — 3-8 words",
      "trajectory": ["6-8 steps, 2-6 words each, in the order they must happen"],
      "conceptualJump": "the single hardest move in that path, and where teaching must be thickest",
      "scaffolding": ["2-3 supports that make the jump survivable"],
      "anchor": "ONE object, copied exactly from this topic's pool",
      "anchorReason": "one clause: why this object makes the trajectory happen",
      "studentAction": "what the child physically does — 4-10 words",
      "observation": "what they notice as a result — 4-10 words",
      "inference": "what they work out from noticing it — one clause, the point of the period",
      "evidence": "what a child who got it would say or do, in their words",
      "gap": "the ONE audited shortfall this period closes, in its words — or \\"\\"",
      "gapKind": "that shortfall's kind, copied from the audit — or \\"\\"",
      "gapCloser": "the one contrasting case or example that supplies it — or \\"\\"",
      "transferTask": "one unshown situation they must classify or explain, and say why"
    }}
  ]
}}

Rules:
- One plan per topic listed above, same index, same order.
- The `inference` must BE this topic's stated gain — the "must leave them able to"
  line, not the "MASTERY REQUIRES" line. Not a restatement in fancier words: the
  same thing, in the words a child would use. If your `inference` is closer to
  MASTERY REQUIRES than to the gain, you have aimed the period at the wrong one
  and it will be snapped back to the gain.
- `anchor` must be one of the objects listed for that topic. Do not invent one,
  and do not reach into another topic's pool.
- `studentAction` is physical and doable in a room of {class_size} with the
  furniture already there. Not "imagine", not "picture in your head".
- `evidence` is what a child SAYS or DOES, never "understands that…". If you
  cannot write what it looks like from outside, the trajectory is still abstract.
- `gap` and `gapCloser` are both present or both empty. A gap with nothing that
  closes it is a complaint about the textbook, which helps nobody at 7am.
- `gapKind` is copied from the audit, never invented. Where you found the gap
  yourself, use the closest of: {kinds}.
- `gapCloser` must run with the anchor and the furniture already in the room. The
  moment it needs a printed card or a prepared set, it is not a closer — find one
  that uses what is there, or leave the gap empty.
- `transferTask` uses a case NOT already on the page for this topic. Re-asking
  the book's own example measures recall of that example and nothing else.
- `transferTask` must be decidable by a child who has MASTERY REQUIRES and not by
  one who has only the gain. If both children answer it, it is a comprehension
  question wearing a transfer question's clothes — and it is the only evidence
  this pipeline collects that the period taught the idea rather than the page.
"""


def _covers(spec: dict, entry: dict) -> str:
    """What the printed book actually puts in front of this topic.

    The mastery pass's own reading of the page when it made one — it had the
    excerpt in hand and this node does not, so its answer is better than anything
    assembled here. Falls back to the structured extraction context assembly
    already paid for, which is what this function was before the audit existed and
    is still what a chapter cached under an older version gets.
    """
    supplied = entry.get("supplied") or []
    if supplied:
        return "; ".join(str(item) for item in supplied)

    knowledge = spec.get("knowledge") or {}
    names = spec.get("canonical_names") or {}
    parts = []
    for label, key in (("concepts", "concepts"), ("vocabulary", "vocabulary")):
        items = (names.get(key) or knowledge.get(key) or [])[:8]
        if items:
            parts.append(f"{label}: {', '.join(str(i) for i in items)}")
    outcomes = (knowledge.get("learning_outcomes") or [])[:4]
    if outcomes:
        parts.append("it asks them to: " + "; ".join(str(o) for o in outcomes))
    return " | ".join(parts) or "(the excerpt yielded nothing structured)"


def _audited(entry: dict) -> str:
    """The shortfalls the mastery pass found, as one line per topic.

    Rendered as `[kind] detail` so the prompt can be told to copy the kind back
    rather than invent a label for what it chose — which is the only thing that
    makes the choice countable afterwards.
    """
    missing = entry.get("missing") or []
    if not missing:
        return "(the audit found nothing missing — decide for yourself, and \"\" is a fine answer)"
    return " | ".join(f"[{item['kind']}] {item['missing']}" for item in missing)


def _topic_lines(topics: list[dict], reasoning: dict) -> str:
    lines = []
    for spec in topics:
        entry = spec.get("reasoning") or topic_reasoning(reasoning, spec["index"])
        pool = entry.get("anchors") or anchors_for(reasoning, entry.get("strand"))
        lines.append(
            f"T{spec['index']}. {spec['topic']}"
            + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
            + f"\n    must leave them able to: {entry.get('gained') or '(not derived)'}"
            # BESIDE the gain, not above it, and the difference is not editorial.
            # An earlier version of this comment said "above the gain in
            # importance" and the prompt agreed with it — and on the first real
            # chapter T2 came back with an `inference` of "families can be
            # different sizes and have different members" when its gain was "can
            # identify grandparents, parents, children". The period had been
            # designed to land the CHAPTER's principle instead of the period's
            # own step, which broke the chain seam underneath it as well.
            #
            # The gain is what this period lands. The target is what the gap and
            # the transfer task are measured against. Neither outranks the other;
            # they answer different questions, and the prompt below now says so
            # where the two are printed together.
            + f"\n    MASTERY REQUIRES:        {entry.get('masteryTarget') or '(not derived)'}"
            + f"\n    they arrive able to:     {entry.get('assumes') or '(the chapter prerequisites)'}"
            + f"\n    they will wrongly think: {'; '.join(entry.get('misconceptions') or []) or '(nothing flagged)'}"
            + f"\n    the book gives them:     {_covers(spec, entry)}"
            + f"\n    THE PAGE DOES NOT GIVE: {_audited(entry)}"
            + f"\n    objects available:       {', '.join(pool) or '(none derived)'}"
        )
    return "\n".join(lines)


def _clause(value, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _fallback(spec: dict, reasoning: dict) -> dict:
    """A plan assembled from what reasoning already knows.

    Not a good plan — it has no trajectory, and everything downstream that reads
    one degrades accordingly. It exists so the boundary holds: generation never
    runs without a plan, and one failed window never costs the chapter.
    """
    entry = spec.get("reasoning") or topic_reasoning(reasoning, spec["index"])
    pool = entry.get("anchors") or anchors_for(reasoning, entry.get("strand"))
    return {
        "index": spec["index"],
        "entryPoint": "", "trajectory": [], "conceptualJump": "", "scaffolding": [],
        "anchor": pool[0] if pool else "",
        "anchorReason": "",
        "studentAction": "", "observation": "",
        "inference": entry.get("gained") or "",
        "evidence": "",
        # No excerpt was read and no trajectory was written, so there is nothing
        # to measure a gap against. Empty is the honest answer, and every reader
        # of these fields already treats empty as "not derived".
        "gap": "", "gapKind": "", "gapCloser": "", "transferTask": "",
        # Carried even here, and it is the one thing this fallback can still be
        # right about: the target came off the mastery pass, which ran before this
        # window failed and does not depend on it. A sheet generated from a
        # fallback plan has no trajectory, but it still knows what the idea
        # requires — and that is the difference between degrading and going blind.
        "masteryTarget": entry.get("masteryTarget") or "",
        # Nothing was returned to aim anywhere, so nothing was snapped back. Set
        # explicitly rather than left absent so the metric counts fallbacks as
        # "did not drift" instead of as missing data.
        "aimedAtMastery": False,
        "misconception": (entry.get("misconceptions") or [""])[0],
        "forwardBridge": entry.get("bridgesTo") or "",
        "derived": False,
    }


def _normalise(raw: dict, spec: dict, reasoning: dict) -> dict:
    entry = spec.get("reasoning") or topic_reasoning(reasoning, spec["index"])
    pool = entry.get("anchors") or anchors_for(reasoning, entry.get("strand"))

    trajectory = [_clause(step, 60) for step in (raw.get("trajectory") or [])]
    trajectory = [step for step in trajectory if step][:8]

    # An anchor from outside the pool is snapped back rather than accepted. The
    # pool is what the room actually has; a model reaching past it has invented a
    # prop, and a lesson that opens by asking for something nobody owns fails in
    # front of the class rather than here.
    anchor = _clause(raw.get("anchor"), 40)
    if pool and anchor and not any(anchor.lower() == p.lower() for p in pool):
        match = next((p for p in pool if p.lower() in anchor.lower()
                      or anchor.lower() in p.lower()), None)
        anchor = match or pool[0]
    elif not anchor and pool:
        anchor = pool[0]

    # THE PERIOD'S TARGET, snapped back the way an out-of-pool anchor is above.
    #
    # `inference` is supposed to BE `gained`, and the prompt says so twice. It
    # still drifts, and it drifts in one specific direction: onto the mastery
    # target, which is the more interesting sentence sitting two lines above it in
    # the prompt. On the first real chapter T2 aimed at "families can be different
    # sizes and have different members" while its gain was "can identify
    # grandparents, parents, children" — a chapter-long principle promoted into one
    # period.
    #
    # Detected narrowly rather than by "does it match the gain": an inference that
    # merely PARAPHRASES its gain can legitimately share no stems with it, and
    # replacing those would trade a child's phrasing for a curriculum clause. This
    # only fires when the inference has landed on the TARGET instead — no overlap
    # with the gain, real overlap with the target — which is the failure and not a
    # wording difference.
    #
    # `check_plan_fidelity` reports the same drift at validation time and stays
    # advisory there, deliberately: by then the sheet is written and the finding is
    # for a human. Here it is still cheap to correct, and the sheet has not been
    # generated against the wrong target yet.
    inference = _clause(raw.get("inference")) or (entry.get("gained") or "")
    gained = entry.get("gained") or ""
    target = entry.get("masteryTarget") or ""
    aimed_at_mastery = bool(
        inference and gained and target
        and not (clause_roots(inference) & clause_roots(gained))
        and (clause_roots(inference) & clause_roots(target)))
    if aimed_at_mastery:
        print(f"[prep_flow:experience] T{spec['index']} aimed at the mastery target "
              f"rather than its own gain; snapping back to \"{gained[:48]}\"")
        inference = gained

    # Only ever a word from the audit's fixed vocabulary, and only on a gap that
    # survived the pairing below. An invented label would be counted as a kind in
    # the metrics and staged as none of them by the generator, which is worse than
    # an untyped gap — the gap itself still reads perfectly well without one.
    kind = _clause(raw.get("gapKind"), 20).lower()
    if kind not in MASTERY_KINDS or not (_clause(raw.get("gap"))
                                         and _clause(raw.get("gapCloser"))):
        kind = ""

    return {
        "index": spec["index"],
        "entryPoint": _clause(raw.get("entryPoint"), 80),
        "trajectory": trajectory,
        "conceptualJump": _clause(raw.get("conceptualJump")),
        "scaffolding": [_clause(s, 80) for s in (raw.get("scaffolding") or []) if _clause(s)][:3],
        "anchor": anchor,
        "anchorReason": _clause(raw.get("anchorReason")),
        "studentAction": _clause(raw.get("studentAction"), 90),
        "observation": _clause(raw.get("observation"), 90),
        "inference": inference,
        # Recorded rather than silently corrected: a chapter where this fires often
        # is a chapter whose mastery targets are too close to their gains, and the
        # snap-back hides that unless somebody counts it.
        "aimedAtMastery": aimed_at_mastery,
        "evidence": _clause(raw.get("evidence")),
        # Paired on purpose: a gap the model could not close is dropped rather
        # than passed on. Generation can act on "here is the contrasting case to
        # stage"; it can do nothing useful with "the book is thin here" except
        # pad, which is the failure mode this whole field was added to avoid.
        "gap": _clause(raw.get("gap"), 120) if _clause(raw.get("gapCloser")) else "",
        "gapKind": kind,
        "gapCloser": _clause(raw.get("gapCloser"), 150) if _clause(raw.get("gap")) else "",
        "transferTask": _clause(raw.get("transferTask"), 180),
        # Carried, not regenerated, for the reason `misconception` below is: the
        # mastery pass settled it against the printed page, and a second call
        # restating it is how two artefacts start disagreeing about what the topic
        # is for. Every consumer of the plan reads it from here.
        "masteryTarget": entry.get("masteryTarget") or "",
        # Carried, not regenerated. Reasoning already settled both, and asking a
        # second call to restate them is how two artefacts start disagreeing.
        "misconception": (entry.get("misconceptions") or [""])[0],
        "forwardBridge": entry.get("bridgesTo") or "",
        "derived": True,
    }


def _kind_counts(plans: dict) -> dict:
    counts: dict[str, int] = {}
    for plan in plans.values():
        kind = (plan or {}).get("gapKind")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


async def experience_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["experience: no topics to plan for"]}

    reasoning = state.get("reasoning") or {}
    settings = state.get("teacher_settings") or {}
    grade, subject = state.get("grade", ""), state.get("subject", "")

    # Cached alongside the reasoning it is built from: both depend on the
    # textbook, the grade and the subject and on nothing about the class, and the
    # spine check that guards one guards the other.
    cached = reasoning.get("experience")
    if isinstance(cached, dict) and cached:
        plans: dict[int, dict] = {}
        for raw, plan in cached.items():
            try:
                plans[int(raw)] = plan
            except (TypeError, ValueError):
                continue
        if len(plans) == len(topics):
            print(f"[prep_flow:experience] reusing {len(plans)} cached experience plan(s)")
            return {
                "experience": plans,
                "topics": [{**s, "experience": plans.get(s["index"], {})} for s in topics],
                "metrics": {"experience_cached": True, "experience_plans": len(plans)},
            }

    total = len(topics)
    plans: dict[int, dict] = {}
    errors: list[str] = []

    for offset in range(0, total, PLAN_WINDOW):
        window = topics[offset:offset + PLAN_WINDOW]
        first_index, last_index = window[0]["index"], window[-1]["index"]
        prompt = _EXPERIENCE_PROMPT.format(
            grade=grade, subject=subject,
            chapter_title=state.get("chapter_title") or "(untitled)",
            class_size=settings.get("classSize", 40),
            resource_level=settings.get("resourceLevel", 0),
            band_block=band_block(grade),
            prerequisites="; ".join(reasoning.get("prerequisites") or []) or "(none derived)",
            first=first_index, last=last_index, total=total,
            topics=_topic_lines(window, reasoning),
            # From the constant rather than written into the prompt, so a kind
            # added to the audit's vocabulary cannot go missing from the one
            # place a model is told which words are legal.
            kinds=", ".join(MASTERY_KINDS),
        )
        try:
            # See the equivalent seam in agents/reasoning.py. Same contract: the
            # bridge returns the same `{"plans": [...]}` this prompt asks for, so
            # the anchor snap-back, the mastery-target correction and the
            # per-topic fallback below are unchanged.
            if (state.get("config") or {}).get("deep_agents"):
                from deep_agents.bridge import derive_experience_plans
                data = await derive_experience_plans(state, window)
            else:
                data = await call_json(
                    prompt, label=f"experience[{first_index}-{last_index}]",
                    required=("plans",), temperature=0.45,
                    max_tokens=1200 + 260 * len(window),
                )
        except Exception as exc:
            errors.append(f"experience: topics {first_index}-{last_index}: {exc}")
            for spec in window:
                plans[spec["index"]] = _fallback(spec, reasoning)
            continue

        by_index = {}
        for raw in (data.get("plans") or []):
            if isinstance(raw, dict):
                try:
                    by_index[int(raw.get("index"))] = raw
                except (TypeError, ValueError):
                    continue
        ordered = [p for p in (data.get("plans") or []) if isinstance(p, dict)]
        for position, spec in enumerate(window):
            raw = by_index.get(spec["index"]) or (
                ordered[position] if position < len(ordered) else None)
            plans[spec["index"]] = (_normalise(raw, spec, reasoning) if raw
                                    else _fallback(spec, reasoning))

    derived = sum(1 for p in plans.values() if p.get("derived"))
    distinct = len({json.dumps(p.get("trajectory")) for p in plans.values() if p.get("trajectory")})

    # Written back into the same cache row. The reasoning blob and the plans built
    # on it are invalidated by the same things, so keeping them apart would let a
    # reused chain meet a stale set of plans.
    #
    # Which is also why `_uncacheable` is honoured here. Reasoning refuses to cache
    # a chain in which most topics teach nothing; writing the plans would write the
    # chain back with them, and the refusal would last exactly one node.
    if derived and not reasoning.get("_uncacheable"):
        await store_reasoning(
            cache_key(grade, subject, state.get("chapter_title") or "",
                      state.get("chapter_number")),
            grade=grade, subject=subject,
            chapter_title=state.get("chapter_title") or "",
            chapter_number=state.get("chapter_number"),
            reasoning={**reasoning, "experience": {str(i): p for i, p in plans.items()}},
        )

    return {
        "experience": plans,
        "topics": [{**s, "experience": plans.get(s["index"], {})} for s in topics],
        "errors": errors,
        "metrics": {
            "experience_cached": False,
            "experience_plans": len(plans),
            "experience_derived": derived,
            "experience_distinct_trajectories": distinct,
            # Reported rather than floored. A chapter whose book genuinely covers
            # its own trajectory should show a low number here, and a target would
            # only teach the model to invent deficiencies to hit it.
            "experience_gaps_found": sum(1 for p in plans.values() if p.get("gap")),
            # Which KINDS of shortfall this chapter is actually closing, and it is
            # the one number here worth reading as a signal about the pipeline
            # rather than about the book. A chapter whose gaps are all `example`
            # is a chapter where the audit found the easy thing every time, which
            # is what a mastery target that was really the gain in disguise
            # produces.
            "experience_gap_kinds": _kind_counts(plans),
            "experience_transfer_tasks": sum(1 for p in plans.values()
                                             if p.get("transferTask")),
            "experience_targeted": sum(1 for p in plans.values()
                                       if p.get("masteryTarget")),
            # How many periods had to be pulled back off the chapter's principle
            # onto their own step. Zero is the expected number; a chapter with
            # several is telling you its targets and its gains are not far enough
            # apart to be two different fields.
            "experience_aimed_at_mastery": sum(1 for p in plans.values()
                                               if p.get("aimedAtMastery")),
        },
    }


def experience_block(plan: dict) -> str:
    """One topic's Experience Plan, rendered for the generation prompt."""
    if not plan or not plan.get("derived"):
        return ""
    lines = ["",
             "  THE EXPERIENCE THIS PERIOD DELIVERS. The six sections are the staging;",
             "  this is what has to actually happen inside them:"]
    if plan.get("entryPoint"):
        lines.append(f"    start from:     {plan['entryPoint']}")
    if plan.get("anchor"):
        lines.append(f"    using:          {plan['anchor']}"
                     + (f" — {plan['anchorReason']}" if plan.get("anchorReason") else ""))
    if plan.get("studentAction"):
        lines.append(f"    they do:        {plan['studentAction']}")
    if plan.get("observation"):
        lines.append(f"    they notice:    {plan['observation']}")
    if plan.get("inference"):
        lines.append(f"    they work out:  {plan['inference']}")
    if plan.get("trajectory"):
        lines += ["", "    THE PATH THEIR THINKING TAKES, in this order. The material must walk",
                  "    it — skipping a step is where a class gets lost:",
                  "      " + "  ->  ".join(plan["trajectory"])]
    if plan.get("conceptualJump"):
        lines.append(f"    hardest move:   {plan['conceptualJump']}")
    if plan.get("floor"):
        lines += ["",
                  "    THE FLOOR. What the child furthest behind must leave able to",
                  "    do -- not a lowered target, the first reachable step on the",
                  "    same road. USE THIS SENTENCE, not an invented one, as the easy",
                  "    entry Concept's first bullet and Challenge's PLAY both require:",
                  f"      {plan['floor']}"]
    if plan.get("scaffolding"):
        lines.append(f"    hold them up with: {'; '.join(plan['scaffolding'])}")
    if plan.get("evidence"):
        lines.append(f"    you will know they have it when: {plan['evidence']}")
    if plan.get("gap") and plan.get("gapCloser"):
        kind = plan.get("gapKind") or ""
        lines += ["",
                  "    WHERE THE BOOK STOPS SHORT. The page does not supply this, and",
                  "    without it the class can follow the lesson and still not have the",
                  "    idea. Stage the closer inside Concept or Challenge — it is not an",
                  "    extra section and must not make the sheet longer:",
                  f"      missing: {plan['gap']}" + (f"  [{kind}]" if kind else ""),
                  f"      close it with: {plan['gapCloser']}"]
        # THE ONE thing the page is short of, chosen from an audited list of up to
        # three. Said out loud here because a generator handed a shortfall reads it
        # as an invitation to be thorough, and a sheet that closes three gaps in
        # thirty minutes closes none of them.
        lines.append("      this is the ONLY shortfall this period closes — do not "
                     "go looking for others")
    if plan.get("transferTask"):
        lines += ["",
                  "    THE TRANSFER, and it belongs in LEVEL SET. One case they have not",
                  "    been shown, which they must decide about AND say why. This is the",
                  "    only evidence that the period taught the idea rather than the",
                  "    example, so it replaces one of Level Set's checking bullets — it",
                  "    is not added on top of them:",
                  f"      {plan['transferTask']}"]
    return "\n".join(lines)
