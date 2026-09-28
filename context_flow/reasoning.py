"""The one structured reasoning call — the whole of Node 2's model spend.

§2's "One reasoning boundary": one structured LLM call per lesson window, not a
second multi-agent system. Node 1 was rebuilt to reduce a dozen narrow calls into
merged stages; repeating that mistake in the node built afterwards would be the
more embarrassing version of it.

WHAT THE MODEL IS AND IS NOT ASKED.

  It is NOT asked which factors are available — `activation.py` settled that, and
  §5 says so explicitly. It is NOT asked what the mastery target is; the contract
  states it. It is NOT asked whether the room has a projector. Every one of those
  is a fact somebody recorded, and a model invited to reconsider a fact will
  eventually improve one.

  It IS asked the one thing code cannot decide: given these active factors, this
  fixed target and this period, does any contextual condition create a real
  barrier, opportunity or delivery constraint — and if so, what is the smallest
  change that removes it without moving the target.

THE PROMPT IS ASSEMBLED, NOT TEMPLATED, and that is where §14's token argument
lands. An inactive factor contributes no line. A factor ruled out for this topic
contributes no line. A profile field no active factor reads was already dropped
in `profile.py`. What reaches the model on a school with no verified community
data is eleven baseline factors and their evidence — not twenty factors, five of
them described as unavailable.

THE FOUR REFUSALS ARE IN THE PROMPT AS RULES, and each one names the failure it
prevents rather than stating a preference. A model told "do not stereotype"
complies with the word; a model told "an adaptation whose only evidence is that
this is a rural school is rejected — name the verified field instead" complies
with the check it is about to face. The gate catches these anyway; the prompt
exists so the gate has less to catch, because a rejected adaptation is a paid
call that produced nothing.
"""
import json
from typing import Optional

from . import activation as activation_module
from . import profile as profile_module
from . import plan as plan_module
from .factors import ACCESS, DELIVERY, LEARNING

try:
    from prep_flow.llm import call_json
except ImportError:  # pragma: no cover — the package layout in the real backend
    from ..prep_flow.llm import call_json


# ── Prompt fragments ─────────────────────────────────────────────────────────

def _factor_block(activations: list) -> str:
    """The active factors, one line each, with what they may read.

    `role` is copied from the registry rather than paraphrased here: the registry
    is what the gate reads too, so a factor whose boundary is written once cannot
    be described one way to the model and enforced another way afterwards.
    """
    lines = []
    for entry in activation_module.proposable(activations):
        factor = entry.factor
        purposes = "/".join(factor.purposes) or "—"
        lines.append(f"  [{factor.id}] {factor.name}  (may produce: {purposes})")
        lines.append(f"      role:  {factor.role}")
        if entry.hint:
            lines.append(f"      this topic: {entry.hint}")
        if entry.exposes:
            lines.append(f"      evidence you may cite: {', '.join(entry.exposes)}")
        else:
            lines.append("      evidence you may cite: (none recorded — safe "
                         "defaults only, and you may NOT state a local fact)")
    return "\n".join(lines) or "  (no factor is active for this window)"


# `profile.ROOM_FACTS` is the one list — the gate reads it too, and a second
# spelling here is how the prompt would come to offer a field the gate then
# refuses. That is not hypothetical: the first real run refused two good
# adaptations because this block showed the model the board and the gate would
# not accept a citation of it.
_ROOM_FACTS = tuple(sorted(profile_module.ROOM_FACTS))


def _profile_block(profile, activations: list) -> str:
    """The fields an active, still-relevant factor may read — plus the room's
    physical facts, which constrain everything.

    Computed from the activations rather than from the profile, so a field that
    survived normalisation but belongs to a factor ruled out for this topic still
    does not reach the prompt. `_ROOM_FACTS` is the one exception, and the
    comment above it says why.
    """
    allowed: set[str] = set()
    for entry in activation_module.proposable(activations):
        allowed |= set(entry.exposes)
    physical = {name for name in _ROOM_FACTS if profile.has(name)}

    if not allowed and not physical:
        return "  (nothing recorded that any active factor may read)"

    lines = []
    for name in sorted(allowed | physical):
        field = profile.get(name)
        if field is None:
            continue
        stale = "  [STALE - confirmed over a year ago]" if field.stale else ""
        # Marked, so the model does not read a physical fact as a licence to
        # reason about a factor that was switched off. "You may cite" is a
        # per-factor permission and is stated on the factor lines below; this
        # says only what the room contains.
        cite = "" if name in allowed else "   [room constraint - any factor may cite it]"
        lines.append(f"  {name} = {json.dumps(field.value, ensure_ascii=False)}"
                     f"   ({field.provenance}, from {field.origin}){stale}{cite}")
    return "\n".join(lines)


