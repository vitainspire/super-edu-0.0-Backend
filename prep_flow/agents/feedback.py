"""The feedback loop: telemetry -> pattern matcher -> optimization -> back into
Context Assembly.

    teacher's form (YES / SOMEWHAT / NO)
        -> telemetry store (~40 days)
        -> pattern matcher      (deterministic: counting, thresholds, trend)
        -> optimization agent   (LLM: patterns -> a generation state)
        -> adaptive generation state, versioned
        -> read by the NEXT chapter's Context Assembly

Two decisions shape this module more than anything else.

**The pattern matcher does not use a model.** "Explore was rated 'no' 11 times
out of 14 in the last three weeks, and that is worse than the 4-of-30 before it"
is arithmetic. Asking a model to find patterns in ratings invites it to find
patterns that are not there, and this output steers every subsequent chapter.

**The optimizer is aggressively bounded.** It writes directives that go into
every future prompt, and its failure mode is not being wrong once — it is
accumulating. Twelve cycles of "and also try to..." produces a prompt that is
mostly qualifications. So: a hard cap on directives, a minimum sample size before
anything is emitted at all, weights clamped to a narrow band, and every version
kept so a bad one can be rolled back rather than argued with.
"""
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime

from .. import db
from ..llm import call_json
from ..sections import SECTION_LABELS, SECTION_ORDER, canonical_section
from ..state import FeedbackState

# Below this, there is no pattern — there is a small number of opinions. Chosen
# against the 40-day window in the design: a section taught ~1 period a day
# accumulates well past this, so the guard only bites on genuinely thin data.
MIN_SAMPLE = 12
MIN_PER_SECTION = 6

# A section is in trouble below this, and healthy above the second. The gap
# between them is deliberate — a section hovering at 0.6 flips category every
# cycle otherwise, and the directives thrash with it.
WEAK_THRESHOLD = 0.55
STRONG_THRESHOLD = 0.78

MAX_DIRECTIVES = 6
MAX_LIST_ITEMS = 5

_SCORE = {"yes": 1.0, "somewhat": 0.5, "no": 0.0}


async def telemetry_node(state: FeedbackState) -> dict:
    """Read the window. Nothing more — no filtering, no scoring.

    Kept as its own node so the raw responses are checkpointed before anything
    interprets them: when an adaptive state turns out to be wrong, the question
    is always "what did the teachers actually say", and that has to still be
    answerable.
    """
    key = state["scope_key"]
    window = int(state.get("window_days") or 40)
    rows = await db.fetch_feedback_window(key, window)
    return {
        "responses": rows,
        "sample_size": len(rows),
        "previous_state": await db.fetch_active_adaptive_state(key),
    }


def _rate(ratings: list[str]) -> dict:
    counts = Counter(r for r in ratings if r in _SCORE)
    total = sum(counts.values())
    if not total:
        return {"n": 0, "score": None, "yes": 0, "somewhat": 0, "no": 0}
    return {
        "n": total,
        # SOMEWHAT counted as a half rather than dropped or counted as a no. It
        # is the answer a teacher gives when the sheet worked but needed
        # patching, which is real information and the most common answer.
        "score": round(sum(_SCORE[r] * c for r, c in counts.items()) / total, 3),
        "yes": counts.get("yes", 0),
        "somewhat": counts.get("somewhat", 0),
        "no": counts.get("no", 0),
    }


def _sort_key(row: dict) -> str:
    """`created_at` as something sortable.

    Supabase returns it as an ISO-8601 string; a direct psycopg read or a test
    fixture hands back a datetime. Both are ordered correctly by their ISO form,
    and normalising here avoids a TypeError from comparing the two the first time
    a deployment mixes them.
    """
    value = row.get("created_at")
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _trend(rows: list[dict]) -> dict:
    """Recent half versus older half. Direction matters more than level: a
    section at 0.6 and climbing needs leaving alone, and the same 0.6 falling
    needs attention now."""
    dated = [r for r in rows if r.get("created_at") and r.get("rating") in _SCORE]
    if len(dated) < MIN_SAMPLE * 2:
        return {}
    dated.sort(key=_sort_key)
    midpoint = len(dated) // 2
    older = _rate([r["rating"] for r in dated[:midpoint]])
    recent = _rate([r["rating"] for r in dated[midpoint:]])
    if older["score"] is None or recent["score"] is None:
        return {}
    delta = round(recent["score"] - older["score"], 3)
    return {
        "older": older["score"], "recent": recent["score"], "delta": delta,
        "direction": "improving" if delta > 0.08 else ("declining" if delta < -0.08 else "flat"),
    }


