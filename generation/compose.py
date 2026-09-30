"""Prep Material Generation Agent — writes the six sections, in sequence.

Generation runs in WINDOWS of consecutive topics rather than one call per topic
or one call for the chapter, and the window size is the only interesting design
decision in this module.

  * One call per topic loses the chain. The Refresher of T8 has to be built from
    the Explore of T7, and handing a model a summary of T7's Explore produces a
    Refresher about roughly the right thing. Handing it T7's Explore as something
    it just wrote produces one that lands.
  * One call for forty topics truncates. Six sections of teacher-grade detail is
    ~1300 output tokens; forty of them is not a response, it is a document.
  * A window of three writes T7, T8 and T9 in one pass, so two of the three seams
    are internal — the model reads its own Explore two paragraphs up — and only
    the seam at the window boundary needs an explicit hand-off.

Windows are also why this is a graph loop rather than a for-loop inside one node:
each window is checkpointed, so a 40-topic run that dies at window 9 resumes at
window 9 instead of paying for the first eight again.
"""
import json
import os
import re

from prep_flow import provenance
from prep_flow.deps import textbook_prompt_block
from prep_flow.moves import (
    book_activity,
    challenge_name_from,
    usable_challenge_name,
    moves_prompt_block,
    staging_note,
    teaching_order,
)
from . import reinforcement
from prep_flow.clauses import normalise_activity_name, same_activity
from prep_flow.llm import call_json
from prep_flow.sections import (
    BULLETS_PER_SECTION,
    SECTION_LABELS,
    SECTION_ORDER,
    SECTION_POLICY,
    handoff_of,
    handoff_summary,
)
from prep_flow.agents.planning import adaptive_directive_block
from prep_flow.agents.experience import experience_block
from validation_flow.learner import diagnosis_block
from prep_flow.agents.reasoning import chapter_block, mastery_block, topic_anchors_block

# Generation (and repair, which is the same prompt rewriting a subset of
# sections) is the one stage where the actual prose quality is the whole
# product — every "pre-generation" stage (sequencing, context assembly,
# reasoning, experience, planning, activity selection) just gathers and
# structures data ai.py's global MODEL already handles fine. Separate env
# vars so this stage alone can be routed to a different, potentially slower
# or heavier model without moving every call in the pipeline onto it.
# Unset (the default) means "use ai.py's global MODEL", same as before this
# existed.
GENERATION_MODEL = os.environ.get("OPENROUTER_GENERATION_MODEL") or None
GENERATION_TIMEOUT_S = (
    float(os.environ["OPENROUTER_GENERATION_TIMEOUT_S"])
    if os.environ.get("OPENROUTER_GENERATION_TIMEOUT_S") else None
)
# Whether GENERATION_MODEL is asked to think before answering (OpenRouter's
# `reasoning` map) — only meaningful if that model supports thinking tokens,
# and quietly ignored by every model that does not.
#
# Default OFF, and that default is load-bearing rather than cautious. Turned
# on against tencent/hy3, a three-topic window came back with
# finish_reason='length' after 11600 reasoning tokens and NO content: the
# model had spent the entire completion budget thinking and had nothing left
# to write six sections with. Structured JSON generation is also the case
# that benefits least from chain-of-thought — the prompt already carries the
# plan, the activity, the grounding and the section contract, so the thinking
# has largely been done for it by the earlier stages.
GENERATION_REASONING = os.environ.get(
    "OPENROUTER_GENERATION_REASONING", "false").strip().lower() in ("1", "true", "yes", "on")
# When reasoning IS on, this is both the cap handed to the model
# (`reasoning.max_tokens`, so it must stop thinking and start answering) and
# the amount added to the completion budget to pay for it. Capping matters
# more than the size: an uncapped reasoning model treats the whole
# max_tokens as thinking budget, which is exactly the failure above.
GENERATION_REASONING_HEADROOM = int(
    os.environ.get("OPENROUTER_GENERATION_REASONING_TOKENS", "12000"))


def _reasoning_option() -> dict | None:
    """OpenRouter's `reasoning` map for the generation/repair calls, or None.

    Always capped when enabled — see GENERATION_REASONING_HEADROOM.
    """
    if not GENERATION_REASONING:
        return None
    return {"enabled": True, "max_tokens": GENERATION_REASONING_HEADROOM}

# Same reasoning as ai.py's own startup print: this is what actually answers
# "did my .env change take effect" for the generation-only overrides,
# printed once at import time rather than guessed at.
print(f"[prep_flow:generation] model override: {GENERATION_MODEL or '(none — uses ai.py default)'} "
      f"| timeout override: {GENERATION_TIMEOUT_S if GENERATION_TIMEOUT_S is not None else '(ai.py default)'} "
      f"| reasoning: {GENERATION_REASONING}")

_BULLET = ('{"text": "6-12 word headline", "detail": "2-4 sentences, UNDER 60 WORDS TOTAL: what '
           'to do, how long to wait, how students respond, what to watch for -- cover what this '
           'bullet actually needs, in more depth than a one-line note. Still an instruction to '
           'follow in your own words, never a line to read aloud -- more coverage, not padding."}')


def section_contract(order: tuple = SECTION_ORDER) -> str:
    """The six-section rules, rendered from SECTION_POLICY.

    Rendered rather than written out, so a change to the contract cannot land in
    the prompt and miss the validator, or the reverse.

    `order` is the sequence to number them in. It stays canonical for the
    chapter-wide block at the top of the prompt — where the contract is being
    stated, not sequenced — and each topic block then carries its own order,
    read off its own pages by moves.teaching_order().
    """
    lines = []
    for i, section in enumerate(order, start=1):
        policy = SECTION_POLICY[section]
        licence = {
            "grounded": "NO INVENTION",
            "mixed": "HALF GROUNDED",
            "creative": "YOUR CREATIVITY, FULLY",
        }[policy["mode"]]
        lines.append(
            f"{i}. {policy['label']}  [{licence}]  — source: "
            f"{policy['source'].replace('_', ' ')}\n   {policy['rule']}"
        )
    return "\n".join(lines)


def _visuals_shape() -> str:
    return """,
  "visuals": {
    "objects": [{"label": "one or two words a 6-year-old would say", "emoji": "ONE emoji", "quantity": 1, "attributes": {"<keyAttribute>": "value", "<decoy>": "value"}}, "... 4-6 objects, all named somewhere in THIS topic's sections"],
    "keyAttribute": "the ONE attribute this topic teaches — what decides whether two objects belong together",
    "decoyAttributes": ["0-2 attributes students wrongly match on — each must appear in EVERY object"],
    "riddle": {"clues": ["2-3 clues describing one object by its key attribute, never naming it"], "answer": "that object's label", "examples": ["2-3 everyday Indian things sharing that attribute"]}
  }"""


