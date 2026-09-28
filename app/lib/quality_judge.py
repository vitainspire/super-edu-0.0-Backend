"""An independent second model rates a generated lesson against a fixed
rubric, and its low-scoring, genuinely fixable findings drive one more
rewrite pass -- capped at a small number of rounds, because this is a
judge+fix loop, not a loop that stops when it runs out of things to improve.

WHY A SEPARATE MODEL, NOT ANOTHER PASS OF THE SAME ONE. Everything else in
this pipeline that judges its own output (the learner gate, the realism gate)
runs on the same model family that wrote the lesson. A model checking its own
work is a known soft spot -- it tends to rate its own choices more kindly than
an independent reader would. JUDGE_MODEL is deliberately a different model
family from GENERATION_MODEL for that reason alone.

WHY THE RUBRIC IS RATED IN FULL, NOT TRIMMED DOWN FIRST. Some of these 19
dimensions describe things the current six-bucket schema has no field for at
all (branching by ability, an explicit mastery threshold, a diagnose-reteach-
reassess structure) -- no rewrite of Concept's prose adds those. Trimming them
out of the rubric ahead of time would mean guessing which is which. Instead
the judge rates everything AND says, per dimension, whether a targeted
rewrite in this lesson's own sections could plausibly move the score
(`revisable`). Only low-scoring, revisable findings feed the fix pass; the
rest are reported honestly as a ceiling this format has, not chased with
spend that cannot move them.

THIS IS NOT THE PRODUCTION VALIDATION GATE. validation_flow decides ship /
needs_review / refuse for the shared-batch system, unchanged by anything
here. This module is the founder-facing quality loop: generate (or reuse
already-generated material) -> rate -> fix -> re-rate, a bounded number of
times, with every step saved to disk so the run can be read back later.
"""
import json
import os
from typing import Callable, Optional

from prep_flow.llm import call_json
from prep_flow.sections import BULLETS_PER_SECTION

# Different family from GENERATION_MODEL (google/gemini-2.5-flash) on purpose
# -- see the module docstring. Overridable so a cheaper or newer judge can be
# swapped in without touching call sites.
JUDGE_MODEL = os.environ.get("QUALITY_JUDGE_MODEL", "anthropic/claude-haiku-4.5")

# A rewrite pass costs real money and this loop is meant to be cheap and
# bounded -- 2 fix rounds after the first rating, 3 rating passes total,
# matching the "2-3 loops, that's it" the loop was scoped to.
MAX_FIX_ROUNDS = int(os.environ.get("QUALITY_MAX_FIX_ROUNDS", "2"))

