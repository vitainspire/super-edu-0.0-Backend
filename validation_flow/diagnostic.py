"""The diagnostic-thinking gate: where a real gap was named for this topic,
does the sheet actually give the teacher a response for it?

WHY THIS EXISTS. Every other check in this node asks a question about the
sheet in isolation. This one is different -- it checks whether the sheet
DELIVERED on something the pipeline already promised itself it would: Node 1's
own audit (or the textbook critique layer) names one specific gap a topic
should close, and generation is told to close it. This session's whole
history of testing that promise -- the `floor` field, the textbook-critique
injection -- found the same failure every time: the gap reaches the prompt,
correctly, and the written sheet still doesn't contain an if-then response
for it. That is not a maybe. It is the single most repeated finding across
every quality-judge run this session. This gate is what makes it blocking
instead of something only an offline experiment ever measured.

ONLY JUDGES TOPICS WHERE A GAP WAS ACTUALLY NAMED. A topic with nothing to
close has nothing for this gate to check -- silence there is correct, not a
skip. See `experience.closesGap` (adapter.py) for where that comes from.

FINDINGS, NOT REWRITES. Same contract as realism.py and learner.py: this
judges, `graph.py` merges it into the shared issues/repair_targets shape,
and Generation's repair pass is what actually rewrites anything.
"""
from prep_flow.llm import call_json, gather_bounded
from prep_flow.sections import SECTION_ORDER, section_text

_DIAGNOSTIC_PROMPT = """A gap was identified for this topic before it was written -- a specific wrong
idea a child might hold, and what the teacher should do about it. Check
ONLY whether the finished sheet actually delivers that response, not
whether the sheet is otherwise good.

TOPIC: {topic}
THE GAP THAT WAS SUPPOSED TO BE CLOSED: {gap}
WHAT WAS SUPPOSED TO CLOSE IT: {gap_closer}

THE SHEET:
{sheet}

Does the sheet contain an explicit IF-THEN response for this specific gap --
"if a child says/thinks X, the teacher does Y" -- concrete, naming the actual
wrong idea and the actual teacher response? Not a mention of the right idea
near the gap, not a general "address misconceptions" note, not the main
explanation repeated -- an actual named response to the wrong answer.

Return ONLY valid JSON:
{{"delivered": true or false,
  "section": "{sections}" or "" if you cannot tell which section should have carried it,
  "reason": "one sentence -- quote what the sheet actually says, or state plainly that nothing addresses it"}}"""


async def _judge_one(index: int, gap_row: dict, material: dict, topic: str) -> tuple:
    parts = [f"{s}: {t}" for s in SECTION_ORDER if (t := section_text(material, s))]
    sheet = "\n\n".join(parts) if parts else "(the sheet is empty)"
    prompt = _DIAGNOSTIC_PROMPT.format(
        topic=topic, gap=gap_row.get("gap") or "", gap_closer=gap_row.get("gapCloser") or "",
        sheet=sheet, sections=" | ".join(SECTION_ORDER),
    )
    data = await call_json(
        prompt, label=f"diagnostic[T{index}]", required=("delivered",),
        temperature=0.1, max_tokens=600)
    return index, data


async def run(adapted: dict) -> dict:
    """Judge only the topics that named a real gap. Same return shape as
    realism.py / learner.py, so graph.py merges it without special-casing."""
    materials = adapted.get("materials") or {}
    experience = adapted.get("experience") or {}
    config = adapted.get("config") or {}
    rows = {int(r["index"]): r for r in (adapted.get("contract", {}).get("topics") or [])
            if r.get("index") is not None}

    pending, indices = [], []
    for index in sorted(materials):
        material = materials.get(index)
        gap_row = experience.get(index) or {}
        if not isinstance(material, dict):
            continue
        if not (gap_row.get("gap") and gap_row.get("gapCloser")):
            continue  # nothing named for this topic -- nothing to check
        topic = (rows.get(index) or {}).get("topic") or f"topic {index}"
        indices.append(index)
        pending.append(_judge_one(index, gap_row, material, topic))

    if not pending:
        return {"issues": {}, "repair_targets": [], "repair_sections": {},
                "errors": [], "metrics": {"diagnostic_gate_judged": 0, "diagnostic_gate_named": 0}}

    results = await gather_bounded(pending, limit=int(config.get("concurrency", 4)))

    issues: dict[int, list] = {}
    sections: dict[int, list] = {}
    targets: list[int] = []
    errors: list[str] = []
    judged = delivered_count = 0

    for index, result in zip(indices, results):
        if isinstance(result, Exception):
            errors.append(f"diagnostic[T{index}]: {result}")
            continue
        judged += 1
        _, data = result
        if data.get("delivered"):
            delivered_count += 1
            continue
        section = str(data.get("section") or "").strip()
        section = section if section in SECTION_ORDER else "concept"
        reason = str(data.get("reason") or "").strip()
        issues.setdefault(index, []).append({
            "section": section,
            "message": f"The gap named for this topic was never given an if-then "
                       f"response in the sheet. {reason} Add: 'if a child says/thinks "
                       f"[the wrong idea], the teacher [the specific response]'.",
            "severity": "blocking",
            "check": "diagnostic_thinking",
        })
        targets.append(index)
        sections[index] = [section]

    return {
        "issues": issues,
        "repair_targets": sorted(set(targets)),
        "repair_sections": sections,
        "errors": errors,
        "metrics": {
            "diagnostic_gate_judged": judged,
            "diagnostic_gate_named": len(pending),
            "diagnostic_gate_delivered": delivered_count,
        },
    }