def _topic_block(row: dict) -> str:
    """One topic of the contract, as the fixed destination plus the planned route.

    The two halves are labelled because the whole node turns on the distinction,
    and a model reading an undifferentiated dump of the contract has to infer
    which fields it may touch. FIXED first, so it is read first.
    """
    academic = row.get("academic") or {}
    chain = row.get("knowledgeChain") or {}
    audit = row.get("masteryAudit") or {}
    experience = row.get("experiencePlan") or {}
    plan = row.get("lessonPlan") or {}
    activity = row.get("selectedActivity") or {}
    closes = experience.get("closesGap") or {}

    minutes = plan.get("minutes") or {}
    sections = "  ".join(
        f"{e.get('label')}({e.get('minutes')}m,{e.get('mode')})"
        for e in (row.get("sectionSpec") or []))

    open_shortfalls = [f"[{m.get('kind')}] {m.get('missing')}"
                       for m in (audit.get("missing") or [])
                       if m.get("kind") != closes.get("kind")]

    return f"""
TOPIC T{row.get('index')} — {row.get('topic')}

  ── FIXED. You adapt the route to this. You do not touch it. ──
  mastery target:  {academic.get('masteryTarget') or '(not derived)'}
  required concepts: {', '.join(academic.get('requiredConcepts') or []) or '(none)'}
  competencies:    {', '.join(academic.get('competencies') or []) or '(none)'}
  this period leaves the class able to: {chain.get('gained') or '(not derived)'}
  and owes the next period:             {chain.get('bridgesTo') or '(nothing — it closes a strand)'}
  it assumes:                           {chain.get('assumes') or '(nothing — it opens a strand)'}
  misconception to surface: {'; '.join(row.get('misconceptions') or []) or '(none named)'}

  ── THE PLANNED ROUTE. This is what you may change. ──
  thinking path:   {' -> '.join(experience.get('trajectory') or []) or '(none)'}
  hardest move:    {experience.get('conceptualJump') or '(not named)'}
  object:          {experience.get('anchor') or '(none)'} — {experience.get('anchorReason') or ''}
  other objects already judged to fit: {', '.join(experience.get('anchorPool') or []) or '(none)'}
  what the class physically does: {experience.get('studentAction') or '(not specified)'}
  activity:        {activity.get('name') or '(none)'} ({activity.get('source') or '?'}),
                   needs: {', '.join(activity.get('materials') or []) or '(nothing)'}
  new vocabulary:  {', '.join(plan.get('newVocabulary') or []) or '(none)'}
  period shape:    {sections or '(not planned)'}   total {sum(minutes.values()) if minutes else '?'} min
  the page already supplies: {'; '.join(audit.get('supplied') or []) or '(not audited)'}
  this period already closes: [{closes.get('kind') or 'nothing'}] {closes.get('gap') or ''}
  shortfalls STILL OPEN (a real local example has somewhere to land here):
    {chr(10) + '    ' if open_shortfalls else ''}{(chr(10) + '    ').join(open_shortfalls) or '(none)'}
""".rstrip()


_SYSTEM = (
    "You are a teaching adviser who knows one classroom well. You are given a "
    "lesson that is already academically correct and already planned. You are "
    "not rewriting it. You are deciding whether anything about THIS room makes "
    "the planned route harder than it needs to be, and proposing the smallest "
    "change that fixes it."
)

