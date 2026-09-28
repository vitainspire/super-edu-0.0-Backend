"""CLI for the chapter pipeline — the tool the prompts get iterated on.

Runs the real graph in the foreground against a real chapter, printing what each
agent produced, so a change to a prompt can be judged before it reaches a
teacher. Works with no database at all (in-memory checkpointer, nothing
persisted), which is the mode most of this work happens in.

    # what can I generate from?
    python -m prep_flow.cli list --folder "data/books/Class 3 - Maths - 3EM_MAT"

    # sequence and plan only — no generation, no LLM spend on prose
    python -m prep_flow.cli chapter --folder "data/books/Class 3 - Maths - 3EM_MAT" \\
        --chapter 2 --grade 3 --subject Maths --topics 8 --stop-after lesson_design

    # the whole thing, written to a Markdown file
    python -m prep_flow.cli chapter --folder "data/books/Class 3 - Maths - 3EM_MAT" \\
        --chapter 2 --grade 3 --subject Maths --topics 30 --out chapter02.md

    # one turn of the feedback loop
    python -m prep_flow.cli feedback --grade 3 --subject Maths --window 40
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

# Windows' default console codepage cannot encode the box-drawing and arrow
# characters below — same reason scripts/prep_material_cli.py does this.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# The repo root, which is now this file's GRANDparent: every CLI moved into the
# package it drives. Both it and its parent go on the path — the second is what
# lets a flat prep-material checkout and the real backend tree both import
# `ai`, `canonical_mapping` and the other modules that live beside the package.
_ROOT = Path(__file__).resolve().parent.parent
for candidate in (_ROOT, _ROOT.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

try:
    from dotenv import load_dotenv
    for env in (_ROOT / ".env", _ROOT.parent / ".env"):
        if env.exists():
            load_dotenv(env)
            break
except ImportError:
    pass

from prep_flow.deps import ai_module  # noqa: E402  (model names for the run banner)
from prep_flow import contract as contract_module  # noqa: E402
from prep_flow import db, gate, provenance, terminal, tools, trace  # noqa: E402
from prep_flow.agents import reasoning as reasoning_agent  # noqa: E402
from prep_flow.health import feedback_health  # noqa: E402
from prep_flow.graph import build_chapter_graph, recursion_limit_for, run_feedback_cycle  # noqa: E402
from prep_flow.state import DEFAULT_CONFIG  # noqa: E402

_RULE = "─" * 78


def _header(title: str) -> None:
    print(f"\n{_RULE}\n{title}\n{_RULE}")


# ── list ─────────────────────────────────────────────────────────────────────

def cmd_books(args) -> None:
    """Every book the textbook API publishes — the `--book` ids `chapter` accepts."""
    _header(f"PUBLISHED BOOKS — {tools.API_BASE}")
    books = tools.list_api_books()
    if not books:
        print("  (none — the API returned nothing. Cold-starting free hosts can take "
              "~30-60s on the first request; try again.)")
        return
    for b in books:
        print(f"  {b['bookId']}")
        print(f"      grade {b['grade']}  {b['subject']}  "
              f"({b['chapterCount']} chapters, {b['board']}, {b['language']})")


def cmd_list(args) -> None:
    if args.book:
        chapters = tools.list_api_chapters(args.book)
        _header(f"Chapters in {args.book} (textbook API)")
    else:
        chapters = tools.list_local_chapters(args.folder)
        _header(f"Chapters in {args.folder}")
    print(f"{'#':>3}  {'pages':>9}  {'ingested':>9}  {'md':>3}  title")
    for c in chapters:
        mark = "✓" if c["usable"] else "✗"
        print(f"{c['chapterNumber']:>3}  "
              f"{str(c['pageStart']) + '-' + str(c['pageEnd']):>9}  "
              f"{str(c['pagesIngested']) + '/' + str(c['pagesTotal']):>9}  "
              f"{'yes' if c['hasChapterMarkdown'] else ' no':>3}  {mark} {c['chapterTitle']}")
    usable = [c["chapterNumber"] for c in chapters if c["usable"]]
    print(f"\nUsable: {', '.join(map(str, usable)) or 'none'}")
    print("A chapter with no ingested pages and no chapter markdown has nothing to "
          "generate from — the sheets would be grounded in an empty string.")


# ── chapter ──────────────────────────────────────────────────────────────────

def _print_topics(topics: list) -> None:
    for spec in topics:
        print(f"  T{spec['index']:>2}. {spec['topic']}"
              + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
              + f"   [p{spec.get('page_start')}"
              + (f"-{spec.get('page_end')}" if spec.get("page_end") != spec.get("page_start") else "")
              + f", {len(spec.get('excerpt') or '')} chars]")
        if spec.get("why"):
            print(f"        why here: {spec['why']}")


def _print_context(topics: list, adaptive: dict) -> None:
    for spec in topics:
        knowledge = spec.get("knowledge") or {}
        print(f"  T{spec['index']:>2}. competencies: "
              f"{', '.join(knowledge.get('competencies') or []) or '(none)'}"
              f" | contexts: {', '.join(knowledge.get('contexts') or []) or '(none)'}"
              f" | {len(spec.get('activities') or [])} activity template(s)")
    if adaptive:
        print(f"\n  Adaptive state v{adaptive.get('_version')} in force "
              f"({adaptive.get('_sampleSize')} responses):")
        for section, directive in (adaptive.get("sectionDirectives") or {}).items():
            print(f"    - {section}: {directive}")
    else:
        print("\n  No adaptive state — cold start, the prompt carries no feedback "
              "directives yet.")


def _print_reasoning(reasoning: dict, topics: list) -> None:
    """The chapter block, then the chain as one column.

    Printed as a column on purpose: the chain is the artefact that is only wrong
    when you read it end to end. A break between T6 and T7 is invisible in two
    separate paragraphs and obvious in two adjacent lines.
    """
    if not reasoning:
        print("  (no reasoning derived — planning falls back to its own judgement)")
        return
    print(f"  arrives with:   {'; '.join(reasoning.get('prerequisites') or []) or '(none)'}")
    for group in reasoning_agent.anchor_groups(reasoning):
        print(f"  objects [{group.get('strand') or 'main'}]: "
              + ", ".join(group["objects"]))
    for entry in reasoning.get("misconceptions") or []:
        tagged = ", ".join(f"T{i}" for i in entry.get("topics") or []) or "chapter-wide"
        print(f"  ✗ they will think: {entry['belief']}  [{tagged}]")

    chain = reasoning.get("chain") or {}
    print("\n  THE KNOWLEDGE CHAIN — read this top to bottom; a break shows up as two")
    print("  adjacent lines that are not about the same thing:\n")
    previous_strand = None
    for spec in topics:
        entry = chain.get(spec["index"]) or {}
        strand = entry.get("strand") or "main"
        if previous_strand is not None and strand != previous_strand:
            # Drawn, not mentioned. A thread boundary is the one place the chain is
            # SUPPOSED to stop, and a reader scanning the column needs to see the
            # difference between that and a chain that simply failed.
            print(f"        ╌╌╌ the chapter changes subject here: "
                  f"{previous_strand} → {strand} ╌╌╌")
        previous_strand = strand
        print(f"  T{spec['index']:>2}. {spec['topic']}  [{strand}]")
        if entry.get("assumes"):
            print(f"        ↑ stands on: {entry['assumes']}")
        print(f"        ● leaves:    {entry.get('gained') or '(not derived)'}")
        if entry.get("bridgesTo"):
            print(f"        ↓ becomes:   {entry['bridgesTo']}")


def _print_experience(plans: dict, topics: list) -> None:
    """Trajectories side by side, because the failure is repetition.

    A model asked for a thinking path per topic will happily give the same path
    six times with the nouns swapped, and that is invisible one topic at a time.
    """
    by_index = {t["index"]: t for t in topics}
    for index in sorted(plans):
        plan = plans[index]
        if not plan.get("derived"):
            print(f"  T{index:>2}. (not derived — falling back to the reasoning alone)")
            continue
        print(f"  T{index:>2}. {by_index.get(index, {}).get('topic', '')}")
        print(f"        path:     {' -> '.join(plan.get('trajectory') or []) or '(none)'}")
        print(f"        jump:     {plan.get('conceptualJump') or '(none)'}")
        print(f"        with:     {plan.get('anchor') or '(none)'}"
              + (f" — {plan['anchorReason']}" if plan.get("anchorReason") else ""))
        print(f"        they do:  {plan.get('studentAction') or '(none)'}")
        print(f"        work out: {plan.get('inference') or '(none)'}")
    distinct = len({" -> ".join(p.get("trajectory") or []) for p in plans.values()
                    if p.get("trajectory")})
    print(f"\n  {distinct} distinct thinking path(s) across {len(plans)} topics"
          + ("  — repeated paths mean the concept was not actually analysed"
             if distinct < max(1, len(plans) // 2) else ""))


def _print_plans(plans: dict, chapter_arc: str) -> None:
    print(f"  ARC: {chapter_arc or '(none)'}\n")
    for index in sorted(plans):
        plan = plans[index]
        hook = plan.get("exploreHook") or {}
        print(f"  T{index:>2}. {plan.get('focus')}")
        print(f"        heaviest: {(plan.get('emphasis') or {}).get('heaviest')}"
              f" | {plan.get('difficultyStep')} | {plan.get('minutes')}")
        if plan.get("refresherBridge"):
            print(f"        ← refresher brings back: {plan['refresherBridge']}")
        print(f"        → explore leaves: {hook.get('question') or '(open)'}")


def _print_selections(selections: dict) -> None:
    for index in sorted(selections):
        selection = selections[index]
        activity = selection.get("activity") or {}
        source = "invented" if selection.get("invented") else (
            "library" if activity.get("id") else "none")
        print(f"  T{index:>2}. {activity.get('name') or '(none)'} [{source}]"
              f" | context: {(selection.get('context') or {}).get('name') or '—'}")
        formats = selection.get("formats") or {}
        if formats:
            print("        " + " | ".join(f"{k}: {v}" for k, v in formats.items()))


def _print_contract(document: dict) -> None:
    """Node 1's output. Delegated to prep_flow/terminal.py so the CLI and the
    viewer print the same thing.

    `_print_learner` (per-dimension learner scores and the question behind any
    that failed) and `_print_findings` (every validation finding's real message)
    were here. Both read generated material; see prep_flow/graph.py for where
    the stages that produced it went.
    """
    terminal.print_contract(document)


# The graph's node names, in order — see prep_flow/graph.py. Four nodes: the
# sub-stages that used to be stoppable (move extraction, planning, ...) are not
# boundaries, because they run inside a merged node with no checkpoint between
# them, and generation/validation/simulation are not here at all.
#
# `persist` is deliberately absent. It is where the contract is BUILT, so
# stopping after it is the same as not stopping — and stopping before it means
# no contract, which is the one thing a run of this CLI is for.
_STOP_AFTER = ("sequencing", "curriculum", "lesson_design")


def _print_moves(topics: list) -> None:
    """Each topic's move sequence and the section order it implies.

    Prints the order line only when it differs from the canonical six, because a
    forty-topic chapter that agrees with the canonical order everywhere should
    say so by staying quiet rather than by repeating itself forty times.
    """
    from prep_flow.moves import reordered, teaching_order

    for spec in topics:
        moves = spec.get("moves") or []
        source = spec.get("moves_source") or "none"
        print("")
        print(f"  T{spec['index']} {spec.get('topic')}  "
              f"[{len(moves)} move(s), read from {source}]")
        for move in moves:
            page = f"p{move['page']}" if move.get("page") else "p?"
            print(f"     {move['ord']}. {page:>4}  {move['type']:<14} {move['gist'][:66]}")
            if move.get("verbatim"):
                print(f'              the book: "{move["verbatim"][:72]}"')
        if reordered(moves):
            print(f"     -> teaches in the BOOK'S order: "
                  f"{' > '.join(teaching_order(moves))}")


def _resolve_api_chapter(args) -> dict:
    """--book resolution: grade/subject come from the book itself (it cannot be
    Class 5 EVS and generate as grade 3 Maths), and a --chapter-title is matched
    the same way the local-folder path matches one, since the API only takes a
    chapter number."""
    books = {b["bookId"]: b for b in tools.list_api_books()}
    book = books.get(args.book)
    if not book:
        print(f"no published book '{args.book}'. Available:")
        for b in books.values():
            print(f"  {b['bookId']}  (grade {b['grade']}, {b['subject']})")
        sys.exit(1)
    args.grade = args.grade or book["grade"]
    args.subject = args.subject or book["subject"]

    chapters = tools.list_api_chapters(args.book)
    number = args.chapter
    if number is None and args.chapter_title:
        best = sorted(chapters, key=lambda c: tools.title_score(
            args.chapter_title, c["chapterTitle"]), reverse=True)[0]
        if tools.title_score(args.chapter_title, best["chapterTitle"]) <= 0:
            print(f'no chapter matches "{args.chapter_title}". Available:')
            for c in chapters:
                print(f"  {c['chapterNumber']:>2}. {c['chapterTitle']}")
            sys.exit(1)
        number = best["chapterNumber"]
    if number is None:
        print(f"give --chapter N or --chapter-title for {args.book}. Chapters:")
        for c in chapters:
            print(f"  {c['chapterNumber']:>2}. {c['chapterTitle']}")
        sys.exit(1)

    chapter = tools.load_api_chapter(args.book, number)
    if not chapter:
        print(f"chapter {number} of {args.book} has no content on the API")
        sys.exit(1)
    return chapter


async def run_chapter_cli(args) -> None:
    if args.book:
        chapter = _resolve_api_chapter(args)
    elif args.folder:
        chapter = tools.load_local_chapter(args.folder, chapter_number=args.chapter,
                                           chapter_title=args.chapter_title)
    else:
        chapter = await tools.load_chapter(school_id=args.school, grade=args.grade,
                                           subject=args.subject, chapter_number=args.chapter,
                                           chapter_title=args.chapter_title)
    _header(f"CHAPTER {chapter.get('chapterNumber')}: {chapter['chapterTitle']}")
    print(f"pages {chapter.get('pageStart')}-{chapter.get('pageEnd')} | "
          f"{len(chapter['markdown'])} chars | source: {chapter.get('source')}")

    config = {
        **DEFAULT_CONFIG,
        "target_topics": args.topics,
        "min_topics": min(args.min_topics, args.topics),
        "max_topics": max(args.topics, args.min_topics),
        "moves_window": args.moves_window,
    }
    print(f"config: {json.dumps(config)}")

    # A real uuid, because persist_node writes to Supabase whenever it is
    # configured and prep_flow_runs.id is a uuid column — the previous "cli"
    # placeholder made every CLI run end with two 22P02 errors and no stored
    # material. `--no-persist` is the way to skip storage, not a fake id.
    run_id = args.run_id or str(uuid.uuid4())
    persist = db.configured() and not args.no_persist
    if persist:
        schema = await db.verify_schema()
        if not schema["ok"]:
            print(f"\n! Missing table(s) {', '.join(schema['missing'])} — apply "
                  f"{db.MIGRATION}. Continuing without persistence.")
            persist = False
    if persist:
        # The run row has to exist before persist_node's topics reference it.
        await db.create_run({
            "id": run_id, "thread_id": args.thread or f"cli:{run_id}",
            "school_id": args.school, "class_id": args.class_id,
            "grade": str(args.grade), "subject": args.subject,
            "chapter_number": chapter.get("chapterNumber"),
            "chapter_title": chapter["chapterTitle"], "status": "running",
            "config": config,
        })
        print(f"persisting to Supabase as run {run_id}")
    elif args.no_persist:
        print("not persisting (--no-persist)")
    else:
        print("not persisting — Supabase is not configured "
              "(NEXT_PUBLIC_SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    initial = {
        # persist_node keys off this: None means "do not store", which is what a
        # pure prompt-iteration run wants.
        "run_id": run_id if persist else None,
        "thread_id": args.thread or f"cli:{args.run_id or 'chapter'}",
        # Always False here: this CLI builds and streams the graph itself and
        # awaits every step directly (see the astream loop below) — nothing
        # schedules it to keep running after this process has moved on, which is
        # the actual distinction background_task records. See provenance.py.
        "background_task": False,
        "school_id": args.school, "class_id": args.class_id,
        "grade": str(args.grade), "subject": args.subject,
        "chapter_title": chapter["chapterTitle"],
        "chapter_number": chapter.get("chapterNumber"),
        "chapter_markdown": chapter["markdown"],
        "page_start": chapter.get("pageStart"), "page_end": chapter.get("pageEnd"),
        "chapter_figures": chapter.get("figures") or [],
        "teacher_settings": {
            "duration": args.duration, "classSize": args.class_size,
            "resourceLevel": args.resource_level, "language": args.language,
            "learningObjective": args.objective, "teachingStyle": args.teaching_style,
            "plainLanguage": args.plain_language,
        },
        "config": config, "status": "running",
        "plans": {}, "selections": {}, "metrics": {}, "errors": [],
    }

    stop_at = _STOP_AFTER.index(args.stop_after) if args.stop_after else len(_STOP_AFTER)
    printed: set[str] = set()

    # The data-flow trace: which agent produced each input, what each node
    # actually wrote, which agent reads it next, and every LLM call, attributed.
    # Activated for the run so ai.py can post each model call into it. See
    # prep_flow/trace.py — the viewer's backend prints the identical thing.
    tracer = trace.activate(trace.RunTrace(
        f"PREP FLOW RUN -- {args.subject} grade {args.grade}, "
        f"chapter {chapter.get('chapterNumber')}: {chapter['chapterTitle']}"))
    tracer.banner([
        f"source:    {chapter.get('source')} "
        f"({len(chapter['markdown']):,} chars, pages {chapter.get('pageStart')}-{chapter.get('pageEnd')})",
        f"models:    primary {ai_module.MODEL} | fallback {ai_module.FALLBACK_MODEL}",
        f"topics:    target {config['target_topics']} "
        f"(bounds {config['min_topics']}-{config['max_topics']})",
        f"output:    the academic contract for NODE 2 — no sheet is written here "
        f"and none is judged",
        f"window:    {config['moves_window']} topic(s) per move-extraction call",
        f"persisted: {'yes, run ' + run_id if persist else 'no'}",
    ])

    # Streamed rather than invoked, so each agent's output is printed as it lands
    # — on a 40-topic run the generation loop alone is several minutes, and a CLI
    # that shows nothing until the end is unusable for prompt work.
    async with db.checkpointer() as saver:
        graph = build_chapter_graph(saver)
        final: dict = dict(initial)
        async for update in graph.astream(
            initial,
            {"configurable": {"thread_id": args.thread or f"cli:{args.run_id or 'chapter'}"},
             "recursion_limit": recursion_limit_for(config["max_topics"], config)},
            stream_mode="updates",
        ):
            for node, delta in update.items():
                final = _merge(final, delta)
                # The wiring block first — what this node read, from whom, what
                # it wrote, who takes it next, and what it spent on the model.
                try:
                    tracer.stage(node, final, delta)
                except Exception as exc:
                    print(f"[prep_flow:console-trace] could not trace '{node}' "
                          f"(the run itself is unaffected): {exc}")
                # Printed unconditionally, even on a node this loop has already
                # shown its full block for once — that's the whole point of a
                # live line: a second validation/simulation pass after repair, or
                # the next generation window, gets its own line, so numbers like
                # blockingFindings visibly change round to round instead of only
                # appearing once at the top.
                line = provenance.live_line(node, final)
                if line:
                    print(f"  METRICS {line}")

                if node in printed:
                    continue
                printed.add(node)

                if node == "sequencing":
                    _header("SEQUENCING — the ordered spine")
                    print(f"  note: {final.get('sequencing_note')}")
                    _print_topics(final.get("topics") or [])
                # One node, three sub-stages, still printed as three: they are
                # separate artefacts a reader checks separately, and collapsing
                # them into one block because the graph collapsed the node would
                # make the output worse to read for no gain. What is gone is the
                # ability to see them land one at a time — the node commits once,
                # so all three appear together when it finishes.
                elif node == "curriculum":
                    _header("MOVE EXTRACTION — the order the BOOK explains each topic in")
                    _print_moves(final.get("topics") or [])
                    _header("CONTEXT ASSEMBLY — competencies, activities, adaptive state")
                    _print_context(final.get("topics") or [], final.get("adaptive_state") or {})
                    cached = (final.get("metrics") or {}).get("reasoning_cached")
                    _header("REASONING + MASTERY — what the textbook does not say, "
                            "and what mastering it needs"
                            + ("  (from cache)" if cached else ""))
                    _print_reasoning(final.get("reasoning") or {},
                                     final.get("topics") or [])
                elif node == "lesson_design":
                    cached = (final.get("metrics") or {}).get("experience_cached")
                    _header("EXPERIENCE PLAN — the thinking path, and the object for it"
                            + ("  (from cache)" if cached else ""))
                    _print_experience(final.get("experience") or {},
                                      final.get("topics") or [])
                    _header("PLANNING — what each period needs, and owes the next")
                    _print_plans(final.get("plans") or {}, final.get("chapter_arc") or "")
                    _header("ACTIVITY SELECTION — one per topic, varied across the chapter")
                    _print_selections(final.get("selections") or {})
                elif node == "persist":
                    _header("CONTRACT — what Node 2 is handed")
                    _print_contract(final.get("contract") or {})

            if any(n in _STOP_AFTER[stop_at:] for n in update) and args.stop_after:
                print(f"\n(--stop-after {args.stop_after} — stopping here.)")
                break

    try:
        tracer.summary(final)
    except Exception as exc:
        print(f"[prep_flow:console-trace] summary failed (run unaffected): {exc}")
    finally:
        trace.deactivate()

    _header("RESULT")
    print(f"status: {final.get('status')}")
    print(f"metrics: {json.dumps(final.get('metrics') or {}, indent=2)}")
    if final.get("errors"):
        print("errors:")
        for error in final["errors"]:
            print(f"  - {error}")

    document = final.get("contract") or {}
    if document:
        print()
        print(contract_module.summary(document))
        blob = json.dumps(document, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(blob, encoding="utf-8")
            print(f"wrote {len(blob)} chars to {args.out} — this file is Node 2's input")
        elif args.json:
            print()
            print(blob)
        else:
            # The contract is already printed topic by topic above, in the
            # CONTRACT block. Dumping the raw JSON again by default would bury
            # it; --json asks for the machine version and --out writes it.
            print("(pass --json to print the contract document, or --out FILE "
                  "to write it)")
    elif args.stop_after:
        print(f"\n(no contract: --stop-after {args.stop_after} stopped the run "
              f"before persist, which is where it is built)")



def _merge(state: dict, delta: dict) -> dict:
    """Apply a streamed node update the way the graph's reducers would.

    Only the index-keyed maps and the appended list need special handling; those
    are exactly the fields a node returns partially, and replacing them wholesale
    would make the CLI show three plans where the graph has forty.
    """
    merged = dict(state)
    for key, value in (delta or {}).items():
        if key in ("plans", "selections", "experience", "metrics") and isinstance(value, dict):
            merged[key] = {**(merged.get(key) or {}), **value}
        elif key == "errors" and isinstance(value, list):
            merged[key] = (merged.get(key) or []) + value
        elif value is not None:
            merged[key] = value
    return merged


# ── feedback ─────────────────────────────────────────────────────────────────

async def run_feedback_cli(args) -> None:
    _header(f"FEEDBACK CYCLE — grade {args.grade} {args.subject}, "
            f"last {args.window} days")
    if not db.configured():
        print("Supabase is not configured, so there is no telemetry to read and this "
              "will report an empty window. Set NEXT_PUBLIC_SUPABASE_URL and "
              "SUPABASE_SERVICE_ROLE_KEY to use the feedback loop.")
    else:
        schema = await db.verify_schema()
        if not schema["ok"]:
            print(f"Missing table(s): {', '.join(schema['missing'])} — apply "
                  f"{db.MIGRATION} first.")

    result = await run_feedback_cycle(
        grade=args.grade, subject=args.subject, school_id=args.school,
        window_days=args.window)

    print(f"responses in window: {result.get('sample_size', 0)}")
    aggregates = result.get("aggregates") or {}
    overall = aggregates.get("overall") or {}
    if overall.get("score") is not None:
        print(f"overall: {overall['score']:.2f} "
              f"({overall['yes']} yes / {overall['somewhat']} somewhat / {overall['no']} no)")
    for section, stats in (aggregates.get("sections") or {}).items():
        if stats.get("score") is not None:
            print(f"  {section:>10}: {stats['score']:.2f}  (n={stats['n']})")

    print("\npatterns matched:")
    for pattern in result.get("patterns") or []:
        print(f"  - [{pattern['kind']}] {pattern['detail']}")

    print(f"\napplied: {result.get('applied')}")
    print("changelog:")
    for line in result.get("changelog") or []:
        print(f"  - {line}")
    print("\nnew adaptive state:")
    print(json.dumps(result.get("adaptive_state") or {}, indent=2, ensure_ascii=False))


# ── golden fixture + loop health ─────────────────────────────────────────────

async def run_golden_cli(args) -> None:
    """Freeze a golden fixture for the activation gate.

    Run rarely and deliberately: everything the gate compares afterwards reuses
    these exact topics, which is what makes two gate runs a month apart
    comparable at all.
    """
    from pathlib import Path as _Path
    chapter = (
        tools.load_local_chapter(args.folder, chapter_number=args.chapter,
                                 chapter_title=args.chapter_title)
        if args.folder else
        await tools.load_chapter(school_id=args.school, grade=args.grade,
                                 subject=args.subject, chapter_number=args.chapter,
                                 chapter_title=args.chapter_title))
    out = _Path(args.out) if args.out else gate.golden_path(
        db.scope_key(args.school, args.grade, args.subject))
    _header(f"FREEZING GOLDEN FIXTURE -> {out}")
    print(f"chapter {chapter.get('chapterNumber')}: {chapter['chapterTitle']} "
          f"({len(chapter['markdown'])} chars)")
    fixture = await gate.build_golden(
        chapter_markdown=chapter["markdown"], chapter_title=chapter["chapterTitle"],
        chapter_number=chapter.get("chapterNumber"), grade=args.grade,
        subject=args.subject, page_start=chapter.get("pageStart"),
        page_end=chapter.get("pageEnd"), topics=args.topics, out=out)
    for spec in fixture["topics"]:
        print(f"  T{spec['index']}. {spec['topic']} "
              f"[p{spec.get('page_start')}-{spec.get('page_end')}, "
              f"{len(spec.get('excerpt') or '')} chars, "
              f"{len(spec.get('moves') or [])} moves]")
    # Reported because a fixture with no moves silently pins `book_order` to 3 on
    # every gate and regression run it is ever used for: judge_sheet is told to
    # abstain when a topic carries none, and a criterion stuck at a constant
    # dilutes every real movement in the other nine by a tenth. The fixture
    # frozen before move extraction existed did exactly that, unnoticed.
    extracted = fixture.get("movesExtracted", 0)
    if not extracted:
        print("\n  WARNING: no topic carries the book's own explanatory order, so "
              "`book_order`\n  will score a flat 3 everywhere this fixture is used.")
    else:
        print(f"\n  {extracted}/{len(fixture['topics'])} topics carry the book's "
              f"explanatory order — `book_order` is measurable.")
    print(f"\nwrote {out}. Enable the gate with PREP_FLOW_ACTIVATION_GATE=1.")


async def run_health_cli(args) -> None:
    _header(f"FEEDBACK LOOP HEALTH — grade {args.grade} {args.subject}")
    if not db.configured():
        print("Supabase is not configured; nothing to report.")
        return
    h = await feedback_health(args.school, args.grade, args.subject, args.window)
    mark = {"healthy": "OK", "warming": "..", "stale": "!!", "silent": "!!",
            "broken": "XX"}.get(h["status"], "??")
    print(f"[{mark}] {h['status'].upper()}  scope={h['scopeKey']}")
    print(f"  responses: {h['responsesInWindow']} in {h['windowDays']}d "
          f"({h['responsesEver']} ever)")
    print(f"  last response: {h.get('daysSinceLastResponse')} days ago")
    print(f"  last cycle   : {h.get('daysSinceLastCycle')} days ago")
    print(f"  active state : v{h.get('activeVersion')} {h['stateContent']}")
    for issue in h["issues"]:
        print(f"  -> {issue}")


# ── argument parsing ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("books", help="which books the textbook API publishes")

    listing = sub.add_parser("list", help="which chapters can be generated from")
    listing_source = listing.add_mutually_exclusive_group(required=True)
    listing_source.add_argument("--folder", help="an ingested textbook folder")
    listing_source.add_argument("--book", help="a published book id from the textbook API")

    chapter = sub.add_parser("chapter", help="generate a chapter batch")
    source = chapter.add_argument_group("curriculum source")
    source.add_argument("--book", help="a published book id from the textbook API "
                                       "(see `books`) — wins over --folder/--school")
    source.add_argument("--folder", help="ingested textbook folder (wins over --school)")
    source.add_argument("--school", help="schoolId, to read published textbook_chapters")
    source.add_argument("--chapter", type=int, help="chapter number")
    source.add_argument("--chapter-title", help="match a chapter by title instead")

    # Required unless --book, which reads its own grade/subject from the book.
    chapter.add_argument("--grade", default=None)
    chapter.add_argument("--subject", default=None)
    chapter.add_argument("--class-id", default=None, help="for class interests/weak topics")

    chapter.add_argument("--topics", type=int, default=30, help="target topic count (default 30)")
    chapter.add_argument("--min-topics", type=int, default=6)
    chapter.add_argument("--moves-window", type=int, default=4,
                         help="topics per move-extraction call (default 4)")

    chapter.add_argument("--duration", type=int, default=30, help="minutes per period")
    chapter.add_argument("--class-size", type=int, default=40)
    chapter.add_argument("--resource-level", type=int, default=0, choices=[0, 1, 2])
    chapter.add_argument("--language", default="English")
    chapter.add_argument("--objective", default="new lesson")
    chapter.add_argument("--teaching-style", default="interactive")
    chapter.add_argument("--plain-language", action="store_true",
                         help="wording only — short sentences, everyday words; "
                              "same numbers and thinking demand as the grade")

    # --window / --repair-rounds / --visuals / --learner-threshold /
    # --no-simulate were here. Every one of them tuned generation, the repair
    # loop or the learner gate; all three now run after Node 2 or inside Node 3.
    chapter.add_argument("--stop-after", choices=_STOP_AFTER, default=None,
                         help="stop after this node — no contract is built, since "
                              "that happens at persist")
    chapter.add_argument("--out", default=None,
                         help="write the contract here as JSON — this file is Node 2's input")
    chapter.add_argument("--json", action="store_true",
                         help="print the contract document to stdout")
    chapter.add_argument("--run-id", default=None, help="uuid; generated if omitted")
    chapter.add_argument("--no-persist", action="store_true",
                         help="do not store this run in Supabase (pure prompt iteration)")
    chapter.add_argument("--thread", default=None,
                         help="reuse a thread_id to resume an interrupted run")

    golden = sub.add_parser("golden", help="freeze a golden fixture for the gate")
    golden.add_argument("--folder", help="ingested textbook folder")
    golden.add_argument("--school", default=None)
    golden.add_argument("--chapter", type=int, default=None)
    golden.add_argument("--chapter-title", default=None)
    golden.add_argument("--grade", required=True)
    golden.add_argument("--subject", required=True)
    golden.add_argument("--topics", type=int, default=4,
                        help="topics in the fixture (default 4 — each costs ~$0.008 per gate arm)")
    golden.add_argument("--out", default=None)

    health = sub.add_parser("health", help="is the feedback loop alive and fed?")
    health.add_argument("--grade", required=True)
    health.add_argument("--subject", required=True)
    health.add_argument("--school", default=None)
    health.add_argument("--window", type=int, default=40)

    feedback = sub.add_parser("feedback", help="run one turn of the feedback loop")
    feedback.add_argument("--grade", required=True)
    feedback.add_argument("--subject", required=True)
    feedback.add_argument("--school", default=None)
    feedback.add_argument("--window", type=int, default=40, help="days (default 40)")

    args = parser.parse_args()
    if args.command == "books":
        cmd_books(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "golden":
        if not args.folder and not args.school:
            parser.error("pass --folder or --school")
        asyncio.run(run_golden_cli(args))
    elif args.command == "health":
        asyncio.run(run_health_cli(args))
    elif args.command == "chapter":
        if not (args.book or args.folder or args.school):
            parser.error("pass --book, --folder, or --school")
        if not args.book and not (args.grade and args.subject):
            parser.error("--grade and --subject are required unless --book "
                         "(the book carries its own)")
        asyncio.run(run_chapter_cli(args))
    else:
        asyncio.run(run_feedback_cli(args))


if __name__ == "__main__":
    main()