def aggregate(rows: list[dict]) -> dict:
    """The numbers. Sliced the four ways that can actually be acted on."""
    by_section: dict[str, list[str]] = defaultdict(list)
    by_activity: dict[str, list[str]] = defaultdict(list)
    overall: list[str] = []

    for row in rows:
        rating = (row.get("rating") or "").strip().lower()
        if rating not in _SCORE:
            continue
        overall.append(rating)
        section = (row.get("section") or "").strip()
        if section in SECTION_ORDER:
            by_section[section].append(rating)
        activity = (row.get("activity") or "").strip()
        if activity:
            by_activity[activity].append(rating)

    notes = [(row.get("note") or "").strip() for row in rows
             if (row.get("note") or "").strip()]

    return {
        "overall": _rate(overall),
        "trend": _trend(rows),
        "sections": {s: _rate(by_section.get(s, [])) for s in SECTION_ORDER},
        "activities": {
            name: _rate(ratings) for name, ratings in by_activity.items()
            if len(ratings) >= 3
        },
        "notes": notes[:40],
        "noteThemes": _note_themes(notes),
    }


_NOTE_STOPWORDS = frozenset("""
the and for was were with this that they them their have has had not but you your are
were them then than very much really quite lesson lesson's class students student
teacher time section sheet material materials prep topic today good well bit too
""".split())


def _note_themes(notes: list[str]) -> list[dict]:
    """Recurring words in the free-text notes.

    Crude by design. Its job is to hand the optimizer evidence it can quote —
    "eight notes mention 'materials'" — not to interpret. Interpretation is the
    model's half of this pipeline, and it does it better with the raw notes than
    with a summary of them.
    """
    words = Counter()
    for note in notes:
        for word in re.findall(r"[a-z][a-z'-]{3,}", note.lower()):
            if word not in _NOTE_STOPWORDS:
                words[word] += 1
    return [{"word": w, "count": c} for w, c in words.most_common(8) if c >= 3]


