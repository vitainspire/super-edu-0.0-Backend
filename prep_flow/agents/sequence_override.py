"""Sequence Override — lets a teacher present a chapter's topics in a
different order than the book's, without pretending the underlying
dependency chain doesn't exist.

Two orders, never merged into one:

  * CANONICAL order — what sequencing and reasoning already produced. Concept
    stays anchored to it permanently: page citations and figure placement are
    facts about where the book printed something, not about teaching order,
    and nothing here moves them.
  * TEACHER order — the sequence a teacher actually wants to walk the class
    through. This module reconciles the two. It does not re-sequence, re-reason,
    or touch Concept/Real Life/Challenge/Level Set — only the Refresher, the
    one section whose entire job is "pick up where we left off", gets rewritten
    to say what was ACTUALLY just taught, not what the book's neighbouring
    topic happens to be.

Reuses the same dependency-satisfaction test the validator's knowledge-chain
check uses (bridgesTo vs. assumes, root-word overlap) rather than inventing a
second one — "does what came before actually support this topic" is one
question, and it should have one answer whichever order it is asked about.

TWO HALVES, AND ONLY ONE OF THEM IS NODE 1's.

  `classify_transitions` and `resequence_contract` ask whether a teaching order
  is supported by the knowledge chain. That is a question about the chain, the
  chain is Node 1's, and both run here with no material and no model call.

  `resequence` also RESEAMS: it rewrites the Refresher of every topic whose
  predecessor changed, which needs the generated sheets and a model call. It
  stays in this file because the two halves share `classify_transitions` and
  splitting them would duplicate it — but nothing in Node 1 calls it, and it
  will be called from wherever the generation stage lands downstream of Node 2.
  It is unchanged and still correct; it just needs material to be given any.

`_roots` NOW COMES FROM `clauses.py` rather than from the validator. It always
was `clauses.roots` — validation aliased it — and the validator left this
package with the node split, so this reaches for the original.
"""
from typing import Optional

from ..clauses import roots as _roots
from ..llm import call_json
from ..sections import handoff_summary

SAFE, ADAPT, WARN = "safe", "adapt", "warn"

# An `assumes` clause this short (after stopword/short-word stripping) doesn't
# carry enough content to score meaningfully — treating it as satisfied is the
# safe direction, since the alternative is flagging every topic whose assumes
# clause was written thin.
_MIN_ASSUMES_ROOTS = 1


def _has_support(assumes: str, gained_so_far: list[str]) -> bool:
    wanted = _roots(assumes)
    if len(wanted) < _MIN_ASSUMES_ROOTS:
        return True
    covered: set[str] = set()
    for gained in gained_so_far:
        covered |= _roots(gained)
    return bool(wanted & covered)


def classify_transitions(reasoning: dict, canonical_order: list[int],
                         teacher_order: list[int]) -> dict[int, dict]:
    """One verdict per topic in the teacher's order: SAFE (nothing to patch),
    ADAPT (a real gap, small enough to plant in one Refresher bullet), or WARN
    (several canonical predecessors are missing — flagged, never hidden).

    The chain has no notion of "how much" is missing, only free-text `assumes`
    and `gained` — so the gap is SIZED by counting canonical predecessors not
    yet covered in the teacher's order, rather than inventing a discrete
    concept graph the reasoning agent was never asked to build.
    """
    chain = (reasoning or {}).get("chain") or {}
    canonical_pos = {idx: pos for pos, idx in enumerate(canonical_order)}

    verdicts: dict[int, dict] = {}
    gained_so_far: list[str] = []
    covered: set[int] = set()

    for position, index in enumerate(teacher_order):
        entry = chain.get(index) or {}
        assumes = entry.get("assumes") or ""
        gained = entry.get("gained") or ""

        if position == 0 or _has_support(assumes, gained_so_far):
            verdicts[index] = {"verdict": SAFE, "assumes": assumes, "missing": []}
        else:
            my_pos = canonical_pos.get(index, position)
            missing = [i for i in canonical_order[:my_pos] if i not in covered]
            verdicts[index] = {
                "verdict": ADAPT if len(missing) <= 1 else WARN,
                "assumes": assumes,
                "missing": missing,
            }

        covered.add(index)
        if gained:
            gained_so_far.append(gained)

    return verdicts


