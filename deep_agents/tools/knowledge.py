"""Knowledge tools — architecture §17(B). The canonical library, read and (rarely) written.

THE ONE TOOL IN THIS PACKAGE WITH A SIDE EFFECT is `write_canonical_knowledge`,
and it is the reason §11's human-in-the-loop section exists. `resolve_canonical`
CREATES a library entry for a name it has never seen — that is its designed
behaviour and it is correct for a batch job with a human reading the diff, but
an autonomous agent holding it can grow a shared, cross-school vocabulary by
accident, one plausible synonym at a time. `registry.py` puts it behind an
approval gate; a background run does not get it at all.

COMPETENCIES ARE READ-ONLY HERE, and the asymmetry is deliberate — it is
`prep_flow/tools.py::map_to_library_competencies`'s argument, restated at the
tool boundary. A competency is a JOIN KEY onto the activity library. Inventing
one does not add a row, it removes every activity the topic could have used, and
a "no match" answer that sends the agent back to a competency that does exist is
worth more than a match it was allowed to manufacture.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from prep_flow import tools as gateway
from prep_flow.agents import reasoning as reasoning_agent

from .runtime import MAX_ROWS, RunContext


def build(ctx: RunContext, *, allow_writes: bool = False) -> list[BaseTool]:
    """Knowledge tools bound to this run.

    `allow_writes` adds `write_canonical_knowledge`. Off by default: a tool that
    is absent cannot be called, which is a stronger guarantee than a tool that
    is present and refused.
    """

    @tool
    async def lookup_competencies(names: list[str]) -> str:
        """Match competency phrases onto the EXISTING library competencies.

        Pass the competency wording you were going to use. What comes back is
        either the library id it maps to, or nothing — and nothing is a real
        answer, not a failure. An unmatched competency costs the topic every
        activity template it could have drawn on, so if a phrase does not match,
        rephrase it toward one that does rather than keeping your wording.
        """
        if not names:
            return "Pass at least one competency phrase."
        try:
            mapped = await gateway.map_to_library_competencies(
                {"competencies": list(names)[:MAX_ROWS]}, grade=ctx.grade)
        except Exception as exc:
            return f"Competency lookup unavailable ({exc}). Proceed without ids."

        pairs = (mapped or {}).get("competencies") or {}
        if not pairs:
            return ("None of those matched a library competency. "
                    "Try wording closer to a syllabus outcome.")
        lines = [f"  {raw!r} -> {cid}" for raw, cid in list(pairs.items())[:MAX_ROWS]]
        unmatched = [n for n in names if n not in pairs]
        out = "Matched:\n" + "\n".join(lines)
        if unmatched:
            out += "\n\nNo match (do not invent ids for these):\n" + \
                   "\n".join(f"  {n!r}" for n in unmatched[:MAX_ROWS])
        return out

    @tool
    async def get_cached_reasoning() -> str:
        """What a previous run already concluded about THIS chapter, if any.

        Curriculum reasoning is cached on (grade, subject, chapter) because it
        depends on the textbook and not on the class — see
        `prep_flow/agents/reasoning.py`. Read it before deriving your own: if it
        is present and still aligned to the topic spine, the useful work is
        checking and correcting it, not replacing it.
        """
        key = reasoning_agent.cache_key(ctx.grade, ctx.subject, ctx.chapter_title,
                                        ctx.chapter_number)
        try:
            cached = await gateway.cached_reasoning(key)
        except Exception as exc:
            return f"Reasoning cache unavailable ({exc}). Derive from the book."
        if not cached:
            return "Nothing cached for this chapter. Derive it from the book."

        prereqs = cached.get("prerequisites") or []
        miscon = cached.get("misconceptions") or []
        anchors = cached.get("anchors") or []
        return (
            f"Cached reasoning (prompt version {cached.get('_version', '?')}):\n"
            f"  prerequisites ({len(prereqs)}): "
            + "; ".join(str(p) for p in prereqs[:8]) + "\n"
            f"  misconceptions ({len(miscon)}): "
            + "; ".join(str(m.get('belief', m)) if isinstance(m, dict) else str(m)
                        for m in miscon[:6]) + "\n"
            f"  anchor objects ({len(anchors)}): "
            + "; ".join(str(a.get('object', a)) if isinstance(a, dict) else str(a)
                        for a in anchors[:12])
        )

    tools: list[BaseTool] = [lookup_competencies, get_cached_reasoning]

    if allow_writes:
        @tool
        async def write_canonical_knowledge(kind: str, names: list[str]) -> str:
            """Add names to the shared canonical library. REQUIRES APPROVAL.

            `kind` is one of: concepts, vocabulary, contexts.

            This writes to a library every school and every future chapter reads.
            Use it only for a name the chapter genuinely introduces and the
            library genuinely lacks — check with `lookup_competencies` and
            `get_cached_reasoning` first. Competencies cannot be written here at
            all; they are matched, never created.
            """
            allowed = {"concepts", "vocabulary", "contexts"}
            if kind not in allowed:
                return f"kind must be one of {sorted(allowed)} — competencies are read-only."
            if not names:
                return "Pass at least one name."
            seeded: dict = {}
            try:
                resolved = await gateway.resolve_knowledge(
                    {kind: list(names)[:MAX_ROWS]}, kinds=(kind,),
                    capture_seeded=seeded)
            except Exception as exc:
                return f"Canonical write failed ({exc}). Nothing was written."
            created = (seeded.get(kind) or {}).get("created") or []
            matched = (seeded.get(kind) or {}).get("matchedExisting") or []
            return (f"Resolved {len(resolved.get(kind) or {})} name(s) under {kind}.\n"
                    f"  already existed: {matched}\n"
                    f"  newly created:   {created}")

        tools.append(write_canonical_knowledge)

    return tools