# The rubric, trimmed to 10 after two real runs on real material. `key` is
# what the judge and the revise prompt both key findings on; `label`/`what`
# are rendered into the prompt so the model is scored against the SAME
# description every time, not a paraphrase that drifts loop to loop.
#
# CUT FROM THE ORIGINAL 18, AND WHY (kept here rather than just deleted, so
# the reasoning survives the next person who wonders where a dimension went):
#   adaptivity, recovery       -- no field for either in the six-section
#                                 format; always scored ~1, never revisable,
#                                 pure waste on every single judge call.
#   teacherSupport             -- real overlap with teacherUsability (kept);
#                                 one extra dimension for little extra signal.
#   mastery, assessmentCoverage -- both ask "does this get checked properly",
#                                 from two angles close enough to not be worth
#                                 two separate judge answers every round.
#   instructionalSequencing    -- overlapped continuity (kept): both ask
#                                 whether the structure actually hangs
#                                 together, just at different grain.
#   sourceTraceability +
#   textbookIntegration        -- were two rubric rows measuring the same
#                                 thing (grounded in a real cited page) --
#                                 merged into `textbookGrounding` below.
#   largeClassPracticality     -- merged INTO `classroomRealism` (its "what"
#                                 now covers the large-class fallback
#                                 question too) rather than dropped -- kept
#                                 by explicit request, not diluted.
DIMENSIONS: list[dict] = [
    {"key": "diagnosticThinking", "label": "Diagnostic thinking",
     "what": "Names a misconception AND maps it to a specific move -- not just 'watch for X', but 'if you see X, do Y'."},
    {"key": "belowLevelSupport", "label": "Below-level support",
     "what": "A child behind grade level has a concrete, simpler path in -- not just concrete objects, an explicit easier example or step before the main one."},
    {"key": "realWorldLearning", "label": "Real-world learning",
     "what": "Local examples ask the child to solve or decide something, not just observe or imagine."},
    {"key": "exploration", "label": "Exploration",
     "what": "Explore gives a real reason to look outside class, self-checkable, costing nothing."},
    {"key": "creativity", "label": "Creativity",
     "what": "Room for more than one right answer or more than one way to solve it."},
    {"key": "teacherUsability", "label": "Teacher usability",
     "what": "Timing, materials, what to say, what to expect back, and what to do if it goes sideways are all actually on the page."},
    {"key": "continuity", "label": "Continuity across lessons",
     "what": "Each topic visibly follows from the one before it -- concretely, the Refresher actually picks up the previous topic's Explore, not just related by subject."},
    {"key": "contentPrecision", "label": "Content precision",
     "what": "Every claim, definition and vocabulary word is correct and pitched at this grade -- nothing borrowed from a harder register."},
    {"key": "textbookGrounding", "label": "Textbook grounding",
     "what": "Concepts, examples and tasks trace to specific things this chapter's pages actually say, with a real page cited -- no placeholder, no citation where none exists."},
    {"key": "classroomRealism", "label": "Classroom realism",
     "what": "Every activity can actually run with one teacher, 30-60 children, fixed benches, a blackboard and the textbook -- nothing else guaranteed -- and a fallback exists for anything that assumes small groups or a lot of movement. Two failure modes that slip past a logistics-only read: (1) a child past the first couple of rows cannot actually perceive the detail being taught -- 'observe X and describe what you see' on one small held-up object fails everyone but the front row; (2) the physical action described does not mechanically produce the thing it claims to teach -- e.g. rotating a held-up object at chest height shows front/back/side but never a genuine top view, so 'show the top view by rotating it' is asking for something that cannot happen as written. Read every demo literally: what would each child actually see or do, not what the activity intends."},
]

_DIMENSION_KEYS = {d["key"] for d in DIMENSIONS}

# Dimensions that are only judgeable when the source chapter actually carries
# page numbers. The Class 3 Maths chapter has ZERO page markers in the
# published API (measured, not assumed), so `pagesCited` is empty and every
# citation the model writes is unverifiable -- this then pins at 1.0
# whatever the prose does, which is a property of the INPUT, not the lesson.
PAGE_DEPENDENT_DIMENSIONS = frozenset({"textbookGrounding"})


def blocked_dimensions(lesson: dict) -> dict[str, str]:
    """Which dimensions cannot be moved for THIS lesson, and why -- so a
    report can say so out loud instead of showing an unexplained floor."""
    blocked: dict[str, str] = {}
    has_pages = bool(lesson.get("pagesCited")) or any(
        p is not None for p in (lesson.get("pageRange") or []))
    if not has_pages:
        for key in PAGE_DEPENDENT_DIMENSIONS:
            blocked[key] = "this chapter has no page numbers in the source textbook API"
    return blocked


def summarise(report: dict, lesson: dict) -> dict:
    """Two numbers, deliberately. `overall` is the honest full picture for a
    reader; `targetable` excludes what cannot be moved, and is the only one
    it makes sense to hold the fix loop to."""
    ratings = [r for r in (report.get("ratings") or []) if isinstance(r, dict)]
    blocked = blocked_dimensions(lesson)
    targetable = [r for r in ratings if r.get("dimension") not in blocked]
    avg = lambda rs: round(sum(int(r.get("score") or 0) for r in rs) / len(rs), 2) if rs else 0.0
    return {
        "overall": avg(ratings),
        "targetable": avg(targetable),
        "targetableCount": len(targetable),
        "atOrAbove4": sum(1 for r in targetable if int(r.get("score") or 0) >= 4),
        "blocked": blocked,
    }


