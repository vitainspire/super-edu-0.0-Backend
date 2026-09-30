"""The comparison as Markdown — the same findings as the app, in a file you can paste.

The HTML viewer is for reading a run interactively. This is for the other two
things people do with a result: paste it into a review thread, and diff two of
them against each other. Both want plain text, and both want the verdict before
the evidence.

ORDERED BY WHAT NEEDS ATTENTION, not by topic. The app can afford to lay
everything out at once and let the eye pick; a document is read top to bottom, so
the adaptations that did NOT land come first — they are the only lines anybody
has to act on. Topic order returns below, in the detail.

THE CAVEATS ARE IN THE DOCUMENT, not in a footnote. A markdown report gets pasted
somewhere the surrounding context does not travel with it, so the two things that
would otherwise be assumed — that a word-overlap figure is the same claim as a
section comparison, and that a run without a noise arm has a ruler — are stated
where they cannot be scrolled past.
"""
from typing import Optional

_LANDED = {
    "yes":         "landed",
    "elsewhere":   "**not where aimed**",
    "noise-only":  "**noise only**",
    "no":          "**did not land**",
    "unplaced":    "no section named",
}


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def render(result: dict) -> str:
    meta = result.get("meta") or {}
    summary = result.get("summary") or {}
    topics = result.get("topics") or []
    out: list[str] = []

    chapter = meta.get("chapter") or "chapter"
    out.append(f"# Did the context land? — {chapter}")
    out.append("")
    out.append(
        f"The same chapter written twice: once from Node 1's contract alone, once "
        f"with Node 2's context plan added. Everything else held identical, so any "
        f"difference below is the plan's doing.")
    out.append("")
    out.append(f"- **Grade {meta.get('grade')} {meta.get('subject')}**, "
               f"chapter {meta.get('chapterNumber')}")
    out.append(f"- contract `{(meta.get('contractFingerprint') or '')[:16]}`")
    out.append(f"- noise arm: {'ran' if result.get('noiseArmRan') else '**NOT RUN**'}")
    if meta.get("sampleRun"):
        out.append("- **sample data — not a live model run**")
    out.append("")

    # ── the verdict ──────────────────────────────────────────────────────────
    out.append("## The verdict")
    out.append("")
    out.append("| | |")
    out.append("|---|---:|")
    out.append(f"| Handed to the generator | {summary.get('adaptationsHandedOver', 0)} |")
    out.append(f"| Landed where aimed | {summary.get('landedWhereAimed', 0)} |")
    out.append(f"| Landed elsewhere | {summary.get('landedElsewhere', 0)} |")
    out.append(f"| Did not land | {summary.get('didNotLand', 0)} |")
    out.append(f"| Sections changed | {summary.get('sectionsChanged', 0)} / "
               f"{summary.get('sectionsCompared', 0)} ({_pct(summary.get('changeRate'))}) |")
    out.append(f"| Topics Node 2 left alone | {summary.get('topicsUnchanged', 0)} |")
    # Counted on the same table as `landed`, deliberately: the two answer
    # different questions about the same adaptation, and an adaptation can score
    # a clean `landed` while carrying one. Reading only the landing row is how a
    # run with a flagged citation gets reported as unqualified success.
    flagged = sum(1 for t in topics for a in (t.get("adaptations") or [])
                  if a.get("warnings"))
    if flagged:
        out.append(f"| Landed **and** flagged for review | {flagged} |")
    out.append("")

    # ── what needs acting on, first ──────────────────────────────────────────
    problems = [(t, a) for t in topics for a in t.get("adaptations") or []
                if a.get("landed") in ("elsewhere", "no", "noise-only")]
    if problems:
        out.append("## Needs attention")
        out.append("")
        for topic, a in problems:
            out.append(f"- **T{topic['index']} · {a.get('factor')}** — aimed at "
                       f"`{a.get('section') or 'no section'}`, {_LANDED.get(a.get('landed'))}."
                       f"  \n  _{a.get('change')}_")
        out.append("")
    else:
        out.append("## Needs attention")
        out.append("")
        out.append("Nothing — every adaptation handed over changed the section it named.")
        out.append("")
        # ...which is a statement about LANDING only, and saying just that under a
        # heading called "needs attention" reads as an all-clear the run did not
        # earn. An advisory means the change reached its section and the citation
        # behind it may still be decoration.
        flagged = [(t, a) for t in topics for a in (t.get("adaptations") or [])
                   if a.get("warnings")]
        if flagged:
            n = len(flagged)
            out.append(f"That is about *placement*. {n} of them landed cleanly and "
                       f"{'still carries' if n == 1 else 'still carry'} an advisory "
                       f"about the evidence cited:")
            out.append("")
            for topic, a in flagged:
                for warning in a["warnings"]:
                    out.append(f"- **T{topic['index']} · {a.get('factor')}** — {warning}")
            out.append("")

    # ── caveats, in the document rather than a footnote ─────────────────────
    caveats = []
    adoption = summary.get("contextAdoptionConfirmed")
    if adoption is not None and summary.get("landedWhereAimed") != summary.get("adaptationsHandedOver"):
        caveats.append(
            f"**Two measures disagree, and the stricter one is right.** The "
            f"word-overlap adoption check reports {_pct(adoption)} confirmed because it "
            f"searches the whole sheet and a paraphrase satisfies it. The section "
            f"comparison is exact — it asks whether the section an adaptation *named* "
            f"differs between two runs that varied by one input. Where they disagree, "
            f"read the section comparison.")
    if not result.get("noiseArmRan"):
        aimed = sum(len(t.get("sectionsTargeted") or []) for t in topics)
        moved = sum(len(t.get("sectionsChanged") or []) for t in topics)
        caveats.append(
            f"**No noise arm ran, so there is no ruler.** Generation runs at "
            f"temperature 0.6 — two runs of the *same* arm differ. {moved} section(s) "
            f"changed but only {aimed} had an adaptation aimed at them; the rest is "
            f"the sampler. Read the per-adaptation verdicts, not the section count. "
            f"Sections below are marked either way.")
    if caveats:
        out.append("## Read this before the detail")
        out.append("")
        for c in caveats:
            out.append(f"> {c}")
            out.append(">")
        out.append("")

    # ── per topic ────────────────────────────────────────────────────────────
    out.append("## Topic by topic")
    for topic in topics:
        out.append("")
        out.append(f"### T{topic['index']} · {topic.get('topic') or ''}")
        if topic.get("masteryTarget"):
            out.append("")
            out.append(f"*Must leave them able to: {topic['masteryTarget']}*")

        adaptations = topic.get("adaptations") or []
        out.append("")
        if not adaptations:
            out.append("Node 2 proposed nothing here — it judged the lesson already fits "
                       "this room. That is a real answer, not a gap.")
            # Markdown needs the blank line or the next paragraph joins this one.
            out.append("")
        else:
            out.append("| Factor | Type | Aimed at | Outcome |")
            out.append("|---|---|---|---|")
            for a in adaptations:
                out.append(f"| `{a.get('factor')}` | {a.get('type')} | "
                           f"`{a.get('section') or '—'}` | {_LANDED.get(a.get('landed'), a.get('landed'))} |")
            out.append("")
            for a in adaptations:
                out.append(f"**{a.get('factor')}** — {a.get('change')}")
                out.append("")
                out.append(f"  - why: {a.get('reason')}")
                if a.get("evidence"):
                    out.append("  - from: " + ", ".join(f"`{e}`" for e in a["evidence"]))
                # Advisories ride BESIDE an accepted adaptation, not instead of
                # it: `unechoed_evidence` fires on a citation that looks like
                # grounding and is not, which is a thing to check rather than a
                # reason to refuse. Rendered here because a warning nobody reads
                # is a warning nobody raised.
                for warning in a.get("warnings") or []:
                    out.append(f"  - ⚠ **worth a look:** {warning}")
                out.append("")

        changed = [s for s in topic.get("sections") or [] if s.get("changed")]
        targeted = set(topic.get("sectionsTargeted") or [])
        if changed:
            out.append("**What changed in the sheet**")
            out.append("")
            # Sections an adaptation ACTUALLY AIMED AT come first and are marked.
            # Without a noise arm this ordering is the only thing separating a
            # finding from the sampler, so it is not cosmetic: a reader who takes
            # the untargeted sections as evidence of the plan working has drawn
            # the wrong conclusion from a true number.
            changed.sort(key=lambda s: s["section"] not in targeted)
            for section in changed:
                aim = ("  ← **an adaptation aimed here**"
                       if section["section"] in targeted
                       else "  *(nothing aimed here — sampler variation)*")
                noise = (" *(within the sampler's own noise)*"
                         if section.get("aboveNoise") is False else "")
                out.append(f"<details><summary><strong>{section['label']}</strong> — "
                           f"{_pct(1 - section['similarity'])} new{noise}{aim}</summary>")
                out.append("")
                out.append("_Without the plan_")
                out.append("")
                out.append(f"> {section.get('before') or '(empty)'}")
                out.append("")
                out.append("_With Node 2's plan_")
                out.append("")
                out.append(f"> {section.get('after') or '(empty)'}")
                out.append("")
                out.append("</details>")
                out.append("")
        else:
            out.append("**What changed in the sheet:** nothing.")
            out.append("")

    out.append("---")
    out.append("")
    out.append(
        "Generated by `python -m generation.cli compare --md`. The baseline arm's "
        "prompt is byte-identical to what generation produced before Node 2 existed "
        "(`generation/reinforcement.py` contributes an empty string when there is no "
        "plan), which is what lets a difference here be attributed to the plan rather "
        "than to a prompt rewrite. Whether a landed change is an *improvement* is a "
        "separate question this report does not answer — that is Node 3's learner gate "
        "and a teacher's judgement.")
    return "\n".join(out) + "\n"
