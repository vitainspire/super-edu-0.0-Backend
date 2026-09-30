"""Context tools - architecture section 17(D). Node 2's window onto the room.

THE STRONGEST RULE IN THIS FILE IS ABOUT WHAT IS *NOT* HERE. Section 11:
inactive factors never enter the prompt, and activation happens
deterministically in code BEFORE the agent is built
(`context_flow/activation.py`). These tools serve what that computation left
standing and nothing else. There is no tool for "list all twenty factors" and no
tool for "read this profile field" - either one would hand the model back the
ability to personalise from data the deterministic layer already ruled
inadmissible, which is precisely the invention this design exists to prevent.

PROVENANCE TRAVELS WITH EVERY VALUE. A field is `verified`, `assumed` or absent,
and the difference decides what may be built on it: a LOCAL factor may only be
proposed from verified evidence, because "the model believed the village grows
rice" and "an administrator recorded that the village grows rice" produce the
same sentence and are not the same claim. `context_flow/gate.py` re-checks this
deterministically afterwards; these tools make it visible while the reasoning is
still happening, which is cheaper than being refused.

THE CONTRACT IS READ-ONLY AND THERE IS NO TOOL TO CHANGE IT. Sections 9 and 12:
Node 2 produces a patch. `permissions.py` blocks the file path; this toolset
simply offers no verb.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from context_flow import factors as factors_module

from .runtime import MAX_ROWS, RunContext, truncated


def _field_line(name: str, field: dict) -> str:
    provenance = field.get("provenance", "unknown")
    stale = " [STALE]" if field.get("stale") else ""
    origin = field.get("origin") or "?"
    return f"  {name} = {field.get('value')!r} ({provenance}, from {origin}){stale}"


def _evidence_of(*factor_ids: str) -> tuple:
    """The profile fields those factors read, straight from the registry.

    DERIVED, NOT LISTED, and the first version of this file got it wrong by
    listing them: `language_proficiency` and `classroom_layout` were plausible
    names that no factor actually uses, so the language tool reported "no
    language data recorded" for a room whose `medium_of_instruction` was on file.

    The registry is also what `activation.py` and `gate.py` read, so deriving
    from it is what keeps the three of them describing the same profile.
    """
    names: list[str] = []
    for factor in factors_module.FACTORS:
        if factor.id in factor_ids:
            names.extend(n for n in factor.evidence if n not in names)
    return tuple(names)


# The room's physical facts, and the language in it. Named by factor rather than
# by field so that adding an evidence field to a factor reaches these tools.
_RESOURCE_FIELDS = _evidence_of("resources", "technology", "classroom")
_LANGUAGE_FIELDS = _evidence_of("language")


def build(ctx: RunContext) -> list[BaseTool]:
    """Node 2's tools, bound to one contract and one normalised profile."""

    fields: dict = (ctx.profile or {}).get("fields") or {}

    @tool
    def get_academic_contract(topic_index: int) -> str:
        """What Node 1 decided this topic must land. READ ONLY - you may not change it.

        Returns the mastery target, the required concepts, the competencies and
        the textbook grounding for one topic. Everything you propose must leave
        all of it intact: you are adapting how the lesson is DELIVERED, never
        what it teaches or what a child must end up able to do.
        """
        topics = (ctx.contract or {}).get("topics") or []
        row = next((t for t in topics if int(t.get("index", -1)) == int(topic_index)), None)
        if row is None:
            available = ", ".join(str(t.get("index")) for t in topics[:MAX_ROWS])
            return f"No topic {topic_index} in this contract. Available: {available}"

        academic = row.get("academic") or {}
        grounding = row.get("grounding") or {}
        experience = row.get("experience") or {}
        return truncated(
            f"T{row.get('index')} - {row.get('topic')} / {row.get('subtopic', '')}\n"
            f"MASTERY TARGET (immovable): {academic.get('masteryTarget') or '(none stated)'}\n"
            f"Required concepts: {', '.join(academic.get('concepts') or []) or '(none)'}\n"
            f"Competencies: {', '.join(academic.get('competencies') or []) or '(none)'}\n"
            f"Vocabulary: {', '.join(academic.get('vocabulary') or []) or '(none)'}\n"
            f"Cognitive trajectory: {experience.get('trajectory') or '(none)'}\n"
            f"Anchor object: {experience.get('anchor') or '(none)'}\n"
            f"Textbook pages: {grounding.get('pageStart')} to {grounding.get('pageEnd')}\n"
        )

    @tool
    def get_active_context(topic_index: int) -> str:
        """The context factors that are ACTIVE and PROPOSABLE for this topic,
        with the evidence each one is allowed to draw on.

        This is the complete set you may propose adaptations from. A factor not
        listed here was ruled out deterministically - because the room has no
        verified evidence for it, or because it is irrelevant to this topic -
        and proposing from it will be rejected by the gate.
        """
        summary = (ctx.activation or {}).get(int(topic_index)) \
            or (ctx.activation or {}).get(str(topic_index)) or {}
        proposable = summary.get("proposable") or []
        if not proposable:
            return ("No factor is proposable for this topic. Return an empty "
                    "adaptation list - that is a correct and expected answer, "
                    "not a failure.")

        out = []
        for factor_id in proposable[:MAX_ROWS]:
            factor = next((f for f in factors_module.FACTORS if f.id == factor_id), None)
            if factor is None:
                continue
            evidence = [n for n in factor.evidence if n in fields]
            out.append(
                f"[{factor.id}] {factor.name} (tier {factor.tier})\n"
                f"  role: {factor.role}\n"
                f"  purposes: {', '.join(factor.purposes)}\n"
                + ("  evidence available:\n" + "\n".join(
                    _field_line(n, fields[n]) for n in evidence)
                   if evidence else
                   "  evidence available: none - safe defaults only, invent no local facts")
            )
        ruled_out = summary.get("ruledOutForTopic") or {}
        tail = ""
        if ruled_out:
            tail = "\n\nRuled out for THIS topic (do not propose from these):\n" + \
                   "\n".join(f"  {k}: {v}" for k, v in list(ruled_out.items())[:MAX_ROWS])
        return truncated("\n\n".join(out) + tail)

    @tool
    def get_resource_profile() -> str:
        """What this room physically has.

        Any adaptation that names a material must name one that appears here.
        The gate re-checks this deterministically and rejects a plan that asks
        for chart paper a school does not have - which is not pedantry: a
        teacher who cannot follow step two abandons the sheet.
        """
        present = [n for n in _RESOURCE_FIELDS if n in fields]
        if not present:
            return ("Nothing recorded about resources. Assume a blackboard, "
                    "chalk, and whatever the children already carry. Propose "
                    "nothing that needs more.")
        return "Recorded resources:\n" + "\n".join(_field_line(n, fields[n])
                                                   for n in present)

    @tool
    def get_language_profile() -> str:
        """The languages in the room and the language of instruction.

        A gap between home language and medium of instruction is an ACCESS
        problem, not a content one: the adaptation is a bridge into the same
        mastery target, never a smaller target.
        """
        present = [n for n in _LANGUAGE_FIELDS if n in fields]
        if not present:
            return ("No language data recorded. Do not assume a mismatch and do "
                    "not propose translation on speculation.")
        return "Recorded language context:\n" + "\n".join(_field_line(n, fields[n])
                                                          for n in present)

    @tool
    def get_verified_local_context() -> str:
        """Verified community and cultural facts about this place.

        VERIFIED ONLY. Anything an administrator or teacher actually recorded
        appears here; anything assumed does not, because a Real Life section
        built on an assumed local fact is a fabrication about a real village.
        If this is empty, write from the textbook's own examples.
        """
        local = [n for n, f in sorted(fields.items())
                 if f.get("provenance") == "verified"
                 and n in factors_module.ALL_EVIDENCE_FIELDS]
        if not local:
            return ("No verified local context on record. Use the textbook's own "
                    "examples. Do not invent village detail.")
        return "Verified local context:\n" + "\n".join(_field_line(n, fields[n])
                                                       for n in local[:MAX_ROWS])

    return [get_academic_contract, get_active_context, get_resource_profile,
            get_language_profile, get_verified_local_context]
