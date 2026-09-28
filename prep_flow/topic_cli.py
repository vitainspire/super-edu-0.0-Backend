"""Ask for a topic, get its academic contract — the narrow-scope Node 1 CLI.

The chapter CLI answers "derive all of chapter 2". This answers the question
that comes up far more often: *"I'm teaching tracing 2D shapes on Thursday —
what does that lesson actually have to land?"* You name the topic, it finds
where that topic lives in an ingested textbook, and it derives Node 1's contract
for it.

IT NO LONGER RETURNS A SHEET. It used to end in the rendered prep material, and
generation left this package when the master orchestration was split into nodes
— it runs downstream of Node 2 now (see prep_flow/graph.py). What comes back is
the mastery target, the chain, the cognitive path, the period plan and the
activity: everything the sheet would have been written FROM.

    python -m prep_flow.topic_cli --folder "data/books/Class 3 - Maths - 3EM_MAT" \
        --topic "Trace and identify 2D shapes"

FINDING THE TOPIC IS THE NEW PART. Everything after it is the existing pipeline,
unchanged: move extraction, knowledge, reasoning, experience plan, planning,
activity selection. The search scores every ingested page against the words of
the request, picks the peak, and widens to a teachable window around it.

Scored on content words, not on a model call. "Trace and identify 2D shapes"
against a page that says "trace around the match-box and name the shape you get"
overlaps on trace/shape and wins on arithmetic — which is instant, free, and
inspectable, and the ranking is printed so a wrong match is obvious before any
tokens are spent. `--search-only` stops there deliberately: seeing which pages
were chosen is usually enough to know whether the request was understood.

WHY IT DERIVES A SMALL WINDOW RATHER THAN ONE TOPIC IN ISOLATION. A single topic
derived alone has no predecessor, so its chain entry has nothing to `assume` and
its lesson plan has no Explore seam to hand back to — legitimate for the first
topic of a chapter, wrong for a topic that sits in the middle of one. Asking for
`--topics 2` gives the requested topic a real predecessor. The topic you asked
for is marked in the output; the one before it is context, and is written to the
file too.
"""
import argparse
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

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

from prep_flow import contract as contract_module  # noqa: E402
from prep_flow import db, terminal, tools  # noqa: E402
from prep_flow.graph import build_chapter_graph, recursion_limit_for  # noqa: E402
from prep_flow.state import DEFAULT_CONFIG  # noqa: E402

_RULE = "─" * 78

# Words that match everything in a textbook and so rank nothing. Deliberately
# short: this is a search over one book, not a general index, and over-stemming
# a request like "count in tens" would throw away the only words that matter.
_NOISE = frozenset("""
a an the and or of in on to for with without from by at as is are was were be
this that these those it its their there here how what when where which who why
we you they i me my your our will can could would should do does did have has
had not no yes if then than so such very too also more most much many few some
any each other into over under again once out up down off above below page
""".split())

_WORD = re.compile(r"[a-z][a-z'-]{1,}")


def terms(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower())
            if w not in _NOISE and len(w) > 2}