def _sheet_text(lesson: dict) -> str:
    """The WHOLE sheet as a teacher would receive it.

    THIS USED TO BE SECTION PROSE ONLY, and that was a measurement bug rather
    than a shortcut. Six of the eighteen dimensions are scored on things that
    live OUTSIDE the section bullets -- `timings` and `materialsUsed` decide
    teacher usability, `objective` is what mastery and assessment coverage are
    judged AGAINST, `pagesCited`/`bookMoves` are the entire evidence for
    source traceability and textbook integration. Hiding them meant the judge
    marked the sheet down for missing exactly what it was never shown: it
    rated timings absent on a sheet that has them.
    """
    from prep_flow.sections import SECTION_LABELS, SECTION_ORDER, bullets_of, section_text

    parts: list[str] = []
    if lesson.get("objective"):
        parts.append(f"OBJECTIVE FOR THIS PERIOD: {lesson['objective']}")
    if lesson.get("planningNote"):
        parts.append(f"PLANNING NOTE: {lesson['planningNote']}")

    timings = lesson.get("timings") or {}
    if timings:
        total = sum(v for v in timings.values() if isinstance(v, (int, float)))
        per = ", ".join(f"{SECTION_LABELS.get(k, k)} {v} min" for k, v in timings.items())
        parts.append(f"TIMINGS ({total} min total): {per}")
    if lesson.get("materialsUsed"):
        parts.append("MATERIALS THE TEACHER NEEDS: " + ", ".join(str(m) for m in lesson["materialsUsed"]))

    pages = lesson.get("pagesCited") or []
    page_range = [p for p in (lesson.get("pageRange") or []) if p is not None]
    if pages or page_range:
        parts.append(f"TEXTBOOK PAGES THIS PERIOD TEACHES FROM: "
                     f"{', '.join(str(p) for p in pages) or '-'}"
                     + (f" (topic spans pages {page_range[0]}-{page_range[-1]})" if page_range else ""))

    # The pipeline's own record of which printed page each teaching move came
    # off, and which section it was staged into -- the actual evidence for
    # "is this grounded in the book", which prose alone cannot show.
    moves = [m for m in (lesson.get("bookMoves") or []) if isinstance(m, dict)]
    if moves:
        lines = [f"    - {m.get('section') or '?'} <- page {m.get('page')}: {m.get('gist') or ''}".rstrip()
                 for m in moves[:12]]
        parts.append("WHERE EACH PART CAME FROM IN THE BOOK:\n" + "\n".join(lines))

    for section in SECTION_ORDER:
        text = section_text(lesson, section)
        if not text:
            continue
        head = f"{SECTION_LABELS[section]}"
        mins = timings.get(section)
        if isinstance(mins, (int, float)) and mins:
            head += f" ({mins} min)"
        # A picture the class actually has in front of them is part of the
        # material, and "no visual support" is otherwise scored against a
        # sheet that has one.
        has_image = any(isinstance(b, dict) and (b.get("image") or {}).get("url")
                        for b in (bullets_of(lesson, section) or []))
        if has_image:
            head += " [has a printed textbook picture attached]"
        parts.append(f"{head}: {text}")

    watch = lesson.get("sectionWatch") or {}
    watch_lines = [f"    - {SECTION_LABELS.get(k, k)}: {(v or {}).get('text') if isinstance(v, dict) else v}"
                   for k, v in watch.items() if v]
    if watch_lines:
        parts.append("WHAT THE TEACHER IS TOLD TO WATCH FOR:\n" + "\n".join(watch_lines))

    return "\n\n".join(parts) if parts else "(the sheet is empty)"


