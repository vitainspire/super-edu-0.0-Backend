"""Cognitive Learner Simulation — would a child at this grade actually get there?

Validation asks whether the sheet is well made. This asks the only question that
matters afterwards: reading nothing but this sheet, could the intended learner
reach the intended understanding? A sheet can pass every structural check — six
sections, three bullets, page citations, a Refresher that picks up the Explore —
and still teach nobody anything.

THE QUESTIONS ARE WRITTEN FROM THE SHEET, and that is deliberate. An earlier
version wrote them blind, from the plan alone, on the theory that a model which
can see the material will write questions the material passes. That theory has a
hole in it: three of the four dimensions are *defined* in terms of the sheet.
Core understanding is supposed to use a situation the sheet itself used;
application is supposed to use one it deliberately did NOT. Neither is writable
without reading it, and a blind question about a scenario the lesson never
mentioned measures the question-writer's imagination rather than the teaching.

So the safeguard moved rather than disappeared, and it is worth knowing exactly
what it is now:

  * **The targets are not the model's to choose.** What each question must test is
    fixed by the plan — `gained` for understanding, the named misconception for
    resistance, `bridgesTo` for forward readiness. The sheet supplies the scene;
    it does not get to supply the standard. A sheet that taught something other
    than its topic still faces its topic's question.
  * **Application must leave the sheet.** The one dimension that reaches outside
    demands a situation the material never used, which is the part a
    teaching-to-the-test question cannot fake.
  * **The questions are committed before the answers exist.** They come first in
    the response and are stored with the verdict, so a question can be read back
    and argued with. A grader that could soften the question after seeing the
    answer would be grading nothing.
  * **Nothing is cached.** Questions follow the prose, so a repaired sheet is
    re-questioned rather than re-judged against the old paper.

FOUR DIMENSIONS, AND THREE OF THEM ARE GATES.

    understanding   did the central idea land                       30%   gate
    application     does the idea survive leaving the example       25%   gate
    misconception   do they avoid the specific wrong belief         30%   gate
    forward         are they ready for what the next topic needs    15%

Weights and gates are applied in code, never by the model. A sheet scoring 0.88
overall while failing misconception resistance has taught the class a wrong idea
confidently, and an average is exactly the wrong instrument for noticing that.
Forward readiness carries no gate on purpose: the topic prepares for the next
lesson rather than delivering it, and gating it would demand mastery of material
the class has not been taught.

APPLICATION IS GATED, AND IT WAS NOT ALWAYS. It was the one dimension that
reaches outside the sheet and the one whose failure the average was most willing
to absorb — 25% against three others meant a sheet could be shown not to travel
past its own example and still ship at 0.79. That is the exact outcome this
pipeline exists to prevent. A period that lands its example and nothing else has
taught the example, and "taught the example" is what a chapter looks like when a
teacher runs it and the class cannot answer anything in the exam that is worded
differently.

So it is now a gate, and the question it asks is measured against the topic's
MASTERY TARGET rather than its gain — see the mastery pass in reasoning.py. The
gain is what the period leaves; the target is what the idea requires, and only the
second of them can be tested on a case the page never chose. Turn the gate off
with `config['gate_transfer'] = False`, which says what it costs: the pipeline goes
back to averaging away the one measurement that separates teaching an idea from
teaching an instance of it.

WHERE THE QUESTION COMES FROM, when there is one. The experience plan already
wrote a `transferTask` for this topic — an unshown case, chosen before any prose
existed, validated as having reached Level Set. The evaluator is handed it and told
to ASK THAT, rather than inventing a transfer question of its own. Left to invent
one, it writes a question the sheet happens to answer, which is the whole failure
mode this module's docstring opens by describing; and a pipeline that plans a
transfer task, validates that the sheet contains it, and then grades against a
different question has measured neither.

WHAT THIS IS NOT. It is not a child, and a model reading prose written by a model
shares its blind spots — the caveat `simulate.py` already carries about the
simulated teacher. It cannot tell you the instruction was incomprehensible in
Telugu, or that the Challenge died because nobody had scissors. Read a failure as
"this sheet does not carry its own weight on the page", never as "this was
measured on children".
"""
from prep_flow import repair_surface
from prep_flow.llm import call_json, gather_bounded
from prep_flow.sections import SECTION_LABELS, SECTION_ORDER, section_text
from prep_flow.state import ChapterState

