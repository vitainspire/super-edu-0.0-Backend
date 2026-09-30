"""Move Extraction Agent — reads each topic's teaching order off the page.

Runs between sequencing and context assembly, and the position is the point.
Sequencing has just decided WHICH pages each period teaches from; every node
after this one — planning, activity selection, generation, validation — needs to
know in what order those pages explain themselves, because that order is now the
order the sheet teaches in. Extracting it later would mean the planner had
already allocated minutes to a section order the book disagrees with.

It is no longer a graph node of its own: it is the first sub-step of the merged
`curriculum` stage (prep_flow/agents/stages.py), which checks whether moves have
already been read before calling it, so a resumed run still does not pay for
forty extractions twice.

WINDOWED, at `config['moves_window']` topics per call. The per-topic call re-sent
the move vocabulary and the eight rules once per topic — see the comment on
MAX_MOVE_EXCERPT_CHARS, which notes a 40-topic chapter pays for that forty times.
The excerpts are sent once either way; what a window saves is the preamble and
39 round trips.

Batching is safe HERE and is deliberately not done for the learner or teacher
gates. This call reports a fact that is written on the page — the order the book
explains itself in — and every returned page is still checked against the topic's
own range by `normalise_moves`. A gate returns a verdict, and a model asked for
four verdicts in one response carries one across; both gate modules describe the
real run where that happened. Set `moves_window` to 1 to restore the per-topic
call exactly.

Never raises. A topic whose moves cannot be read falls back to the headings, and
a topic whose headings say nothing falls back to the canonical section order —
which is exactly the behaviour the pipeline had before this node existed, so the
worst case is the old behaviour rather than a broken run. A WINDOW that fails
falls back that way per topic, not wholesale: one bad response must not cost four
topics their reading when the headings can still be read off all four.
"""
from ..llm import call_json, gather_bounded
from ..moves import (
    MAX_MOVE_EXCERPT_CHARS,
    build_moves_prompt,
    build_moves_window_prompt,
    moves_from_headings,
    normalise_moves,
    reordered,
    teaching_order,
)
from ..state import ChapterState

# Four, not twelve. The excerpt is the bulk of this prompt and it is sent once per
# topic whatever the window, so a bigger window buys progressively less — while
# the chance of a truncated response, which loses the LAST topics in the window
# silently, grows with every topic added. Four holds ~16k characters of textbook
# and ~4 move lists, which is comfortably inside a response.
DEFAULT_MOVES_WINDOW = 4


def _page_range(spec: dict) -> tuple:
    return (spec.get("page_start"), spec.get("page_end"))


def _from_headings(spec: dict) -> dict:
    """The no-LLM reading, which is what every failure path here lands on."""
    excerpt = (spec.get("excerpt") or "").strip()
    fallback = moves_from_headings(excerpt[:MAX_MOVE_EXCERPT_CHARS], _page_range(spec))
    return {**spec, "moves": fallback, "moves_source": "headings" if fallback else "none"}


async def _extract_moves(spec: dict, grade: str, subject: str) -> dict:
    """One topic, one call. Used when the window is 1."""
    excerpt = (spec.get("excerpt") or "").strip()
    if not excerpt:
        return {**spec, "moves": [], "moves_source": "none"}

    try:
        answer = await call_json(
            build_moves_prompt(spec, grade, subject, excerpt),
            label=f"moves[T{spec['index']}]",
            required=("moves",),
            # Low, because this call reports a fact about the page rather than
            # composing anything. A creative reading of "in what order does this
            # book explain itself" is a wrong reading.
            temperature=0.1,
            max_tokens=1500,
        )
        moves = normalise_moves(answer.get("moves"), _page_range(spec))
        if moves:
            return {**spec, "moves": moves, "moves_source": "extracted"}
        print(f"[prep_flow:moves] T{spec['index']} extraction returned nothing usable")
    except Exception as exc:
        print(f"[prep_flow:moves] T{spec['index']} extraction failed: {exc}")

    return _from_headings(spec)