def match_patterns(aggregates: dict, sample_size: int) -> list[dict]:
    """Turn the numbers into named, evidenced patterns.

    Every pattern carries the count it rests on, because the optimizer's prompt
    shows it and the whole guard against overreacting is that it can see
    `n=7` next to a claim.
    """
    patterns: list[dict] = []
    if sample_size < MIN_SAMPLE:
        return [{
            "kind": "insufficient_data",
            "detail": f"only {sample_size} response(s) in the window; {MIN_SAMPLE} needed "
                      f"before any adjustment is justified",
            "n": sample_size,
        }]

    overall = aggregates.get("overall") or {}
    if overall.get("score") is not None:
        patterns.append({
            "kind": "overall",
            "detail": f"overall satisfaction {overall['score']:.2f} "
                      f"({overall['yes']} yes / {overall['somewhat']} somewhat / {overall['no']} no)",
            "n": overall["n"], "score": overall["score"],
        })

    trend = aggregates.get("trend") or {}
    if trend.get("direction") in ("improving", "declining"):
        patterns.append({
            "kind": "trend",
            "detail": f"satisfaction is {trend['direction']} — {trend['older']:.2f} in the "
                      f"older half of the window, {trend['recent']:.2f} in the recent half",
            "n": overall.get("n", 0), "delta": trend["delta"],
        })

    for section in SECTION_ORDER:
        stats = (aggregates.get("sections") or {}).get(section) or {}
        if stats.get("n", 0) < MIN_PER_SECTION or stats.get("score") is None:
            continue
        if stats["score"] < WEAK_THRESHOLD:
            patterns.append({
                "kind": "weak_section", "section": section,
                "detail": f"{SECTION_LABELS[section]} scores {stats['score']:.2f} over "
                          f"{stats['n']} responses ({stats['no']} outright no)",
                "n": stats["n"], "score": stats["score"],
            })
        elif stats["score"] >= STRONG_THRESHOLD:
            patterns.append({
                "kind": "strong_section", "section": section,
                "detail": f"{SECTION_LABELS[section]} scores {stats['score']:.2f} over "
                          f"{stats['n']} responses — whatever it is doing, keep it",
                "n": stats["n"], "score": stats["score"],
            })

    activities = aggregates.get("activities") or {}
    ranked = sorted(
        ((name, s) for name, s in activities.items() if s.get("score") is not None),
        key=lambda item: item[1]["score"],
    )
    for name, stats in ranked[:3]:
        if stats["score"] < WEAK_THRESHOLD:
            patterns.append({
                "kind": "weak_activity", "activity": name,
                "detail": f"activity '{name}' scores {stats['score']:.2f} over {stats['n']} uses",
                "n": stats["n"], "score": stats["score"],
            })
    for name, stats in reversed(ranked[-3:]):
        if stats["score"] >= STRONG_THRESHOLD:
            patterns.append({
                "kind": "strong_activity", "activity": name,
                "detail": f"activity '{name}' scores {stats['score']:.2f} over {stats['n']} uses",
                "n": stats["n"], "score": stats["score"],
            })

    for theme in aggregates.get("noteThemes") or []:
        patterns.append({
            "kind": "note_theme",
            "detail": f"'{theme['word']}' appears in {theme['count']} teacher notes",
            "n": theme["count"],
        })

    return patterns


async def pattern_matcher_node(state: FeedbackState) -> dict:
    rows = state.get("responses") or []
    aggregates = aggregate(rows)
    patterns = match_patterns(aggregates, len(rows))

    pattern_id = str(uuid.uuid4())
    await db.save_patterns(
        pattern_id, state["scope_key"], int(state.get("window_days") or 40),
        len(rows), aggregates, patterns)

    return {"aggregates": aggregates, "patterns": patterns}


_OPTIMIZATION_PROMPT = """You are tuning the prompt that generates lesson prep material, using what
teachers reported after actually teaching from it.

SCOPE: grade {grade} {subject}
WINDOW: the last {window} days | RESPONSES: {sample}

Every prep sheet has these six sections, and each has a fixed source of content
you cannot change — only how it is written:
{sections}

WHAT THE TELEMETRY SHOWS. Each line carries the number of responses behind it:
{patterns}

TEACHERS' OWN WORDS (a sample of the free-text notes):
{notes}

THE STATE CURRENTLY IN FORCE (v{previous_version}) — you are revising this, not
starting over:
{previous}

Write the next version. Rules, and they are the point of this task:
- MAX {max_directives} section directives total. This text is prepended to every
  future generation prompt; past about six lines it stops being guidance and
  becomes noise the real instructions have to compete with.
- A directive must be ACTIONABLE and SPECIFIC. "Make Challenge better" changes
  nothing. "Challenge: state the worked answer in 'detail' so the teacher never
  computes in front of the class" changes something.
- EVIDENCE OR SILENCE. Every directive must trace to a pattern above. If a
  section has no pattern, it gets no directive — leaving a working section alone
  is the correct action, not a missed opportunity.
- RESPECT SAMPLE SIZE. A pattern resting on 6 responses justifies a cautious
  nudge; one resting on 60 justifies a firm instruction. Do not write a firm
  instruction from thin data.
- KEEP WHAT WORKS. A directive in the current state whose section is still
  scoring well should survive into this version, worded as it is. Churning
  directives that are working is how a state gets worse over time.
- DROP WHAT IS DONE. A directive whose section has recovered has served its
  purpose. Remove it and say so in the changelog.
- `activityCategoryWeights` nudges which activity categories get selected: 1.0 is
  neutral, and the effective range is 0.6 to 1.4. Only weight a category a
  pattern actually named.
- `difficultyShift` is "up", "down" or "none". Use anything other than "none"
  only on a clear, evidenced signal across the whole window — this moves every
  topic in every future chapter.

Every key of "sectionDirectives" MUST be exactly one of:
  refresher | concept | realLife | challenge | levelSet | explore
Use those, never the display label — "levelSet" and not "Level Set", "realLife"
and not "Real Life". A directive under any other key is discarded.

Return ONLY valid JSON, no markdown fences:
{{
  "sectionDirectives": {{"explore": "one specific instruction, under 30 words", "levelSet": "..."}},
  "prefer": ["up to {max_items} concrete things that are working — keep doing them"],
  "avoid": ["up to {max_items} concrete things to stop doing"],
  "activityCategoryWeights": {{"movement": 1.2, "worksheet": 0.7}},
  "difficultyShift": "none",
  "rationale": "2-3 sentences: what the data says and what you changed because of it",
  "changelog": ["one line per change from the previous version, each naming the pattern it answers"]
}}
"""