# The four dimensions, their weights, and which are pass/fail gates rather than
# contributions to an average. Declared once and read by the scorer, the router
# and the CLI alike — a weight living in a prompt is a weight nothing enforces.
DIMENSIONS: dict[str, dict] = {
    "understanding": {
        "label": "Core understanding",
        "weight": 0.30,
        "gate": 0.70,
        "tests": "that they reached this topic's stated gain",
        "scene": "use a situation THIS SHEET actually used — same object, same setup",
    },
    "application": {
        "label": "Application",
        "weight": 0.25,
        # Gated, unlike forward readiness, because the two ungated dimensions were
        # ungated for opposite reasons. Forward readiness asks about material this
        # period was never meant to deliver. This asks whether this period
        # delivered its own idea in a form that survives leaving the page, which is
        # not a stretch goal — it is the difference between teaching the concept
        # and teaching the example. Overridable per run: config['gate_transfer'].
        "gate": 0.70,
        "tests": "that the idea travels beyond the example it was taught with — "
                 "measured against the topic's MASTERY TARGET, not its gain",
        "scene": "ask the PLANNED TRANSFER TASK above if one is given, in your own "
                 "words. Only if none is given, invent one: an everyday situation the "
                 "sheet NEVER mentions. This is the one question that must leave the "
                 "material; reusing its example measures memory, not transfer",
    },
    "misconception": {
        "label": "Misconception resistance",
        "weight": 0.30,
        "gate": 0.70,
        "tests": "that they do NOT hold the specific wrong belief named for this topic",
        "scene": "put the wrong belief in a named child's mouth and ask whether that "
                 "child is right — a learner who still holds it will agree with them",
    },
    "forward": {
        "label": "Forward readiness",
        "weight": 0.15,
        "gate": None,
        "tests": "that they are READY for what the next topic needs — not that they know it",
        "scene": "ask something the next topic will build on, answerable by noticing "
                 "rather than by knowing",
    },
}

PASS_THRESHOLD = 0.75          # overridable per run: config['learner_pass_threshold']

# Below this a dimension is failing, whether or not it carries a gate. Named
# because three separate things now consult it — which diagnoses are kept, which
# ones the repair prompt is shown, and which count as evidence once the run is
# over — and a floor that drifts between them would make the third disagree with
# the first about what a failure is.
DIAGNOSIS_FLOOR = 0.7

# The evaluator returns a WORD; the number is looked up here.
#
# It returned a number once, and the first real chapter came back with six topics
# scoring 1.0 / 0.7 / 1.0 / 0.4 — identical to four decimal places across lessons
# about 3D views and about three-digit place value. Those were the illustrative
# values in the JSON template, copied straight through. A numeric field in an
# example is an anchor, and a gate anchored to its own example measures nothing.
#
# Words cannot be copied from a template that does not contain them, and mapping
# them here keeps the arithmetic where the rest of the scoring already lives.
VERDICTS: dict[str, float] = {
    "confident": 1.0,     # answers it, and can say why
    "unexplained": 0.7,   # right answer, no reasoning behind it
    "partial": 0.4,       # a piece of it, or right for the wrong reason
    "cannot": 0.0,        # cannot answer from this sheet, or answers wrongly
}


