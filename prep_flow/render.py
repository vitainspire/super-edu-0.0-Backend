"""Markdown for a whole chapter batch.

Per-sheet rendering is the pilot's `render_prep_material_markdown`, reused
unchanged — a teacher who has seen one prep sheet should recognise these. What is
added here is what only exists at chapter scale: a contents table showing the
order and the page each topic teaches from, the seam between consecutive sheets
made visible, and the validator's findings printed beside the material they
concern rather than in a log nobody reads.
"""
import re

from .deps import render_prep_material_markdown
from .sections import SECTION_LABELS, SECTION_ORDER, handoff_of

# The pilot renders a standalone sheet, so its objective is an H1 and its
# sections are H2s. Embedded in a chapter document those collide with the
# chapter's own structure — every topic would open a new top-level heading and
# the contents links would point into a flat list of H1s. Demoting by two puts
# each sheet under its topic heading where it belongs.
_HEADING = re.compile(r"^(#{1,4})(\s+)", re.M)


def _demote(markdown: str, levels: int = 2) -> str:
    return _HEADING.sub(lambda m: "#" * min(6, len(m.group(1)) + levels) + m.group(2), markdown)


def _findings_block(findings: list[dict]) -> str:
    if not findings:
        return ""
    blocking = [f for f in findings if f.get("severity") == "blocking"]
    advisory = [f for f in findings if f.get("severity") != "blocking"]
    lines = ["", "> **Review notes**"]
    for finding in blocking + advisory:
        mark = "❗" if finding.get("severity") == "blocking" else "•"
        where = SECTION_LABELS.get(finding.get("section") or "", finding.get("section") or "sheet")
        lines.append(f"> {mark} _{where}_ — {finding.get('message')}")
    return "\n".join(lines)


def _run_summary_lines(run_provenance: dict) -> list[str]:
    """Two prose lines about the RUN as a whole — not a table, and
    deliberately thin: whether it was a background task, and how many
    canonical-library rows it seeded in total. Everything else — what
    specifically fed a given sheet — is cited inline, in that sheet's own
    content, by render_topic() below. See provenance.py.
    """
    if not run_provenance:
        return []
    execution = run_provenance.get("execution") or {}
    out = ["", f"> **Pipeline:** "
               f"{'ran as a background task' if execution.get('backgroundTask') else 'ran in the foreground'}"
               f" — {execution.get('note', '')}"]
    seeded_total = sum(
        (stage.get("seeded") or {}).get("canonicalEntriesCreated", 0)
        for stage in run_provenance.get("stages") or [] if stage.get("seeded"))
    if seeded_total:
        out.append(f"> **Seeded:** this run added {seeded_total} new "
                   f"entr{'y' if seeded_total == 1 else 'ies'} to the shared canonical "
                   f"library — see the Sources list under each affected topic below.")
    return out


def page_label(spec: dict) -> str:
    start, end = spec.get("page_start"), spec.get("page_end")
    if not start:
        return ""
    return f"Page {start}" if not end or end == start else f"Pages {start}–{end}"