_JUDGE_PROMPT = """You are an independent instructional-design reviewer. You did not write this
material -- another system did, and your job is to judge it honestly, neither
defending it nor point-scoring against it.

WHAT YOU ARE JUDGING IT AGAINST. This is a one-period PREP SHEET for a teacher
in an Indian government school: one teacher, 30-60 children, fixed benches, a
blackboard, and the children's own textbook. It is not a personalised adaptive
learning system and was never meant to be. Score it as "is this a good prep
sheet for that teacher, in that room" -- not against an idealised product.

CONTEXT: Grade {grade} {subject}, chapter "{chapter_title}", topic "{topic}"
(the textbook calls this section "{book_heading}").

THE MATERIAL:
{sheet}

Rate it against EXACTLY these {n} dimensions. For each one:
  - "score": 1-5, anchored like this and not on vibes:
      5 = genuinely strong; a good teacher could run this as-is and it would
          work. Reachable -- do not reserve it for perfection.
      4 = solid and usable; a small gap a teacher would close themselves
          without noticing.
      3 = usable but with a real gap they would have to work around.
      2 = present but weak enough to cause a problem mid-lesson.
      1 = absent, or actively wrong.
    Judge what IS there. If a dimension is genuinely well served, say 4 or 5 --
    an accurate 5 is as useful as an accurate 1, and marking everything 2
    tells the team nothing about where to spend.
  - "reason": one or two sentences, SPECIFIC to this material -- quote or
    closely paraphrase the actual text that earns or costs the score. Never
    write a generic sentence that could apply to any lesson.
  - "section": which of refresher/concept/realLife/challenge/levelSet/explore
    this finding is mainly about, or "none" if it's about the whole sheet
  - "revisable": true if a targeted REWRITE of that one section could
    plausibly raise this score, false if the real fix is a feature the
    current six-section format has no place for at all (an explicit branch
    for below/on/advanced level, a scored mastery field, a full
    diagnose-reteach-reassess sequence). Be honest here -- marking something
    revisable that a rewrite cannot actually fix wastes the next round.

DIMENSIONS:
{dimension_list}

Return ONLY valid JSON:
{{"ratings": [{{"dimension": "<key>", "score": 1-5, "reason": "...", "section": "...", "revisable": true}}, ...]}}
One entry per dimension listed above, every key present, in the order given."""


async def judge_lesson(lesson: dict, *, topic: str, book_heading: str,
                       grade: str, subject: str, chapter_title: str) -> dict:
    """One independent rating pass. Returns {"ratings": [...]}, one entry per
    DIMENSIONS key. Raises on a malformed judge response rather than
    returning a partial rubric silently -- a founder-facing report with rows
    quietly missing is worse than a run that stopped and said why."""
    dimension_list = "\n".join(f"- {d['key']}: {d['label']} -- {d['what']}" for d in DIMENSIONS)
    prompt = _JUDGE_PROMPT.format(
        grade=grade, subject=subject, chapter_title=chapter_title,
        topic=topic, book_heading=book_heading or topic,
        sheet=_sheet_text(lesson), n=len(DIMENSIONS), dimension_list=dimension_list,
    )
    data = await call_json(
        prompt, label="quality-judge", required=("ratings",),
        model=JUDGE_MODEL, temperature=0.2, max_tokens=3000)
    ratings = data.get("ratings") or []
    got = {r.get("dimension") for r in ratings if isinstance(r, dict)}
    missing = _DIMENSION_KEYS - got
    if missing:
        raise ValueError(f"judge left out dimension(s): {sorted(missing)}")
    return {"ratings": ratings}


def low_scoring_revisable(report: dict, lesson: Optional[dict] = None, *,
                          threshold: int = 4) -> list[dict]:
    """Findings worth spending a rewrite on: below the bar, the judge said a
    rewrite could plausibly move them, AND they are not structurally blocked
    for this lesson.

    The blocked filter is what stops the loop paying, every round of every
    run, to rewrite a section for `adaptivity` when the format has nowhere to
    put a branch -- the exact spend that bought nothing in the first two runs.
    """
    blocked = blocked_dimensions(lesson) if lesson is not None else {}
    return [r for r in (report.get("ratings") or [])
            if isinstance(r, dict) and r.get("revisable")
            and r.get("dimension") not in blocked
            and int(r.get("score") or 5) < threshold]