_EVALUATION_PROMPT = """You are measuring whether one prep sheet TEACHES.

You will write four questions about this topic, then answer them as a Grade
{grade} child who sat through exactly this lesson and had no other teaching, and
then score how well those answers show the learning.

THE RULE THAT MAKES THE MEASUREMENT REAL:

    When answering, use ONLY what this sheet contains or has the class do. Not
    what a Grade {grade} child probably knows already. Not what the textbook says
    elsewhere. Not what you know about the subject.

If the sheet never has the class notice something, the child did not notice it,
however obvious it is to you. You are measuring what this sheet carries on its
own, because that is the only thing rewriting it can change.

TOPIC {index}: {topic}

WHAT THIS TOPIC MUST LEAVE THEM ABLE TO DO
    {gained}
WHAT MASTERING THIS IDEA ACTUALLY REQUIRES
    {mastery}
THE THINKING PATH IT WAS DESIGNED TO WALK
    {trajectory}
THE WRONG BELIEF IT WAS MEANT TO SURFACE
    {misconception}
WHAT THE NEXT TOPIC WILL BUILD ON
    {bridge}
THE TRANSFER TASK THIS PERIOD WAS PLANNED AROUND
    {transfer_task}

Those are the STANDARD, and they are fixed. Write your questions against them —
not against whatever this sheet happens to cover well. If the sheet taught
something other than the topic, its questions do not move to meet it.

THE TWO STANDARDS ARE NOT THE SAME STANDARD, and the difference decides two of
the four questions. The gain is what this period leaves. Mastery is what the idea
requires. A child can have the first and not the second — they can name the two
families printed on the page and guess at a third — and a sheet that produces
that child has taught the page rather than the idea.

  understanding  measured against the GAIN. The sheet's own situation.
  application    measured against MASTERY. A situation the sheet never used.

Where a planned transfer task is given above, the application question IS that
task, asked in your own words. Do not substitute your own: it was chosen before
this sheet existed, which is what makes it a measurement rather than a question
the prose can have been written to satisfy.

THE SHEET, as the teacher will run it:
{sheet}

WRITE ONE QUESTION PER DIMENSION. Each tests the fixed standard above; the sheet
supplies only the scene:

  understanding  {understanding_tests}
                 {understanding_scene}
  application    {application_tests}
                 {application_scene}
  misconception  {misconception_tests}
                 {misconception_scene}
  forward        {forward_tests}
                 {forward_scene}

Then answer each one in the child's own words, and judge that answer with ONE of
these four words:

  confident     answers it, and could say why
  unexplained   right answer, but could not explain it
  partial       a piece of it, or right for the wrong reason
  cannot        cannot answer from this sheet at all, or answers wrongly

Judge each dimension on its own. Four identical verdicts across a chapter would
mean six different lessons taught equally well in exactly the same way, which
does not happen — if you are about to write the same four words again, read the
sheet again first.

Where a verdict is "partial" or "cannot", diagnose it. Not "improve the content" — name what the
child could not do, what about THIS SHEET caused that, and which sections would
fix it. The rewrite receives only what you write here, so a vague diagnosis buys a
vague rewrite.

Return ONLY valid JSON, no markdown fences. Write each question BEFORE its answer:
{{
  "answers": {{
    "understanding": {{"question": "...", "childWouldSay": "...", "verdict": "one of the four words"}},
    "application":   {{"question": "...", "childWouldSay": "...", "verdict": "one of the four words"}},
    "misconception": {{"question": "...", "childWouldSay": "...", "verdict": "one of the four words"}},
    "forward":       {{"question": "...", "childWouldSay": "...", "verdict": "one of the four words"}}
  }},
  "diagnosis": [
    {{
      "dimension": "misconception",
      "failure": "what the child could not do, one sentence",
      "rootCause": "what about the sheet caused it — name the gap, not the symptom",
      "repairTarget": ["concept", "levelSet"]
    }}
  ]
}}

Sections you may name in repairTarget: {sections}.
Name only sections that would actually fix it — a diagnosis listing all six found
nothing. Where everything scored 0.7 or above, return an empty diagnosis list.
"""


def _sheet_text(material: dict) -> str:
    """The sheet as prose, section by section. What the child is judged on."""
    parts = []
    for section in SECTION_ORDER:
        body = section_text(material, section)
        if body.strip():
            parts.append(f"[{SECTION_LABELS[section]}]\n{body}")
    return "\n\n".join(parts)