_GENERATION_PROMPT = """You are an expert teaching assistant writing lesson prep material for teachers in
an Indian primary classroom. This is a TEACHER'S prep sheet, not a student
handout: "text" says WHAT happens, "detail" tells the teacher HOW to run it —
what to do, how long to wait, whether students answer aloud or in pairs, and
what to watch for. This is GUIDANCE, not a script: never write the exact
sentence a teacher is meant to read aloud, and never put words in quotation
marks as if they are dialogue. Describe the move a teacher would recognise
from their own classroom, in plain instructional language — not a line to
perform. A first-time teacher should not have to improvise anything, but they
should still sound like themselves, not an actor reading lines.

You are writing topics {first}-{last} of {total} in one chapter. These sheets are
consumed in order, one per class period, by a teacher working through the chapter.

THE ROOM THIS IS WRITTEN FOR, EVERY TIME: one teacher, 30-60 children, fixed
benches, a blackboard, and the children's own textbook -- nothing else
guaranteed. No projector, no printer, no internet, no per-child materials
beyond what the sheet itself tells the teacher to distribute. If a bullet
needs the whole class to see something happen at the front (an object's angle
changing, sticks forming a shape, a pattern being assembled), the teacher
puts the result on the blackboard as it happens -- never "hold it up" or
"project it" as the only way the class sees it. This is not a style note; it
is the room every sheet is graded against.

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
CHAPTER ARC: {arc}

{engagement}

THE SIX SECTIONS. Every sheet has all six, always. What differs between them is
where each one's content is ALLOWED to come from:

{contract}

THE ORDER THEY RUN IN IS NOT FIXED — IT IS THE BOOK'S.
Refresher always opens and Explore always closes, because those two answer to
yesterday's period and tomorrow's rather than to the page. The four in between
run in whatever order THIS topic's textbook pages explain the topic in, and every
topic block below states its own order and the moves it was read from. A book
that opens on a village scene and names the rule afterwards gets a sheet that
does the same. You are not free to explain first because explaining first is
tidier.
{adaptive_block}{reasoning_block}
HOW ONE SHEET BECOMES THE NEXT — the rule that makes this a chapter and not
{total} loose sheets:

    Explore of topic N  ─────>  Refresher of topic N+1

{chaining}
{carry_in}
{topic_blocks}

Return ONLY valid JSON, no markdown fences. One object per topic, in order:
{{
  "materials": [
    {{
      "index": {first},
      "teachingOrder": ["the six section keys in the order THIS topic's block says to teach them"],
      "planningNote": "1-2 sentences of your own reasoning, written first: what this period bridges from and what it sets up.",
      "objective": "ONE verb-first headline, 4-8 words. Good: 'Count and compare small groups of objects'. Bad: 'Students will be able to count objects.'",
      "floor": "ONE sentence naming a SPECIFIC weaker child's path through THIS sheet's own "
               "objects and tasks -- not a new object, not a new task, the same ones already "
               "named above, reduced. State what they can do and how, concretely: 'given the "
               "same tumbler shown in Concept, can say whether the top view and side view look "
               "the same or different, just by looking at the real object from both sides -- "
               "no drawing or comparison to a second object needed yet.' A floor that names a "
               "different object than the one Concept or Challenge actually uses is not this "
               "topic's floor, it is a different, easier topic -- the whole point is that the "
               "SAME lesson has a path in for the weakest child, not a separate one.",
      "previousTopicRefresher": {{
        "previousTopic": "the previous topic's title",
        "recap": [{bullet}, "... EXACTLY {bullets} — built from the PREVIOUS topic's Explore, nothing else"]
      }},
      "concept": {{"points": [{bullet}, "... EXACTLY {bullets} — from the textbook pages below and nowhere else, with (Page N) citations"]}},
      "realLife": {{"points": [{bullet}, "... EXACTLY {bullets}"]}},
      "challenge": {{
        "activity": "the SELECTED ACTIVITY name for this topic, character-for-character",
        "points": [{bullet}, "... EXACTLY {bullets}, as PLAY -> REFLECT -> ACT"]
      }},
      "levelSet": {{"points": [{bullet}, "... EXACTLY {bullets}"]}},
      "explore": {{
        "points": [{bullet}, "... EXACTLY {bullets} — each detail starts with 'Tell students "
                  "that...' / 'Tell students to...' / 'Ask students to...', reporting the "
                  "TEACHER instructing the class, in the room, on something to go notice in "
                  "their own real life outside class. Never a direct second-person command to "
                  "the child ('Look at...', 'Take a...', 'Find an object...')."],
        "imageFocus": "one short phrase: the single most useful thing to sketch on the board — "
                       "must be an object actually named in explore.points above, never a "
                       "different section's anchor object"
      }},
      "sectionWatch": {{
        "refresher": {bullet}, "concept": {bullet}, "realLife": {bullet},
        "challenge": {bullet}, "levelSet": {bullet}, "explore": {bullet}
      }},
      "materialsUsed": ["every item actually referenced above — nothing invented, nothing unused"],
      "pagesCited": [12, 13],
      "figureRefs": ["ids of the printed pictures this sheet actually tells the class to look at, copied exactly from the list above — [] if none"],
      "timings": {{"refresher": 3, "concept": 6, "realLife": 4, "challenge": 8, "levelSet": 4, "explore": 5}}{visuals}
    }}
  ]
}}

Rules, all of them non-negotiable:
- THE BOOK CARRIES THE LESSON; YOU CARRY THE TEACHING. Roughly FOUR IN EVERY
  FIVE things the class meets on this sheet must come off these printed pages —
  the definitions, the numbers, the worked examples, the named places and
  people, the pictures, and the tasks the book itself sets. Roughly ONE IN FIVE
  is yours: how it is staged, the pacing, the pair-or-whole-class choice, the
  local framing, and the worked answers the book leaves out. The children have
  this exact book open in front of them, and a sheet that teaches around it
  instead of through it leaves thirty children looking at a page nobody
  mentioned. When the book offers a usable example, a usable context or a
  usable task, USE THE BOOK'S — reach for your own only where the book gives
  you nothing, or where what it gives genuinely will not land for a rural
  government-school class. Invention is the exception you justify, not the
  default you fall back on.
- Every one of the six sections wraps its bullets in {{"points": [...]}}, with the
  single exception of the Refresher, whose list is called "recap". Close each list
  with ] and each object with }}. Check the brackets before you answer.
- EXACTLY {bullets} bullets in every one of the six sections. Not two, not four.
- "text" is a 6-12 word headline. "detail" is 2-4 sentences, UNDER 60 WORDS,
  and carries the substance: what the teacher does, the wait time, how
  students respond, what to watch for -- cover what THIS bullet actually
  needs from that list, in real depth, not a clipped one-liner. Still not a
  paragraph of reasoning: a teacher reading this sheet before first period
  needs the fuller instruction, not an essay on why -- more coverage of the
  action itself, not padding around it.
- GUIDANCE, NOT A SCRIPT. Never write "detail" as a sentence for the teacher to
  read aloud, and never wrap words in quotation marks as if they are dialogue.
  Describe the move ("ask what they remember about X"), not the words to say
  ("say: 'what do you remember about X?'"). A teacher should finish reading this
  sheet knowing what to do, in their own words — not lines to perform.
- STUDENT ACTION FIRST IN THE FOUR STUDENT SECTIONS — Refresher, Challenge,
  Level Set and Explore. Each of those opens with something students DO — pair
  up, try, guess, count, build. A "text" that starts "Explain...", "Tell
  them..." or "Say that..." is wrong there; rewrite it around the student action.
  CONCEPT AND REAL LIFE ARE THE EXCEPTION AND ARE TEACHER-LED BY DESIGN: in
  those two the teacher explains and demonstrates while the class watches,
  answers and thinks. Do not stage pair work, group work, timed tasks or
  "give each pair..." in Concept or Real Life — that is what the Challenge is
  for, and a period that runs an activity in all six sections has the minutes
  for none of them. See each section's own rule above, which is authoritative.
- ANSWERS ARE FOR THE TEACHER. "detail" states the worked answer so the teacher
  is confident, but what reaches the class stays invitational — what to listen
  for, never "the answer is X" announced aloud.
- USE THE PRINTED PICTURES. Where a picture is listed for these pages, prefer
  pointing the class at it over asking the teacher to draw something. "Look at
  the family tree on page 13" beats "sketch a family tree on the board" — the
  picture is already in front of every child.
- CONCEPT TAKES ITS FACTS FROM THE TEXTBOOK AND ITS TEACHING FROM YOU. Same
  numbers, same definitions, same technical words, cited as (Page N). If the book
  says one-third, the lesson says one-third, and you never add a fact those pages
  do not contain. But the EXAMPLE that carries the idea is yours to choose: use
  the book's when it is the best one for these children, and use something they
  can hold when it is not. Cite the page for where the IDEA comes from — never
  tell the class to look at a picture that is not printed there.
- CHALLENGE, LEVEL SET AND EXPLORE ARE YOURS. This is where the lesson lives or
  dies. Invent the play, the stakes, the local framing, the surprise. The only
  fixed thing is the activity's name.
- "timings" must match the planned minutes for that topic and sum to {duration}.
- Use one consistent currency (₹) if money appears.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall",
  or "prerequisite" anywhere.
- Every topic's "explore.handoff" must be answerable by the topic after it. The
  planner has already told you what each one needs — land it.
- "floor" IS REQUIRED, NOT OPTIONAL, THE SAME AS "objective". Write it AFTER
  the six sections, not before — it has to describe a real path through what
  you actually wrote, and you cannot describe a path through sections that do
  not exist yet. Reread Concept and Challenge once you have written them, find
  the smallest thing in either one, and state the weakest child's path through
  THAT — never a new object, never an easier version of the topic that isn't
  actually on this sheet.
"""


