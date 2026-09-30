"""Prep Material Planning Agent — decides what each topic needs, and what each
topic OWES the next one.

The consistency problem in a 30–40 topic batch is not that individual sheets are
bad; it is that they are each locally reasonable and collectively incoherent —
the same activity four times, a term used in T6 and defined in T19, thirty
unrelated Explore scenes. Fixing that after generation means regenerating
everything, so it is decided here, before a single bullet is written.

What this node does NOT decide, any more: what a topic means, what a child needs
before it, which objects make it concrete, and what it must leave behind. The
Curriculum Reasoning Agent settles all four once for the whole chapter and this
node plans against them — so `assumes` and `gained` arrive here as given, rather
than being re-invented forty times by a prompt that is also doing arithmetic on
minutes. The misconception this period should expect arrives the same way, which
is why there is no longer a `watchOut` field for the planner to guess at.

Three artefacts come out of this node, and each one exists to be enforced later:

  * the handoff chain. For every topic, `exploreHook` says what its Explore must
    end on, and `refresherBridge` says what its Refresher must bring back. The
    planner writes both halves of each seam, so Explore(n) and Refresher(n+1) are
    agreeing to the same thing rather than hoping to.
  * the vocabulary ledger — which term is introduced by which topic. Validation
    uses it to catch a topic leaning on a word the class has not met yet.
  * a per-topic emphasis and minute budget, so a chapter has a shape: some
    periods are heavy on Challenge, some on Concept, and none of them pretend
    all six sections matter equally every single day.

Planned in windows rather than one call: a plan for topic 30 written while the
model can still see topics 1–29 is better than one written from a summary, but
forty topics of planning in a single response truncates, and a truncated plan
loses the end of the chapter silently.
"""
import json

from ..bands import band_block
from ..llm import call_json
from ..sections import SECTION_ORDER
from ..state import ChapterState
from .reasoning import anchor_groups

PLAN_WINDOW = 12

_PLANNING_PROMPT = """You are the lead planner for a chapter's worth of lesson prep material. You are
NOT writing the lessons — you are deciding what each lesson must do, so that
{total} separate prep sheets read as one coherent chapter.

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
CHAPTER ARC (from sequencing): {arc_note}
PERIOD LENGTH: {duration} minutes
{band_block}

EVERY prep sheet has the same six sections, in this order, and you plan all six:
{section_brief}

THE SEAM THAT MATTERS MOST — how one lesson becomes the next:
    Explore of topic N  ──>  Refresher of topic N+1
The Refresher is not a summary of the previous topic. It is the class picking
back up the exact scene, object and unanswered question that the previous
Explore left them holding. You write BOTH ends of every seam: `exploreHook` for
topic N and `refresherBridge` for topic N+1 must describe the SAME moment, from
the two sides.
{carry_in}
THE OTHER CHAIN, which is not the same one. The seam above carries the EXPERIENCE
from period to period — the scene the class was in. Underneath it runs the
KNOWLEDGE: what each topic leaves the child understanding, and what the next one
stands on. That chain has already been worked out for this whole chapter and is
printed under each topic below as `assumes` / `gained`. It is not yours to
redesign. Plan the period that gets the class from one to the other.
{adaptive_block}{reasoning_block}{class_block}
TOPICS TO PLAN NOW (topics {first}-{last} of {total}):
{topics}

{already_introduced}

Return ONLY valid JSON, no markdown fences:
{{
  {arc_field}"plans": [
    {{
      "index": {first},
      "focus": "10-18 words: the one thing this period must achieve",
      "refresherBridge": {bridge_hint},
      "exploreHook": {{
        "scene": "the concrete situation the Explore ends inside — a place, a moment",
        "object": "the one thing students are holding, counting, or looking at",
        "question": "the open question they leave with, in the words a child would use",
        "answeredNextLesson": "how the NEXT topic settles it — one clause"
      }},
      "emphasis": {{"heaviest": "one of {section_names}", "lightest": "one of {section_names}"}},
      "minutes": {{"refresher": 3, "concept": 6, "realLife": 4, "challenge": 8, "levelSet": 4, "explore": 5}},
      "newVocabulary": ["terms THIS topic introduces for the first time — [] if none"],
      "reusesFrom": [{{"index": 3, "what": "the skill or term carried forward"}}],
      "difficultyStep": "same | up | down — relative to the previous topic"
    }}
  ]
}}

Rules:
- One plan object per topic listed above, in the same order, with the same index.
- "minutes" must sum to {duration}. Give the heaviest section the most; a
  section can be as low as 2 minutes but never 0 — all six always run.
- Keep "explore" at 5 minutes or fewer — it is homework-style noticing, not
  classroom time, and anything you give it above 5 is cut and handed to the
  teacher as flex time instead of staying in the lesson you're planning.
- Vary "emphasis" across the chapter. If four consecutive topics are heaviest on
  Concept, the chapter is a lecture series.
- Never put a term in "newVocabulary" that an earlier topic already introduced.
- "exploreHook" must be answerable by the NEXT topic in the list. The last topic
  in the whole chapter still gets one — it points at the next chapter.
- The Explore you plan must be the experience that DELIVERS this topic's `gained`.
  A hook that is merely charming and leaves the next topic's `assumes` unmet has
  broken the chapter, however well it reads.
- Give the most minutes to the section that carries the HARDEST MOVE listed for
  that topic. A period spending eight minutes on the easy half of the thinking
  path and three on the jump has the budget backwards.
- Plan the teaching, not the content. The Concept section's facts come from the
  textbook and are not yours to choose; everything about how the period feels is.
- "difficultyStep": "up" means a bigger version of the same thinking, never a
  jump to a kind of thinking the band above cannot do.
"""