def score(answers: dict, threshold: float = None, zero_fails: bool = True,
          gates: dict = None) -> dict:
    """Weighted total, the gates, and the zero floor — all computed here.

    The gates exist because an average is the wrong instrument for a critical
    failure: 0.88 overall while failing misconception resistance describes a sheet
    that has confidently taught a wrong idea, and a threshold on the mean waves it
    through. Forward readiness is deliberately ungated — the topic prepares for
    the next lesson rather than delivering it.

    THE ZERO FLOOR closes the hole the gates leave. `cannot` does not mean "did
    it badly"; it means the child cannot answer from this sheet at all. On an
    ungated dimension a weighted average lets that through whenever the other
    three carry it, and one real run shipped exactly that: application scored
    0.0 — the child answered "I don't know, we only traced matchboxes and books
    and bangles" — and the sheet passed at 0.750 against a 0.750 bar. The failure
    was diagnosed, recorded, and never repaired, because nothing was below the
    line.

    So any dimension at zero fails the sheet regardless of weights. It is kept
    separate from `failedGates` because it is a different kind of finding: a gate
    says "this fell below the standard", a floor says "this is not addressed".
    Turn it off with `config['zero_dimension_fails'] = False`.

    `gates` overrides the per-dimension bars declared in DIMENSIONS — a mapping of
    dimension name to a float, or to None to lift that gate entirely. Passed rather
    than read from config here so this function stays the pure scorer the CLI and
    the regression harness can call with no run around it; `_evaluate` below is
    where a run's config becomes a bar.
    """
    scores: dict[str, float] = {}
    for name in DIMENSIONS:
        raw = (answers or {}).get(name)
        entry = raw if isinstance(raw, dict) else {}
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict in VERDICTS:
            scores[name] = VERDICTS[verdict]
            continue
        # A numeric score is still accepted rather than discarded: an older cached
        # answer or a model that ignored the word list has still said something,
        # and throwing it away would score a judged sheet as if it failed.
        try:
            scores[name] = max(0.0, min(1.0, float(entry.get("score", raw))))
        except (TypeError, ValueError):
            scores[name] = 0.0

    bar = PASS_THRESHOLD if threshold is None else float(threshold)
    weighted = round(sum(scores[n] * DIMENSIONS[n]["weight"] for n in DIMENSIONS), 3)
    # `in` rather than truthiness, so an override of None genuinely lifts a gate
    # instead of falling back to the declared one.
    thresholds = {n: ((gates or {})[n] if n in (gates or {}) else spec["gate"])
                  for n, spec in DIMENSIONS.items()}
    failed_gates = [n for n in DIMENSIONS
                    if thresholds[n] is not None and scores[n] < thresholds[n]]
    unaddressed = ([n for n in DIMENSIONS if scores.get(n) == 0.0]
                   if zero_fails else [])
    passed = weighted >= bar and not failed_gates and not unaddressed

    if passed:
        reason = "passed"
    elif failed_gates:
        reason = f"gate failed: {', '.join(DIMENSIONS[n]['label'] for n in failed_gates)}"
    elif unaddressed:
        # Named ahead of the weighted total when both are true, because "the
        # sheet does not address this" is the actionable half and "0.75 is below
        # 0.75" is the arithmetic that follows from it.
        reason = (f"not addressed at all: "
                  f"{', '.join(DIMENSIONS[n]['label'] for n in unaddressed)}")
    else:
        reason = f"weighted {weighted} below {bar}"

    return {
        "scores": scores,
        "weighted": weighted,
        "failedGates": failed_gates,
        # The bars this verdict was actually measured against, stored with it. A
        # run made with `gate_transfer` off and one made with it on produce
        # different verdicts on identical prose, and without this the two are
        # indistinguishable in the history that selection reads.
        "gates": {n: t for n, t in thresholds.items() if t is not None},
        # Separate from failedGates on purpose: a gate says this fell below the
        # standard, a floor says this is missing. They want different rewrites.
        "unaddressed": unaddressed,
        "passed": passed,
        "reason": reason,
    }