def _stem(word: str) -> str:
    """Crude, and only ever used for search ranking.

    "shapes"/"shape" and "tracing"/"trace" have to meet for a request phrased in
    a teacher's words to find a page phrased in the book's.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def score_page(page_text: str, wanted: set[str]) -> float:
    """How much of the request this page actually covers.

    Scored as the share of the REQUEST that appears, not the share of the page —
    a long page is not a better match for being long, and dividing by page length
    would rank the thinnest page top.
    """
    if not wanted:
        return 0.0
    have = {_stem(w) for w in terms(page_text)}
    hit = sum(1 for w in wanted if _stem(w) in have)
    return hit / len(wanted)


def split_pages(markdown: str) -> list[tuple[int, str]]:
    found = re.findall(r"<!-- page (\d+) -->(.*?)(?=<!-- page \d+ -->|\Z)", markdown, re.S)
    return [(int(p), body) for p, body in found]


def resolve_source(args) -> dict:
    """Where the curriculum comes from, and what it says about itself.

    Two sources, one shape. The API knows its own grade and subject, so those
    are read from the book rather than asked for — a book cannot be Class 5 EVS
    and be generated as grade 3 Maths, and making the caller retype it is just
    an opportunity to get it wrong.
    """
    if args.book:
        books = {b["bookId"]: b for b in tools.list_api_books()}
        book = books.get(args.book)
        if not book:
            print(f"  no published book '{args.book}'. Available:")
            for b in books.values():
                print(f"    {b['bookId']}  (grade {b['grade']}, {b['subject']})")
            sys.exit(1)
        return {
            "label": f"{book['subject']} grade {book['grade']} — {args.book}",
            "chapters": tools.list_api_chapters(args.book),
            "load": lambda n: tools.load_api_chapter(args.book, n),
            "grade": book["grade"],
            "subject": book["subject"],
        }
    return {
        "label": args.folder,
        "chapters": tools.list_local_chapters(args.folder),
        "load": lambda n: tools.load_local_chapter(args.folder, chapter_number=n),
        "grade": args.grade,
        "subject": args.subject,
    }


def search(source: dict, query: str, per_chapter: int = 1) -> list[dict]:
    """Every chapter's best-matching page, ranked. No model call."""
    wanted = terms(query)
    results: list[dict] = []
    for entry in source["chapters"]:
        if not entry.get("usable"):
            continue
        try:
            chapter = source["load"](entry["chapterNumber"])
        except Exception as exc:
            print(f"  ! chapter {entry['chapterNumber']}: {exc}")
            continue
        if not chapter:
            continue
        pages = split_pages(chapter.get("markdown") or "")
        if not pages:
            # No `<!-- page N -->` markers on this chapter (the API's
            # classified-block shape has none at all) — score the whole
            # chapter as one page rather than skipping it outright, which
            # silently dropped every such book from every search.
            start = chapter.get("pageStart") or entry.get("pageStart") or 0
            pages = [(start, chapter.get("markdown") or "")]
        # Scored over a WINDOW of consecutive pages, not a single peak.
        #
        # A peak alone picks the wrong page reliably: the back matter of a
        # chapter — "Academic Standards to be achieved through this text book" —
        # names every idea in the chapter and so covers 100% of any request about
        # it, while teaching none of them. Sustained relevance across neighbouring
        # pages is what actually marks the part of the book that teaches a topic,
        # and one anomalous listing page cannot carry a window on its own.
        scores = [(page, score_page(body, wanted), body) for page, body in pages]
        span = min(len(scores), 3)
        windows = []
        for i in range(len(scores) - span + 1):
            block = scores[i:i + span]
            mean_score = sum(s for _, s, _ in block) / span
            best_page, _, best_body = max(block, key=lambda t: t[1])
            windows.append((mean_score, best_page, best_body))
        for value, page, body in sorted(windows, reverse=True)[:per_chapter]:
            if value <= 0:
                continue
            results.append({
                "chapterNumber": entry["chapterNumber"],
                "chapterTitle": entry["chapterTitle"],
                "page": page,
                "score": value,
                "snippet": " ".join(re.sub(r"<!--.*?-->", " ", body).split())[:150],
                "chapter": chapter,
            })
    return sorted(results, key=lambda r: -r["score"])


def window_around(chapter: dict, page: int, topics: int) -> tuple[int, int]:
    """A page range wide enough to carry `topics` sheets, centred on the hit.

    Widened BACKWARDS by preference. The requested topic is the one being asked
    for, so it should sit at the end of the window with its predecessor in front
    of it — that is what gives its Refresher something real to pick up.
    """
    pages = [p for p, _ in split_pages(chapter.get("markdown") or "")]
    if not pages:
        return chapter.get("pageStart"), chapter.get("pageEnd")
    lo = hi = pages.index(page) if page in pages else 0
    # ~2 pages of this book per topic, measured on the Class 3 ingest.
    span = max(2, topics * 2)
    while (hi - lo + 1) < span:
        if lo > 0:
            lo -= 1
        elif hi < len(pages) - 1:
            hi += 1
        else:
            break
    return pages[lo], pages[hi]


def slice_markdown(markdown: str, start: int, end: int) -> str:
    """The page markers are kept — grounding, citations and figure captions all
    depend on them, and a slice that drops them silently degrades every one.

    A chapter with none at all (window_around already returned its full
    pageStart/pageEnd for exactly this case) has nothing to filter by — return
    it whole rather than the empty string every page would otherwise fail to
    match against.
    """
    pages = split_pages(markdown)
    if not pages:
        return markdown
    keep = [f"<!-- page {p} -->{body}" for p, body in pages if start <= p <= end]
    return "\n".join(keep) if keep else markdown