def _section_brief() -> str:
    from ..sections import SECTION_POLICY
    return "\n".join(
        f"  {i}. {p['label']} — {p['source'].replace('_', ' ')}; "
        f"{'grounded, invent nothing' if p['mode'] == 'grounded' else ('half grounded' if p['mode'] == 'mixed' else 'your creativity, fully')}"
        for i, (_, p) in enumerate(SECTION_POLICY.items(), start=1)
    )


def _topic_lines(topics: list[dict]) -> str:
    lines = []
    for spec in topics:
        knowledge = spec.get("knowledge") or {}
        names = spec.get("canonical_names") or {}
        reasoning = spec.get("reasoning") or {}
        entry = (
            f"T{spec['index']}. {spec['topic']}"
            + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
            + f"\n    pages {spec.get('page_start')}-{spec.get('page_end')}"
            + f" | bloom: {knowledge.get('bloom_level') or '?'}"
            + f" | difficulty: {knowledge.get('difficulty') or '?'}"
            + f"\n    concepts: {', '.join((names.get('concepts') or knowledge.get('concepts') or [])[:6]) or '(none)'}"
            + f"\n    competencies: {', '.join((names.get('competencies') or knowledge.get('competencies') or [])[:6]) or '(none)'}"
            + f"\n    textbook's own examples: {', '.join((knowledge.get('contexts') or [])[:6]) or '(none)'}"
            + f"\n    activity templates available: {len(spec.get('activities') or [])}"
        )
        # The knowledge chain, printed under the topic it belongs to rather than
        # in a block of its own: the planner reads one topic at a time, and a
        # chain it has to cross-reference is a chain it ignores.
        if reasoning.get("gained") or reasoning.get("assumes"):
            entry += (
                f"\n    assumes  (already understood): {reasoning.get('assumes') or '— the chapter prerequisites above'}"
                f"\n    gained   (must leave behind):  {reasoning.get('gained') or '(not derived)'}"
                f"\n    bridges to next topic:         {reasoning.get('bridgesTo') or '(not derived)'}"
            )
        if reasoning.get("misconceptions"):
            entry += ("\n    likely misconception here:     "
                      + "; ".join(reasoning["misconceptions"][:2]))
        # The experience plan, printed under the topic it belongs to. The planner
        # is deciding where the minutes go, and the hardest move is the only thing
        # in this block that tells it where they should go.
        experience = spec.get("experience") or {}
        if experience.get("trajectory"):
            entry += ("\n    thinking path to walk:         "
                      + " -> ".join(experience["trajectory"]))
        if experience.get("conceptualJump"):
            entry += f"\n    hardest move, teach thickest:  {experience['conceptualJump']}"
        if experience.get("anchor"):
            entry += f"\n    object chosen for it:          {experience['anchor']}"
        lines.append(entry)
    return "\n".join(lines)