def _challenge_source(moves: list) -> str:
    """What the Challenge is allowed to be, given what the book prints.

    Where these pages set the children a task, that task IS the Challenge. This
    is the substitution a teacher notices from the back of the room: thirty
    children have "Activity-3: paper folding to make a boat" open in front of
    them and the sheet is running a card-sorting game the library matched on a
    competency string.
    """
    printed = book_activity(moves)
    if not printed:
        return "\nTHE BOOK PRINTS NO ACTIVITY on these pages, so the Challenge is the\nselected activity below, staged freely.\n"

    where = f"  (Page {printed['page']})" if printed.get("page") else ""
    return "\n".join([
        "",
        "THE BOOK'S OWN ACTIVITY IS ON THESE PAGES, AND IT IS THE CHALLENGE:",
        f'  "{printed["verbatim"]}"{where}',
        "  The children have this printed in front of them. Set THIS task. What is",
        "  yours is how it runs — the pairing, the timing, the either/or choice inside",
        "  it, the PLAY -> REFLECT -> ACT staging, and the worked answer the book",
        "  omits. Use the staging template below only for HOW to run it.",
        '  "challenge.activity" is a SHORT NAME FOR that task — verb-first, 3-6 words,',
        "  the way a teacher would say it aloud (\"Trace the matchbox edges\", \"Colour",
        "  the matching shapes\"). Not the book's sentence copied out: that is a",
        "  heading on a printed sheet, and the task's actual wording belongs in the",
        "  bullets where the teacher reads it.",
        "",
    ])