def resequence_contract(document: dict, teacher_order: list[int]) -> dict:
    """Reorder a CONTRACT for a teacher's chosen sequence, and say what it costs.

    The half of `resequence` below that Node 1 can answer. No material, no model
    call: it reorders the contract's topics and reports one finding per
    transition the knowledge chain does not support.

    WHAT IT DELIBERATELY DOES NOT DO is plant the bridge. `resequence` writes a
    short bridge into the Refresher of every ADAPT transition, and there is no
    Refresher yet — the sheet is written downstream of Node 2. So this states
    the gap and hands it on: an ADAPT verdict here is an instruction to whoever
    writes that Refresher, carried on the contract rather than acted on.

    Raises ValueError on an order naming topics this contract does not have, for
    the same reason `resequence` does — a silently dropped topic is a chapter
    the teacher did not ask for.
    """
    rows = {int(r.get("index")): r for r in (document.get("topics") or [])
            if r.get("index") is not None}
    canonical_order = [int(r.get("index")) for r in (document.get("topics") or [])
                       if r.get("index") is not None]

    unknown = [i for i in teacher_order if i not in rows]
    if unknown:
        raise ValueError(f"not real topics in this chapter: {unknown}")
    if len(set(teacher_order)) != len(teacher_order):
        raise ValueError("teacher_order repeats a topic")

    # Rebuilt into the chain shape `classify_transitions` reads, rather than
    # passing the contract through: the contract states each topic's link under
    # `knowledgeChain`, and the classifier wants them keyed by index. One shape
    # conversion here beats a second classifier that reads contracts.
    reasoning = {"chain": {index: row.get("knowledgeChain") or {}
                           for index, row in rows.items()}}
    verdicts = classify_transitions(reasoning, canonical_order, teacher_order)

    findings = []
    for index in teacher_order:
        verdict = verdicts[index]
        if verdict["verdict"] == SAFE:
            continue
        missing_titles = [rows.get(i, {}).get("topic", f"T{i}")
                          for i in verdict["missing"]]
        findings.append({
            "section": None,
            "severity": "advisory",
            "check": "sequence_override",
            "message": (
                f"T{index} ({rows[index].get('topic')}) assumes "
                f"\"{verdict['assumes'][:70]}\" — in this teaching order, "
                + (f"{', '.join(missing_titles)} hasn't been taught yet"
                   if missing_titles else "that hasn't been fully established yet")
                + f". Verdict: {verdict['verdict'].upper()}"
                + (" — the Refresher written for it must plant a short bridge."
                   if verdict["verdict"] == ADAPT else
                   " — several prerequisites are missing; one Refresher bridge "
                   "will not cover them. Consider covering the others first or "
                   "budgeting extra time.")
            ),
        })

    reordered = [rows[i] for i in teacher_order]
    return {
        # A new document rather than a mutation: the caller usually still wants
        # the book's own order to compare against, and `integrity` must keep
        # describing the same topics whatever order they are listed in.
        "contract": {**document, "topics": reordered,
                     "teachingOrder": list(teacher_order),
                     "canonicalOrder": canonical_order},
        "findings": findings,
        "verdicts": {i: v["verdict"] for i, v in verdicts.items()},
    }


_REFRESHER_RULE = (
    "Built ENTIRELY from what the class did in the PREVIOUS period — bullet 1 "
    "brings back that scene or object by name so students recognise it "
    "instantly, bullet 2 has pairs answer the open question it left hanging, "
    "bullet 3 turns that answer into the doorway to today's topic. Never "
    "introduce a new example here — if it was not in that previous period, it "
    "does not belong in the Refresher."
)

_REFRESHER_PROMPT = """Rewrite ONLY the Refresher for one period. The teacher is presenting this
chapter's topics in a different order than the book's, so the Refresher must
open on what the class ACTUALLY just did — not the book's neighbouring topic.

TOPIC NOW: {topic}

WHAT THE CLASS ACTUALLY JUST DID (the real previous period, in the teacher's
own order):
{previous_summary}
{bridge_note}
RULE FOR THIS SECTION: {rule}

Return ONLY valid JSON, no markdown fences:
{{
  "recap": [
    {{"text": "6-12 word headline", "detail": "1-2 sentences: the exact words the teacher says (quoted), how long to wait, how students respond, what to watch for"}}
  ]
}}
Exactly 3 items in "recap".
"""


def _bridge_note(verdict: dict, topics_by_index: dict) -> str:
    if verdict["verdict"] == SAFE or not verdict["assumes"]:
        return ""
    missing_titles = ", ".join(
        topics_by_index.get(i, {}).get("topic", f"T{i}") for i in verdict["missing"][:3]
    ) or "an earlier idea"
    return (
        f"\nTHE GAP TO BRIDGE: this topic assumes the class already has "
        f"\"{verdict['assumes']}\" — in this teaching order, that has not been "
        f"explicitly covered yet (normally comes from: {missing_titles}). Before "
        f"moving into today's topic, use ONE bullet to briefly and concretely "
        f"plant just enough of that idea for today's lesson to make sense — "
        f"do not teach a full lesson on it, just enough to stand on.\n"
    )


