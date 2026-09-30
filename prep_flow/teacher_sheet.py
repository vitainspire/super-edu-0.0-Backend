"""The chapter as a teacher actually reads it.

WHAT THIS IS FOR. `render.py` renders the sheet the pilot always rendered: an
objective, then six sections of numbered bullets. Everything in it is true and
most of what a teacher needs is missing from the page — not from the DATA, from
the page. Generation already produces the minutes per section, the materials the
period needs, what to watch for in each section, and the handoff into the next
period. The old renderer prints none of them.

So this is not a new prompt and not a second generation pass. It is the same
material, laid out the way somebody standing in front of forty children needs it:

    what am I teaching, how long have I got, what do I need on the desk,
    what do I do, what do I watch for, and how does this end so tomorrow starts

THE THREE THINGS ONLY CHAPTER SCALE CAN GIVE, printed at the end because they are
what makes forty sheets one chapter rather than forty lessons: the quick
reference table, the progression through the anchor objects, and the one sentence
per period the teacher should carry in their head.

Section order is the material's own `teachingOrder`, never a fixed list. A period
that teaches Real Life before Concept does so because the book's own moves said
to (see `prep_flow/moves.py`), and a renderer that imposed the canonical six
would silently undo that.

THREE LEVELS, AND THE REASON THERE ARE THREE. The pipeline knows a great deal
about why each period is shaped as it is, and the temptation is to tell the
teacher all of it. That is backwards: intelligence should rise upstream and
cognitive load should fall downstream, so the last layer before a classroom is a
COMPRESSOR, not another author.

    QUICK   what you hold while teaching. One page: goal, materials, a running
            clock, what you do, what you say, what you check. No rationale.
    FULL    what you read the night before. Everything above, plus the
            misconceptions, the detail behind each move, and the handoff.
    TRACE   what a curriculum designer reads. Full, plus where each decision
            came from — the mastery target, the chain, the audited gap.

None of the three calls a model. The material already contains all of it; what
differs is how much reaches the page. A compressor cannot invent a fact, which
is the whole reason this is not a fifth generation stage.
"""
from __future__ import annotations

import re
from typing import Optional

from .sections import SECTION_LABELS, SECTION_ORDER

# What the six sections are called on a page a teacher is holding. Upper case
# because they are being scanned, not read.
_HEADINGS = {
    "refresher": "REFRESHER",
    "realLife": "REAL LIFE",
    "concept": "CONCEPT",
    "challenge": "CHALLENGE",
    "levelSet": "LEVEL SET",
    "explore": "EXPLORE",
}

# A book heading that names nothing — the printed page's own scaffolding rather
# than a topic. Sequencing cuts on the book's boundaries and is right to; these
# are simply not titles a teacher can plan from.
_GENERIC_HEADING = re.compile(
    r"^(activity|exercise|task|question|do this|try this|let's do|practice|"
    r"worksheet|revision)\s*[-–—]?\s*\d*$|^\d+[.)]?$|^[IVX]+[.)]?$", re.I)

# A quoted question inside a teacher instruction. These are the words the teacher
# actually says, and on a page being read at arm's length they belong on their own
# line rather than buried mid-paragraph.
_SPOKEN = re.compile(r"['‘“\"]([^'’”\"]{15,180}\?)['’”\"]")


def period_title(material: dict, spec: dict = None) -> str:
    """What to call this period.

    PREFERS THE OBJECTIVE, which generation already writes as a verb-first
    headline of four to eight words — precisely a period title. The book's own
    heading is kept underneath rather than promoted, because the headings a
    textbook prints are frequently `Activity-1`, `NUMBERS` or `5.`, and a teacher
    planning a week from a contents page cannot tell those apart.

    The heading is not discarded: it is what the sheet is grounded in, and it is
    printed as the textbook line directly below.
    """
    objective = (material.get("objective") or "").strip()
    heading = (material.get("topic") or (spec or {}).get("topic") or "").strip()

    if objective:
        return objective.rstrip(".").upper()
    if heading and not _GENERIC_HEADING.match(heading):
        return heading.upper()
    # Last resort: what the period leaves the child able to do.
    gained = ((material.get("_meta") or {}).get("masteryTarget") or "").strip()
    return (gained or heading or "UNTITLED PERIOD").rstrip(".").upper()