def _clamp_state(data: dict, previous: dict) -> tuple[dict, list[str]]:
    """Force the optimizer's answer inside its bounds.

    This runs unconditionally rather than as a validation step, because the
    bounds are what make the loop safe to leave running unattended. A prompt can
    ask for six directives; only this can guarantee six.
    """
    directives: dict[str, str] = {}
    unmapped: list[str] = []
    for section, text in (data.get("sectionDirectives") or {}).items():
        # Normalised, not matched exactly: the prompt names sections by their
        # labels ("Level Set"), so that is what comes back, and an exact-match
        # filter discards the optimizer's most valuable output.
        key = canonical_section(section)
        if key is None or not isinstance(text, str):
            unmapped.append(str(section))
            continue
        cleaned = " ".join(text.split())[:220]
        if cleaned:
            directives[key] = cleaned
    if unmapped:
        # Logged rather than swallowed. A directive dropped in silence is a
        # feedback cycle that reports success and changes nothing.
        print(f"[prep_flow:feedback] ignored {len(unmapped)} directive key(s) that "
              f"match no section: {', '.join(unmapped[:6])}")
    # Truncated in SECTION_ORDER order, so if the cap bites it is the later
    # sections that lose out — and Refresher/Concept, the grounded ones with the
    # least room to vary, are the ones a directive helps least anyway.
    if len(directives) > MAX_DIRECTIVES:
        directives = {s: directives[s] for s in SECTION_ORDER if s in directives}
        directives = dict(list(directives.items())[:MAX_DIRECTIVES])

    def _list(key: str) -> list[str]:
        return [" ".join(str(v).split())[:160]
                for v in (data.get(key) or []) if isinstance(v, str) and v.strip()][:MAX_LIST_ITEMS]

    weights: dict[str, float] = {}
    for category, value in (data.get("activityCategoryWeights") or {}).items():
        try:
            weights[str(category).strip().lower()] = max(0.6, min(1.4, float(value)))
        except (TypeError, ValueError):
            continue

    shift = str(data.get("difficultyShift") or "none").strip().lower()
    if shift not in ("up", "down", "none"):
        shift = "none"

    changelog = [" ".join(str(c).split())[:200]
                 for c in (data.get("changelog") or []) if isinstance(c, str) and c.strip()][:10]

    state = {
        "sectionDirectives": directives,
        "prefer": _list("prefer"),
        "avoid": _list("avoid"),
        "activityCategoryWeights": dict(list(weights.items())[:8]),
        "difficultyShift": shift,
        "rationale": " ".join(str(data.get("rationale") or "").split())[:600],
    }
    if not changelog:
        changed = sorted(set(directives) ^ set((previous.get("sectionDirectives") or {})))
        changelog = ([f"revised directives for: {', '.join(changed)}"] if changed
                     else ["no directive changes this cycle"])
    return state, changelog