async def _regenerate_refresher(topic_spec: dict, bridge_note: str,
                                previous_material: Optional[dict],
                                previous_topic: str) -> dict:
    """One Refresher, rewritten for the teacher's actual previous period.
    Never raises — on failure the caller keeps the original Refresher, which
    is wrong for the teacher's order but was at least a complete section
    rather than a missing one."""
    previous_summary = (
        handoff_summary(previous_material, previous_topic) if previous_material
        else f'(no material was generated for the preceding topic, "{previous_topic}")'
    )
    prompt = _REFRESHER_PROMPT.format(
        topic=topic_spec.get("topic") or "",
        previous_summary=previous_summary,
        bridge_note=bridge_note,
        rule=_REFRESHER_RULE,
    )
    try:
        data = await call_json(prompt, label="sequence-override-refresher",
                               required=("recap",), temperature=0.4, max_tokens=900)
    except Exception as exc:
        print(f"[prep_flow:sequence_override] T{topic_spec.get('index')} "
              f"refresher regeneration failed: {exc}")
        return {}
    recap = data.get("recap")
    if not isinstance(recap, list) or not recap:
        return {}
    return {"previousTopicRefresher": {"recap": recap[:3]}}


async def resequence(topics: list[dict], materials: dict, reasoning: dict,
                     teacher_order: list[int]) -> dict:
    """Reorder a GENERATED chapter for the teacher's chosen sequence and reseam it.

    NOT CALLED FROM NODE 1 — it needs the sheets, and Node 1 writes none. Use
    `resequence_contract` above to reorder a contract; call this from wherever
    the generation stage lands downstream of Node 2, with the material it
    produced.

    Concept/Real Life/Challenge/Level Set are untouched — they are anchored to
    their own pages and their own content, never to teaching order. Only the
    Refresher is rewritten, for every topic whose predecessor changed from the
    book's. Returns the topics list IN THE TEACHER'S ORDER (render_chapter
    iterates whatever order it is given, for both the Contents table and the
    sheets themselves, so reordering this list is the entire rendering change
    needed) plus the patched materials and one finding per non-SAFE transition.
    """
    topics_by_index = {t["index"]: t for t in topics}
    canonical_order = [t["index"] for t in topics]

    unknown = [i for i in teacher_order if i not in topics_by_index]
    if unknown:
        raise ValueError(f"not real topics in this chapter: {unknown}")
    if len(set(teacher_order)) != len(teacher_order):
        raise ValueError("teacher_order repeats a topic")

    verdicts = classify_transitions(reasoning, canonical_order, teacher_order)
    patched_materials = dict(materials)
    findings: list[dict] = []

    for position, index in enumerate(teacher_order):
        verdict = verdicts[index]

        if position > 0:
            predecessor_index = teacher_order[position - 1]
            predecessor_material = materials.get(predecessor_index)
            predecessor_topic = topics_by_index.get(predecessor_index, {}).get("topic", "")
            note = _bridge_note(verdict, topics_by_index)
            patch = await _regenerate_refresher(
                topics_by_index[index], note, predecessor_material, predecessor_topic)
            if patch and index in patched_materials:
                patched_materials[index] = {**patched_materials[index], **patch}

        if verdict["verdict"] != SAFE:
            missing_titles = [topics_by_index.get(i, {}).get("topic", f"T{i}")
                              for i in verdict["missing"]]
            findings.append({
                "section": None,
                "severity": "advisory",
                "check": "sequence_override",
                "message": (
                    f"T{index} ({topics_by_index[index]['topic']}) assumes "
                    f"\"{verdict['assumes'][:70]}\" — in this teaching order, "
                    + (f"{', '.join(missing_titles)} hasn't been taught yet"
                       if missing_titles else "that hasn't been fully established yet")
                    + f". Verdict: {verdict['verdict'].upper()}"
                    + (" — a short bridge was added to the Refresher."
                       if verdict["verdict"] == ADAPT else
                       " — several prerequisites are missing; the Refresher plants "
                       "one bridge, but consider covering the others first or "
                       "budgeting extra time.")
                ),
            })

    reordered_topics = [topics_by_index[i] for i in teacher_order]
    return {
        "topics": reordered_topics,
        "materials": patched_materials,
        "findings": findings,
        "verdicts": {i: v["verdict"] for i, v in verdicts.items()},
    }