def reasoning_block(reasoning: dict) -> str:
    """The chapter-wide half of the reasoning, for the planning prompt.

    Only the parts a planner acts on. The chain is not repeated here — it is
    printed per topic in `_topic_lines`, where it is read.
    """
    if not reasoning:
        return ""
    lines: list[str] = []
    if reasoning.get("prerequisites"):
        lines.append("  - Class arrives with: " + "; ".join(reasoning["prerequisites"]))
    for group in anchor_groups(reasoning):
        label = group.get("strand") or "main"
        lines.append(f"  - Objects for the '{label}' thread: "
                     + ", ".join(group["objects"]))
    untagged = [m["belief"] for m in (reasoning.get("misconceptions") or [])
                if not m.get("topics")]
    if untagged:
        lines.append("  - Chapter-wide misconceptions: " + "; ".join(untagged[:3]))
    if not lines:
        return ""
    return (
        "\nWHAT THIS CHAPTER RESTS ON, worked out once for the whole chapter before "
        "any planning. Treat it as established:\n" + "\n".join(lines) + "\n"
    )


def adaptive_directive_block(adaptive: dict) -> str:
    """The feedback loop's output, rendered for a prompt.

    Kept short on purpose. A directive list that grows every optimization cycle
    ends up longer than the instructions it is qualifying, and then it is the
    prompt.
    """
    if not adaptive:
        return ""
    lines: list[str] = []
    for section in SECTION_ORDER:
        directive = (adaptive.get("sectionDirectives") or {}).get(section)
        if directive:
            lines.append(f"  - {section}: {directive}")
    for key, label in (("avoid", "Avoid"), ("prefer", "Prefer")):
        items = [i for i in (adaptive.get(key) or []) if isinstance(i, str)][:6]
        if items:
            lines.append(f"  - {label}: {'; '.join(items)}")
    if adaptive.get("difficultyShift") in ("up", "down"):
        lines.append(f"  - Overall difficulty: shift {adaptive['difficultyShift']}")
    if not lines:
        return ""
    return (
        "\nWHAT TEACHER FEEDBACK HAS ALREADY ESTABLISHED for this grade and subject "
        f"(adaptive state v{adaptive.get('_version', '?')}, from "
        f"{adaptive.get('_sampleSize', 0)} responses). These are findings, not "
        "suggestions — plan around them:\n" + "\n".join(lines) + "\n"
    )


def _class_block(cohort: dict) -> str:
    if not cohort:
        return ""
    bits = []
    if cohort.get("classInterests"):
        bits.append(f"  - Class interests: {', '.join(cohort['classInterests'][:5])}")
    if cohort.get("weakTopics"):
        bits.append(f"  - Weak on: {', '.join(cohort['weakTopics'][:3])}")
    if cohort.get("personalization"):
        bits.append(f"  - Teaching profile: {cohort['personalization']}")
    if cohort.get("classProfile"):
        bits.append(f"  - Class feedback profile: {cohort['classProfile']}")
    return "\nTHIS CLASS:\n" + "\n".join(bits) + "\n" if bits else ""


EXPLORE_CEILING = 5