async def _evaluate(state: ChapterState, spec: dict, material: dict,
                    threshold: float = None) -> dict:
    index = spec["index"]
    reasoning = spec.get("reasoning") or {}
    plan = (state.get("experience") or {}).get(index) or {}

    scene_fields = {}
    for name, dimension in DIMENSIONS.items():
        scene_fields[f"{name}_tests"] = dimension["tests"]
        scene_fields[f"{name}_scene"] = dimension["scene"]

    # From the reasoning slice first and the plan second, in that order, because
    # the plan carries a copy of the target rather than an opinion about it — and a
    # chapter whose experience window failed still has the audit.
    mastery = reasoning.get("masteryTarget") or plan.get("masteryTarget") or ""
    transfer_task = plan.get("transferTask") or ""

    prompt = _EVALUATION_PROMPT.format(
        grade=state.get("grade"),
        index=index, topic=spec["topic"],
        gained=reasoning.get("gained") or plan.get("inference") or "(not derived)",
        # Falls back to the gain rather than to "(not derived)". The two standards
        # collapsing into one is a real and correct answer for a drill topic, and
        # it is what a chapter cached before the mastery pass existed gets — the
        # measurement is then what it was before this change, which is the right
        # degradation. Telling the model the standard is missing invites it to
        # invent one, and an invented standard is graded against nothing.
        mastery=mastery or reasoning.get("gained")
        or plan.get("inference") or "(not derived)",
        trajectory=" -> ".join(plan.get("trajectory") or []) or "(not derived)",
        misconception=(reasoning.get("misconceptions") or ["(none flagged)"])[0],
        bridge=reasoning.get("bridgesTo") or "(end of the thread)",
        transfer_task=transfer_task
        or "(none planned — invent one the sheet never used)",
        sheet=_sheet_text(material),
        sections=", ".join(SECTION_ORDER),
        **scene_fields,
    )
    data = await call_json(
        prompt, label=f"learner[T{index}]", required=("answers",),
        temperature=0.2, max_tokens=2200)

    config = state.get("config") or {}
    answers = data.get("answers") or {}
    verdict = score(answers, threshold,
                    zero_fails=bool(config.get("zero_dimension_fails", True)),
                    # Lifted rather than lowered when it is turned off: a gate at
                    # 0.0 would still fail a `cannot`, which is the zero floor's
                    # job and not this flag's.
                    gates=None if config.get("gate_transfer", True)
                    else {"application": None})

    diagnosis = []
    for entry in (data.get("diagnosis") or []):
        if not isinstance(entry, dict) or not (entry.get("failure") or "").strip():
            continue
        targets = [s for s in (entry.get("repairTarget") or []) if s in SECTION_ORDER]
        diagnosis.append({
            "dimension": entry.get("dimension"),
            "failure": str(entry.get("failure")).strip()[:300],
            "rootCause": str(entry.get("rootCause") or "").strip()[:300],
            "repairTarget": targets,
        })

    return {
        **verdict,
        "index": index,
        # Kept with the verdict so a score can be argued with. A gate whose
        # question nobody can read back is a number, not a measurement.
        "questions": {n: (answers.get(n) or {}).get("question", "") for n in DIMENSIONS},
        "answers": answers,
        # Only diagnoses for dimensions that actually failed reach repair. A model
        # asked to diagnose will cheerfully diagnose something that scored 1.0.
        "diagnosis": [d for d in diagnosis
                      if verdict["scores"].get(d.get("dimension"), 1.0) < DIAGNOSIS_FLOOR],
    }


async def simulation_node(state: dict) -> dict:
    """Judge every generated sheet, and name the sections that need rewriting."""
    topics = state.get("topics") or []
    materials = state.get("materials") or {}
    config = state.get("config") or {}
    if not materials:
        return {"learner": {}, "metrics": {"learner_evaluated": 0}}

    by_index = {t["index"]: t for t in topics}
    previous = state.get("learner") or {}
    round_number = int(state.get("sim_round") or 0) + 1
    revision_of = {i: int(((materials[i] or {}).get("_meta") or {}).get("revision", 0))
                   for i in materials}

    # A sheet that already passed at this revision is not re-judged: the prose is
    # unchanged, so the second call buys the same verdict twice. A REPAIRED sheet
    # is always re-judged, questions and all, because the questions came from the
    # prose that just changed.
    pending = [i for i in sorted(materials) if i in by_index
               and not (previous.get(i, {}).get("passed")
                        and previous.get(i, {}).get("revision") == revision_of[i])]
    if not pending:
        return {}

    threshold = config.get("learner_pass_threshold")
    results = await gather_bounded(
        [_evaluate(state, by_index[i], materials[i], threshold) for i in pending],
        limit=int(config.get("concurrency", 4)))

    learner: dict[int, dict] = {}
    history: dict[int, list[dict]] = {}
    errors: list[str] = []
    for index, result in zip(pending, results):
        if isinstance(result, BaseException):
            # A sheet that could not be judged is not a sheet that failed. Calling
            # it failed would send it for a repair nobody diagnosed.
            errors.append(f"simulation: T{index}: {result}")
            continue
        learner[index] = {**result, "revision": revision_of[index]}
        history[index] = [_history_entry(learner[index], round_number)]

    failing = sorted(i for i, r in learner.items() if not r["passed"])
    # Targeted repair: only the sections a diagnosis actually named. With none
    # named, the whole sheet is fair game — but that is the fallback, not the norm.
    sections: dict[int, list[str]] = {}
    for index in failing:
        named = [s for d in learner[index]["diagnosis"] for s in d["repairTarget"]]
        if repair_surface.was_clamped(named):
            # Logged rather than absorbed: a diagnosis that keeps naming every
            # section says something about the evaluator, not the sheet.
            print(f"[prep_flow:simulation] T{index}: diagnosis named "
                  f"{len(set(named))} sections; rewriting at most "
                  f"{repair_surface.MAX_SECTIONS}")
        sections[index] = repair_surface.clamp(named)

    judged = {**previous, **learner}
    scored = [r["weighted"] for r in judged.values()]
    metrics = {
        "learner_evaluated": len(judged),
        "learner_passed": sum(1 for r in judged.values() if r["passed"]),
        "learner_mean_score": round(sum(scored) / len(scored), 3) if scored else None,
    }
    # Per dimension, because the mean hides WHICH one is failing — and that is the
    # difference between a prompt problem and a chapter problem.
    for name in DIMENSIONS:
        if judged:
            metrics[f"learner_mean_{name}"] = round(
                sum(r["scores"].get(name, 0) for r in judged.values()) / len(judged), 3)

    return {
        "learner": learner,
        "learner_history": history,
        "repair_targets": failing,
        "repair_sections": sections,
        "sim_round": round_number,
        "errors": errors,
        "metrics": metrics,
    }