async def generate(args, hit: dict) -> None:
    chapter = hit["chapter"]
    start, end = window_around(chapter, hit["page"], args.topics)
    markdown = slice_markdown(chapter["markdown"], start, end)

    print(f"\n{_RULE}\nGENERATING — pages {start}-{end} of chapter "
          f"{hit['chapterNumber']}, {len(markdown)} chars\n{_RULE}")

    config = {
        **DEFAULT_CONFIG,
        "target_topics": args.topics,
        "min_topics": 1,
        "max_topics": max(1, args.topics),
        "moves_window": max(1, args.topics),
    }

    initial = {
        "run_id": None,                      # nothing is persisted from this CLI
        "school_id": None, "class_id": None,
        "grade": str(args.grade), "subject": args.subject,
        "chapter_title": chapter["chapterTitle"],
        "chapter_number": hit["chapterNumber"],
        "chapter_markdown": markdown,
        "page_start": start, "page_end": end,
        "chapter_figures": chapter.get("figures") or [],
        "teacher_settings": {
            "duration": args.duration, "classSize": args.class_size,
            "resourceLevel": args.resource_level, "language": "English",
            "learningObjective": "new lesson", "teachingStyle": "interactive",
            "plainLanguage": args.plain_language,
        },
        "config": config, "status": "running",
        "plans": {}, "selections": {}, "metrics": {}, "errors": [],
    }

    seen: set[str] = set()
    async with db.checkpointer() as saver:
        graph = build_chapter_graph(saver)
        final = dict(initial)
        async for update in graph.astream(
            initial,
            {"configurable": {"thread_id": f"topic:{uuid.uuid4()}"},
             "recursion_limit": recursion_limit_for(config["max_topics"], config)},
            stream_mode="updates",
        ):
            for node, delta in update.items():
                for key, value in (delta or {}).items():
                    if key in ("plans", "selections", "experience") and isinstance(value, dict):
                        final[key] = {**final.get(key, {}), **value}
                    elif value is not None and key not in ("errors", "metrics"):
                        final[key] = value
                if node not in seen:
                    seen.add(node)
                    print(f"  · {node}")

    document = final.get("contract") or {}
    rows = document.get("topics") or []
    if not rows:
        print("\nNo contract was derived. " +
              "; ".join((final.get("errors") or ["(no reason recorded)"])[:3]))
        return

    # The requested topic is last in the window — the pages were widened BACKWARDS
    # from the hit, so everything before it exists to give it a predecessor.
    wanted_index = max(int(r.get("index") or 0) for r in rows)

    print(f"\n{_RULE}\nRESULT\n{_RULE}")
    for row in rows:
        index = int(row.get("index") or 0)
        readiness = row.get("readiness") or {}
        state = ("ready" if readiness.get("ready")
                 else "MISSING " + ", ".join(readiness.get("missing") or []))
        mark = "<- the topic you asked for" if index == wanted_index else "   (context)"
        print(f"  T{index}. {row.get('topic')}  [{state}] {mark}")

    print()
    terminal.print_contract(document)

    body = json.dumps(document, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"\nwrote {len(body)} chars to {args.out} — this file is Node 2's input")
    else:
        print(f"\n{contract_module.summary(document)}")
        print("(pass --out FILE to write the contract document)")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = p.add_mutually_exclusive_group()
    source.add_argument("--book", help="a published book id from the textbook API")
    source.add_argument("--folder", help="an ingested textbook folder on disk")
    p.add_argument("--list-books", action="store_true",
                   help="show what the textbook API publishes, and stop")
    p.add_argument("--topic", help="what you are teaching")
    p.add_argument("--subtopic", default="", help="narrows the search")
    # Only consulted for --folder. A book from the API carries its own.
    p.add_argument("--grade", default="3")
    p.add_argument("--subject", default="Maths")
    p.add_argument("--topics", type=int, default=2,
                   help="topics to derive. 2 (default) gives the requested topic a "
                        "predecessor, so its chain entry has something to assume")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--class-size", type=int, default=40)
    p.add_argument("--resource-level", type=int, default=0)
    # --learner-threshold and --no-simulate were here. Both tuned the learner
    # gate, which is Node 3's now; nothing in this run judges material.
    p.add_argument("--plain-language", action="store_true",
                   help="wording only — short sentences, everyday words; same "
                        "numbers and thinking demand as the grade")
    p.add_argument("--search-only", action="store_true",
                   help="show where the topic lives and stop, spending nothing")
    p.add_argument("--pick", type=int, default=1,
                   help="derive from the Nth ranked match instead of the best")
    p.add_argument("--out", help="write the contract to this file as JSON — "
                                 "this file is Node 2's input")
    args = p.parse_args()

    if args.list_books:
        print(f"\n{_RULE}\nPUBLISHED BOOKS\n{_RULE}")
        for b in tools.list_api_books():
            print(f"  {b['bookId']}")
            print(f"      grade {b['grade']}  {b['subject']}  ({b['chapterCount']} chapters, {b['board']})")
        return

    if not (args.book or args.folder):
        p.error("give either --book (from the API) or --folder (on disk); "
                "--list-books shows what is published")
    if not args.topic:
        p.error("--topic is required")

    source = resolve_source(args)
    args.grade, args.subject = source["grade"], source["subject"]

    query = f"{args.topic} {args.subtopic}".strip()
    print(f"\n{_RULE}\nSEARCHING {source['label']}\n  for: {query}\n{_RULE}")

    hits = search(source, query)
    if not hits:
        print("  nothing matched. Try fewer or more common words, or check "
              "`prep_flow/cli.py list --folder ...` for what is ingested.")
        sys.exit(1)

    for rank, hit in enumerate(hits[:6], start=1):
        marker = ">" if rank == args.pick else " "
        print(f" {marker}{rank}. ch{hit['chapterNumber']:>2} p{hit['page']:<4} "
              f"{hit['score']:.0%} of the request  {hit['chapterTitle'][:38]}")
        print(f"      {hit['snippet'][:110]}")

    if args.pick < 1 or args.pick > len(hits):
        print(f"\n--pick {args.pick} is out of range (1-{len(hits)})")
        sys.exit(1)
    chosen = hits[args.pick - 1]

    if chosen["score"] < 0.34:
        print(f"\n  ! the best match only covers {chosen['score']:.0%} of the request — "
              "the topic may not be in this book. Check the ranking above before "
              "spending a generation on it.")

    if args.search_only:
        print("\n(--search-only — stopping here.)")
        return

    asyncio.run(generate(args, chosen))


if __name__ == "__main__":
    main()