def _topic_block(spec: dict, plan: dict, selection: dict, include_pages: bool = True,
                 context_plan: dict = None) -> str:
    knowledge = spec.get("knowledge") or {}
    activity = (selection or {}).get("activity") or {}
    context = (selection or {}).get("context") or {}
    formats = (selection or {}).get("formats") or {}
    hook = (plan or {}).get("exploreHook") or {}
    reasoning = spec.get("reasoning") or {}
    experience = spec.get("experience") or {}

    contexts = [c for c in (knowledge.get("contexts") or []) if isinstance(c, str) and c.strip()]

    grounding = ""
    if include_pages and (spec.get("excerpt") or "").strip():
        grounding = textbook_prompt_block({
            "matchedTopic": spec.get("anchor_heading") or spec["topic"],
            "chapterNumber": spec.get("chapter_number") or "",
            "chapterTitle": spec.get("chapter_title") or "",
            "pageStart": spec.get("page_start"),
            "pageEnd": spec.get("page_end"),
            "excerpt": spec["excerpt"],
            "truncated": False,
            "images": [],
        })
    elif include_pages:
        grounding = (
            "NO TEXTBOOK TEXT was ingested for these pages. The Concept section must "
            "therefore stay to what the topic title itself implies and must not invent "
            "facts, worked examples or definitions. Say less rather than more."
        )

    moves = spec.get("moves") or []

    lines = [
        f"═══ TOPIC {spec['index']}: {spec['topic']}"
        + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
        + f"  (pages {spec.get('page_start')}-{spec.get('page_end')}) ═══",
        "",
        # First, above the plan and above the pages, because it is the thing the
        # rest of this block is arranged around: not what these pages say, but in
        # what order they say it.
        moves_prompt_block(moves, spec.get("topic") or ""),
        "",
        staging_note(moves),
        "",
        "PLAN FOR THIS PERIOD:",
        f"  Focus: {plan.get('focus') or '(none recorded)'}",
        f"  Heaviest section: {plan.get('emphasis', {}).get('heaviest') or 'none'}"
        f" | lightest: {plan.get('emphasis', {}).get('lightest') or 'none'}",
        f"  Minutes: {json.dumps(plan.get('minutes') or {})}",
        f"  Difficulty vs previous topic: {plan.get('difficultyStep') or 'same'}",
    ]
    if plan.get("newVocabulary"):
        lines.append(f"  New words THIS topic introduces: {', '.join(plan['newVocabulary'])}")

    # The knowledge this period moves the class through, underneath the experience
    # the sections stage. It is what the Level Set is actually checking for, and
    # what the Explore has to leave true — a sheet that entertains for 30 minutes
    # and does not land `gained` has broken the next lesson, not this one.
    if reasoning.get("gained") or reasoning.get("assumes"):
        lines += [
            "",
            "  THE UNDERSTANDING THIS PERIOD CARRIES:",
            f"    they arrive able to:   {reasoning.get('assumes') or 'nothing from this chapter yet — this is where it starts'}",
            f"    they must leave able to: {reasoning.get('gained') or '(not derived)'}",
        ]
        if reasoning.get("bridgesTo"):
            lines.append(f"    which the next topic builds on as: {reasoning['bridgesTo']}")

    # Underneath the gain, because it qualifies it rather than replacing it: the
    # gain is what this period leaves and the target is what the idea requires, and
    # a sheet written to the gain alone lands the page's own example. The audited
    # shortfalls come with it ONLY when no experience plan chose one — see
    # mastery_block's docstring for why printing both stages all three.
    audit = mastery_block(reasoning,
                          missing=not (experience.get("gap")
                                       and experience.get("gapCloser")))
    if audit:
        lines.append(audit)
    if reasoning.get("misconceptions"):
        lines += [
            "",
            "  WHAT THEY WILL GET WRONG HERE. Do not warn them off it, and do not",
            "  correct it in advance. Level Set bullet 1 offers it back as a real option,",
            "  in these words, so the teacher hears who still holds it:",
        ] + [f"    - {belief}" for belief in reasoning["misconceptions"][:2]]

    # The Experience Plan supersedes the loose pool when one was derived: it has
    # already chosen WHICH object, and why. Printing both would invite the model
    # to shop around after the choice was made.
    staged = experience_block(experience)
    if staged:
        lines.append(staged)
    else:
        pool = topic_anchors_block(reasoning.get("anchors") or [])
        if pool:
            lines += ["", pool]
    if plan.get("refresherBridge"):
        lines += [
            "",
            "  PLANNED REFRESHER BRIDGE — what this topic's Refresher must bring back:",
            f"    {plan['refresherBridge']}",
        ]

    # NODE 2's adaptations, last: everything above establishes what the lesson is,
    # and this says what to do differently about it in THIS room. Empty string
    # when there is no plan or none of it lands on this topic, so a chapter run
    # without contextual reinforcement produces the identical prompt it always
    # did — which is what makes the A/B comparison in `generation/compare.py`
    # measure the plan rather than a prompt rewrite. See reinforcement.py.
    reinforcement_block = reinforcement.block(context_plan, spec["index"])
    if reinforcement_block:
        lines.append(reinforcement_block)

    lines += [
        "",
        "CURRICULUM KNOWLEDGE extracted from these pages:",
        f"  Concepts: {', '.join(knowledge.get('concepts') or []) or '(none)'}",
        f"  Competencies: {', '.join(knowledge.get('competencies') or []) or '(none)'}",
        f"  Vocabulary: {', '.join(knowledge.get('vocabulary') or []) or '(none)'}",
        f"  Learning outcomes: {', '.join(knowledge.get('learning_outcomes') or []) or '(none)'}",
        f"  Bloom: {knowledge.get('bloom_level') or '?'} | Difficulty: {knowledge.get('difficulty') or '?'}",
        "",
        "TEXTBOOK CONTEXTS — real-world things THIS BOOK itself names on these pages:",
        f"  {', '.join(contexts) or '(none named)'}",
        "  These outrank anything you would invent: the class has already met them on",
        "  the page. Use them first in Real Life and Explore. Skip any that is a",
        "  classroom tool rather than something a child could see outside school (a",
        "  place-value chart is not a real-world context).",
        "",
        _challenge_source(moves),
        # Reworded, not just reordered. Telling the model the book's task IS the
        # Challenge and then, four lines later, "copy the name exactly" is an
        # argument between two instructions, and the more imperative one wins:
        # on a six-topic Class 3 Maths run the model reached for the library
        # template in three of the six topics that printed their own activity.
        # The template is still here — a book task of five words ("Fill the empty
        # boxes") needs one — but it is named for what it now is.
        ("STAGING TEMPLATE — how to RUN the book's task above. Its name is NOT the "
         "Challenge name; name the Challenge for what the book asks the children to do:"
         if book_activity(moves) else
         "SELECTED ACTIVITY for this topic's Challenge — copy the name exactly:"),
        # THE FIELD LABEL BRANCHES TOO, and that is the whole fix. The header above
        # already said "its name is NOT the Challenge name" and was ignored, because
        # four words later came a field labelled `Name:` — and a labelled field beats
        # a sentence. Measured on a three-topic run: all three Challenges were titled
        # with the library template ("Clap Patterns" over a match-stick task), which
        # is the very confusion the header was rewritten to prevent.
        (f"  Staging template, NOT the Challenge name: {activity.get('name') or '(none)'}"
         if book_activity(moves) else
         f"  Name: {activity.get('name') or '(none matched — invent one true to the topic)'}")
        + (f" ({activity['category']})" if activity.get("category") else ""),
        f"  Description: {activity.get('description') or '(none)'}",
        f"  Grouping: {activity.get('grouping') or 'pair'}"
        f" | Assessment: {activity.get('assessmentMethod') or 'observation'}",
        f"  Suggested context: {context.get('name') or '(none — use a textbook context above)'}",
    ]
    if formats:
        lines += [
            "",
            "SECTION FORMATS chosen for this topic, to keep the chapter varied — use",
            "these shapes rather than defaulting to the same one every period:",
        ]
        lines += [f"  {SECTION_LABELS.get(k, k)}: {v}" for k, v in formats.items()]

    figures = [f for f in (spec.get("figures") or []) if (f.get("caption") or "").strip()]
    # Captions arrive two different ways, and only one of them has ids.
    #
    # `spec["figures"]` is the structured form, from a classified ingest. Most
    # books do not have one. What they DO have is the vision pass's captions
    # grafted inline into the excerpt as [Figure: …] — 54 of them on the Class 3
    # Maths chapter, every topic covered — and for a while this block told the
    # model there were no pictures while it was looking straight at them. It
    # responded by inventing ids for figures it could plainly see, which the
    # normaliser then stripped. The captions were never the problem; the
    # instruction was.
    # Case-insensitive, and on the marker: "[Figure: ...]" from the inline-caption
    # path and "[FIGURE fig_11_1 -> ...: ...]" from a classified ingest are both
    # pictures, and only the first spells it that way.
    captioned = len(re.findall(r"\[FIGURE(?![A-Za-z])", spec.get("excerpt") or "", re.I))
    if not figures and captioned:
        lines += [
            "",
            f"THESE PAGES PRINT {captioned} PICTURE(S), described inline in the textbook text",
            "below as [Figure: …]. They are really in the children's book, and every child",
            "has that book open — so pointing at one beats asking the teacher to draw",
            "something, and beats describing it in words the class can simply look at.",
            "Refer to a picture BY WHAT IT SHOWS and by the page it sits on: \"look at the",
            "bucket at the top of page 12\". Take the description from the [Figure: …] note",
            "itself — never promise a picture those pages do not contain, and never invent",
            "a figure id or number. Leave \"figureRefs\" empty: these captions carry no ids.",
        ]
    elif not figures:
        # Genuinely no pictures. Silence here was read as permission, so it is said.
        lines += [
            "",
            "NO PICTURES ARE INGESTED FOR THESE PAGES. Leave \"figureRefs\" empty and never",
            "name, number or describe a printed picture — there is nothing to point at, and",
            "\"look at the picture on page 13\" is a defect the teacher meets mid-lesson.",
            "Where a visual would help, have the class look at the real object instead.",
        ]
    if figures:
        lines += [
            "",
            "PICTURES PRINTED ON THESE PAGES. These are the REAL illustrations in the",
            "children's book — the class can be told to look at them, which beats asking",
            'the teacher to draw one. Reference a picture by its id in "figureRefs" and',
            "say in the bullet what to point at:",
        ]
        lines += [f"  {f['id']} (page {f['page']}): {f['caption'][:220]}"
                  for f in figures[:12]]
        lines.append("  Only these ids exist. Never invent one, and never describe a "
                     "picture that is not listed here.")

    if grounding:
        lines += ["", grounding]
    return "\n".join(lines)


def _chaining_note(count: int, first_index: int) -> str:
    """Where each topic in this response gets its Refresher from.

    This paragraph used to be one unconditional sentence: "each topic's Refresher
    is built from the Explore you wrote for the topic immediately above it."
    True of the second and later topics in a window. FALSE of the first, which
    has no topic above it in the response — its previous Explore was written in
    an earlier call and arrives quoted in `_carry_in` below.

    Stated unconditionally, it pointed the model at something that did not exist
    and the model filled the gap by inventing a previous lesson out of the
    current topic's own textbook content. Measured over 8 repeats of a 4-topic
    fixture at window size 3, where T4 is the only window-opener:

        in-window successors (T2, T3)   seam 3.19
        cross-window successor (T4)     seam 1.75   — scored 1 in 5 of 8 runs

    A 1.44-point hole in the criterion that makes a chapter a chapter, on every
    window boundary — one sheet in three at the default window size, and
    invisible to a teacher who cannot diff two sheets.
    """
    if count <= 1:
        return (
            "You are writing ONE topic in this response. There is no topic above it "
            "here, so there is no Explore of yours to reach back into — the previous "
            "period was written in an earlier call and is quoted below. Build the "
            "Refresher from that quoted block and from nothing else."
        )
    return (
        f"You are writing {count} consecutive topics in this one response. Write them "
        "IN ORDER, finishing each completely before starting the next.\n"
        f"  * Topic {first_index} is the FIRST of this response and has no topic above "
        "it here. Its Refresher comes from the previous period quoted below, which you "
        "did not write.\n"
        f"  * Topics {first_index + 1} onward take their Refresher from the Explore YOU "
        "wrote for the topic immediately above.\n"
        "Either way: do not summarise that Explore — reach back into it and bring its "
        "actual scene, its actual object and its actual unanswered question back into "
        "the room."
    )