def _normalise_minutes(raw, duration: int) -> tuple[dict, int]:
    """Minutes that sum to the period, with every section present, plus
    whatever's left over as flex time. Returns (minutes, flex_minutes).

    Nudged rather than rejected: a plan whose timings are 2 minutes off is not
    worth an LLM retry, and a section silently dropped to zero is — all six
    sections always run, which is the one thing about this contract that never
    bends.

    EXPLORE IS HARD-CAPPED AT {EXPLORE_CEILING} MINUTES, not proportionally
    capped like the other five. Explore's own content is "notice this on your
    way home" — it structurally cannot use more than a handful of classroom
    minutes no matter how the model phrases it, so this is a literal ceiling,
    not a ratio. Whatever the model wanted to give it beyond that ceiling does
    NOT get redistributed into the other five sections automatically — it
    becomes explicit FLEX TIME, returned separately, for the teacher to spend
    however their own class needs on the day (more practice, a slower pace, a
    question that came up) rather than the pipeline silently deciding Concept
    or Challenge gets it. (Superseded the previous "cap at 1.75x default share
    and redistribute the surplus among all six sections" scheme, which fixed
    Explore eating 48% of the period but still let it run to 13-14 minutes and
    still hid the freed time inside another section's total instead of naming
    it.)

    The other five sections still cap against SECTION_POLICY'S OWN DEFAULT
    SHAPE, same as before: each is capped at roughly 1.75x its own default's
    share of the period (now measured against the period minus Explore and
    flex), and anything trimmed off is handed to the sections that were AT OR
    BELOW their default share, in proportion to their own default weight.
    """
    from ..sections import SECTION_POLICY
    defaults = {s: p["minutes"] for s, p in SECTION_POLICY.items()}
    non_explore = [s for s in SECTION_ORDER if s != "explore"]
    default_total_non_explore = sum(defaults[s] for s in non_explore) or 1

    values: dict[str, int] = {}
    for section in SECTION_ORDER:
        try:
            values[section] = max(2, int((raw or {}).get(section, defaults[section])))
        except (TypeError, ValueError):
            values[section] = defaults[section]

    total = sum(values.values())
    if total != duration and total > 0:
        # Scale first, same as before -- this is what turns "whatever the
        # model chose" into minutes that actually sum to the real period.
        scaled = {s: max(2, round(v * duration / total)) for s, v in values.items()}
        drift = duration - sum(scaled.values())
        if drift:
            biggest = max(scaled, key=lambda s: scaled[s])
            scaled[biggest] = max(2, scaled[biggest] + drift)
        values = scaled

    explore_natural = values["explore"]
    explore_minutes = min(explore_natural, EXPLORE_CEILING)
    flex_minutes = explore_natural - explore_minutes
    remaining_duration = duration - explore_minutes - flex_minutes

    # THEN cap the remaining five against their own default shape, in the
    # same units (this period's minutes minus Explore and flex), so the cap
    # scales with `duration` instead of being a fixed number that made sense
    # for 30 minutes and not 45 or 60.
    capped: dict[str, int] = {}
    surplus = 0
    below_default_weight = 0
    for section in non_explore:
        minutes = values[section]
        default_share = defaults[section] / default_total_non_explore * remaining_duration
        ceiling = max(2, round(default_share * 1.75))
        if minutes > ceiling:
            surplus += minutes - ceiling
            capped[section] = ceiling
        else:
            capped[section] = minutes
            if minutes <= default_share:
                below_default_weight += defaults[section]

    if surplus and below_default_weight:
        # Give the trimmed time back to sections still at or under their own
        # default share, weighted by that default -- Concept and Challenge
        # (the heavier defaults) get more of it back than Refresher does.
        distributed = 0
        for section, minutes in capped.items():
            default_share = defaults[section] / default_total_non_explore * remaining_duration
            if minutes <= default_share:
                add = round(surplus * defaults[section] / below_default_weight)
                capped[section] += add
                distributed += add
        drift = surplus - distributed
        if drift:
            biggest = max(capped, key=lambda s: capped[s])
            capped[biggest] += drift
    elif surplus:
        # Nothing was under its default share to give the surplus to --
        # everyone was already at or above it. Hand it to the single
        # heaviest-by-default section instead of dropping it.
        heaviest_default = max(non_explore, key=lambda s: defaults[s])
        capped[heaviest_default] += surplus

    capped["explore"] = explore_minutes
    return capped, flex_minutes


def _normalise_plan(entry: dict, spec: dict, duration: int, is_first: bool) -> dict:
    hook = entry.get("exploreHook")
    if isinstance(hook, str):
        hook = {"question": hook}
    if not isinstance(hook, dict):
        hook = {}

    emphasis = entry.get("emphasis") if isinstance(entry.get("emphasis"), dict) else {}
    heaviest = emphasis.get("heaviest") if emphasis.get("heaviest") in SECTION_ORDER else None
    lightest = emphasis.get("lightest") if emphasis.get("lightest") in SECTION_ORDER else None

    bridge = entry.get("refresherBridge")
    if isinstance(bridge, dict):
        bridge = " ".join(str(v) for v in bridge.values() if v)
    bridge = (bridge or "").strip() or None

    minutes, flex_minutes = _normalise_minutes(entry.get("minutes"), duration)

    return {
        "index": spec["index"],
        "topic": spec["topic"],
        "focus": (entry.get("focus") or "").strip(),
        # T1 has no previous Explore, so a bridge here would be an instruction to
        # invent a memory the class does not have.
        "refresherBridge": None if is_first else bridge,
        "exploreHook": {
            "scene": (hook.get("scene") or "").strip(),
            "object": (hook.get("object") or "").strip(),
            "question": (hook.get("question") or "").strip(),
            "answeredNextLesson": (hook.get("answeredNextLesson") or "").strip(),
        },
        "emphasis": {"heaviest": heaviest, "lightest": lightest},
        "minutes": minutes,
        # Time freed by Explore's hard 5-minute ceiling, kept OUT of `minutes`
        # (never shown to the generation model as a section to write content
        # for) and surfaced separately so a teacher sees it as their own
        # discretionary time, not folded silently into another section's total.
        "flexMinutes": flex_minutes,
        "newVocabulary": [v.strip() for v in (entry.get("newVocabulary") or [])
                          if isinstance(v, str) and v.strip()][:8],
        "reusesFrom": [r for r in (entry.get("reusesFrom") or []) if isinstance(r, dict)][:4],
        "difficultyStep": (entry.get("difficultyStep") or "same").strip().lower(),
    }