_PROMPT = """A lesson has been designed for Grade {grade} {subject}. It is academically
settled: what the class must master, what they will gain, the thinking path, the
object, the activity and the minutes are all decided and correct.

Your ONLY question is whether the conditions in this particular room and
community make that planned route harder than it needs to be — and if so, what
the smallest change is that removes the difficulty WITHOUT changing what the
class ends up knowing.

═══ THE ROOM ═══
{profile_block}

═══ WHAT YOU MAY REASON ABOUT ═══
Only these factors. Others were switched off because the data to support them
does not exist, and proposing one is not a judgement call — it is a claim about
a room nobody described.

{factor_block}

═══ THE LESSON(S) ═══
{topic_block}
{policy_block}

═══ RULES ═══

1. THE DESTINATION IS FIXED. You may change examples, language, scaffolding,
   grouping, participation route, materials, pacing and teacher moves. You may
   NOT change what the class must end up knowing, remove a required concept,
   drop the misconception, or make the task easier in order to make it reachable.
   "They will struggle with this, so ask for less" is the one answer that is
   always wrong. Ask for the same thing by a different route.

2. EVERY ADAPTATION NAMES ITS EVIDENCE. Put the exact field name(s) from THE ROOM
   above in `evidence`. An adaptation resting on nothing recorded — "rural
   schools usually...", "children here likely..." — is rejected before anyone
   reads it. If a factor's evidence line above says "(none recorded)", you may
   still adapt for it using the LESSON's own facts (seven new terms in thirty
   minutes is a real cognitive-load claim), but you may NOT state a fact about
   this community.

3. EVERY ADAPTATION NAMES WHAT IT SERVES. `supports` must name the mastery
   target or a required concept it helps the class reach. An adaptation that is
   locally interesting and unrelated to the target is the most common way this
   goes wrong, and it is rejected.

4. FEWER IS BETTER, AND ZERO IS A REAL ANSWER. At most {max_adaptations} per
   topic. If this lesson genuinely fits this room as planned, return one entry of
   type "no_change" for that topic saying so. Do not adapt to look responsive —
   a teacher handed four changes to a {duration}-minute period has been handed a
   different period.

5. NOTHING YOU PROPOSE MAY NEED SOMETHING THIS ROOM DOES NOT HAVE. Not just
   substitutes and fallbacks - every adaptation. Check THE ROOM first. Replacing
   a projector step with "a printed handout" in a school with no paper recorded
   is not a fallback, and neither is a scaffolding step that needs the same
   handout. Lines marked [room constraint] are physical facts about the room and
   ANY factor may cite them - if your change depends on there being a board, say
   so and cite `has_board`. They are not, however, a licence to reason about a
   factor that was switched off: what the room contains is not a claim about the
   community.

6. NO ADAPTATION MAY REQUIRE A CHILD TO DISCLOSE ANYTHING PERSONAL about their
   home, income, family circumstances or absence, or single a child out.
   Community context is used to make a concept meaningful — never to make a
   child the example.

Return ONLY valid JSON, no markdown fences:
{{
  "adaptations": [
    {{
      "topicIndex": {first_index},
      "factor": "one id from the list above",
      "type": "add | modify | substitute | fallback | no_change",
      "purpose": "{learning} | {access} | {delivery}",
      "section": "refresher | concept | realLife | challenge | levelSet | explore",
      "change": "what the teacher does differently — concrete, one or two sentences",
      "reason": "the condition in THIS room that makes it worth doing",
      "supports": ["the mastery target or required concept this helps reach"],
      "evidence": ["exact field name(s) from THE ROOM, or [] if none"],
      "data_source": "verified | assumed",
      "confidence": 0.0
    }}
  ],
  "lowers_standard": false,
  "note": "one sentence: what you changed overall, or why nothing needed changing"
}}

`data_source` describes the DATA, not your certainty: "verified" only if every
field in `evidence` is marked verified above. `confidence` is your certainty that
the adaptation is useful. A high confidence never upgrades assumed data.

`lowers_standard` is the one judgement only you can make: read your own
adaptations back and say whether ANY of them, taken together, would leave the
class knowing less than the mastery target requires. Answering honestly costs you
nothing; the plan is checked either way."""


def _policy_block(policy) -> str:
    """Rendered by `reinforcement.render`, never here.

    The liveness proof asserts that this text reaches this prompt, and it can
    only assert that honestly if it calls the same function. A second renderer
    living here is exactly the arrangement in which the loop reports success and
    changes nothing.
    """
    if not policy:
        return ""
    try:
        from reinforcement.render import prompt_block
    except Exception:                   # noqa: BLE001 - the node runs without it
        return ""
    block = prompt_block(policy)
    return ("\n" + block + "\n") if block.strip() else ""


