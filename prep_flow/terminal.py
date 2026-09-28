"""Rich, human-readable console output for one chapter run — the detail
BEHIND `provenance.py`'s one-line `live_line()`.

`live_line()` answers "did this stage run, and roughly how much did it do" —
one line, safe to print every time a stage fires. This module answers "what,
specifically, did it pull and use" — every topic's actual title and page
range, every extracted concept/vocabulary word, exactly which canonical
entries got seeded (not just how many), every activity actually selected,
every finding's actual message. Meant to be printed once per stage, right
after `live_line()`, not on every repair/window repeat — call sites decide
that, this module just knows how to print a stage's data richly.

Shared by `prep_flow/cli.py` (which already printed most of this, stage by
stage, before this module existed) and `textbook_viewer.py` (which, before
this, only ever got the compact one-line summary in its own terminal) — one
implementation, so a developer watching either tool's console sees the same
depth. Deliberately plain ASCII throughout: this gets imported by whichever
tool wants it, and not every caller reconfigures its console to UTF-8.
"""

_RULE = "-" * 78


def header(title: str) -> None:
    print(f"\n{_RULE}\n{title}\n{_RULE}")


def print_topics(topics: list) -> None:
    """Sequencing — every topic's title, page range, and how much text it
    actually has to teach from."""
    for spec in topics:
        end = spec.get("page_end")
        pages = f"p{spec.get('page_start')}" + (f"-{end}" if end != spec.get("page_start") else "")
        print(f"  T{spec['index']:>2}. {spec['topic']}"
              + (f" -- {spec['subtopic']}" if spec.get("subtopic") else "")
              + f"   [{pages}, {len(spec.get('excerpt') or '')} chars]")
        if spec.get("why"):
            print(f"        why here: {spec['why']}")


def print_context(topics: list, adaptive: dict) -> None:
    """Context assembly — what was actually extracted and matched, per topic,
    including exactly WHICH canonical entries this run seeded, not just how
    many. This is the stage most worth seeing in full: it is the only one
    that writes new rows into the shared curriculum library."""
    for spec in topics:
        knowledge = spec.get("knowledge") or {}
        print(f"  T{spec['index']:>2}. {spec['topic']}")
        print(f"        concepts:      {', '.join(knowledge.get('concepts') or []) or '(none)'}")
        print(f"        competencies:  {', '.join(knowledge.get('competencies') or []) or '(none)'}")
        print(f"        vocabulary:    {', '.join(knowledge.get('vocabulary') or []) or '(none)'}")
        contexts = knowledge.get("contexts") or []
        print(f"        contexts:      {', '.join(contexts) or '(none)'}")
        n_activities = len(spec.get("activities") or [])
        print(f"        activities:    {n_activities} template(s) matched"
              f" | bloom={knowledge.get('bloom_level') or '?'} difficulty={knowledge.get('difficulty') or '?'}")
        if n_activities == 0 and knowledge.get("competencies"):
            # Worth distinguishing from a connection hiccup: this topic DID
            # extract competencies, they just have no Pedagogy Library
            # coverage yet -- a real gap for whoever authors the next batch
            # of templates, not a run that silently lost data.
            print("        why 0: no Pedagogy Library template covers these competencies yet"
                  " -- activity selection will invent one")
        seeded = spec.get("canonical_seeded") or {}
        created = {kind: entry.get("created") for kind, entry in seeded.items() if entry.get("created")}
        if created:
            print("        SEEDED (new to the shared library, because nothing existing matched): "
                  + "; ".join(f"{kind}: {', '.join(names)}" for kind, names in created.items()))
    if adaptive:
        print(f"\n  Adaptive state v{adaptive.get('_version')} in force "
              f"({adaptive.get('_sampleSize')} responses):")
        for section, directive in (adaptive.get("sectionDirectives") or {}).items():
            print(f"    - {section}: {directive}")
    else:
        print("\n  No adaptive state -- cold start, the prompt carries no feedback directives yet.")