def render_topic(material: dict, spec: dict, findings: list[dict] = None,
                 show_handoff: bool = True, standalone: bool = False) -> str:
    """One sheet, with its page range and the hook it leaves behind.

    `standalone=True` keeps the pilot's own heading levels, for the single-sheet
    case (an API response for one topic, a teacher printing one period).
    """
    index = spec.get("index") or material.get("index")
    title = spec.get("topic") or ""
    pages = page_label(spec)

    parts = [f'<a id="t{index}"></a>', f"## T{index} · {title}"]
    if pages:
        parts.append(f"_{pages}_")

    # Printed only when the book's order differs from the standard six, and
    # printed for the reader rather than the pipeline: a teacher comparing the
    # sheet against the open book wants to know that Real Life comes before
    # Concept here on purpose, and a reviewer asking "does this follow the
    # textbook" should not have to take it on trust. A topic taught in the
    # standard order says nothing, because there is nothing to explain.
    order = material.get("teachingOrder") or []
    if order and list(order) != list(SECTION_ORDER):
        sequence = " → ".join(SECTION_LABELS.get(s, s) for s in order)
        parts.append(f"_Taught in the book's own order: {sequence}_")

    body = render_prep_material_markdown(
        material, spec.get("topic") or "", spec.get("subtopic") or "",
        section_citations=(material.get("_meta") or {}).get("sectionCitations"))
    parts.append(body if standalone else _demote(body))

    if show_handoff:
        handoff = handoff_of(material)
        question = (handoff.get("question") or "").strip()
        if question:
            # Printed on purpose: this line is the contract with the next sheet.
            # A teacher who reads it knows exactly how tomorrow's period opens,
            # and a reviewer can see the seam without diffing two sheets.
            parts += [
                "",
                "### Leads into the next lesson",
                f"- **Scene:** {handoff.get('scene') or '—'}",
                f"- **In their hands:** {handoff.get('object') or '—'}",
                f"- **They leave asking:** {question}",
            ]

    figures = [f for f in (material.get("figures") or []) if f.get("caption")]
    if figures:
        # Linked, not just named. A prep sheet that says "look at fig_13_1" is
        # useless on paper; one that shows the picture is what a teacher holds up.
        parts += ["", "### Pictures in the book to point at"]
        for figure in figures:
            where = f"page {figure['page']}"
            if figure.get("path"):
                parts.append(f"- **{where}** — {figure['caption'][:180]}")
                parts.append(f"  ![{figure['id']}]({figure['path']})")
            else:
                parts.append(f"- **{where}** — {figure['caption'][:180]}")

    block = _findings_block(findings or [])
    if block:
        parts.append(block)
    return "\n".join(parts)


def render_chapter(state: dict) -> str:
    """The whole batch: header, contents, then every sheet in teaching order."""
    topics = state.get("topics") or []
    materials = state.get("materials") or {}
    issues = state.get("issues") or {}

    title = state.get("chapter_title") or "Chapter"
    number = state.get("chapter_number")
    out = [
        f"# {('Chapter ' + str(number) + ': ') if number else ''}{title}",
        f"_Grade {state.get('grade')} · {state.get('subject')} · "
        f"{len(materials)} of {len(topics)} topics generated_",
    ]

    if state.get("chapter_arc"):
        out += ["", f"> {state['chapter_arc']}"]

    blocking = sum(1 for f in issues.values() for i in f if i.get("severity") == "blocking")
    advisory = sum(1 for f in issues.values() for i in f if i.get("severity") != "blocking")
    out += ["", f"**Review:** {blocking} blocking, {advisory} advisory finding(s). "
                f"Status: {state.get('status', 'unknown')}."]

    batch = state.get("batch_issues") or []
    if batch:
        out += ["", "## Chapter-level review"]
        out += [f"- {('❗' if i.get('severity') == 'blocking' else '•')} {i.get('message')}"
                for i in batch]

    out += _run_summary_lines(state.get("run_provenance") or {})

    out += ["", "## Contents", "", "| # | Topic | Pages | Challenge activity | Findings |",
            "|---|-------|-------|--------------------|----------|"]
    for spec in topics:
        index = spec["index"]
        material = materials.get(index) or {}
        activity = (material.get("challenge") or {}).get("activity") or "—"
        found = issues.get(index) or []
        flag = ("—" if not found else
                f"{sum(1 for f in found if f.get('severity') == 'blocking')}❗ "
                f"{sum(1 for f in found if f.get('severity') != 'blocking')}•")
        pages = page_label(spec).replace("Pages ", "").replace("Page ", "") or "—"
        status = "" if material else " _(not generated)_"
        out.append(f"| [T{index}](#t{index}) | {spec['topic']}{status} | {pages} | {activity} | {flag} |")

    for spec in topics:
        material = materials.get(spec["index"])
        if not material:
            continue
        out += ["", "---", "", render_topic(material, spec, issues.get(spec["index"]))]

    metrics = state.get("metrics") or {}
    if metrics:
        out += ["", "---", "", "## Run metrics", ""]
        out += [f"- {key.replace('_', ' ')}: {value}" for key, value in sorted(metrics.items())]

    errors = state.get("errors") or []
    if errors:
        out += ["", "## Errors during this run", ""]
        out += [f"- {e}" for e in errors]

    return "\n".join(out)