def _points(material: dict, section: str) -> list[dict]:
    """The bullets for one section, in the one shape the rest of this file reads.

    The six sections are not uniformly built: the refresher is nested under
    `previousTopicRefresher.recap`, four carry `points`, and challenge pairs
    `points` with the activity's name. Normalising here keeps that irregularity
    in one function instead of at every use.
    """
    if section == "refresher":
        value = (material.get("previousTopicRefresher") or {}).get("recap")
    else:
        value = material.get(section)
        if isinstance(value, dict):
            value = value.get("points") or value.get("recap")
    if isinstance(value, str):
        value = [value]
    out = []
    for item in (value or []):
        if isinstance(item, dict):
            text = (item.get("text") or "").strip()
            detail = (item.get("detail") or "").strip()
        else:
            text, detail = "", str(item).strip()
        if text or detail:
            out.append({"text": text, "detail": detail})
    return out


def _watch(material: dict, section: str) -> str:
    entry = (material.get("sectionWatch") or {}).get(section)
    if isinstance(entry, dict):
        parts = [entry.get("text") or "", entry.get("detail") or ""]
        return " — ".join(p.strip() for p in parts if (p or "").strip())
    return (entry or "").strip()


def _lift_spoken(detail: str) -> tuple[str, list[str]]:
    """Pull the teacher's own words out of the prose.

    Conservative on purpose: only quoted spans that END IN A QUESTION MARK and
    run to at least fifteen characters. That is the shape of a teacher script —
    "Did the cup move, or did you move?" — and it will not catch a quoted noun or
    an aside. Anything it misses stays in the paragraph, which is where it was.
    """
    spoken = [m.group(1).strip() for m in _SPOKEN.finditer(detail)]
    return detail, spoken


QUICK, FULL, TRACE = "quick", "full", "trace"
LEVELS = (QUICK, FULL, TRACE)


def _watch_headline(watch: str) -> str:
    """The first clause of a watch-for, for the inline pointer.

    The full text lives in the period's WATCH FOR block; inline, a teacher
    mid-lesson needs the reminder, not the paragraph.
    """
    head = watch.split(" — ")[0].strip()
    return head if len(head) > 8 else watch[:110].strip()


def _watch_block(material: dict, order: list) -> list[str]:
    """Every watch-for the period carries, in one place, before the lesson."""
    rows = [(section, _watch(material, section)) for section in order]
    rows = [(s, w) for s, w in rows if w]
    if not rows:
        return []
    lines = ["### Watch for", ""]
    for section, watch in rows:
        lines.append(f"* **{_HEADINGS[section].title()}** — {watch}")
    lines.append("")
    return lines