def print_reasoning(reasoning: dict, topics: list) -> None:
    """Reasoning — the chapter-wide prerequisites/misconceptions, and the
    knowledge chain, printed as one column: a break between two adjacent
    topics is obvious read this way and invisible read any other."""
    if not reasoning:
        print("  (no reasoning derived -- planning falls back to its own judgement)")
        return
    print(f"  arrives with: {'; '.join(reasoning.get('prerequisites') or []) or '(none)'}")

    try:
        from .agents.reasoning import anchor_groups
        for group in anchor_groups(reasoning):
            print(f"  objects [{group.get('strand') or 'main'}]: " + ", ".join(group["objects"]))
    except Exception:
        pass

    for entry in reasoning.get("misconceptions") or []:
        tagged = ", ".join(f"T{i}" for i in entry.get("topics") or []) or "chapter-wide"
        print(f"  they will think: {entry['belief']}  [{tagged}]")

    chain = reasoning.get("chain") or {}
    mastery = reasoning.get("mastery") or {}
    print("\n  THE KNOWLEDGE CHAIN -- read top to bottom:\n")
    previous_strand = None
    for spec in topics:
        entry = chain.get(spec["index"]) or {}
        strand = entry.get("strand") or "main"
        if previous_strand is not None and strand != previous_strand:
            print(f"        --- the chapter changes subject here: {previous_strand} -> {strand} ---")
        previous_strand = strand
        print(f"  T{spec['index']:>2}. {spec['topic']}  [{strand}]")
        if entry.get("assumes"):
            print(f"        assumes: {entry['assumes']}")
        print(f"        gained:  {entry.get('gained') or '(not derived)'}")
        if entry.get("bridgesTo"):
            print(f"        bridges to: {entry['bridgesTo']}")

        # Directly under `gained`, because the pair is the thing worth reading and
        # the failure worth catching is them being the same sentence twice. A
        # chapter whose targets all restate their gains has an audit that found the
        # easy answer every time, and printed anywhere else that is invisible.
        audit = mastery.get(spec["index"]) or mastery.get(str(spec["index"])) or {}
        if audit.get("masteryTarget"):
            print(f"        mastery: {audit['masteryTarget']}")
        for item in audit.get("missing") or []:
            print(f"        page lacks [{item.get('kind')}]: {item.get('missing')}")