def _carry_in(state: dict, first_index: int) -> str:
    """The seam at the window boundary: the previous topic's Explore, quoted.

    Topic 1 gets the one legitimate exception in the whole contract — there is no
    previous Explore, so it has NO Refresher at all, not a substitute one built
    from general prior knowledge. Saying so explicitly matters: left unsaid, the
    model either invents a previous lesson the class never had, or writes a
    "cold open" recap that reads exactly like the real thing to a teacher who
    has no way to tell topic 1 apart from topic 2. `_normalise_material()`
    enforces this too — nulling it there is what makes it a guarantee rather
    than a request.
    """
    if first_index <= 1:
        return (
            "\nTHIS IS THE FIRST TOPIC OF THE CHAPTER. There is no previous Explore, and "
            "no Refresher belongs here at all — set \"previousTopicRefresher\" to null and "
            "start the sheet at Concept. Do not invent a previous lesson, and do not "
            "write a substitute recap from \"everyday life\" or an earlier chapter — a "
            "teacher's first period on this chapter should not open with a review of "
            "anything.\n"
        )
    previous = (state.get("materials") or {}).get(first_index - 1)
    if not previous:
        return (
            f"\nWARNING: topic {first_index - 1}'s material is not available, so its "
            f"Explore cannot be quoted. Build topic {first_index}'s Refresher from that "
            "topic's TITLE and the planned bridge above, and keep it short rather than "
            "inventing a scene the class may not have seen.\n"
        )
    topics = {t["index"]: t for t in (state.get("topics") or [])}
    previous_title = topics.get(first_index - 1, {}).get("topic", "")
    return (
        f"\nPREVIOUS EXPLORE — this is what the class actually did last period, as "
        f"written. Topic {first_index}'s Refresher is built from THIS and nothing else:\n"
        "--- BEGIN PREVIOUS EXPLORE ---\n"
        f"{handoff_summary(previous, previous_title)}\n"
        "--- END PREVIOUS EXPLORE ---\n"
        # The same prohibition topic 1 has carried all along, which the
        # cross-window case never got. The measured failure was not vagueness —
        # it was invention: Refreshers opening on a match-stick activity the
        # class had never done, lifted from THIS topic's own textbook pages.
        f"Do not build the Refresher from topic {first_index}'s own pages, and do not "
        "invent a previous activity. If the quoted Explore gives you no scene, use its "
        "object and its open question and keep the Refresher short — a thin Refresher "
        "that is TRUE is worth more than a rich one the class never lived.\n"
    )


def _normalise_material(entry: dict, spec: dict, plan: dict, selection: dict,
                        previous_material: dict = None) -> dict:
    """Fill in what the model is not trusted to get right, and nothing else.

    Deliberately narrow. This repairs identity and bookkeeping — which topic this
    is, which activity was selected, what the timings were planned as — and never
    touches the prose. Prose problems are the validator's to report, because a
    silently patched-up sheet is one nobody ever fixes properly.

    `previous_material`: the previous topic's finished material, when the
    caller has it — purely so the Refresher's citation (below) can quote the
    actual scene/object/question it is built from, instead of a generic
    "built from the previous Explore" sentence. Never used to touch prose.
    """
    material = dict(entry)
    material["index"] = spec["index"]
    material["topic"] = spec["topic"]
    material["subtopic"] = spec.get("subtopic") or ""
    material["pageRange"] = [spec.get("page_start"), spec.get("page_end")]

    # The teaching order is bookkeeping, not prose: it was DERIVED from this
    # topic's moves before the prompt was built, so the model echoing it back is
    # never the source of truth. Taken from the spec unconditionally.
    #
    # The echo is still asked for and still kept, one key over. The sections are
    # JSON keys, so a model CANNOT express the order structurally — it can only
    # write each section's content as if it sits where it was told. Which means
    # the echo is the one cheap signal available for whether it read the order at
    # all, and a sheet that restated the canonical six when its book does
    # something else is worth looking at before a teacher does.
    material["teachingOrder"] = teaching_order(spec.get("moves") or [])
    material["bookMoves"] = spec.get("moves") or []
    echo = entry.get("teachingOrder")
    if isinstance(echo, list):
        meta = material.setdefault("_meta", {})
        meta["teachingOrderEcho"] = [str(x) for x in echo]

    # Topic 1 has no previous Explore to build a Refresher from — not "one is
    # optional here," NONE belongs here. `_carry_in()` already asks the model
    # for this; enforced again here so a model that writes a "cold open" recap
    # anyway (from general prior knowledge, or an invented previous lesson)
    # cannot land one in front of a teacher. A first period should not open
    # with a review of anything.
    if spec["index"] == 1:
        material["previousTopicRefresher"] = None
        watch = material.get("sectionWatch")
        if isinstance(watch, dict):
            watch["refresher"] = None
        timings = material.get("timings")
        if isinstance(timings, dict):
            timings.pop("refresher", None)

    # `concept` is asked for as {"points": [...]} like every other section, and
    # stored as a bare list, which is the pilot's contract.
    #
    # Asking for the uniform shape is not cosmetic. With five sections wrapped and
    # one bare, the model normalises toward the majority and closes the bare array
    # with a brace — `"concept": [ {...}, {...} },` — which is a hard JSON parse
    # failure that costs the whole window and every retry with it. Observed on the
    # first real run against the Class 3 textbook, three attempts in a row.
    concept = material.get("concept")
    if isinstance(concept, dict):
        points = concept.get("points")
        if not isinstance(points, list):
            points = next((v for v in concept.values() if isinstance(v, list)), None)
        if points is not None:
            material["concept"] = points

    activity = (selection or {}).get("activity") or {}
    challenge = material.get("challenge")
    if isinstance(challenge, list):
        challenge = {"points": challenge}
    if not isinstance(challenge, dict):
        challenge = {}

    selected_name = (activity.get("name") or "").strip()
    written_name = (challenge.get("activity") or "").strip()
    book_task = book_activity(spec.get("moves") or [])

    if book_task:
        # THE BOOK'S TASK IS THE CHALLENGE, so the library template's name is the
        # wrong title for it — "Clap Patterns" over page 18's match-sticks tells
        # the teacher to do two different things.
        #
        # ALWAYS THE CODE-DERIVED NAME, NOT A BACKSTOP FOR A BROKEN ONE. Three
        # prompt revisions and a shape-check backstop (empty / copied-from-
        # library / not `usable_challenge_name`) all still let bad names
        # through — "Where do birds live?" (a page heading), seventy words of
        # the textbook's own monologue, and "Discuss farmers' cultivation and
        # dependence" (technically well-formed, technically on-topic, and
        # still too generic to tell a teacher what the task actually is — it
        # even shares real words with the book's own task, so a word-overlap
        # check does not catch it either). A name the model invents cannot be
        # verified as specific enough short of another model call, so this
        # stops trying to verify it and uses `challenge_name_from(book_task)`
        # unconditionally instead — built directly from the book's own gist,
        # grounded by construction rather than by inspection. The model's own
        # name only survives when the gist itself yields nothing usable.
        derived = challenge_name_from(book_task)
        if derived:
            challenge["activity"] = derived
    elif selected_name and same_activity(written_name, selected_name):
        # Snap back to the exact selected name. The model reliably decorates it —
        # "Object View Sketch (drawing)", the category appended from the prompt's
        # own activity line — and left alone that decoration becomes a SECOND
        # activity in the telemetry, so the feedback loop computes per-activity
        # satisfaction rates over names that differ only by a parenthetical.
        challenge["activity"] = selected_name
    material["challenge"] = challenge

    planned_minutes = (plan or {}).get("minutes") or {}
    timings = material.get("timings")
    if not isinstance(timings, dict) or not timings:
        material["timings"] = dict(planned_minutes)

    # Explore's hard 5-minute ceiling (planning.py's EXPLORE_CEILING) frees up
    # time that was never handed to another section — surfaced here as its own
    # field so a teacher sees it as time they choose how to spend, not as a
    # bigger number quietly folded into Concept or Challenge.
    material["flexMinutes"] = (plan or {}).get("flexMinutes") or 0

    # Drop any figure id the topic's pages do not actually contain. An invented id
    # renders as a link to nothing, and "look at the picture on page 13" when
    # there is no such picture is a defect the teacher meets mid-lesson.
    known = {f["id"]: f for f in (spec.get("figures") or []) if f.get("id")}
    refs = [r for r in (material.get("figureRefs") or []) if isinstance(r, str)]
    kept = [r for r in refs if r in known]
    if len(kept) != len(refs):
        print(f"[prep_flow:generation] T{spec['index']} referenced "
              f"{len(refs) - len(kept)} figure id(s) not printed on its pages")
    material["figureRefs"] = kept
    material["figures"] = [{"id": r, "page": known[r].get("page"),
                            "caption": known[r].get("caption") or "", "path": known[r].get("path")}
                           for r in kept]

    material["_meta"] = {
        # The academic destination this sheet was WRITTEN FOR, stamped so that
        # `validation_flow.integrity` can compare it against the contract's own
        # target. Until this existed that check read an absent field and stayed
        # silent on every topic, which made target preservation a property
        # nothing actually enforced downstream of Node 1 — the exact hole an
        # adaptation that quietly changes what the lesson teaches would fall
        # through. Read from `spec["reasoning"]`, which `adapt._reasoning_slice`
        # fills from the contract, so the two can only disagree if something
        # rewrote the target in between. Which is the thing being detected.
        "masteryTarget": (spec.get("reasoning") or {}).get("masteryTarget") or "",
        "selectedActivity": activity.get("name"),
        "activitySource": activity.get("source", "library" if activity.get("id") else "invented"),
        "selectedContext": ((selection or {}).get("context") or {}).get("name"),
        "formats": (selection or {}).get("formats") or {},
        "plannedMinutes": planned_minutes,
        "plannedHook": (plan or {}).get("exploreHook") or {},
        "plannedBridge": (plan or {}).get("refresherBridge"),
        # What fed THIS sheet specifically — textbook pages, canonical library ids
        # used, any canonical entries this topic's own extraction seeded, and the
        # activity/context source. The chapter-wide story (which stages ran, in
        # what order, whether this run was a background task) lives one level up,
        # on the run itself (prep_flow_runs.provenance — see provenance.py).
        "provenance": provenance.topic_provenance(spec, plan, selection),
        # Same facts, reshaped per SECTION rather than per stage — this is what
        # render_topic() actually cites inline, next to each bullet, rather than
        # in a separate table. See provenance.section_citations().
        "sectionCitations": provenance.section_citations(
            spec, plan, selection,
            previous_handoff=handoff_of(previous_material) if previous_material else None),
    }
    return material


