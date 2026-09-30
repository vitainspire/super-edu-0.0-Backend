"""The classroom-reality gate: could THIS sheet actually run, in THIS room?

WHY THIS EXISTS SEPARATELY FROM THE LEARNER GATE. `learner.py` asks whether a
child could get to the idea from this sheet — a question about the teaching.
This one asks whether the period could happen at all in the room it is written
for, which is a different question with a different failure mode. A sheet can
teach beautifully and still be unrunnable: it asks for a photocopy per child in
a school with no photocopier, it frames the idea around an escalator for
children who have never seen one, or it spends its Real Life minutes on a
tangent that pulls thirty children off the topic they came to learn.

THE ROOM THIS JUDGES AGAINST is a rural Indian government school:

  * one teacher, 30-60 children, often more than one grade in the room
  * a blackboard and chalk, and the children's own textbook — that is the
    reliable equipment list. No projector, no printer, no photocopies, no
    internet, no laboratory, no per-child worksheet, no per-group stationery.
  * what is genuinely free and to hand: stones, sticks, leaves, seeds, bottle
    caps, string, chalk, slates, and whatever children bring from home
  * a period of roughly 35-40 minutes, interrupted
  * children whose English is well behind their grade level, and whose world is
    the village, the field, the market and the kitchen

FINDINGS, NOT REWRITES. Like every other pass in this node, this one only
judges: it returns findings in the same shape `checks.py` produces, they land in
the same per-topic `issues` bucket, and `verdict._topic_verdict` turns a blocking
one into `needs_review`. Nothing here edits a sheet.

BLOCKING IS RESERVED FOR "THIS PERIOD CANNOT RUN AS WRITTEN." A sheet that would
merely be better with a different example is advisory — the bar for withholding
material from teachers is that it fails them in the room, not that it could be
improved.
"""
from typing import Optional

from prep_flow.llm import call_json, gather_bounded
from prep_flow.sections import SECTION_LABELS, SECTION_ORDER, section_text

# The dimensions judged, and what a failure in each one means. Kept as data
# rather than prose in the prompt so the finding's `check` value and the thing
# the model was asked about cannot drift apart.
DIMENSIONS = {
    "materials": "needs something this school does not have",
    "classroom": "cannot run with one teacher and 30-60 children in a fixed-bench room",
    "language": "language or abstraction is above what this grade can follow",
    "context": "uses a setting or object these children have not met",
    "focus": "spends the period's minutes on something other than the topic",
}

_REALISM_PROMPT = """You are a senior government-school teacher trainer in rural India. You have
taught Grade {grade} yourself, in a village school, with sixty children and one
blackboard. A prep sheet has been written for one period. Judge ONLY whether it
could actually run in that room, and whether those children would come out
understanding the topic.

THE ROOM, and it is not negotiable:
- one teacher, 30-60 children, sometimes two grades at once, fixed benches
- a blackboard, chalk, and every child's own copy of the textbook
- NO projector, printer, photocopier, internet, laboratory, or per-child worksheet
- free and to hand: stones, sticks, leaves, seeds, bottle caps, string, slates,
  and things children bring from home
- about 35-40 minutes, and it will be interrupted
- the children's English is well behind Grade {grade}; their world is the
  village, the field, the market, the kitchen

TOPIC: {topic}
OBJECTIVE AS WRITTEN: {objective}
MATERIALS THE SHEET ASKS FOR: {materials}

THE SHEET:
{sheet}

Judge these five things, and nothing else:

1. MATERIALS — does it need anything the school does not have? Photocopies,
   printed cards per group, chart paper, scissors or colours per child, a
   projector, or "prepare X beforehand" that costs the teacher money or an
   evening. Things children can bring from home, or that cost nothing, are fine.
2. CLASSROOM — can ONE teacher run this with 30-60 children on fixed benches?
   Group work that needs the room rearranged, anything needing the teacher in
   two places, or anything that only works with a small class, fails here.
3. LANGUAGE — is every instruction and idea within reach of a Grade {grade}
   child whose English is weak? Flag abstract words, long sentences and
   technical terms the book itself did not introduce.
4. CONTEXT — is every example something these children have actually seen? A
   mall, escalator, swimming pool, pizza, aquarium or family car is not.
   A bullock, a well, a ration shop, a bus stand, a paddy field is.
5. FOCUS — does every part of the period serve the stated objective? Flag
   tangents, a second idea smuggled in, or a story that entertains without
   teaching. Thirty children pulled off the topic do not come back.

Return ONLY valid JSON:
{{
  "runnable": true or false,
  "findings": [
    {{
      "dimension": "materials" | "classroom" | "language" | "context" | "focus",
      "section": "{sections}" or "" if it is the whole sheet,
      "severity": "blocking" or "advisory",
      "problem": "what specifically fails, naming the thing — one sentence",
      "fix": "the smallest change that would make it run here — one sentence"
    }}
  ]
}}

SEVERITY. "blocking" means this period CANNOT run as written in that room, or
the children would not understand it. "advisory" means it would run, but a
better choice exists. Be strict about blocking and honest about advisory: a
sheet with nothing wrong returns "findings": [] and runnable true, and that is a
normal answer, not a failure to find something."""