def _history_entry(result: dict, sim_round: int) -> dict:
    """One judged round, kept as evidence rather than as an answer.

    Deliberately not the whole verdict. `answers` is the child's prose for four
    questions and is the largest thing this node produces, and nothing downstream
    clusters on it — it is read by a human arguing with one sheet, which the
    stored `learner` verdict already serves. The QUESTIONS stay: a recurring
    failure nobody can read the question for is a number again, which is the
    thing this whole module exists to stop being.
    """
    return {
        "simRound": sim_round,
        "revision": result.get("revision", 0),
        "passed": bool(result.get("passed")),
        "weighted": result.get("weighted"),
        "scores": dict(result.get("scores") or {}),
        "failedGates": list(result.get("failedGates") or []),
        "unaddressed": list(result.get("unaddressed") or []),
        "questions": dict(result.get("questions") or {}),
        "diagnosis": [dict(d) for d in (result.get("diagnosis") or [])],
    }


def _later_round_cleared(history: list[dict], position: int, dimension: str,
                         trigger: str) -> bool:
    """Did a later round actually fix THIS failure — which depends on what it was.

    A `threshold` row records a dimension that was never under the floor: the
    SHEET was under its bar, and this dimension was merely the weakest part of
    it. Asking whether it later cleared the floor is then trivially true on the
    first round it is checked, and marks a dimension "repaired" that did not move
    at all. Live data caught exactly that — application scoring 0.7 in round one
    and 0.7 again in round two, recorded as resolved. What resolves a threshold
    failure is the sheet passing, and nothing less.

    A `diagnosed` or `gate` row is the other case, and there the per-dimension
    reading is the right one: that dimension was genuinely below the floor, and
    bringing it back over is a fix whether or not the sheet as a whole passed.
    """
    for entry in history[position + 1:]:
        if trigger == "threshold":
            if entry.get("passed"):
                return True
        elif (entry.get("scores") or {}).get(dimension, 0) >= DIAGNOSIS_FLOOR:
            return True
    return False