def _merge_sections(original, rewritten: dict, targets) -> dict:
    """Keep everything the diagnosis did not name — not just the other sections.

    A learner failure is diagnosed down to the sections that caused it, and
    regenerating a whole sheet from a two-line diagnosis throws away five good
    sections to fix one. So the rewrite is asked for the named sections only, and
    everything else is taken from the ORIGINAL.

    BUILT FROM THE ORIGINAL, NOT FROM THE REWRITE, and that direction is the
    whole correctness of this function. It used to start from `rewritten` and
    copy the untargeted SECTIONS back — which silently dropped every top-level
    field the narrowed rewrite had not been asked to write. `objective` is the
    one that bit: it is not a section, so nothing restored it, and
    `check_structure` fails a sheet without one. On the first real run this cost
    an entire repair round — three sheets were narrowly repaired, came back
    without an objective, and the next gate visit reported "objective is missing"
    as BLOCKING on exactly those three topics, spending round 2 rewriting them
    wholesale to put back a field nobody had asked to change.

    Starting from the original inverts that: every field survives by default, and
    only what the rewrite actually returned overwrites it. A field the model DID
    choose to rewrite still wins — a repair that changes the Challenge and
    updates `materials` to add scissors keeps the new list — so nothing that
    genuinely changed is lost either.

    No targets means a structural failure rather than a diagnosed one, and those
    genuinely do want the whole sheet, so the rewrite is returned untouched.
    """
    if not original or not targets:
        return rewritten
    from prep_flow.sections import _CONTAINERS

    named = [s for s in targets if s in _CONTAINERS]
    if not named:
        # Every target was a section name this pipeline does not have. Treated as
        # "no narrowing", which is what repair_node already decided upstream when
        # it sized the call — the two must agree or the sheet is merged against a
        # scope the prompt never used.
        return rewritten

    section_keys = {container for container, _ in _CONTAINERS.values()}
    merged = dict(original)
    # Copied rather than shared: `_meta['revision']` is stamped on the result by
    # the caller, and mutating the original's dict would rewrite the revision
    # number of the sheet this one is supposed to be superseding.
    merged["_meta"] = dict(original.get("_meta") or {})

    # Bookkeeping and any other top-level field the rewrite actually produced —
    # `index`, `topic`, `pageRange`, `teachingOrder` are set by
    # _normalise_material and are correct for this revision, and `objective` or
    # `materials` are kept when the model chose to restate them.
    for key, value in (rewritten or {}).items():
        if key in section_keys or key == "sectionWatch":
            continue
        merged[key] = value

    for section in named:
        key = _CONTAINERS[section][0]
        if key in rewritten:
            merged[key] = rewritten[key]

    # The teacher's own notes, per section, merged on the same rule as the
    # sections they describe.
    watch, fresh = original.get("sectionWatch"), (rewritten or {}).get("sectionWatch")
    if isinstance(watch, dict):
        merged["sectionWatch"] = ({**watch, **{s: fresh[s] for s in named if s in fresh}}
                                  if isinstance(fresh, dict) else dict(watch))
    return merged


def _window_tokens(count: int, include_visuals: bool) -> int:
    # ~2200 output tokens per six-section sheet, plus ~900 when the visuals block
    # is on, plus headroom for the JSON scaffolding. Sized generously because a
    # truncated window costs the whole window, and ai.py's retry then spends a
    # second call on the same overflow.
    #
    # Plus GENERATION_REASONING_HEADROOM when reasoning is on: those tokens are
    # spent BEFORE the six sections, out of the same budget, and a window sized
    # only for the prose risks the exact failure ai.py already has a named
    # error for — a budget that covered the thinking and nothing after it.
    # ~4200 per sheet, not ~2200 -- "detail" doubled from ~30 to ~60 words per
    # bullet (18 bullets/sheet: six sections, three bullets each), which is
    # most of a sheet's output. Headlines and JSON scaffolding stay the same
    # size, so this is not a flat doubling, but sized generously for the same
    # reason the original estimate was: a truncated window costs the whole
    # window.
    base = 2000 + count * (4200 + (900 if include_visuals else 0))
    return base + (GENERATION_REASONING_HEADROOM if GENERATION_REASONING else 0)