async def _extract_window(specs: list[dict], grade: str, subject: str) -> list[dict]:
    """Several consecutive topics, one call.

    A topic the response omitted, or returned nothing usable for, falls back to
    its headings on its own — the same outcome it would have had from its own
    failed call. That per-topic granularity is the point: a window is a way to
    pay for one preamble instead of four, and it must not also become a way to
    lose four readings to one bad response.
    """
    readable = [s for s in specs if (s.get("excerpt") or "").strip()]
    if not readable:
        return [{**s, "moves": [], "moves_source": "none"} for s in specs]

    first, last = readable[0]["index"], readable[-1]["index"]
    by_index: dict[int, list] = {}
    try:
        answer = await call_json(
            build_moves_window_prompt(readable, grade, subject),
            label=f"moves[{first}-{last}]",
            required=("topics",),
            temperature=0.1,
            # The excerpts dominate the INPUT; this bounds the output, which is a
            # move list per topic.
            #
            # 1800, not the 1200 this shipped with, and the first real run is why:
            # a two-topic window came back at 10,300 characters against a 2,800
            # token ceiling three times in a row, truncated mid-JSON each time, and
            # both topics fell back to their headings. 1200 per topic was actually
            # LESS than the 1500 the single-topic call had always used — and a
            # window of activity-heavy pages is where move lists are longest,
            # because `verbatim` carries up to 60 words of the book's own task per
            # activity.
            #
            # A ceiling is not a spend. The only cost of setting it high is a
            # response that could have been longer; the cost of setting it low is
            # three wasted calls and the no-LLM fallback.
            max_tokens=600 + 1800 * len(readable),
        )
        entries = [e for e in (answer.get("topics") or []) if isinstance(e, dict)]
        for position, entry in enumerate(entries):
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                # Positional rescue, the same one the planner and the mastery pass
                # use: a model that renumbered still answered in order.
                index = readable[position]["index"] if position < len(readable) else None
            if index is not None:
                by_index[index] = entry.get("moves")
    except Exception as exc:
        print(f"[prep_flow:moves] window T{first}-T{last} failed: {exc}")

    out = []
    for spec in specs:
        if not (spec.get("excerpt") or "").strip():
            out.append({**spec, "moves": [], "moves_source": "none"})
            continue
        moves = normalise_moves(by_index.get(spec["index"]), _page_range(spec))
        if moves:
            out.append({**spec, "moves": moves, "moves_source": "extracted"})
        else:
            print(f"[prep_flow:moves] T{spec['index']} not usable in the window "
                  f"response; reading its headings instead")
            out.append(_from_headings(spec))
    return out


async def move_extraction_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["move extraction: no topics to read"]}

    config = state.get("config") or {}
    grade, subject = state.get("grade", ""), state.get("subject", "")

    size = max(1, int(config.get("moves_window", DEFAULT_MOVES_WINDOW)))
    windows = [topics[i:i + size] for i in range(0, len(topics), size)]

    # Windows are still fanned out against the same concurrency gate the
    # per-topic calls used. Both dimensions matter: the window cuts how many
    # calls there are, the gate caps how many are in flight, and a 40-topic
    # chapter at window 4 is ten calls rather than forty.
    results = await gather_bounded(
        [(_extract_moves(window[0], grade, subject) if size == 1
          else _extract_window(window, grade, subject))
         for window in windows],
        limit=int(config.get("concurrency", 4)),
    )

    read: list[dict] = []
    errors: list[str] = []
    for window, result in zip(windows, results):
        if isinstance(result, BaseException):
            span = (f"T{window[0]['index']}" if len(window) == 1
                    else f"T{window[0]['index']}-T{window[-1]['index']}")
            errors.append(f"move extraction: {span}: {result}")
            # Not empty: the headings are still readable without a model, and the
            # two moves they catch reliably are the two the design refuses to let
            # a library activity replace.
            read += [_from_headings(spec) for spec in window]
        elif isinstance(result, dict):
            read.append(result)          # the window-of-one path
        else:
            read += result

    # Two numbers worth printing, because between them they say whether this node
    # earned its call. `reordered` is the one that matters: if it is near zero the
    # book agrees with the canonical order and the sheets are unchanged, and if it
    # is high the pipeline was previously teaching most of this chapter in an
    # order the book does not use.
    extracted = sum(1 for s in read if s.get("moves_source") == "extracted")
    differs = sum(1 for s in read if reordered(s.get("moves") or []))
    print(f"[prep_flow:moves] read {extracted}/{len(read)} topics from the page in "
          f"{len(windows)} call(s), {differs} teach in an order the canonical six "
          f"would have got wrong")

    return {
        "topics": read,
        "errors": errors,
        "metrics": {
            "topics_with_moves": extracted,
            "topics_reordered_by_book": differs,
            # What the windowing actually bought, in the unit that costs money.
            # Reported rather than assumed: a chapter whose windows keep failing
            # back to headings is paying for the calls and getting the no-LLM
            # answer, and the two numbers together are the only way to see that.
            "moves_calls": len(windows),
        },
    }