async def planning_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["planning: no topics to plan"]}

    settings = state.get("teacher_settings") or {}
    duration = int(settings.get("duration", 45))
    total = len(topics)
    section_names = " | ".join(SECTION_ORDER)

    plans: dict[int, dict] = {}
    arc = state.get("sequencing_note") or ""
    introduced: list[str] = []
    errors: list[str] = []

    for offset in range(0, total, PLAN_WINDOW):
        window = topics[offset:offset + PLAN_WINDOW]
        first_index, last_index = window[0]["index"], window[-1]["index"]
        is_first_window = offset == 0

        previous_hook = plans.get(first_index - 1, {}).get("exploreHook") if not is_first_window else None
        carry_in = (
            "\nTHE PREVIOUS WINDOW ENDED HERE. Topic "
            f"{first_index}'s `refresherBridge` must pick this up:\n"
            f"{json.dumps(previous_hook, ensure_ascii=False)}\n"
            if previous_hook else ""
        )

        prompt = _PLANNING_PROMPT.format(
            total=total,
            chapter_title=state.get("chapter_title") or "(untitled)",
            grade=state.get("grade"), subject=state.get("subject"),
            arc_note=arc or "(none recorded)",
            band_block=band_block(state.get("grade")),
            duration=duration,
            section_brief=_section_brief(),
            carry_in=carry_in,
            adaptive_block=adaptive_directive_block(state.get("adaptive_state") or {}),
            reasoning_block=reasoning_block(state.get("reasoning") or {}),
            class_block=_class_block(state.get("class_context") or {}),
            first=first_index, last=last_index,
            topics=_topic_lines(window),
            already_introduced=(
                f"TERMS ALREADY INTRODUCED by topics 1-{first_index - 1}, so do NOT list "
                f"them as new again: {', '.join(introduced[:60]) or '(none)'}"
                if introduced else
                "No terms have been introduced yet — this is the start of the chapter."
            ),
            arc_field=('"arc": "2-3 sentences: the chapter\'s through-line, '
                       'as the teacher would describe it",\n  ' if is_first_window else ""),
            bridge_hint=('null   // topic 1 has no previous Explore to build on'
                         if is_first_window and first_index == 1 else
                         '"what the class brings back from the previous topic\'s Explore — '
                         'name its scene and its unanswered question"'),
            section_names=section_names,
        )

        try:
            data = await call_json(
                prompt, label=f"planning[{first_index}-{last_index}]",
                required=("plans",), temperature=0.4, max_tokens=8000,
            )
        except Exception as exc:
            errors.append(f"planning: topics {first_index}-{last_index}: {exc}")
            for spec in window:
                plans[spec["index"]] = _normalise_plan({}, spec, duration, spec["index"] == 1)
            continue

        if is_first_window and (data.get("arc") or "").strip():
            arc = data["arc"].strip()

        by_index = {}
        for entry in data.get("plans") or []:
            if isinstance(entry, dict):
                try:
                    by_index[int(entry.get("index"))] = entry
                except (TypeError, ValueError):
                    continue
        # Positional fallback: a model that renumbered the window still returned
        # the plans in order, and matching on position recovers them.
        ordered = [e for e in (data.get("plans") or []) if isinstance(e, dict)]
        for position, spec in enumerate(window):
            entry = by_index.get(spec["index"]) or (
                ordered[position] if position < len(ordered) else {})
            plan = _normalise_plan(entry, spec, duration, spec["index"] == 1)
            plans[spec["index"]] = plan
            introduced.extend(plan["newVocabulary"])

    emphasis_spread = len({p["emphasis"]["heaviest"] for p in plans.values() if p["emphasis"]["heaviest"]})
    return {
        "plans": plans,
        "chapter_arc": arc,
        "errors": errors,
        "metrics": {
            "plans_written": len(plans),
            "vocabulary_introduced": len(introduced),
            "emphasis_variety": emphasis_spread,
        },
    }


def vocabulary_ledger(plans: dict) -> dict[str, int]:
    """term (lowercased) -> the topic index that introduces it. The validator's
    check for a lesson leaning on a word the class has not met reads this."""
    ledger: dict[str, int] = {}
    for index in sorted(plans):
        for term in plans[index].get("newVocabulary") or []:
            ledger.setdefault(term.strip().lower(), index)
    return ledger