def _repair_tokens(section_count: int, include_visuals: bool) -> int:
    """The budget for a repair that only has to write SOME of the six sections.

    `_window_tokens` sizes a whole sheet, which is what a structural repair
    genuinely needs. A diagnosed repair does not: it names the sections that
    caused the failure, and `_merge_sections` takes every other section from the
    ORIGINAL no matter what came back. So the sheet-sized budget was paying for
    four sections that were discarded on arrival.

    Sized per section off the same ~4200-per-sheet figure. The scaffolding
    allowance comes down with it but not proportionally: a two-key JSON object
    needs a fraction of a six-key one's envelope, and `repair_surface.MAX_SECTIONS`
    caps a diagnosis at three sections so this never has to cover a near-whole
    sheet. Left at the window's full 2000 it swamped the saving — a two-section
    repair came out at 2733 of 4200, most of it envelope for keys that were not
    being written.

    Still deliberately loose, because the asymmetry has not changed: a truncated
    repair costs the whole call and ai.py's retry then spends a second one on the
    same overflow, while an over-generous budget costs nothing at all. `max_tokens`
    is a ceiling, not a spend.
    """
    per_section = (4200 + (900 if include_visuals else 0)) / len(SECTION_ORDER)
    base = 900 + int(per_section * max(1, section_count))
    return base + (GENERATION_REASONING_HEADROOM if GENERATION_REASONING else 0)


def _narrowing_note(sections: list) -> str:
    """Tell a diagnosed repair to write only what will actually be kept.

    Without this the prompt asks for all six sections and the merge throws four
    away — the model spends its response on prose nobody reads, and the sections
    it was actually asked to fix get a fraction of its attention.

    The instruction has to be explicit about the OTHER sections too. Told only
    "write these two", a model reasonably assumes the rest are being deleted and
    starts compensating: folding the Explore's job into the Concept, or opening
    the Level Set with a recap of a Refresher it thinks is gone. Saying the rest
    survive untouched is what keeps the two it writes in their own lane.
    """
    if not sections:
        return ""
    named = ", ".join(sections)
    return (
        "\n\nWRITE ONLY THESE SECTIONS: " + named + "\n"
        "Every other section of this sheet is already correct and is being kept "
        "exactly as it is — it is not being deleted and you are not replacing it, "
        "so do not write it, do not summarise it, and do not move its job into "
        "the sections you are writing. Return a `materials` entry containing only "
        "the section key(s) above.\n"
        "The sections you write must still fit the sheet they are going back "
        "into: same topic, same anchor, same activity, and the same six-section "
        "flow around them."
    )