def render_period(material: dict, *, number: int, total: int,
                  spec: dict = None, next_number: Optional[int] = None,
                  level: str = FULL) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# PERIOD {number} — {period_title(material, spec)}")
    add("")

    timings = material.get("timings") or {}
    order = [s for s in (material.get("teachingOrder") or SECTION_ORDER)
             if s in _HEADINGS and _points(material, s)]
    minutes = sum(int(timings.get(s) or 0) for s in order)

    if material.get("objective") and level != QUICK:
        add("### Objective")
        add("")
        add(material["objective"].strip())
        add("")

    add("### Time")
    add("")
    add(f"**{minutes or 30} minutes**")
    add("")

    # The model lists the textbook among its materials ("textbook open to page
    # 12") and the sheet also carries `pagesCited`. Printing both gives a teacher
    # two textbook lines that disagree about the page range, so the model's own
    # phrasing yields to the cited pages, which are the checked ones.
    materials = [m for m in (material.get("materialsUsed") or [])
                 if str(m).strip() and "textbook" not in str(m).lower()]
    pages = material.get("pagesCited") or material.get("pageRange") or []
    if materials or pages:
        add("### Materials")
        add("")
        for item in materials:
            add(f"* {item}")
        if pages:
            span = (f"{min(pages)}–{max(pages)}" if len(set(pages)) > 1
                    else str(pages[0]))
            add(f"* Textbook, pages **{span}**")
        add("")

    heading = (material.get("topic") or "").strip()
    if heading and not _GENERIC_HEADING.match(heading):
        add(f"*Textbook section: {heading}*")
        add("")

    add("---")
    add("")

    if level != QUICK:
        lines += _watch_block(material, order)
        add("---")
        add("")

    # A RUNNING CLOCK, not a list of durations. Mid-lesson the question is never
    # "how long is Concept", it is "am I behind" — and that needs the wall clock,
    # which is a cumulative sum the teacher should not have to do in their head.
    elapsed = 0
    for position, section in enumerate(order, 1):
        mins = int(timings.get(section) or 0)
        window = f"{elapsed}–{elapsed + mins}" if mins else ""
        elapsed += mins
        clock = f"`{window:>7}`  " if window else ""
        add(f"## {clock}{position}. {_HEADINGS[section]}")
        add("")

        if section == "challenge":
            activity = (material.get("challenge") or {}).get("activity")
            if activity:
                add(f"**Activity: {activity}**")
                add("")

        if level != QUICK:
            add("### Teacher does")
            add("")
        for point in _points(material, section):
            detail, spoken = _lift_spoken(point["detail"])
            if level == QUICK:
                # The lead line is the ACTION; the detail behind it is why and
                # how, which belongs in the night-before read. A teacher holding
                # this page mid-lesson needs the verb and the question.
                if point["text"]:
                    add(f"- {point['text']}")
                for line in spoken:
                    add(f'  > “{line}”')
                continue
            if point["text"]:
                add(f"**{point['text']}**")
                add("")
            if detail:
                add(detail)
                add("")
            for line in spoken:
                add(f"> “{line}”")
                add("")
        if level == QUICK:
            add("")

        # NOT "### Teacher watches for" under every section any more. Six of
        # them per period is six paragraphs of caution between the teacher and
        # the next thing they have to do, and they stop being read. The content
        # is hoisted into one WATCH FOR block at the top of the period; what is
        # left inline is a one-line pointer.
        watch = _watch(material, section)
        if watch and level != "quick":
            add(f"> ⚠ **Check** — {_watch_headline(watch)}")
            add("")

    if level == TRACE and spec:
        add("---")
        add("")
        add("### Design trace")
        add("")
        academic = spec.get("academic") or {}
        chain = spec.get("knowledgeChain") or {}
        audit = spec.get("masteryAudit") or {}
        plan = spec.get("experiencePlan") or {}
        for label, value in (
                ("Mastery target", academic.get("masteryTarget")),
                ("This period gains", chain.get("gained")),
                ("Assumes from the last", chain.get("assumes")),
                ("Bridges to the next", chain.get("bridgesTo")),
                ("Cognitive trajectory", " → ".join(plan.get("trajectory") or [])),
                ("Conceptual jump", plan.get("conceptualJump")),
                ("Anchor chosen", plan.get("anchor")),
                ("Gap this period closes", plan.get("gap")),
                ("What the page does not give", "; ".join(
                    str(x.get("missing") if isinstance(x, dict) else x)
                    for x in (audit.get("missing") or []))),
                ("Pages", f"{(spec.get('grounding') or {}).get('pageStart')}"
                          f"–{(spec.get('grounding') or {}).get('pageEnd')}")):
            if value:
                add(f"* **{label}:** {value}")
        add("")

    handoff = (material.get("explore") or {}).get("handoff") or {}
    question = (handoff.get("question") or "").strip()
    if question and next_number:
        add("### End the lesson with")
        add("")
        scene = (handoff.get("scene") or "").strip()
        if scene and level != QUICK:
            add(scene)
            add("")
        add(f"**Bridge into Period {next_number}:**")
        add("")
        add(f"> “{question}”")
        add("")

    return "\n".join(lines).rstrip() + "\n"