async def feedback_optimization_node(state: FeedbackState) -> dict:
    patterns = state.get("patterns") or []
    previous = state.get("previous_state") or {}
    sample = int(state.get("sample_size") or 0)

    if any(p.get("kind") == "insufficient_data" for p in patterns) or sample < MIN_SAMPLE:
        # Explicitly a non-event, not a failure. Carrying the previous state
        # forward untouched is the right answer to thin data, and writing a new
        # version that says the same thing would just pollute the history.
        return {
            "adaptive_state": previous,
            "changelog": [f"no change — {sample} response(s) is below the "
                          f"{MIN_SAMPLE} needed to justify one"],
            "applied": False,
        }

    actionable = [p for p in patterns if p.get("kind") in (
        "weak_section", "strong_section", "weak_activity", "strong_activity",
        "trend", "note_theme")]
    if not actionable:
        return {
            "adaptive_state": previous,
            "changelog": ["no change — nothing in the window crossed a threshold"],
            "applied": False,
        }

    aggregates = state.get("aggregates") or {}
    prompt = _OPTIMIZATION_PROMPT.format(
        grade=state.get("grade"), subject=state.get("subject"),
        window=state.get("window_days", 40), sample=sample,
        sections="\n".join(
            # The JSON key is spelled out beside the label. Without it the model
            # answers with the label it has been reading all through this prompt,
            # and every directive lands under a key nothing recognises.
            f'  - {SECTION_LABELS[s]} — write this one as "{s}": source is fixed, '
            f"wording is yours"
            for s in SECTION_ORDER),
        patterns="\n".join(f"  - [{p['kind']}, n={p.get('n', '?')}] {p['detail']}"
                           for p in patterns),
        notes="\n".join(f"  - \"{n[:180]}\"" for n in (aggregates.get("notes") or [])[:12])
              or "  (no written notes in this window)",
        previous_version=previous.get("_version", 0),
        previous=_render_previous(previous),
        max_directives=MAX_DIRECTIVES, max_items=MAX_LIST_ITEMS,
    )

    try:
        data = await call_json(
            prompt, label="feedback-optimization",
            required=("sectionDirectives",), temperature=0.3, max_tokens=2500)
    except Exception as exc:
        return {
            "adaptive_state": previous,
            "changelog": [f"no change — the optimizer did not complete: {exc}"],
            "applied": False,
            "errors": [f"feedback optimization: {exc}"],
        }

    new_state, changelog = _clamp_state(data, previous)

    # Verify BEFORE persisting. A state that cannot reach a prompt is worse than
    # no new version: it supersedes one that could, and reports success doing it.
    liveness = verify_reaches_prompt(new_state, actionable_patterns=len(actionable))
    if not liveness["ok"]:
        for problem in liveness["problems"]:
            print(f"[prep_flow:feedback] LIVENESS: {problem}")
        return {
            "adaptive_state": previous,
            "changelog": changelog + [
                "NOT APPLIED — the new state would not have reached the generation "
                "prompt: " + "; ".join(liveness["problems"])],
            "applied": False,
            "liveness": liveness,
            "errors": [f"feedback optimization: state failed liveness "
                       f"({'; '.join(liveness['problems'])})"],
        }
    # The canary. Off unless PREP_FLOW_ACTIVATION_GATE is set; returns None when
    # off or unusable, which means "no opinion" and activation proceeds — a gate
    # that blocked because it was misconfigured would be worse than no gate.
    from ..gate import gate_candidate

    gate = await gate_candidate(previous, new_state, scope_key=state["scope_key"])
    if gate and not gate["activate"]:
        print(f"[prep_flow:feedback] GATE {gate['verdict']}: {gate['reason']}")
        return {
            "adaptive_state": previous,
            "changelog": changelog + [f"NOT APPLIED — {gate['reason']}"],
            "applied": False,
            "liveness": liveness,
            "gate": gate,
        }

    state_id = str(uuid.uuid4())
    version = await db.activate_adaptive_state(
        state_id, state["scope_key"], new_state,
        derived_from={"patterns": patterns, "aggregates": aggregates,
                      "windowDays": state.get("window_days", 40)},
        changelog=changelog, sample_size=sample)

    if version is None:
        return {
            "adaptive_state": new_state,
            "changelog": changelog + ["WARNING: not persisted — no database configured"],
            "applied": False,
        }

    return {
        "adaptive_state": {**new_state, "_version": version, "_sampleSize": sample},
        "changelog": changelog,
        "applied": True,
        "liveness": liveness,
        "gate": gate,
    }