async def generation_node(state: dict) -> dict:
    """Generate ONE window of consecutive topics, then advance the cursor."""
    topics = state.get("topics") or []
    config = state.get("config") or {}
    cursor = int(state.get("cursor") or 0)
    window_size = max(1, int(config.get("window_size", 3)))
    include_visuals = bool(config.get("include_visuals", False))

    window = topics[cursor:cursor + window_size]
    if not window:
        return {"cursor": cursor}

    plans = state.get("plans") or {}
    selections = state.get("selections") or {}
    settings = state.get("teacher_settings") or {}
    first_index, last_index = window[0]["index"], window[-1]["index"]

    blocks = "\n\n".join(
        _topic_block(
            {**spec, "chapter_title": state.get("chapter_title"),
             "chapter_number": state.get("chapter_number")},
            plans.get(spec["index"]) or {},
            selections.get(spec["index"]) or {},
            context_plan=state.get("context_plan"))
        for spec in window
    )

    prompt = _GENERATION_PROMPT.format(
        first=first_index, last=last_index, total=len(topics), count=len(window),
        chaining=_chaining_note(len(window), first_index),
        chapter_title=state.get("chapter_title") or "(untitled)",
        grade=state.get("grade"), subject=state.get("subject"),
        arc=state.get("chapter_arc") or state.get("sequencing_note") or "(none recorded)",
        engagement=state.get("engagement_guidance") or "",
        contract=section_contract(),
        adaptive_block=adaptive_directive_block(state.get("adaptive_state") or {}),
        reasoning_block=chapter_block(state.get("reasoning") or {}),
        carry_in=_carry_in(state, first_index),
        topic_blocks=blocks,
        bullet=_BULLET, bullets=BULLETS_PER_SECTION,
        duration=settings.get("duration", 45),
        visuals=_visuals_shape() if include_visuals else "",
    )

    # Printed BEFORE the call, not after. This is the single longest-running
    # call in the pipeline — a window of three six-section sheets is a minute
    # or more of real generation — and until now nothing was written until it
    # FINISHED, so a window in flight and a hung process looked exactly alike
    # from the terminal.
    budget = _window_tokens(len(window), include_visuals)
    print(f"[pipeline] Generation: writing T{first_index}-T{last_index} "
          f"({len(window)} topic(s), {len(prompt)} char prompt, {budget} token budget, "
          f"model={GENERATION_MODEL or 'default'}) — this is the slow one, waiting...")
    try:
        data = await call_json(
            prompt, label=f"generation[T{first_index}-T{last_index}]",
            required=("materials",), temperature=0.6,
            max_tokens=budget,
            model=GENERATION_MODEL, timeout_s=GENERATION_TIMEOUT_S,
            reasoning=_reasoning_option(),
        )
    except Exception as exc:
        # Skip the window rather than failing the run. Validation will report
        # every missing topic, and the repair pass gets a second attempt at them
        # one at a time — which is often what a window that overflowed needs.
        return {
            "cursor": cursor + len(window),
            "errors": [f"generation: topics T{first_index}-T{last_index}: {exc}"],
        }

    raw = [m for m in (data.get("materials") or []) if isinstance(m, dict)]
    by_index: dict[int, dict] = {}
    for entry in raw:
        try:
            by_index[int(entry.get("index"))] = entry
        except (TypeError, ValueError):
            continue

    materials: dict[int, dict] = {}
    produced: list[int] = []
    missing: list[int] = []
    for position, spec in enumerate(window):
        entry = by_index.get(spec["index"]) or (raw[position] if position < len(raw) else None)
        if entry is None:
            missing.append(spec["index"])
            continue
        # The previous topic's material — from earlier in THIS window if it was
        # just generated a few lines up, else from an earlier window already in
        # state. Either way, this is only for the Refresher's citation text; the
        # actual prose already gets the same previous Explore via _carry_in().
        previous_material = (materials.get(spec["index"] - 1)
                             or (state.get("materials") or {}).get(spec["index"] - 1))
        materials[spec["index"]] = _normalise_material(
            entry, spec, plans.get(spec["index"]) or {}, selections.get(spec["index"]) or {},
            previous_material=previous_material)
        produced.append(spec["index"])

    return {
        "materials": materials,
        "generated_order": produced,
        "cursor": cursor + len(window),
        "errors": ([f"generation: no material returned for "
                    f"{', '.join(f'T{i}' for i in missing)}"] if missing else []),
        "metrics": {"windows_generated": (cursor // window_size) + 1},
    }


def _sheet_digest(material) -> str:
    """The rejected sheet as the repair investigator needs to read it.

    Section by section, prose only — no `_meta`, no timings, no bookMoves. The
    agent is checking claims against pages, and every token of bookkeeping it
    reads is a token not spent reading the book.
    """
    if not isinstance(material, dict):
        return "(the sheet is not available)"
    parts = []
    for section in SECTION_ORDER:
        text = section_text(material, section)
        if text:
            parts.append(f"{SECTION_LABELS[section]}: {text}")
    return "\n\n".join(parts) if parts else "(the sheet is empty)"


async def repair_node(state: dict) -> dict:
    """Regenerate the topics the gates rejected, one at a time.

    One at a time on purpose: a topic in the repair list is there because
    something about it was hard, and the most common cause of a whole window
    failing is the window itself — three sheets of detail overflowing one
    response. Alone, with its findings quoted back at it, the same prompt usually
    succeeds.

    AND ONLY THE SECTIONS THAT WILL BE KEPT. A diagnosed repair names the
    sections that caused the failure, and `_merge_sections` takes every other
    section from the original regardless of what came back — so asking for a
    whole sheet meant paying for six sections, discarding four, and giving the
    two that mattered a sixth of the response each. It now asks for what it will
    keep, at a budget sized to that (`_repair_tokens`), which is roughly a third
    of a sheet on a typical two-section diagnosis.

    A structural failure still gets the whole sheet, because it has no narrower
    surface: `repair_sections` is empty for those, deliberately, and that empty
    list is what distinguishes the two cases here.

    Which also makes batching thinkable for the first time — three two-section
    repairs are about one sheet of detail, the unit the paragraph above is
    actually worried about. Still not done: the saving is call count, the risk is
    the failure mode this function exists to escape, and the two want measuring
    against each other on a real chapter rather than assuming.
    """
    targets = sorted(set(state.get("repair_targets") or []))
    if not targets:
        return {}

    topics = {t["index"]: t for t in (state.get("topics") or [])}
    plans = state.get("plans") or {}
    selections = state.get("selections") or {}
    issues = state.get("issues") or {}
    settings = state.get("teacher_settings") or {}
    config = state.get("config") or {}
    include_visuals = bool(config.get("include_visuals", False))
    round_number = int(state.get("repair_round") or 0) + 1

    repaired: dict[int, dict] = {}
    history: dict[int, list[dict]] = {}
    errors: list[str] = []
    scoped_count = 0

    for index in targets:
        spec = topics.get(index)
        if not spec:
            continue
        findings = [
            f"- [{i.get('severity', 'blocking')}] {i.get('section') or 'sheet'}: {i.get('message')}"
            for i in (issues.get(index) or [])
        ]
        # The context plan travels into a REPAIR too. A rewrite that dropped the
        # adaptations would silently undo Node 2 on exactly the sheets that
        # needed the most work — and the adoption check in Node 3 would then
        # report the contextual layer as ignored, on a sheet that had it and
        # lost it.
        block = _topic_block(
            {**spec, "chapter_title": state.get("chapter_title"),
             "chapter_number": state.get("chapter_number")},
            plans.get(index) or {}, selections.get(index) or {},
            context_plan=state.get("context_plan"),
        )

        prompt = _GENERATION_PROMPT.format(
            first=index, last=index, total=len(topics), count=1,
            chaining=_chaining_note(1, index),
            chapter_title=state.get("chapter_title") or "(untitled)",
            grade=state.get("grade"), subject=state.get("subject"),
            arc=state.get("chapter_arc") or "(none recorded)",
            engagement=state.get("engagement_guidance") or "",
            contract=section_contract(),
            adaptive_block=adaptive_directive_block(state.get("adaptive_state") or {}),
            reasoning_block=chapter_block(state.get("reasoning") or {}),
            carry_in=_carry_in(state, index),
            topic_blocks=block,
            bullet=_BULLET, bullets=BULLETS_PER_SECTION,
            duration=settings.get("duration", 45),
            visuals=_visuals_shape() if include_visuals else "",
        ) + (
            "\n\nTHIS TOPIC WAS ALREADY GENERATED ONCE AND REJECTED. The review found:\n"
            + "\n".join(findings)
            + "\n\nFix every one of these. Keep whatever was already right — a rewrite "
              "that fixes the Refresher and breaks the Concept is not progress."
            if findings else ""
        ) + diagnosis_block((state.get("learner") or {}).get(index) or {})

        # WHAT THE PAGE ACTUALLY SAYS, when an agent was sent to look.
        #
        # Without this, a grounding failure is repaired blind: the rewrite is
        # told the Concept states a number that is not on these pages, and then
        # guesses a second number with no more access to the page than it had
        # the first time. `deep:repair` reads the pages and reports what is
        # printed there, and only then does this prompt write the fix.
        #
        # Best-effort by contract. The agent failing costs this topic its
        # evidence, not its rewrite — repair worked without it before and still
        # does.
        if config.get("deep_agents") and findings:
            try:
                from deep_agents.bridge import gather_repair_evidence
                evidence = await gather_repair_evidence(
                    state, index=index,
                    topic=str(spec.get("topic") or f"T{index}"),
                    pages=f"{spec.get('page_start')}-{spec.get('page_end')}",
                    findings="\n".join(findings),
                    sheet=_sheet_digest((state.get("materials") or {}).get(index)),
                )
                if evidence:
                    prompt += (
                        "\n\nCHECKED AGAINST THE PRINTED PAGES. A reviewer opened the book "
                        "and read what is actually there:\n" + evidence +
                        "\n\nThese readings are authoritative over anything the rejected "
                        "sheet said. Where one says NOT FOUND, remove the claim — do not "
                        "reword it.")
            except Exception as exc:                      # noqa: BLE001
                print(f"[repair] T{index}: page evidence unavailable "
                      f"(rewriting without it): {exc}")

        # The sections a diagnosis actually named, or empty for a structural
        # failure — which is the signal that the whole sheet is in scope. Read
        # once, here, because it decides both what the prompt asks for and what
        # the response is allowed to cost.
        scoped = [s for s in ((state.get("repair_sections") or {}).get(index) or [])
                  if s in SECTION_ORDER]
        prompt += _narrowing_note(scoped)

        try:
            data = await call_json(
                prompt, label=f"repair[T{index}#{round_number}]",
                required=("materials",), temperature=0.55,
                max_tokens=(_repair_tokens(len(scoped), include_visuals) if scoped
                            else _window_tokens(1, include_visuals)),
                model=GENERATION_MODEL, timeout_s=GENERATION_TIMEOUT_S,
                reasoning=_reasoning_option(),
            )
        except Exception as exc:
            errors.append(f"repair: T{index}: {exc}")
            continue

        entries = [m for m in (data.get("materials") or []) if isinstance(m, dict)]
        if not entries:
            errors.append(f"repair: T{index}: no material returned")
            continue
        material = _normalise_material(
            entries[0], spec, plans.get(index) or {}, selections.get(index) or {},
            previous_material=(state.get("materials") or {}).get(index - 1))
        # The same `scoped` list the prompt was narrowed by, so what the model
        # was asked for and what is kept can never disagree. Passing the raw
        # state value again would reintroduce exactly that gap the moment one of
        # them learns to filter and the other does not.
        material = _merge_sections(
            (state.get("materials") or {}).get(index), material, scoped)
        material["_meta"]["revision"] = round_number
        if scoped:
            scoped_count += 1
        repaired[index] = material
        # Both sides of the rewrite. The one being replaced is appended every
        # round and deduplicated by the reducer on its revision, which is
        # cheaper than tracking whether this is the first repair of this topic
        # and cannot get that answer wrong.
        previous = (state.get("materials") or {}).get(index)
        history[index] = ([] if previous is None else [{
            "revision": int((previous.get("_meta") or {}).get("revision", 0)),
            "material": previous,
        }]) + [{"revision": round_number, "material": material}]

    return {
        "materials": repaired,
        "material_history": history,
        "repair_round": round_number,
        "errors": errors,
        "metrics": {f"repair_round_{round_number}_fixed": len(repaired),
                    # How much of this round was narrow. A round that is all
                    # whole-sheet rewrites costs six times what an all-diagnosed
                    # round does, and the two are otherwise indistinguishable in
                    # a bill.
                    f"repair_round_{round_number}_scoped": scoped_count},
    }