async def propose(
    *, rows: list[dict], profile, activations_by_index: dict[int, list],
    grade: str, subject: str, duration: int = 30,
    label: Optional[str] = None, policy: Optional[dict] = None,
    config: Optional[dict] = None, contract: Optional[dict] = None,
) -> dict:
    """One call for one window of topics. Returns the normalised plan.

    WHY THE WINDOW SHARES ONE CALL AND ONE ACTIVATION VIEW. §14 allows batching
    a small lesson window "if cross-topic context is useful", and it is: the
    room does not change between T7 and T8, and a model that sees both at once
    will not propose the same language bridge twice. The activations are still
    computed per topic — relevance is a topic-level question — and the union is
    what the prompt offers, with each topic's own hint line under it.
    """
    if not rows:
        return plan_module.empty_plan("no topics in this window")

    # The union, ordered by the registry so the prompt reads the same way every
    # time. A factor proposable for ANY topic in the window is offered for the
    # window; the per-topic hint lines say which one it was measured on.
    merged: dict[str, object] = {}
    for index in sorted(activations_by_index):
        for entry in activation_module.proposable(activations_by_index[index]):
            existing = merged.get(entry.factor.id)
            if existing is None:
                merged[entry.factor.id] = entry
            elif entry.hint and entry.hint not in getattr(existing, "hint", ""):
                merged[entry.factor.id] = existing._replace(
                    hint=f"{existing.hint} | T{index}: {entry.hint}",
                    exposes=tuple(sorted(set(existing.exposes) | set(entry.exposes))))
    window_activations = list(merged.values())

    if not window_activations:
        # No paid call. Every factor is either unsupported by data or ruled out
        # for these topics, and a call whose answer is already known is §14's
        # token waste in its purest form.
        return plan_module.empty_plan(
            "no factor was both active and relevant for these topics — no model "
            "call was made")

    indexes = [int(r.get("index")) for r in rows if r.get("index") is not None]
    prompt = _PROMPT.format(
        grade=grade, subject=subject, duration=duration,
        max_adaptations=plan_module.MAX_ADAPTATIONS_PER_TOPIC,
        first_index=indexes[0] if indexes else 1,
        learning=LEARNING, access=ACCESS, delivery=DELIVERY,
        profile_block=_profile_block(profile, window_activations),
        factor_block=_factor_block(window_activations),
        topic_block="\n".join(_topic_block(row) for row in rows),
        # The reinforcement policy's words, or "". THE ONLY ROUTE BY WHICH
        # teacher feedback changes what this call PROPOSES — the dials applied in
        # gate.py can only re-rank a list that already exists, and arrive too late
        # to change what went on it.
        #
        # Empty when no policy is in force, which leaves this prompt
        # byte-for-byte what it was before the loop existed. That is what makes
        # the canary gate's baseline arm a real baseline rather than a second
        # sample of the same thing.
        policy_block=_policy_block(policy),
    )

    first, last = (indexes[0], indexes[-1]) if indexes else (0, 0)

    # THE SEAM. `config["deep_agents"]` routes this one call through
    # `deep_agents/bridge.py`. Everything either side of it is untouched: the
    # window merge above still decides what is offered, `plan.normalise` below
    # still bounds what comes back, and `gate.run` still accepts or rejects every
    # adaptation deterministically afterwards.
    #
    # §19 is respected in both branches — ONE structured reasoning boundary per
    # window, not a swarm of contextual agents. The agent version can look up a
    # topic's contract and the room's resource profile before proposing; it
    # cannot propose from a factor the activation above ruled out, because it is
    # not given one.
    if (config or {}).get("deep_agents"):
        from deep_agents.bridge import derive_context_plan
        summaries = {index: activation_module.summarise(entries)
                     for index, entries in activations_by_index.items()}
        data = await derive_context_plan(
            rows=rows, profile=profile.as_dict() if hasattr(profile, "as_dict") else profile,
            activation_summaries=summaries, contract=contract or {},
            grade=grade, subject=subject, config=config,
            # The same block the plain call gets, from the same renderer. The
            # agent seam must not be a hole the reinforcement policy's text
            # route falls through: the dials would keep re-ranking, and the
            # directives that change what is PROPOSED would reach nothing.
            policy_block=_policy_block(policy))
    else:
        data = await call_json(
            prompt,
            label=label or f"context-reinforcement[{first}-{last}]",
            required=("adaptations",),
            system=_SYSTEM,
            # Lower than Node 1's design stages. This is a judgement about
            # constraints rather than an act of invention, and the failure mode that
            # actually costs money here is a creative adaptation nobody can run.
            temperature=0.3,
            max_tokens=900 + 420 * len(rows),
        )
    return plan_module.normalise(data, topic_indexes=indexes)