def _shape_skeleton(section: str) -> str:
    """The LITERAL JSON shape one section must come back as -- not described
    in English, shown.

    WHY THIS EXISTS. A real diagnostic run caught the actual failure: asked
    to rewrite Challenge in isolation, the model wrote genuinely good content
    ("Choose a view to draw... there is no right or wrong view to pick" --
    exactly the fix the finding asked for) and then wrapped it as
    `{"challenge.activity": "...", "text": "...", "text_2": "...", ...}` --
    flat, numbered keys instead of `{"activity": ..., "points": [...]}`. The
    content was never the problem. The prompt only ever DESCRIBED the shape
    in prose ("keep the same JSON shape"), and a section with an extra field
    alongside its bullets -- Challenge carries "activity", Explore carries
    "imageFocus" and "handoff" -- is exactly where prose leaves room to
    guess. A literal skeleton with the real keys removes the guess.
    """
    example = '[{"text": "a 6-12 word headline", "detail": "1-2 sentences"}, ' \
             '{"text": "...", "detail": "..."}, {"text": "...", "detail": "..."}]'
    if section == "refresher":
        return f'{{"previousTopic": "...", "recap": {example}}}'
    if section == "challenge":
        return f'{{"activity": "short name, character-for-character unchanged unless THIS finding is about the activity name", "points": {example}}}'
    if section == "explore":
        return (f'{{"points": {example}, '
                '"imageFocus": "one short phrase to sketch on the board", '
                '"handoff": {"scene": "...", "object": "...", "question": "...", "discovery": "..."}}')
    return f'{{"points": {example}}}'


_REVISE_PROMPT = """This Grade {grade} {subject} lesson sheet, topic "{topic}", was reviewed and
found weak on specific points. Rewrite ONLY the sections named below -- leave
every other section exactly as it is, character for character.

THE ROOM THIS IS WRITTEN FOR: one teacher, 30-60 children, fixed benches, a
blackboard, and the children's own textbook -- nothing else guaranteed. No
projector, no printer, no internet. If a bullet needs the whole class to see
something happen at the front, the teacher puts it on the blackboard as it
happens -- never "hold it up" or "project it" as the only way the class sees
it. This is the room a low classroomRealism score is measured against.

EVERY "detail" YOU WRITE: 1-2 sentences, UNDER 30 WORDS. Name what the
teacher does, plus at most one of (wait time / how students respond / what to
watch for) -- not all three every time. Cut the reasoning behind the
instruction; keep the instruction. A rewrite that fixes the finding but comes
back at 60 words has traded one problem for the one this sheet already has
too much of.

THE CURRENT SHEET:
{sheet}

WHAT EACH NAMED SECTION STILL HAS TO BE, regardless of what is being fixed --
this is what the section is FOR, not a description of the bug:
{section_rules}

THE EXACT JSON SHAPE EACH NAMED SECTION MUST COME BACK AS -- copy these keys
exactly, do not invent your own numbered keys or flatten anything:
{section_shapes}

WHAT TO FIX IN EACH, using the rule above as the shape and this as the problem:
{findings}

THE ONE MISTAKE THAT MAKES THIS WORSE THAN DOING NOTHING: writing the
CRITIQUE as the section's content. "Classroom realism: the activity does not
scale to 60 children" is not a bullet -- it is a sentence ABOUT the bullet.
The output is a real, better lesson section a teacher reads and teaches from:
a genuine hook, a genuine activity, a genuine check -- built so the named
problem no longer applies, never a summary of what the problem was.

Rules:
- Touch ONLY the sections named above. Do not "improve" anything else.
- Keep the same JSON shape this section already uses (the same keys, the same
  bullet count, {{"text","detail"}} pairs). "text" is a 6-12 word headline of
  what happens, never the name of a dimension being fixed and never a
  sentence describing a problem.
- Fix the SPECIFIC problem named, not a generic rewrite -- if the finding says
  the sheet has no easier path in for a below-level child, add one concretely
  (a simpler first example, a smaller number, a step before the main task),
  don't just reword what is already there.
- Never remove a real page citation or an existing textbook example while
  fixing something else in the same section.

Return ONLY valid JSON: {{"sections": {{"<section>": <the section's full corrected value, same shape as the current sheet>, ...}}}}
One entry per section named above, nothing else."""


_DIMENSION_LABELS_LOWER = {d["label"].lower() for d in DIMENSIONS} | {d["key"].lower() for d in DIMENSIONS}