def _quick_reference(contract: dict, materials: dict) -> list[str]:
    """The table, the progression, and the one line per period.

    All three exist only at chapter scale, and all three are read far more often
    than the sheets are: a teacher planning a week wants the table, and a teacher
    two minutes before a lesson wants the sentence.
    """
    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}
    ordered = sorted(int(i) for i in materials)

    lines = ["# TEACHER QUICK REFERENCE", "",
             "| Period | Core idea | Main object | Textbook |",
             "| --- | --- | --- | --- |"]
    for position, index in enumerate(ordered, 1):
        row = rows.get(index) or {}
        material = materials[index] if index in materials else materials[str(index)]
        chain = row.get("knowledgeChain") or {}
        experience = row.get("experiencePlan") or {}
        grounding = row.get("grounding") or {}
        core = (chain.get("gained") or material.get("objective") or "").rstrip(".")
        anchor = experience.get("anchor") or "—"
        start, end = grounding.get("pageStart"), grounding.get("pageEnd")
        pages = f"pp. {start}–{end}" if start and end and start != end else (
            f"p. {start}" if start else "—")
        lines.append(f"| **{position}** | {core} | {anchor} | {pages} |")
    lines.append("")

    # The progression: the objects the chapter actually moves through, which is
    # the thing a teacher forgets between Monday and Wednesday.
    anchors = []
    for index in ordered:
        row = rows.get(index) or {}
        anchor = (row.get("experiencePlan") or {}).get("anchor")
        if anchor and anchor not in anchors:
            anchors.append(anchor)
    if len(anchors) > 1:
        lines += ["### The progression", "", "  →  ".join(f"**{a}**" for a in anchors), ""]

    lines += ["# WHAT THE TEACHER SHOULD REMEMBER", ""]
    for position, index in enumerate(ordered, 1):
        row = rows.get(index) or {}
        target = ((row.get("academic") or {}).get("masteryTarget")
                  or (row.get("knowledgeChain") or {}).get("gained") or "")
        if target:
            lines += [f"### Period {position}", "", f"**{target.rstrip('.')}.**", ""]

    lines += ["The periods should feel like **one continuous investigation**, "
              "not unrelated lessons.", ""]
    return lines


def render_chapter(contract: dict, materials: dict, *, level: str = FULL) -> str:
    """The whole chapter, as one document a teacher can print."""
    chapter = contract.get("chapter") or {}
    number = chapter.get("number") or contract.get("chapterNumber")
    title = chapter.get("title") or contract.get("chapterTitle") or ""
    arc = (chapter.get("arc") or "").strip()

    keyed = {}
    for index, material in (materials or {}).items():
        try:
            keyed[int(index)] = material
        except (TypeError, ValueError):
            continue
    ordered = sorted(keyed)

    lines = [f"# CHAPTER {number if number is not None else ''} — TEACHER PREP".replace("  ", " "),
             ""]
    lines.append(f"**Grade:** {contract.get('grade', '')}  ")
    lines.append(f"**Subject:** {contract.get('subject', '')}  ")
    # Only when the book gave the chapter a real name. Ingests frequently supply
    # "Chapter 2", which the H1 above already says.
    if title and not re.fullmatch(r"chapter\s*\d+", title.strip(), re.I):
        lines.append(f"**Chapter:** {title}  ")
    if arc:
        lines.append("")
        lines.append("**Chapter arc:**  ")
        lines.append(f"**{arc}**")
    lines += ["", "---", ""]

    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}
    for position, index in enumerate(ordered, 1):
        nxt = position + 1 if position < len(ordered) else None
        lines.append(render_period(keyed[index], number=position, total=len(ordered),
                                   spec=rows.get(index), next_number=nxt, level=level))
        lines += ["", "---", ""]

    lines += _quick_reference(contract, keyed)
    return "\n".join(lines)