def diagnosis_rows(history: list[dict], spec: dict = None) -> list[dict]:
    """One topic's judged rounds, flattened to one row per failed DIMENSION.

    The dimension is the unit because it is the thing that can recur. "T7 failed"
    says nothing that survives the chapter; "misconception resistance failed, and
    the root cause was that the examples state the rule without ever contrasting
    the wrong belief" is a sentence that can be true of forty topics across six
    chapters, and counting it is the entire point of keeping this.

    EVERY FAILED VERDICT LEAVES AT LEAST ONE ROW, and the `trigger` field says
    what put it there:

      diagnosed  the evaluator named this dimension and said why
      gate       a gate failed and the evaluator offered no diagnosis — itself a
                 finding, because a dimension the simulator keeps failing and
                 cannot explain is a prompt problem rather than a sheet problem
      zero       the dimension scored 0.0 — the sheet does not address it at all,
                 which fails the sheet whatever the weighted total says
      threshold  the sheet failed on the weighted bar with NO dimension under the
                 floor and no gate tripped, so the weakest dimension is recorded

    That last case is not hypothetical and it is not rare. Four dimensions all
    sitting exactly at the floor score 0.70 weighted, which is under the default
    bar of 0.75 — the sheet fails, `failedGates` is empty because 0.70 is not
    below 0.70, and the diagnosis filter drops everything for the same reason.
    Without the fallback that failure would be recorded nowhere, which is the one
    outcome this whole table exists to prevent.

    `resolvedByRepair` is decided PER DIMENSION rather than per sheet. A later
    round can pass overall while an ungated dimension is still under the floor,
    and marking that resolved would record a failure as fixed on the strength of
    a gate it was never measured by.
    """
    spec = spec or {}
    rows: list[dict] = []
    for position, entry in enumerate(history or []):
        if entry.get("passed"):
            continue
        scores = entry.get("scores") or {}
        gates = [g for g in (entry.get("failedGates") or []) if g in DIMENSIONS]
        zeros = [z for z in (entry.get("unaddressed") or []) if z in DIMENSIONS]
        diagnosed = {d.get("dimension"): d for d in (entry.get("diagnosis") or [])
                     if d.get("dimension") in DIMENSIONS}
        blamed = set(diagnosed) | set(gates) | set(zeros)
        if not blamed:
            # Failed the weighted bar with nothing under the floor. Record what
            # was weakest — several dimensions when they tie, because picking one
            # arbitrarily would invent a culprit the verdict never named.
            weakest = min((scores.get(d, 0.0) for d in DIMENSIONS), default=None)
            blamed = {d for d in DIMENSIONS if scores.get(d, 0.0) == weakest}
        for dimension in sorted(blamed, key=list(DIMENSIONS).index):
            found = diagnosed.get(dimension) or {}
            trigger = ("diagnosed" if dimension in diagnosed
                       else "gate" if dimension in gates
                       else "zero" if dimension in zeros else "threshold")
            rows.append({
                "index": spec.get("index"),
                "topic": spec.get("topic"),
                "subtopic": spec.get("subtopic"),
                "simRound": entry.get("simRound"),
                "revision": entry.get("revision"),
                "dimension": dimension,
                "score": scores.get(dimension),
                "weighted": entry.get("weighted"),
                # Whether this dimension is one of the two that can fail a sheet
                # on its own. Stored rather than looked up later: the weights and
                # gates are tunable, and a row should say what was true when it
                # was written.
                "gated": dimension in gates,
                # What put this row here — see the docstring. Stage-2 clustering
                # should not weigh a named root cause and an inferred weakest
                # dimension as the same kind of evidence.
                "trigger": trigger,
                "failure": found.get("failure"),
                "rootCause": found.get("rootCause"),
                "affectedSections": list(found.get("repairTarget") or []),
                "question": (entry.get("questions") or {}).get(dimension),
                "resolvedByRepair": _later_round_cleared(
                    history, position, dimension, trigger),
            })
    return rows


def diagnosis_block(result: dict) -> str:
    """One sheet's failure, rendered for the repair prompt.

    The spec's point, and it is a good one: "Score: 62%, improve content" is not
    something a rewrite can act on. What failed, why the sheet caused it, and which
    sections to touch — that is.
    """
    if not result or result.get("passed"):
        return ""
    lines = [
        "",
        "A SIMULATED LEARNER READ THIS SHEET AND WAS TESTED ON IT. It did not pass:",
        f"  weighted {result.get('weighted')} — {result.get('reason')}",
    ]
    for name, dimension in DIMENSIONS.items():
        got = (result.get("scores") or {}).get(name)
        if got is None:
            continue
        mark = "FAILED" if name in (result.get("failedGates") or []) else "  ok  "
        asked = (result.get("questions") or {}).get(name) or ""
        lines.append(f"  [{mark}] {dimension['label']}: {got}"
                     + (f"  — asked: {asked}" if asked and got < 0.7 else ""))
    for entry in result.get("diagnosis") or []:
        lines += [
            "",
            f"  {entry.get('dimension')}: {entry.get('failure')}",
            f"    why this sheet caused it: {entry.get('rootCause')}",
            f"    fix in: {', '.join(entry.get('repairTarget') or []) or '(unspecified)'}",
        ]
    lines += [
        "",
        "Rewrite ONLY the sections named above. Reproduce every other section",
        "exactly as it stands — it already passed, and a rewrite that fixes the",
        "Concept and breaks the Challenge is not progress.",
    ]
    return "\n".join(lines)