def _bullets_in(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("points", "recap"):
            if isinstance(value.get(key), list):
                return value[key]
        # RECOVERY, not rejection, for the two shapes the real diagnostic run
        # showed a model actually producing when it has good content but the
        # wrong wrapper: (a) one bullet's fields written bare, with no list
        # around them at all -- {"text": "...", "detail": "..."}; (b) several
        # bullets flattened with numbered-suffix keys instead of a list --
        # {"text": "...", "detail": "...", "text_2": "...", "detail_2": "..."}.
        # Reconstructing the list here is what lets fixable content survive
        # instead of being thrown away and the round wasted on a re-ask.
        if "text" in value:
            recovered = [{"text": value.get("text"), "detail": value.get("detail")}]
            i = 2
            while f"text_{i}" in value:
                recovered.append({"text": value.get(f"text_{i}"), "detail": value.get(f"detail_{i}")})
                i += 1
            return recovered
    return []


def _validate_and_normalise(section: str, value, original) -> tuple[Optional[object], Optional[str]]:
    """(normalised_value, None) if `value` contains genuine lesson bullets,
    else (None, reason). THE FAILURE THIS GUARDS AGAINST IS SILENT: the JSON
    is well-formed either way, so nothing raises and nothing looks wrong
    until a teacher -- or the next judge pass -- reads it. A rewrite this
    loop cannot tell is good must not be allowed to overwrite one already
    known to exist.

    NORMALISES rather than just validates: `_bullets_in` recovers a real
    bullet list even from the flat, numbered-key shape a real diagnostic run
    caught the model producing, but the value actually STORED still needs the
    lesson's real container -- {"points": [...]} plus whatever extra field
    the section carries. `original` (the section's value before this
    rewrite) supplies that extra field -- challenge's "activity", explore's
    "imageFocus"/"handoff", refresher's "previousTopic" -- when the rewrite
    didn't repeat it back, so a targeted content fix cannot silently drop a
    field nothing asked it to touch.
    """
    bullets = _bullets_in(value)
    if not bullets:
        return None, "no bullets came back for this section"
    # Same two checks validation_flow/checks.py makes blocking for the LIVE
    # pipeline. This loop is a SEPARATE repair path (quality_judge's own,
    # not Node 3's) and was found rewriting a section back out of spec after
    # the live gate had already forced it into spec -- a real 4-bullet Explore
    # shipped this way, undetected, because nothing here re-checked count or
    # length after this loop's own rewrite. Enforced here too so neither
    # repair path can undo what the other already fixed.
    if len(bullets) != BULLETS_PER_SECTION:
        return None, f"{len(bullets)} bullets came back, expected exactly {BULLETS_PER_SECTION}"
    for b in bullets:
        if not isinstance(b, dict):
            return None, "a bullet was not an object"
        text = str(b.get("text") or "").strip()
        if not text:
            return None, "a bullet had no text"
        if text.lower() in _DIMENSION_LABELS_LOWER:
            return None, f"bullet text is literally a rubric dimension name ('{text}')"
        if len(text.split()) > 16:
            # A real "text" is a 6-12 word headline (the generation prompt's
            # own rule) -- this long means it is prose ABOUT the section, the
            # exact shape the critique-as-content failure takes.
            return None, f"bullet text reads like a sentence, not a headline ({len(text.split())} words)"
        detail_words = len(str(b.get("detail") or "").split())
        if detail_words > 40:
            return None, f"bullet detail is {detail_words} words, target is under 30"

    bullets = [{"text": b.get("text"), "detail": b.get("detail")} for b in bullets]
    orig = original if isinstance(original, dict) else {}
    vdict = value if isinstance(value, dict) else {}
    if section == "refresher":
        return {"previousTopic": vdict.get("previousTopic") or orig.get("previousTopic"),
               "recap": bullets}, None
    if section == "challenge":
        return {"activity": vdict.get("activity") or orig.get("activity"),
               "points": bullets}, None
    if section == "explore":
        out = {"points": bullets}
        out["imageFocus"] = vdict.get("imageFocus") or orig.get("imageFocus") or ""
        handoff = vdict.get("handoff") if isinstance(vdict.get("handoff"), dict) else orig.get("handoff")
        if isinstance(handoff, dict):
            out["handoff"] = handoff
        return out, None
    if section == "concept":
        return bullets, None  # the one bare-list section, no wrapper at all
    return {"points": bullets}, None


async def revise_lesson(lesson: dict, findings: list[dict], *,
                        topic: str, grade: str, subject: str) -> tuple[dict, list[str]]:
    """Rewrites only the sections `findings` named, using generation's own
    model -- writing prose is what it is for; independence matters for the
    JUDGE, not the writer.

    Returns (lesson_copy, rejected_sections). A section whose rewrite fails
    `_is_real_content` is NOT applied -- the original stands, and its key is
    reported back so the caller's log and the saved report both say plainly
    that this one could not be safely improved this round, rather than
    silently either corrupting it or silently leaving it exactly as flagged.
    One retry, with the specific rejection reason quoted back, before giving
    up on that section -- the same "tell it what was wrong with the last
    answer" shape call_json already uses for a malformed response.
    """
    from generation.compose import GENERATION_MODEL
    from prep_flow.sections import SECTION_POLICY, container_for

    by_section: dict[str, list[str]] = {}
    for f in findings:
        section = f.get("section") or "none"
        by_section.setdefault(section, []).append(
            f"{next((d['label'] for d in DIMENSIONS if d['key'] == f['dimension']), f['dimension'])}: {f['reason']}")
    sections_named = [s for s in by_section if s != "none" and s in SECTION_POLICY]
    out = json.loads(json.dumps(lesson))  # deep copy; never mutate the caller's material
    if not sections_named:
        return out, []

    # ONE CALL PER SECTION, NOT ONE CALL FOR ALL OF THEM. Bundling four
    # sections into a single request is what produced empty content for most
    # of them on the previous run -- the model had to write four different
    # sections, each satisfying its own set of findings, inside one response,
    # and simply returned nothing for most. A section at a time is a small
    # enough ask to actually answer, and a failure costs that one section
    # instead of the whole sheet's round.
    rejected: list[str] = []
    for section in sections_named:
        findings_block = "- " + section + ":\n" + "\n".join(
            f"    * {msg}" for msg in by_section[section])
        section_rules = f"- {section}: {SECTION_POLICY[section]['rule']}"
        section_shapes = f'- "{section}": {_shape_skeleton(section)}'
        current = _sheet_text(lesson)

        extra_instruction = ""
        applied = False
        for attempt in range(2):
            prompt = _REVISE_PROMPT.format(
                grade=grade, subject=subject, topic=topic,
                sheet=current, findings=findings_block,
                section_rules=section_rules, section_shapes=section_shapes) + extra_instruction
            try:
                data = await call_json(
                    prompt, label=f"quality-revise[{section}]", required=("sections",),
                    model=GENERATION_MODEL, temperature=0.5, max_tokens=2000)
            except Exception as exc:                       # noqa: BLE001
                print(f"[quality-revise] {section}: call failed ({exc}); keeping original")
                break

            value = (data.get("sections") or {}).get(section)
            outer_key, _ = container_for(section)
            if value is None:
                # THE THING WE DID NOT HAVE BEFORE: what the model actually
                # sent back, not just the validator's verdict on it. Every
                # earlier attempt to fix this was a guess (bigger context,
                # then one-section-at-a-time) because "no bullets came back"
                # describes the symptom, not the cause.
                print(f"[quality-revise] {section}: RAW RESPONSE ON EMPTY "
                     f"-> {json.dumps(data, ensure_ascii=False)[:800]}")
                problem = "the section was missing from the answer"
            else:
                normalised, problem = _validate_and_normalise(section, value, lesson.get(outer_key))
                if not problem:
                    out[outer_key] = normalised
                    applied = True
                    break
                print(f"[quality-revise] {section}: RAW VALUE REJECTED ({problem}) "
                     f"-> {json.dumps(value, ensure_ascii=False)[:800]}")

            if attempt == 1:
                print(f"[quality-revise] keeping original content for {section} ({problem})")
                break
            extra_instruction = (
                f"\n\nYour previous answer was rejected: {problem}. Write REAL "
                f"lesson content for `{section}` this time -- an activity, a hook, "
                "a question a child answers -- not a description of what was wrong, "
                "and make sure the section key is present in the JSON.")

        if not applied:
            rejected.append(section)

    return out, rejected


async def run_quality_loop(lesson: dict, *, topic: str, book_heading: str,
                           grade: str, subject: str, chapter_title: str,
                           on_round: Optional[Callable[[int, dict, dict, dict], None]] = None) -> dict:
    """The judge -> fix -> re-judge loop itself, not just its building blocks.

    `on_round(loop_n, lesson, report, summary)`, if given, fires after every
    round's judging -- before this function decides whether that round is the
    new best, and before any revise call. It exists so a caller can still
    save every round's own material/rating to disk (as founder-facing runs
    do) without this function having to carry that presentation concern
    itself; this function's only job is looping, scoring, and picking the
    peak.

    RETURNS THE BEST-SCORING ROUND, NOT THE LAST ONE. Every prior caller of
    judge_lesson/revise_lesson (this module's own callers included) just kept
    whatever the final round produced -- but each round's revise is an
    independent attempt with no memory of earlier rounds (see the module
    docstring on why: a stateless judge resists anchoring and catches
    regressions a comparison-based one would miss watching for). Independence
    cuts both ways, though: nothing stops a later round's fix from genuinely
    undoing an earlier round's gain, and a real run proved it -- one topic's
    targetable score went 3.22 -> 3.44 -> 3.22, and the caller reported the
    final 3.22 as the result even though round 2 was strictly better. The
    judge itself must stay stateless for the reasons above; this function is
    where the fix belongs instead -- plain arithmetic comparing each round's
    score to the best seen so far, no LLM involved, no anchoring risk.

    Returns {"finalLesson", "finalReport", "finalSummary", "history"} --
    `history` still lists every round in order (so the full before/after
    story stays visible), but the three "final*" keys are the peak-scoring
    round's own lesson/report/summary, whichever round that was.
    """
    best_lesson, best_report, best_summary = lesson, None, None
    best_score = -1.0
    history: list[dict] = []
    current_lesson = lesson
    loop_n = 1

    while True:
        report = await judge_lesson(
            current_lesson, topic=topic, book_heading=book_heading,
            grade=grade, subject=subject, chapter_title=chapter_title)
        summary = summarise(report, current_lesson)
        low = low_scoring_revisable(report, current_lesson)

        if on_round is not None:
            on_round(loop_n, current_lesson, report, summary)

        if summary["targetable"] > best_score:
            best_score = summary["targetable"]
            best_lesson, best_report, best_summary = current_lesson, report, summary

        # "keptAsBest" is filled in on EVERY entry once the loop ends (below),
        # not here. Marking it True the moment a round becomes the new best
        # and never revisiting it is what produced a real, confirmed bug: once
        # loop 3 later beat loop 1, loop 1's flag was still sitting at True
        # from when it briefly held the lead, so a caller picking "the" best
        # entry via the first True flag reported loop 1 -- the wrong loop --
        # even though finalLesson/finalSummary (tracked separately, above)
        # were already correctly loop 3's. Exactly one entry may end up True;
        # deciding that requires seeing every round, so it happens after.
        history.append({
            "loop": loop_n, "targetable": summary["targetable"], "overall": summary["overall"],
            "atOrAbove4": summary["atOrAbove4"], "targetableCount": summary["targetableCount"],
            "fixableFindings": len(low),
        })

        if not low or loop_n > MAX_FIX_ROUNDS:
            break

        current_lesson, rejected = await revise_lesson(
            current_lesson, low, topic=topic, grade=grade, subject=subject)
        if rejected:
            history[-1]["rejectedSections"] = rejected
        loop_n += 1

    # Exactly one entry marked True: the first round to reach the peak score
    # (ties favour the earliest -- a later round that only matched, not beat,
    # an earlier one bought nothing and shouldn't look like the reason this
    # material is what it is).
    best_loop_n = max(history, key=lambda h: h["targetable"])["loop"]
    for h in history:
        h["keptAsBest"] = h["loop"] == best_loop_n

    return {
        "finalLesson": best_lesson, "finalReport": best_report,
        "finalSummary": best_summary, "history": history,
    }