def main(argv=None) -> int:
    """Render a teacher document from a saved run.

        python -m prep_flow.teacher_sheet data/generated/trace_stages.json

    Accepts either a tracecapture trace or an orchestrator `--out` state dump —
    both carry the contract and the materials, just under different keys.
    """
    import argparse
    import json
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="prep_flow.teacher_sheet")
    parser.add_argument("source", help="a trace JSON or an orchestrator state dump")
    parser.add_argument("--out", help="defaults to CHAPTER_TEACHER_PREP.md beside it")
    parser.add_argument("--no-text", action="store_true",
                        help="skip the plain-text copy")
    parser.add_argument("--level", default=FULL, choices=list(LEVELS),
                        help="quick: hold while teaching | full: read the night "
                             "before | trace: where each decision came from")
    parser.add_argument("--all-levels", action="store_true",
                        help="write one file per level")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.source).read_text(encoding="utf-8"))
    if "events" in data:                      # a tracecapture trace
        found = {e["kind"]: e.get("value") for e in data["events"]
                 if e.get("kind", "").startswith("artifact.")}
        contract = found.get("artifact.contract") or {}
        materials = found.get("artifact.materials") or {}
    else:                                     # an orchestrator state dump
        contract = data.get("contract") or {}
        materials = data.get("materials") or {}

    if not materials:
        print("No materials in that file — nothing to render.", file=sys.stderr)
        return 1

    base = Path(args.out) if args.out else Path(args.source).with_name(
        "CHAPTER_TEACHER_PREP.md")
    levels = list(LEVELS) if args.all_levels else [args.level]

    for level in levels:
        dest = base if len(levels) == 1 else base.with_name(
            f"{base.stem}_{level.upper()}{base.suffix}")
        dest.write_text(render_chapter(contract, materials, level=level),
                        encoding="utf-8")
        print(f"{dest}  ({dest.stat().st_size:,} bytes)")

        # The same document as plain text, beside it. A teacher printing from a
        # phone or pasting into a WhatsApp group has no markdown renderer.
        if not args.no_text:
            txt = dest.with_suffix(".txt")
            txt.write_text(render_chapter_text(contract, materials, level=level),
                           encoding="utf-8")
            print(f"{txt}  ({txt.stat().st_size:,} bytes)")
    return 0



# ── Plain text ───────────────────────────────────────────────────────────────

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_QUOTE = re.compile(r"^>\s?", re.M)
_MD_BULLET = re.compile(r"^\* ", re.M)
_MD_CODE = re.compile(r"`([^`\n]*)`")


def as_text(markdown: str, width: int = 96) -> str:
    """The same document with the markdown taken off.

    NOT a general converter — it only has to handle what `render_chapter` emits,
    which is headings, bold, one italic line, blockquotes, bullets and a table.
    Headings become ruled lines because a teacher scanning a printout finds a
    rule faster than a row of hashes, and the tables are left as they are: pipes
    line up in a monospaced file and nothing better fits in plain text.
    """
    out: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()

        if stripped.startswith("# "):
            title = _MD_BOLD.sub(r"\1", stripped[2:]).strip()
            out += ["", "=" * width, f"  {title.upper()}", "=" * width]
            continue
        if stripped.startswith("## "):
            # The running clock is an inline code span so it renders monospaced
            # in markdown; in plain text the backticks are just furniture.
            title = _MD_CODE.sub(r"\1", _MD_BOLD.sub(r"\1", stripped[3:]))
            title = re.sub(r"\s{2,}", "  ", title).strip()
            out += ["", f"--- {title} " + "-" * max(0, width - len(title) - 5)]
            continue
        if stripped.startswith("### "):
            title = _MD_BOLD.sub(r"\1", stripped[4:]).strip()
            out += ["", f"  {title.upper()}"]
            continue
        if stripped == "---":
            out.append("-" * width)
            continue

        line = _MD_QUOTE.sub("    | ", line)
        line = _MD_CODE.sub(r"\1", line)
        line = _MD_BOLD.sub(r"\1", line)
        line = _MD_ITALIC.sub(r"\1", line)
        line = _MD_BULLET.sub("  - ", line)
        out.append(line)

    # Collapse the runs of blank lines the substitutions leave behind.
    text: list[str] = []
    for line in out:
        if not line.strip() and text and not text[-1].strip():
            continue
        text.append(line.rstrip())
    return "\n".join(text).strip() + "\n"


def render_chapter_text(contract: dict, materials: dict, *, level: str = FULL) -> str:
    return as_text(render_chapter(contract, materials, level=level))


if __name__ == "__main__":
    import sys

    sys.exit(main())