def verify_reaches_prompt(state: dict, actionable_patterns: int = 0) -> dict:
    """Prove this adaptive state actually lands in a generation prompt.

    Written after a bug that hid in plain sight for a whole cycle: the optimizer's
    prompt names sections by their labels ("Level Set"), the model answered with
    those labels, and the filter — which accepted only `levelSet` — dropped every
    directive with a bare `continue`. Version incremented, changelog reported
    three directives added, rollback worked, health was green. The loop reported
    success and changed nothing.

    Every existing check passed because they all asked "did we store a state",
    and none asked "does the state reach the model". This asks the second
    question, deterministically and with no LLM call, so the same class of
    failure cannot be silent again.
    """
    from .planning import adaptive_directive_block

    directives = state.get("sectionDirectives") or {}
    lists = [i for key in ("avoid", "prefer") for i in (state.get(key) or [])]
    shift = state.get("difficultyShift") not in (None, "none")
    weights = state.get("activityCategoryWeights") or {}

    block = adaptive_directive_block(state)
    problems: list[str] = []

    if not (directives or lists or shift or weights):
        # An empty state is legitimate ONLY when nothing demanded action. Empty
        # *after* the matcher found weak sections is the signature of the real
        # bug: the model wrote directives, the filter discarded them, and the
        # changelog reported success. That combination must fail, not pass.
        if actionable_patterns:
            return {
                "ok": False, "empty": True, "reachesPrompt": False,
                "directives": 0,
                "problems": [
                    f"the optimizer produced an EMPTY state despite "
                    f"{actionable_patterns} actionable pattern(s) — either the "
                    f"model returned nothing usable, or its output was dropped "
                    f"before it could be stored"],
            }
        return {"ok": True, "empty": True, "reachesPrompt": False,
                "directives": 0, "problems": [],
                "note": "state carries no directives, lists, weights or shift, and "
                        "no pattern demanded any — generation is unchanged, which "
                        "is the correct outcome for a quiet window"}

    if (directives or lists or shift) and not block.strip():
        problems.append("the state has content but renders to an empty prompt block")

    for section, text in directives.items():
        if section not in SECTION_ORDER:
            problems.append(f"directive key '{section}' is not a section — it will "
                            f"never render")
        elif text and text[:60] not in block:
            problems.append(f"directive for '{section}' does not appear in the "
                            f"rendered block")

    # activityCategoryWeights deliberately never reach a prompt — they are applied
    # numerically in activity selection — so their absence from the block is
    # correct, not a fault. Verified separately.
    unusable = [c for c, w in weights.items()
                if not isinstance(w, (int, float))]
    if unusable:
        problems.append(f"non-numeric activity weight(s): {', '.join(unusable)}")

    return {
        "ok": not problems,
        "empty": False,
        "reachesPrompt": bool(block.strip()),
        "directives": len(directives),
        "listItems": len(lists),
        "weights": len(weights),
        "blockChars": len(block),
        "problems": problems,
    }


def _render_previous(previous: dict) -> str:
    if not previous:
        return "  (none — this is the first optimization for this scope)"
    lines = []
    for section, directive in (previous.get("sectionDirectives") or {}).items():
        lines.append(f"  - {section}: {directive}")
    for key in ("prefer", "avoid"):
        for item in previous.get(key) or []:
            lines.append(f"  - {key}: {item}")
    weights = previous.get("activityCategoryWeights") or {}
    if weights:
        lines.append(f"  - category weights: {weights}")
    if previous.get("difficultyShift") not in (None, "none"):
        lines.append(f"  - difficulty shift: {previous['difficultyShift']}")
    return "\n".join(lines) or "  (the current state is empty)"