def _sheet_text(material: dict) -> str:
    """The sheet as the trainer reads it — section by section, prose only."""
    parts = []
    for section in SECTION_ORDER:
        text = section_text(material, section)
        if text:
            parts.append(f"{SECTION_LABELS[section]}: {text}")
    return "\n\n".join(parts) if parts else "(the sheet is empty)"


async def _judge_one(index: int, topic_row: dict, material: dict, grade: str) -> tuple:
    materials_used = material.get("materialsUsed") or []
    prompt = _REALISM_PROMPT.format(
        grade=grade or "5",
        topic=topic_row.get("topic") or f"topic {index}",
        objective=material.get("objective") or "(none stated)",
        materials=", ".join(str(m) for m in materials_used) or "(none listed)",
        sheet=_sheet_text(material),
        sections=" | ".join(SECTION_ORDER),
    )
    data = await call_json(
        prompt, label=f"realism[T{index}]", required=("findings",),
        temperature=0.1, max_tokens=1600)
    return index, data


async def run(adapted: dict) -> dict:
    """Judge every sheet against the room. Returns the same shape the other
    passes return, so `graph.py` can merge it without special-casing.

    One call per sheet, bounded by the node's own concurrency setting. A topic
    whose call fails is recorded in `errors` and simply contributes no findings
    — the gate going quiet must not be mistaken for the gate passing, which is
    why `metrics.realism_gate_judged` counts what was actually judged.
    """
    contract = adapted.get("contract") or {}
    materials = adapted.get("materials") or {}
    config = adapted.get("config") or {}
    grade = str(contract.get("grade") or adapted.get("grade") or "")

    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}

    pending, indices = [], []
    for index in sorted(materials):
        material = materials.get(index)
        if not isinstance(material, dict):
            continue
        indices.append(index)
        pending.append(_judge_one(index, rows.get(index) or {}, material, grade))

    if not pending:
        return {"issues": {}, "repair_targets": [], "repair_sections": {},
                "errors": [], "metrics": {"realism_gate_judged": 0}}

    results = await gather_bounded(pending, limit=int(config.get("concurrency", 4)))

    issues: dict[int, list] = {}
    sections: dict[int, list] = {}
    targets: list[int] = []
    errors: list[str] = []
    judged = blocking_count = advisory_count = 0

    for index, result in zip(indices, results):
        if isinstance(result, Exception):
            errors.append(f"realism[T{index}]: {result}")
            continue
        judged += 1
        _, data = result
        named_sections: set = set()
        for raw in (data.get("findings") or []):
            if not isinstance(raw, dict):
                continue
            dimension = str(raw.get("dimension") or "").strip().lower()
            if dimension not in DIMENSIONS:
                dimension = "focus"
            severity = "blocking" if str(raw.get("severity")).lower() == "blocking" else "advisory"
            problem = str(raw.get("problem") or "").strip()
            if not problem:
                continue
            fix = str(raw.get("fix") or "").strip()
            section = str(raw.get("section") or "").strip()
            section = section if section in SECTION_ORDER else None

            issues.setdefault(index, []).append({
                "section": section,
                "message": f"{problem} Fix: {fix}" if fix else problem,
                "severity": severity,
                "check": f"realism_{dimension}",
            })
            if severity == "blocking":
                blocking_count += 1
                targets.append(index)
                if section:
                    named_sections.add(section)
            else:
                advisory_count += 1
        if named_sections:
            sections[index] = sorted(named_sections)

    return {
        "issues": issues,
        "repair_targets": sorted(set(targets)),
        "repair_sections": sections,
        "errors": errors,
        "metrics": {
            "realism_gate_judged": judged,
            "realism_blocking": blocking_count,
            "realism_advisory": advisory_count,
        },
    }