def print_experience(experience: dict, topics: list) -> None:
    """Experience plan — the cognitive path per topic, side by side, because
    the failure to watch for is repetition: the same path with the nouns
    swapped, invisible one topic at a time."""
    by_index = {t["index"]: t for t in topics}
    for index in sorted(experience):
        plan = experience[index]
        if not plan.get("derived"):
            print(f"  T{index:>2}. (not derived -- falling back to the reasoning alone)")
            continue
        print(f"  T{index:>2}. {by_index.get(index, {}).get('topic', '')}")
        print(f"        path:     {' -> '.join(plan.get('trajectory') or []) or '(none)'}")
        print(f"        jump:     {plan.get('conceptualJump') or '(none)'}")
        print(f"        anchor:   {plan.get('anchor') or '(none)'}"
              + (f" -- {plan['anchorReason']}" if plan.get("anchorReason") else ""))
        print(f"        they do:  {plan.get('studentAction') or '(none)'}")
        print(f"        work out: {plan.get('inference') or '(none)'}")
        # Only when there is one. A period that closes nothing is the correct
        # answer for a page audited as complete, and printing "closes: (none)" on
        # every such topic would read as a run-wide failure rather than a book
        # doing its job.
        if plan.get("gap") and plan.get("gapCloser"):
            kind = plan.get("gapKind") or "?"
            print(f"        closes [{kind}]: {plan['gap']}")
            print(f"        with:     {plan['gapCloser']}")
        if plan.get("transferTask"):
            print(f"        transfer: {plan['transferTask']}")
    distinct = len({" -> ".join(p.get("trajectory") or []) for p in experience.values()
                   if p.get("trajectory")})
    print(f"\n  {distinct} distinct thinking path(s) across {len(experience)} topic(s)"
          + ("  -- repeated paths mean the concept was not actually analysed"
             if distinct < max(1, len(experience) // 2) else ""))

    # Reported, never scored. A low count here is a claim about the textbook
    # rather than about the run, and the only reading worth taking from it is the
    # one about transfer: a chapter with no transfer tasks has nothing that
    # separates a child who has the idea from one who remembers the example, and
    # the learner gate now measures against exactly that.
    closing = sum(1 for p in experience.values()
                  if p.get("gap") and p.get("gapCloser"))
    transfers = sum(1 for p in experience.values() if p.get("transferTask"))
    print(f"  {closing} period(s) close a shortfall the page was audited for; "
          f"{transfers} put an unshown case to the class")


def print_plans(plans: dict, chapter_arc: str) -> None:
    """Planning — per-period minutes, emphasis, and the seam each topic owes
    the next."""
    print(f"  ARC: {chapter_arc or '(none)'}\n")
    for index in sorted(plans):
        plan = plans[index]
        hook = plan.get("exploreHook") or {}
        print(f"  T{index:>2}. {plan.get('focus')}")
        print(f"        heaviest: {(plan.get('emphasis') or {}).get('heaviest')}"
              f" | step: {plan.get('difficultyStep')} | minutes: {plan.get('minutes')}")
        if plan.get("newVocabulary"):
            print(f"        new words: {', '.join(plan['newVocabulary'])}")
        if plan.get("refresherBridge"):
            print(f"        refresher brings back: {plan['refresherBridge']}")
        print(f"        explore leaves: {hook.get('question') or '(open)'}")


def print_selections(selections: dict) -> None:
    """Activity selection — the actual activity + context landed on for every
    topic, WHY it won over the other candidates (not just the outcome), and
    where it came from."""
    for index in sorted(selections):
        selection = selections[index]
        activity = selection.get("activity") or {}
        source = "invented" if selection.get("invented") else (
            "library" if activity.get("id") else "none")
        print(f"  T{index:>2}. {activity.get('name') or '(none)'} [{source}]"
              f" | context: {(selection.get('context') or {}).get('name') or '(none)'}")
        if selection.get("reason"):
            print(f"        why:     {selection['reason']}")
        formats = selection.get("formats") or {}
        if formats:
            print("        formats: " + " | ".join(f"{k}: {v}" for k, v in formats.items()))


def print_contract(document: dict) -> None:
    """The contract — Node 1's output, and the thing Node 2 is handed.

    Two of this module's printers used to sit here: `print_learner` (every
    topic's learner-simulation scores per dimension, and the actual question
    behind any that failed) and `print_findings` (every validation finding's
    real message, not just a count). Both read generated material, this package
    generates none, and both moved with their agents — see `_deferred/README.md`.

    What replaces them is not a verdict. It is a statement of what was derived
    and whether it is complete, which is the only claim Node 1 is now in a
    position to make about its own output.
    """
    if not document:
        print("  (no contract was built)")
        return

    readiness = document.get("readiness") or {}
    integrity = document.get("integrity") or {}
    print(f"  version {document.get('contractVersion')}  |  "
          f"{document.get('producedBy')} -> {document.get('consumedBy')}")
    print(f"  {readiness.get('topicsReady', 0)}/{readiness.get('topicsTotal', 0)} "
          f"topic(s) complete  |  generationReady="
          f"{readiness.get('generationReady')}")
    print(f"  preserve: {', '.join(document.get('preserve') or [])}")
    print(f"  fingerprint: {integrity.get('chapterFingerprint', '(none)')}")

    chapter_wide = document.get("chapterWide") or {}
    if chapter_wide.get("prerequisites"):
        print()
        print(f"  chapter prerequisites ({len(chapter_wide['prerequisites'])}):")
        for item in chapter_wide["prerequisites"]:
            print(f"      - {item}")
    if chapter_wide.get("misconceptions"):
        print()
        print(f"  chapter misconceptions ({len(chapter_wide['misconceptions'])}):")
        for entry in chapter_wide["misconceptions"]:
            topics = ", ".join(f"T{t}" for t in entry.get("topics") or [])
            print(f"      - {entry.get('belief')}   [{topics}]")

    print()
    print(_RULE)
    for row in document.get("topics") or []:
        ready = (row.get("readiness") or {}).get("ready")
        missing = (row.get("readiness") or {}).get("missing") or []
        academic = row.get("academic") or {}
        chain = row.get("knowledgeChain") or {}
        experience = row.get("experiencePlan") or {}
        activity = row.get("selectedActivity") or {}
        audit = row.get("masteryAudit") or {}
        grounding = row.get("grounding") or {}

        print(f"  T{row.get('index'):<3} {str(row.get('topic'))[:60]}"
              + ("" if ready else f"   [NOT READY: {', '.join(missing)}]"))
        print(f"        pages    p{grounding.get('pageStart')}-{grounding.get('pageEnd')} "
              f"({grounding.get('movesSource')}, "
              f"{len(grounding.get('bookOrder') or [])} book move(s))")
        print(f"        mastery  {str(academic.get('masteryTarget') or '(none derived)')[:88]}")
        print(f"        gains    {str(chain.get('gained') or '')[:88]}")
        if chain.get("bridgesTo"):
            print(f"        bridges  {str(chain['bridgesTo'])[:88]}")
        concepts = academic.get("requiredConcepts") or []
        if concepts:
            print(f"        concepts {len(concepts)}: {', '.join(concepts[:6])}"
                  + (" ..." if len(concepts) > 6 else ""))
        competencies = academic.get("competencies") or []
        if competencies:
            print(f"        compets  {len(competencies)}: {', '.join(competencies[:4])}"
                  + (" ..." if len(competencies) > 4 else ""))
        if experience.get("trajectory"):
            print(f"        path     {' -> '.join(experience['trajectory'])[:88]}")
        if experience.get("anchor"):
            print(f"        anchor   {experience['anchor']}  ({experience.get('anchorReason', '')[:56]})")
        # The audited shortfall this period closes, and the ones it does not.
        # Both matter to a contextual layer: the first is spoken for, and the
        # rest are where a justified local adaptation has somewhere to land.
        closes = experience.get("closesGap") or {}
        if closes.get("kind"):
            print(f"        closes   [{closes['kind']}] {str(closes.get('gap'))[:74]}")
        remaining = [m for m in (audit.get("missing") or [])
                     if m.get("kind") != closes.get("kind")]
        if remaining:
            print(f"        open     " + "; ".join(f"[{m['kind']}] {m['missing'][:40]}"
                                                   for m in remaining))
        if activity.get("name"):
            print(f"        activity {activity['name']}  ({activity.get('source')}"
                  + (f", {activity['context']}" if activity.get("context") else "") + ")")
        sections = row.get("sectionSpec") or []
        if sections:
            print("        sections " + "  ".join(
                f"{e.get('label')}:{e.get('minutes')}m" for e in sections))
