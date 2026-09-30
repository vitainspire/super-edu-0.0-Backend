"""Bridges one textbook chapter into the new agentic prep-material pipeline
(prep_flow -> generation -> validation_flow) and back out as {topic title:
six-section sheet}, for the shared-batch generator (prep_batch_jobs.py) to
consume in place of generate_lesson_core.

context_flow (Node 2, per-classroom adaptation) is deliberately never CALLED
here -- shared/opt_in-mode material is pooled across every section teaching a
grade+subject, with no single classroom to adapt to; context_flow exists for
exactly the opposite situation, which is what full_personalization mode
(smart_lesson_routes.py, untouched) already covers. run_generation's own
docstring calls `context_plan=None` "a first-class case rather than a
degraded one", so this is a supported path, not a workaround. context_flow's
package is nonetheless present in this backend (not wired into any graph) --
deep_agents/tools/room.py imports `context_flow.factors` unconditionally, so
the package is a hard import dependency of deep_agents even for the two seams
below that have nothing to do with per-classroom adaptation.

Node 1 runs with `deep_agents: True`, so its two agentic seams
(prep_flow.agents.reasoning's CurriculumReasoner, prep_flow.agents.experience's
LessonDesigner) read the pedagogy skills under deep_agents/skills/*/SKILL.md
via a bounded multi-step tool conversation, instead of the single structured
call_json request the flag's False branch makes. Both branches return the
same state shape (see deep_agents/__init__.py's own docstring), which is what
makes the two paths interchangeable here. deep_agents' third seam
(context_flow.reasoning's ContextReinforcer) is never reached, because
context_flow's graph is never invoked.

No new database tables are needed. prep_flow/generation/validation_flow each
write their own bookkeeping (prep_flow_runs, prep_flow_topics, ...) only when
db.configured() is true AND those tables exist; production has the Supabase
credentials that make db.configured() true but was never given those
migrations, so every write inside the pipeline logs a "table does not exist"
warning and returns None (see prep_flow/db.py::_write) rather than raising.
The pipeline still runs and still returns its result in memory -- exactly the
"minimal persistence" this integration was scoped to.

generate_lessons_from_published_book() below is the other supported entry
point: chapter text sourced from the separately-hosted published-textbook
catalog (the same service textbook_grounding.py/textbook_catalog.py's own
docstrings reference) instead of this app's own textbook_chapters table.
Nothing here writes to this app's database at all -- fetch, generate,
validate, return (plus the grade/subject/chapter metadata a persisting caller
would otherwise have to re-fetch). Persisting the result (e.g. into
shared_prep_materials -- see prep_batch_jobs.save_published_chapter_lessons)
is the caller's job, same as generate_chapter_lessons().
"""
import asyncio
import os
import re
from typing import Optional

import requests

import prep_flow.graph as prep_flow_graph
import generation.graph as generation_graph
import validation_flow.graph as validation_flow_graph

# `<!-- page 25 -->` and `<img id="img_c5ch2_06" />` — the two markers the
# published chapter's markdown carries. Together they are the only thing that
# says WHICH PAGE a picture is printed on, which is what sequencing.py needs to
# hand each topic the figures on its own pages.
_PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.I)
_IMG_TAG = re.compile(r"<img\s+id=[\"']([^\"']+)[\"']", re.I)

# Overridable so a different deployment of the same catalog service (or a
# local one during development) doesn't require editing code.
PUBLISHED_TEXTBOOK_API = os.environ.get(
    "PUBLISHED_TEXTBOOK_API", "https://eduteach-textbook-api.onrender.com"
)

# How many times a sheet Node 3 could not vouch for is rewritten and re-judged
# before the flag stands. Two, because the first round fixes most of what is
# fixable -- a window that overflowed one response usually succeeds alone, with
# its findings quoted back -- and a third round has historically bought little
# for a third of a chapter's generation cost again. Raising it costs money
# linearly; lowering it to 0 restores the old single-pass behaviour exactly.
MAX_REPAIR_ROUNDS = int(os.environ.get("PREP_MAX_REPAIR_ROUNDS", "2"))

# Bumped whenever a change would make previously generated chapters worse than
# what the pipeline produces now -- a prompt rewrite, a section-policy change, a
# new validation gate. Every cache key carries it, so a bump retires every
# cached chapter at once instead of serving yesterday's output forever. Same
# reason prep_flow's reasoning cache carries `v4` in its own key.
PIPELINE_VERSION = "v1"


def locate_figures(chapter_markdown: str, images: Optional[list],
                   book_id: Optional[str] = None,
                   chapter_number: Optional[int] = None) -> list:
    """The chapter's pictures, each tagged with the page it is printed on.

    WHY THIS HAS TO BE DERIVED. The catalog's per-image record carries an
    `image_id`, a `caption`, an `order_index` and a signed `url` — everything
    except the page. sequencing.py hands each topic only the figures whose
    `page` falls inside that topic's own page range, so an untagged figure
    reaches no topic at all and the chapter's pictures go unused. The markdown
    is where the two facts meet: it carries `<!-- page N -->` markers AND the
    `<img id="..." />` tag for each picture, in reading order, so a picture's
    page is the page marker most recently before its tag.

    Returned in the shape prep_flow reads (`id`/`page`/`caption`), with `url`
    carried along for any renderer that wants the picture itself. An image the
    markdown never references is still returned, page-less, rather than
    dropped: it is real, it is in the book, and a figure with no page simply
    reaches no topic instead of vanishing from the record.
    """
    if not images:
        return []

    page_at: dict[str, int] = {}
    current: Optional[int] = None
    # One pass over the markdown, in order, tracking the page in force as each
    # <img> tag goes by. A single scan rather than a search per image, because a
    # chapter can carry dozens of each and a per-image search is quadratic.
    for match in re.finditer(f"{_PAGE_MARKER.pattern}|{_IMG_TAG.pattern}", chapter_markdown, re.I):
        page, image_id = match.group(1), match.group(2)
        if page is not None:
            current = int(page)
        elif image_id and current is not None:
            page_at.setdefault(image_id, current)

    figures = []
    for image in images:
        if not isinstance(image, dict):
            continue
        image_id = image.get("image_id") or image.get("id")
        if not image_id:
            continue
        # Real gap in the catalog, not this app: roughly 1 in 10 images across
        # the chapters checked so far carry a caption and an id but no `url`
        # at all -- the storage object behind them is simply missing on the
        # service's side. figure_href builds a stable PATH regardless of
        # whether that path resolves to anything, so an image dropped here
        # would otherwise pass every downstream filter and attach as a dead
        # link nobody notices until a teacher taps it. Excluded at the
        # source, once, rather than trusted to every caller to re-check.
        if not image.get("url"):
            continue
        figures.append({
            "id": image_id,
            "page": page_at.get(image_id),
            "caption": image.get("caption") or "",
            # The book's own printing order — the one fact every image carries
            # whether or not this chapter has page markers. What
            # attach_by_position falls back to when `page` is None for
            # everything (see that function's own docstring for why).
            "order": image.get("order_index"),
            # The catalog's own signed URL — for THIS run only. Six hours of
            # life, so it may be read now and must never be stored.
            "url": image.get("url"),
            # The address a saved lesson may keep. Only derivable when the
            # caller says which book and chapter these came from; a chapter
            # sourced from this app's own tables has no catalog route and
            # carries None, which attach_figures treats as "no picture".
            "href": (figure_href(book_id, chapter_number, image_id)
                     if book_id and chapter_number is not None else None),
        })
    return figures


# Headings the book uses for the SHAPE of a task rather than its subject.
# sequencing.py takes a topic's title straight from the markdown heading above
# it, which is correct for "2.3 Seeds" and useless for "Group work" — a
# teacher scanning twenty periods called "Group work (3)" cannot find anything,
# and that list is the first thing they see.
_GENERIC_HEADINGS = frozenset({
    "group work", "think and say", "think and discuss", "let us do", "let us observe",
    "let us play", "let us think", "do it yourself", "activity", "activities",
    "exercise", "exercises", "questions", "answer the following", "observe",
    "observe and discuss", "observe the pictures and discuss", "discuss",
    "project work", "find out", "look at the pictures", "fill in the blanks",
    "conversation", "keywords", "key words", "we learnt", "what we learnt",
    "improve your learning", "reflections",
})

_DUPLICATE_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def _is_generic(heading: str) -> bool:
    """A heading that names the activity format, not what is being taught."""
    stem = _DUPLICATE_SUFFIX.sub("", str(heading or "")).strip().lower()
    stem = re.sub(r"^[\d.\s]+", "", stem).strip(" .:-")
    if not stem:
        return True
    if stem in _GENERIC_HEADINGS:
        return True
    # "1. Some shapes are given below with different colours. Identify the..." —
    # a sentence lifted off the page, not a title. A real heading is short.
    return len(stem.split()) > 8


def figure_href(book_id: str, chapter_number: int, image_id: str) -> str:
    """The address a SAVED lesson may hold for a picture.

    Never the catalog's own signed URL. Those expire six hours after issue
    (the token's own iat/exp claims say so), and a lesson lives in
    shared_prep_materials for months — so a sheet holding one shows a broken
    image by the same evening. This path is resolved per view by
    textbook_catalog_routes.get_catalog_image, exactly as the frontend's
    /api/textbook-image/[imageId] already does for this app's own scans.

    Carries NO school id, deliberately: this chapter is cached once and served
    to every school, so a URL baked into a cached lesson must be true for all
    of them.
    """
    return f"/api/textbook-catalog/image/{book_id}/{chapter_number}/{image_id}"


def attach_cited_figures(lesson: dict, figures: list, used_ids: set) -> int:
    """The strongest possible match: a picture the model ITSELF named, inside
    the exact bullet it was writing, because that bullet's own text points
    the teacher at it -- "Point to the picture of people in queues for seeds
    (img_c5ch2_17)" is the model telling us, in its own words, which bullet
    this picture belongs to. Nothing downstream ever read that signal before:
    attach_figures matches a PAGE to a SECTION and picks that section's FIRST
    open bullet, so a real id spelled out inside bullet 2's own text could
    still end up with a completely different picture attached to bullet 1 --
    measured on a real chapter (Grade 5 EVS ch2): the Ramulu bullet named
    `img_c5ch2_06` in its own words and was handed a poultry-farm picture
    instead, because that is what page-then-first-bullet gave it.

    Runs FIRST, before attach_figures and attach_by_position, on every
    lesson regardless of whether it already has a figureRef -- a topic can
    have three bullets that each cite a different real id by name, and the
    page/position passes must not claim any bullet this pass already
    settled correctly.
    """
    from prep_flow.sections import SECTION_ORDER, bullets_of

    by_id = {f["id"]: f for f in (figures or []) if f.get("id") and f.get("href")}
    if not by_id:
        return 0
    # Longest id first: some catalogs number ids as prefixes of one another
    # (img_1 vs img_10), and matching the longest candidate first is the only
    # way a mention of "img_c5ch2_10" is never mistaken for "img_c5ch2_1".
    ids_by_length = sorted(by_id, key=len, reverse=True)

    attached = 0
    this_lesson: set = set()
    for section in SECTION_ORDER:
        for bullet in (bullets_of(lesson, section) or []):
            if not isinstance(bullet, dict) or (bullet.get("image") or {}).get("url"):
                continue
            text = f"{bullet.get('text') or ''} {bullet.get('detail') or ''}"
            figure_id = next((fid for fid in ids_by_length
                               if fid not in used_ids and fid in text), None)
            if not figure_id:
                continue
            figure = by_id[figure_id]
            bullet["image"] = {"url": figure["href"]}
            if figure.get("caption"):
                bullet["image"]["caption"] = figure["caption"]
            used_ids.add(figure_id)
            this_lesson.add(figure_id)
            attached += 1

    if this_lesson:
        lesson["figureRefs"] = sorted(set(lesson.get("figureRefs") or []) | this_lesson)
    return attached


_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "in", "on", "at", "to",
    "for", "and", "or", "this", "that", "these", "those", "with", "from", "by",
    "it", "its", "as", "be", "been", "has", "have", "had", "will", "can",
    "their", "his", "her", "he", "she", "they", "you", "your", "we", "our",
    "i", "not", "but", "into", "than", "then", "so", "if", "how", "what",
    "which", "who", "do", "does", "did", "about", "each", "some", "one",
    "two", "three", "page", "pages",
}


def _content_words(text: str) -> set:
    words = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if len(w) <= 2 or w in _STOPWORDS:
            continue
        # Cheap plural normalization ("chairs" -> "chair") so a bullet saying
        # "count the chairs" still matches a caption saying "a chair" --
        # measured for real in this fix's own verification test: an exact
        # singular/plural mismatch silently failed a genuinely correct match.
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.add(w)
    return words


def _figure_relevant_to_bullet(figure: dict, bullet: dict) -> bool:
    """A real photo counts as relevant to THIS bullet only if its caption
    shares a genuine content word with what the bullet actually says --
    being printed on the same page, or next in reading order, is a
    candidate FILTER (narrows down "a picture from roughly the right part
    of the book"), never proof by itself that the picture is what this
    specific bullet is teaching.

    Without this check, both attach_figures and attach_by_position picked
    "the next spare real picture" and stapled it onto whichever bullet
    happened to be first in the section -- on a chapter with several real
    photos of similar objects (measured for real: Grade 3 Maths ch1, several
    photos of chairs used across different shape-counting exercises), that
    meant Concept, Real Life and Challenge each got a DIFFERENT chair photo,
    none of them verified to be the chair that section's own bullet was
    actually talking about. A caption-less figure can never be verified this
    way and is therefore never treated as relevant -- silence is safer than
    a guess here.
    """
    caption_words = _content_words(figure.get("caption") or "")
    if not caption_words:
        return False
    bullet_words = _content_words(f"{bullet.get('text') or ''} {bullet.get('detail') or ''}")
    return bool(caption_words & bullet_words)


def attach_figures(lesson: dict, figures: list, used_ids: Optional[set] = None) -> int:
    """Hang the chapter's real pictures on the bullets that teach from them.

    WHY THE PIPELINE HAS TO DO THIS AND NOT A RENDERER. The frontend shows a
    section's picture with `sec.bullets.find(b => b.image?.url)` — an image is
    a property of a BULLET, and nothing downstream of here knows which bullet.
    Feeding `chapter_figures` to Node 1 let the agents READ the captions and
    write "look at the picture on page 27"; it did not put a picture in the
    sheet. Without this step a lesson generated by this pipeline renders with
    no pictures at all, however well it talks about them — which is worse than
    the old engine, whose generate_lesson_images() at least attached something.

    WHICH PICTURE GOES WHERE comes from the sheet's own `bookMoves`: every move
    records the page it was read from AND the section it was staged into, so a
    picture printed on page 27 is a CANDIDATE for whichever part of the period
    actually uses page 27 -- but page co-location alone is not enough to pick
    which specific bullet it belongs to (see _figure_relevant_to_bullet): a
    page can carry several real photos of similar objects, each one meant for
    a different bullet, and attaching "whichever is first available" to
    "whichever bullet is first in the section" used to scatter mismatched
    real photos across a topic's own sections. Only a page-colocated figure
    that ALSO shares a content word with the specific bullet it would attach
    to is used; a section with real candidates but no bullet-level match is
    left bare, for the AI-fallback pass to fill in accurately instead.

    One picture per section, because that is all the renderer shows. Returns
    how many were attached, and never raises — a sheet with no figures is a
    normal sheet.

    `used_ids`, when passed, is the SAME set across every lesson in the
    chapter -- so a chapter-level fallback (attach_by_position,
    generate_board_sketch) can tell which real pictures are still spare
    without re-deriving it. Created locally when omitted, for a caller that
    only wants one lesson done.
    """
    from prep_flow.sections import SECTION_ORDER, bullets_of

    by_page: dict[int, list] = {}
    for figure in (figures or []):
        # `href`, not `url`: the signed one expires in six hours and this goes
        # into a lesson that is stored for months (see figure_href).
        page, href = figure.get("page"), figure.get("href")
        if page is not None and href:
            by_page.setdefault(int(page), []).append(figure)
    if not by_page:
        return 0

    # Which pages each section was built from, per the sheet's own moves.
    pages_for_section: dict[str, list] = {}
    for move in (lesson.get("bookMoves") or []):
        section, page = move.get("section"), move.get("page")
        if section in SECTION_ORDER and page is not None:
            pages_for_section.setdefault(section, []).append(int(page))

    if used_ids is None:
        used_ids = set()
    # Separate from `used_ids`: that set is chapter-wide when a caller shares
    # it, and figureRefs must name only what THIS lesson used, not every
    # picture spent anywhere in the chapter.
    this_lesson: set = set()
    attached = 0
    for section in SECTION_ORDER:
        bullets = bullets_of(lesson, section)
        if not bullets:
            continue
        # Already carries one (a board sketch from elsewhere) — leave it be.
        if any(isinstance(b, dict) and (b.get("image") or {}).get("url") for b in bullets):
            continue

        candidates = [f for page in pages_for_section.get(section, [])
                      for f in by_page.get(page, []) if f["id"] not in used_ids]
        if not candidates:
            continue

        for bullet in bullets:
            if not isinstance(bullet, dict):
                continue
            figure = next((f for f in candidates
                           if f["id"] not in used_ids and _figure_relevant_to_bullet(f, bullet)), None)
            if not figure:
                continue
            bullet["image"] = {"url": figure["href"]}
            if figure.get("caption"):
                bullet["image"]["caption"] = figure["caption"]
            used_ids.add(figure["id"])
            this_lesson.add(figure["id"])
            attached += 1
            break
        # else: no bullet in this section matched any page-colocated
        # candidate on actual content -- left bare rather than forcing an
        # unrelated real photo onto it (see this function's own docstring).

    if this_lesson:
        # The field the generation schema already declares and the model kept
        # returning empty, because it had no figures to name.
        lesson["figureRefs"] = sorted(set(lesson.get("figureRefs") or []) | this_lesson)
    return attached


def _section_bullets_needing_image(lesson: dict) -> list:
    """Every (section, bullet) pair still without a picture after all three
    real-image passes -- one candidate per section, its own first bullet,
    since only one picture is ever shown per section (see attach_figures's
    docstring). What generate_board_sketch (the AI pass, below) now fills in
    PER BUCKET, not once for the whole lesson: a lesson can have several
    genuinely different sections each needing their own accurate picture,
    and generating one sketch for the lesson and calling the rest done left
    the other buckets with nothing at all."""
    from prep_flow.sections import SECTION_ORDER, bullets_of

    out = []
    for section in SECTION_ORDER:
        bullets = bullets_of(lesson, section) or []
        if any(isinstance(b, dict) and (b.get("image") or {}).get("url") for b in bullets):
            continue
        bullet = next((b for b in bullets if isinstance(b, dict)), None)
        if bullet is not None:
            out.append((section, bullet))
    return out


def _lesson_has_image(lesson: dict) -> bool:
    """Ground truth for "does this lesson already show a picture" -- checks
    every bullet directly, rather than trusting `lesson["figureRefs"]`.

    `figureRefs` is NOT reliable for this: generation/compose.py has the
    model self-report which figure ids it INTENDS to cite as part of its own
    JSON output, before any bullet has actually been matched to a picture.
    compose.py only validates that a self-reported id is a real figure that
    exists on this topic's pages -- it never checks that the id actually made
    it into a bullet's visible text. attach_cited_figures then unions its own
    (real, bullet-text-matched) attachments into figureRefs rather than
    replacing it, so a model's unfulfilled self-report survives untouched
    forever if no bullet's text ever literally contained that id.

    Measured for real (Grade 5 EVS ch2, this exact pipeline run): "Explain
    why farmers save seeds" carried figureRefs=["img_c5ch2_17"] while every
    bullet in every section had image=None -- a phantom citation that then
    made attach_by_position and generate_board_sketch both believe this
    lesson already had a picture, skipping the very fallbacks that exist to
    catch a lesson with no picture at all. The topic shipped with nothing."""
    from prep_flow.sections import SECTION_ORDER, bullets_of

    for section in SECTION_ORDER:
        for bullet in (bullets_of(lesson, section) or []):
            if isinstance(bullet, dict) and (bullet.get("image") or {}).get("url"):
                return True
    return False


def attach_by_position(lessons: list, figures: list, used_ids: set) -> int:
    """Real textbook pictures, spent on lessons a page-based pass left empty --
    for chapters with no <!-- page N --> markers at all, where attach_figures
    finds nothing to place (every figure's `page` comes back None; see
    locate_figures's own docstring on why).

    A chapter missing page markers is still a chapter with real pictures in
    real reading order -- `order` on every figure, from the catalog's own
    order_index, survives regardless. So a book with 3 lessons and 9 pictures
    hands lesson 1 pictures 1-3, lesson 2 pictures 4-6, and so on: not the
    exact picture a page lookup would have chosen, but a real one from
    roughly the right part of the chapter, which is what "do not waste the
    textbook's own pictures" asks for when the page lookup has nothing to
    work with.

    ONE PICTURE PER SECTION, THE SAME POLICY AS attach_figures -- not one
    picture for the whole lesson. Measured on a real chapter (Grade 3 Maths,
    26 real pictures, no page markers anywhere): the old version put exactly
    ONE picture on the very first open bullet of each lesson and threw the
    rest of that lesson's own chunk away, unattached, unattachable by anyone
    else -- 23 of 26 real pictures never reached a single sheet. A lesson's
    own order-based chunk is now walked across SECTION_ORDER the same way
    attach_figures walks pages_for_section, giving each section still
    missing a picture the next spare one from THIS lesson's own chunk. Still
    never more than one per section, because that is still all the renderer
    shows (see attach_figures's own docstring) -- this is the same policy,
    applied with ORDER standing in for PAGE, not a different one.

    SAME CONTENT CHECK AS attach_figures, for the same reason: "next in this
    lesson's own chunk" is only a candidate filter, not proof the picture is
    what a specific bullet is teaching. A chunk assigned to one topic can
    hold several real photos of similar objects (measured for real: this
    exact Grade 3 Maths chapter, several photos of chairs across different
    shape-counting exercises) -- picking blindly used to put a different
    chair photo in Concept, Real Life and Challenge, none of them verified
    to match what that section's own bullet actually says. Only a candidate
    that shares a content word with the specific bullet it would attach to
    is used; a section with spare candidates but no bullet-level match is
    left bare, for the AI-fallback pass to fill in accurately instead.

    Runs ONLY on lessons attach_figures left with zero pictures -- a lesson
    that already got one from the page-based pass keeps it untouched. Runs
    ONLY on sections a lesson's own model output left with no picture of its
    own (a board sketch, say) -- same check attach_figures makes.
    """
    from prep_flow.sections import SECTION_ORDER, bullets_of

    empty = [l for l in lessons if not _lesson_has_image(l)]
    spare = sorted(
        (f for f in (figures or []) if f.get("href") and f["id"] not in used_ids),
        key=lambda f: (f.get("order") is None, f.get("order") or 0),
    )
    if not empty or not spare:
        return 0

    attached = 0
    per_lesson = max(1, len(spare) // len(empty))
    cursor = 0
    for lesson in empty:
        chunk = [f for f in (spare[cursor:cursor + per_lesson] or spare[cursor:cursor + 1])
                 if f["id"] not in used_ids]
        cursor += per_lesson
        if not chunk:
            continue

        this_lesson: set = set()
        for section in SECTION_ORDER:
            bullets = bullets_of(lesson, section)
            if not bullets:
                continue
            if any(isinstance(b, dict) and (b.get("image") or {}).get("url") for b in bullets):
                continue
            if not any(f["id"] not in used_ids for f in chunk):
                break  # this lesson's own chunk is spent -- later sections stay bare

            for bullet in bullets:
                if not isinstance(bullet, dict):
                    continue
                figure = next((f for f in chunk
                               if f["id"] not in used_ids and _figure_relevant_to_bullet(f, bullet)), None)
                if not figure:
                    continue
                bullet["image"] = {"url": figure["href"]}
                if figure.get("caption"):
                    bullet["image"]["caption"] = figure["caption"]
                used_ids.add(figure["id"])
                this_lesson.add(figure["id"])
                attached += 1
                break
            # else: nothing left in this lesson's chunk matched any bullet in
            # this section on actual content -- left bare (see docstring).

        if this_lesson:
            lesson["figureRefs"] = sorted(set(lesson.get("figureRefs") or []) | this_lesson)
    return attached


async def generate_board_sketch(lesson: dict, section: str, bullet: dict, key_parts: list) -> bool:
    """The LAST resort, after every real textbook picture has been tried: an
    AI-generated board sketch for ONE section's bullet that has real content
    but still reached here with no picture at all.

    PER BUCKET, NOT PER LESSON. Each of a lesson's sections can genuinely be
    about a different thing, and a section whose real picture got rejected
    as irrelevant (see attach_figures/attach_by_position's own docstrings)
    still deserves an accurate picture of its OWN, not silence just because
    some other section already got a sketch. The caller
    (_section_bullets_needing_image) already narrowed this down to one
    open bullet per section still needing one.

    WHY THIS EXISTS AT ALL. `explore.imageFocus` ("one short phrase: the
    single most useful thing to sketch on the board") has been in the
    generation schema since before today and was NEVER turned into a picture
    -- a teacher got a sentence telling them to draw something, and nothing
    else. For a topic that is itself about shapes and spatial reasoning, a
    sentence is a weak substitute for the sketch.

    `imageFocus` is only ever written for the explore section (that's the
    schema's own scope for it); every other section has no equivalent
    author-written hint, so its own bullet text/detail IS what gets
    illustrated -- the same content a teacher would otherwise read aloud.
    Falls back to the topic's `objective` only when a bullet has no text of
    its own to work from, so a real topic still gets something worth
    looking at rather than nothing.

    Costs one real image-generation call PER SECTION this reaches, not one
    per lesson. Reuses the exact generate_illustration / store_illustration
    pair smart_lesson_routes.py's old engine already calls for the identical
    job -- one working image pipeline, not two. Never raises: a failed image
    is a normal sheet, same contract as the function it mirrors.
    """
    explore = lesson.get("explore") or {}
    focus = str(explore.get("imageFocus") or "").strip() if section == "explore" else ""
    if not focus:
        focus = str(bullet.get("text") or lesson.get("objective") or "").strip()
    if not focus:
        return False

    from .ai import generate_illustration, illustration_key, store_illustration
    from .supabase_clients import create_admin_client

    example = " — ".join(p for p in [bullet.get("text"), bullet.get("detail")] if p)
    prompt = f"""A clean, friendly primary-school WORKBOOK illustration for a teacher to show the class.

Illustrate this: {focus}
It must accurately match this part of the lesson: "{example}"

Requirements:
- Show ONE single clear diagram or scene. No collages, no multiple panels.
- Plain white background, bold simple shapes, bright flat colours.
- At most a few short, correctly-spelled labels -- no paragraphs or banners inside the image.
- Geometrically accurate if this is a shape, view, or spatial-arrangement concept: right angle
  count, right number of sides, right relative sizes -- a wrong shape teaches the wrong idea.
Clean and genuinely explanatory -- a child should understand the idea just by looking."""

    try:
        generated = await generate_illustration(prompt, timeout_s=25)
        if not generated:
            return False
        admin = create_admin_client()
        stored = await store_illustration(
            admin, generated["url"], illustration_key([*key_parts, section, "sketch"]))
        url = stored or generated["url"]
        if not url:
            return False
        bullet["image"] = {"url": url, "aiGenerated": True}
        lesson["figureRefs"] = sorted(set(lesson.get("figureRefs") or []) | {"ai-sketch"})
        return True
    except Exception as e:                                 # noqa: BLE001
        print(f"[prep-pipeline] board-sketch generation failed for section {section!r} "
              f"(sheet ships without it): {e}")
        return False


_BROKEN_CITATION = re.compile(
    r"\s*\(\s*Pages?\s+(?:None|none|N/?A|\?)(?:\s*[-–]\s*(?:None|none|N/?A|\?))?\s*\)", re.I)


def strip_broken_citations(lesson: dict) -> int:
    """Delete "(Page None)" and friends from a sheet's prose.

    A chapter whose source markdown carries no `<!-- page N -->` markers gives
    every topic a page range of None (measured: the Class 3 Maths chapter has
    zero markers in the published API). The generation prompt asks for
    "(Page N)" citations regardless, so the model dutifully writes "(Page
    None)" -- a citation that points nowhere, printed in front of a teacher.

    Worse than useless, because it is not merely missing: an independent
    review of this material scored source traceability and textbook
    integration at 1.0/5 across every topic and every round precisely because
    of these strings, and marked both unfixable-by-rewrite. They were right --
    no rewrite invents a page number. Removing the broken citation is the
    honest fix: the sheet then simply makes its claim without a citation,
    which is what it actually has.

    Returns how many were removed. Never raises.
    """
    from prep_flow.sections import SECTION_ORDER, bullets_of

    removed = 0
    for section in SECTION_ORDER:
        for bullet in (bullets_of(lesson, section) or []):
            if not isinstance(bullet, dict):
                continue
            for key in ("text", "detail"):
                value = bullet.get(key)
                if not isinstance(value, str) or "None" not in value:
                    continue
                cleaned, n = _BROKEN_CITATION.subn("", value)
                if n:
                    bullet[key] = cleaned.strip()
                    removed += n
    return removed


def lesson_title(topic_row: dict, lesson: dict) -> str:
    """What this period should be CALLED in a teacher's list of periods.

    The book's heading when it names the subject ("2.3 Seeds", "Kinds of
    Crops"), and the generated objective when it does not ("Group work",
    "Think and say...", or a whole sentence lifted off the page).

    The objective is the right fallback and it is free: generation already
    writes one per sheet, verb-first and 4-8 words — "Identify and sequence
    crop cultivation stages" — which is exactly what a teacher scanning a list
    needs to see. No extra model call buys this.
    """
    heading = str(topic_row.get("topic") or "").strip()
    objective = str((lesson or {}).get("objective") or "").strip().rstrip(".")

    if heading and not _is_generic(heading):
        return heading
    if objective:
        return objective
    return heading


def chapter_cache_key(book_id: str, chapter_number: int) -> str:
    return f"{PIPELINE_VERSION}:{book_id}:{chapter_number}"


def _cache_get(book_id: str, chapter_number: int) -> Optional[dict]:
    """A previously generated chapter, or None to generate it.

    BEST-EFFORT IN BOTH DIRECTIONS. A missing table (migration 023 not applied),
    an unreachable database or a malformed row all mean "not cached" — never an
    error. The cache is an optimisation; a deployment without it must generate
    exactly as it did before, which is also what makes the migration safe to
    apply whenever rather than as a release gate.
    """
    try:
        from .supabase_clients import create_admin_client, retry_supabase
        row = retry_supabase(lambda: (
            create_admin_client().table("prep_chapter_cache")
            .select("payload").eq("cache_key", chapter_cache_key(book_id, chapter_number))
            .maybe_single().execute()
        ))
        payload = (row.data or {}).get("payload") if row else None
        return payload if isinstance(payload, dict) and payload.get("lessons") else None
    except Exception as e:
        print(f"[prep-pipeline] chapter cache unavailable (generating instead): {e}")
        return None


def _cache_put(book_id: str, chapter_number: int, result: dict) -> None:
    """Store a generated chapter. Never raises, for the same reason as above —
    a chapter that generated correctly must not be lost to a failed cache write.

    Nothing with zero lessons is stored: a run that produced nothing is a run to
    retry, not an answer to serve fifty schools.
    """
    if not (result.get("lessons") or []):
        return
    try:
        from .supabase_clients import create_admin_client, retry_supabase
        retry_supabase(lambda: create_admin_client().table("prep_chapter_cache").upsert({
            "cache_key": chapter_cache_key(book_id, chapter_number),
            "book_id": book_id,
            "chapter_number": int(chapter_number),
            "grade": result.get("grade"),
            "subject": result.get("subject"),
            "chapter_title": result.get("chapterTitle"),
            "payload": result,
            "shipped": result.get("shipped"),
            "needs_review": result.get("needsReview"),
            "total": result.get("total"),
        }, on_conflict="cache_key").execute())
    except Exception as e:
        print(f"[prep-pipeline] chapter cache write failed (result still returned): {e}")


class PipelineError(RuntimeError):
    """The pipeline could not produce usable material for this chapter."""


class PublishedBookNotFound(PipelineError):
    """No such book_id, or no such chapter_number, in the published catalog."""


async def generate_chapter_lessons(
    *, grade: str, subject: str, chapter_title: str, chapter_markdown: str,
    chapter_number: Optional[int] = None,
    page_start: Optional[int] = None, page_end: Optional[int] = None,
    chapter_figures: Optional[list] = None,
    topic_count: Optional[int] = None,
    generate_ai_images: bool = True,
) -> dict:
    """Runs one chapter through Node 1 (curriculum reasoning), generation, and
    Node 3 (validation), skipping Node 2 (context_flow) entirely.

    Returns {"lessons": [...], "shipped": int, "needsReview": int,
    "refused": int, "total": int}. Each lesson carries {"topic", "lesson",
    "bookHeading", "needsReview", "reviewReasons"}.

    "lessons" holds every topic the validator did not REFUSE -- both the ones
    it vouched for (`needsReview` False) and the ones it could not positively
    vouch for (`needsReview` True, with the blocking findings attached). Only a
    refusal is withheld. Shipping the flagged ones is deliberate: a chapter
    handed over with holes at periods 3, 6 and 9 cannot be planned from at all,
    so the teacher falls back to the textbook and none of this material gets
    used -- a gate that protects quality by withholding everything protects
    nothing. The caller decides what to do with a flag; prep_batch_jobs.py
    still falls back to the old engine for anything absent entirely.

    Raises PipelineError if Node 1 produced no usable topics at all (an
    unreadable chapter, or an extraction that failed outright) -- the caller
    should treat that the same as "nothing generated" and fall back for the
    whole chapter, not retry.

    Pictures are attached in three passes, real before invented:
    (1) attach_figures, page-based -- a picture printed on the pages a
        section was written from; (2) attach_by_position, for a chapter with
        no page markers at all -- the book's real pictures, spent in reading
        order across whichever lessons the page-based pass left empty;
        (3) generate_board_sketch, only for a lesson STILL empty after both,
        and only when the sheet names something concrete to draw
        (`explore.imageFocus`) -- an AI sketch of that, not a substitute for
        a real picture, a fallback for when none exists. `generate_ai_images`
        (default True) is the one switch for pass 3 -- it is the only pass
        that spends real money, roughly one image-generation call per lesson
        that reaches it.

    `topic_count`, when given, pins min/target/max topics to that one number
    instead of the pipeline's usual 8/30/40 -- a real chapter cut into a
    handful of periods instead of a full syllabus's worth, at a fraction of
    the cost. Exists for exactly one reason: proving a pipeline change works
    on real content should not require paying for a full chapter every time.
    None (the default) is the unmodified production shape.
    """
    # DEFAULT OFF. `deep_agents: True` routes Node 1's reasoning and
    # experience-design steps through a bounded but iterative agent
    # conversation (up to 14 model calls for the curriculum reasoner, up to
    # 10 for the lesson designer) instead of the single `call_json` call each
    # would otherwise make. That is fine, and cheap, the SECOND time a given
    # book+chapter is processed -- Node 1's reasoning cache means most real
    # runs skip the agent path entirely. It is NOT fine the first time: a
    # genuinely uncached chapter (measured directly: Grade 3 EVS chapter 1,
    # never processed before) burned over $2 generating a SINGLE topic,
    # because each agent step resends a growing conversation rather than one
    # fixed-size prompt. That cost was never visible in any prior measurement
    # of this pipeline, because every chapter tested until then already had
    # cached reasoning. Opt back into the richer agentic path explicitly
    # (PREP_FLOW_DEEP_AGENTS=true) once that cost is something you have
    # chosen to pay, not something a first-time chapter pays by default.
    topic_config: dict = {
        "deep_agents": os.environ.get("PREP_FLOW_DEEP_AGENTS", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    }
    if topic_count is not None:
        n = max(1, int(topic_count))
        topic_config.update(min_topics=n, target_topics=n, max_topics=n)

    state = await prep_flow_graph.run_chapter(
        grade=grade, subject=subject, chapter_title=chapter_title,
        chapter_markdown=chapter_markdown, chapter_number=chapter_number,
        page_start=page_start, page_end=page_end,
        # Without these the chapter's printed pictures reach no topic, and
        # every sheet tells the teacher to draw on the blackboard something the
        # children already have in front of them (see locate_figures).
        chapter_figures=chapter_figures or [],
        config=topic_config,
    )
    contract = state.get("contract") or {}
    topic_rows = contract.get("topics") or []
    if state.get("status") == "failed" or not topic_rows:
        raise PipelineError(
            f"prep_flow produced no usable topics for chapter {chapter_title!r} "
            f"(status={state.get('status')!r})"
        )

    # Without `sources`, `to_state()` hands every topic an empty excerpt no
    # matter what — this was silently happening on every real run through this
    # entry point. See generation/adapt.py::sources_from_chapter for the fix
    # that lets it work even when the chapter has no <!-- page N --> markers.
    from generation.adapt import sources_from_chapter
    generated = await generation_graph.run_generation(
        contract=contract, context_plan=None, chapter_text=chapter_markdown,
        sources=sources_from_chapter(contract, chapter_markdown),
    )
    materials = generated.get("materials") or {}
    if not materials:
        raise PipelineError(
            f"generation produced no lessons for chapter {chapter_title!r}: "
            f"{generated.get('errors')}"
        )

    # VALIDATE ONLY -- the REPAIR loop is disabled.
    #
    # Node 3 names what is wrong; generation has a `repair_node` able to act on
    # that, but the repair round trip (rewrite -> re-validate, up to
    # MAX_REPAIR_ROUNDS) is switched off here rather than driven. A sheet that
    # fails validation now goes straight to `needs_review` below instead of
    # being rewritten and re-checked, which is cheaper and more predictable,
    # at the cost of shipping fewer topics clean on the first pass.
    #
    # OFF BY DEFAULT VALUE, ON BY DEFAULT SPEND: PREP_FLOW_VALIDATE defaults
    # true because validation never rewrites a sheet (see this repo's own
    # cost-audit conversation) -- it only judges and labels one, which is
    # exactly the thing a real school-facing run must not skip. Real,
    # measured cost on one 3-topic run: ~20% of the total spend (consistency +
    # learner + realism + diagnostic, 10 calls). Set false for a cheap
    # iteration pass on an EARLIER stage (prompt tuning, image attachment,
    # bilingual layer) where what changed there is what you're checking, not
    # whether the sheet as a whole holds up -- never leave it false for
    # anything that reaches a teacher. Skipped, not faked: every topic is
    # marked needs_review with an explicit reason saying so, never silently
    # treated as shipped just because nothing said otherwise.
    validate_enabled = os.environ.get("PREP_FLOW_VALIDATE", "true").strip().lower() in (
        "1", "true", "yes", "on")
    if validate_enabled:
        validated = await validation_flow_graph.run_validation(
            contract=contract, materials=materials, plan=None,
            chapter_text=chapter_markdown,
        )
        shipped_indices = set(validation_flow_graph.shippable(validated))
        verdict_rows = {int(r["index"]): r
                        for r in ((validated.get("verdict") or {}).get("topics") or [])
                        if r.get("index") is not None}
    else:
        print(f"[prep_pipeline_bridge] PREP_FLOW_VALIDATE=false -- skipping validation "
              f"for chapter {chapter_title!r}; every topic ships as needs_review")
        shipped_indices = set()
        verdict_rows = {}
    repair_rounds = 0

    by_index = {row.get("index"): row for row in topic_rows}
    lessons = []
    shipped = needs_review = refused = figures_attached = broken_citations = 0
    # Shared across every lesson in this chapter, not reset per lesson --
    # attach_by_position below needs to know which real pictures the
    # page-based pass already spent, chapter-wide.
    used_figure_ids: set = set()
    for index_str, lesson in materials.items():
        index = int(index_str) if isinstance(index_str, str) else index_str
        topic_row = by_index.get(index) or {}
        row = verdict_rows.get(index) or {}
        status = row.get("verdict") or ("ship" if index in shipped_indices else "needs_review")

        # A REFUSED sheet is the only one withheld. `refuse` means a check that
        # cannot be argued with failed -- the sheet teaches a different mastery
        # target than the contract set, or its plan provenance is broken -- and
        # material like that is worse than a gap.
        if status == "refuse":
            refused += 1
            continue

        title = lesson_title(topic_row, lesson)
        if not title:
            continue

        # After validation, not before: a picture is not something the gates
        # judge, and a sheet that gets rewritten in a repair round would lose
        # the attachment anyway when its sections are replaced.
        #
        # Pass 0, FIRST: the model's own citations, matched to the exact
        # bullet that names them -- see attach_cited_figures's own docstring.
        # Runs ahead of the page-based pass because a real id spelled out
        # inside a bullet's own text is a stronger signal than "this page
        # maps to this section", and must not be second-guessed by it.
        #
        # A FRESH set for this call, not used_figure_ids directly -- a
        # citation is the model independently naming a real figure as
        # relevant to THIS lesson's own bullet, and two different topics in
        # the same chapter can both genuinely be about the same picture (a
        # seed-shortage photo is relevant to both "compare farming methods"
        # and "why farmers save seeds"). Chapter-wide exhaustion is correct
        # for attach_figures/attach_by_position below (spreading GENERIC
        # pictures thinly), but was silently dropping a real, deliberate
        # citation the moment an earlier topic happened to cite the same id
        # first -- measured for real on this exact chapter (Grade 5 EVS ch2):
        # two topics each cited img_c5ch2_17 by name, and only the first one
        # ever got the picture. Still merged into used_figure_ids right after,
        # so the later passes see it as chapter-wide spent, same as before.
        cited_ids_this_lesson: set = set()
        figures_attached += attach_cited_figures(lesson, chapter_figures or [], cited_ids_this_lesson)
        used_figure_ids |= cited_ids_this_lesson
        figures_attached += attach_figures(lesson, chapter_figures or [], used_figure_ids)
        # Same reasoning for the citation cleanup: repair rewrites sections,
        # so anything stripped earlier would come back.
        broken_citations += strip_broken_citations(lesson)

        # SHIPPED AND FLAGGED, RATHER THAN SHIPPED AND MISSING. A chapter
        # delivered with holes at periods 3, 6 and 9 is one a teacher cannot
        # plan from, so they fall back to the textbook and the material goes
        # unused -- the validation gate protecting quality by withholding ends
        # up protecting nothing. A `needs_review` sheet is not a broken sheet;
        # it is one no pass could positively vouch for. It travels with the
        # reason attached so the caller can surface it rather than pretend.
        review = status != "ship"
        if review:
            needs_review += 1
        else:
            shipped += 1

        if review and not validate_enabled:
            reasons = ["Validation was skipped for this run (PREP_FLOW_VALIDATE=false) -- "
                       "this sheet has not been checked at all, not that a check found "
                       "something wrong."]
        elif review:
            reasons = [f.get("message") for f in (row.get("findings") or [])
                       if f.get("severity") == "blocking"][:5]
        else:
            reasons = []
        lessons.append({
            "topic": title,
            "lesson": lesson,
            "bookHeading": topic_row.get("topic") or "",
            "needsReview": review,
            "reviewReasons": reasons,
        })

    # Pass 2: real pictures, positionally, for whatever the page-based pass
    # (pass 1, above) left with nothing -- see attach_by_position's own
    # docstring for why a chapter can reach here with pages but no page
    # markers to place them against.
    figures_attached += attach_by_position(
        [entry["lesson"] for entry in lessons], chapter_figures or [], used_figure_ids)

    # Pass 3: AI, last resort, real cost -- PER SECTION now, not once per
    # lesson. _section_bullets_needing_image finds every (section, bullet)
    # still without a picture after both real-image passes; each one that
    # has real content to draw gets its own accurate sketch instead of the
    # lesson getting one picture and its other empty sections staying bare.
    ai_sketches = 0
    if generate_ai_images:
        key_base = [str(grade), subject, chapter_title]
        tasks = [
            generate_board_sketch(entry["lesson"], section, bullet, [*key_base, entry["topic"]])
            for entry in lessons
            for section, bullet in _section_bullets_needing_image(entry["lesson"])
        ]
        if tasks:
            results = await asyncio.gather(*tasks)
            ai_sketches = sum(1 for made in results if made)
            figures_attached += ai_sketches

    # THE BILINGUAL LAYER -- LAST, after everything English is finalized
    # (figures attached in all three passes, citations cleaned). ON by
    # default: real teachers should get the English+Telugu sheet, not just
    # English, for any chapter this pipeline generates. This is one more
    # real, bounded call_json call PER LESSON (see generation/language_layer.py's
    # own docstring for why it is bounded and never an agent loop) -- unlike
    # PREP_FLOW_DEEP_AGENTS, which is unbounded and stays opt-in, this one
    # cost is known and small, so it is opt-OUT: set
    # PREP_FLOW_LANGUAGE_LAYER=false to turn it off for a run. Applied to
    # every lesson regardless of ship/needsReview status -- a reviewer
    # looking at a flagged lesson still wants the Telugu layer there.
    # Every Telugu line produced this way is still an UNREVIEWED DRAFT (see
    # language_layer.py's _meta.teluguReview stamp) -- turning this on means
    # real teachers see that draft, not a native-speaker-reviewed one.
    telugu_layer_applied = 0
    if os.environ.get("PREP_FLOW_LANGUAGE_LAYER", "true").strip().lower() in ("1", "true", "yes", "on"):
        from generation.language_layer import add_telugu_layer
        for entry in lessons:
            entry["lesson"] = await add_telugu_layer(entry["lesson"])
            if (entry["lesson"].get("_meta") or {}).get("teluguBulletsApplied"):
                telugu_layer_applied += 1

    return {
        "lessons": lessons,
        "shipped": shipped,
        "needsReview": needs_review,
        "refused": refused,
        "total": len(topic_rows),
        "repairRounds": repair_rounds,
        "figuresAttached": figures_attached,
        "aiSketches": ai_sketches,
        "brokenCitationsRemoved": broken_citations,
        "teluguLayerApplied": telugu_layer_applied,
    }


def _fetch_catalog_entry(book_id: str) -> dict:
    """One book's row from the catalog listing -- the only place grade/subject
    are available; the per-chapter endpoint below doesn't carry them."""
    resp = requests.get(f"{PUBLISHED_TEXTBOOK_API}/published/books", timeout=30)
    resp.raise_for_status()
    for entry in resp.json():
        if entry.get("book_id") == book_id:
            return entry
    raise PublishedBookNotFound(
        f"'{book_id}' is not in the published catalog at "
        f"{PUBLISHED_TEXTBOOK_API}/published/books"
    )


def _fetch_published_chapter(book_id: str, chapter_number: int) -> dict:
    resp = requests.get(
        f"{PUBLISHED_TEXTBOOK_API}/published/books/{book_id}/chapters/{chapter_number}",
        timeout=60,
    )
    if resp.status_code == 404:
        raise PublishedBookNotFound(f"'{book_id}' has no published chapter {chapter_number}")
    resp.raise_for_status()
    return resp.json()


async def generate_lessons_from_published_book(book_id: str, chapter_number: int,
                                                topic_count: Optional[int] = None,
                                                generate_ai_images: bool = True) -> dict:
    """The full "fetch -> generate -> validate" path in one call: given just a
    book_id and a chapter number, looks up that book's grade/subject from the
    published catalog, fetches the chapter's real text, and runs it through
    generate_chapter_lessons() above -- the same generation/validation Node 1
    output as the DB-backed path, just sourced from the published-textbook
    service instead of this app's own textbook_chapters table.

    `topic_count` forwards to generate_chapter_lessons -- see its docstring.
    Passing it SKIPS the chapter cache in both directions: a cut-down test run
    must never be served to a real school in place of the full chapter, and a
    real cached chapter must never be shadowed by a test run's 3-topic answer.

    Raises PublishedBookNotFound if the book_id or chapter_number doesn't
    exist in the catalog; PipelineError (from generate_chapter_lessons) if
    the chapter's own content couldn't produce usable material.
    """
    # CACHED CHAPTERS COST NOTHING AND ARE NOT SCHOOL-SPECIFIC. This whole
    # function's inputs are a book and a chapter; no school, no class, no
    # teacher reaches the pipeline (context_flow is never called). So the
    # fiftieth school to teach this chapter gets the first school's answer,
    # already repaired and already validated, for free. Checked before the
    # catalog fetch so a cache hit costs no network at all.
    if topic_count is None:
        cached = _cache_get(book_id, chapter_number)
        if cached is not None:
            return {**cached, "fromCache": True}

    catalog_entry = _fetch_catalog_entry(book_id)
    chapter = _fetch_published_chapter(book_id, chapter_number)

    figures = locate_figures(chapter.get("content") or "", chapter.get("images"),
                             book_id=book_id, chapter_number=chapter_number)

    result = await generate_chapter_lessons(
        grade=catalog_entry["grade"],
        subject=catalog_entry["subject"],
        chapter_title=chapter["chapter_title"],
        chapter_markdown=chapter["content"],
        topic_count=topic_count,
        generate_ai_images=generate_ai_images,
        chapter_number=chapter.get("chapter_number", chapter_number),
        page_start=chapter.get("page_start"),
        page_end=chapter.get("page_end"),
        chapter_figures=figures,
    )
    result["figureCount"] = len(figures)
    result["fromCache"] = False
    if topic_count is None:
        _cache_put(book_id, chapter_number, result)
    # Callers that persist this result (prep_batch_jobs.save_published_chapter_lessons)
    # need to know what grade+subject+chapter it came from -- cheap metadata
    # already in hand here, so no reason to make them re-fetch the catalog.
    result["grade"] = catalog_entry["grade"]
    result["subject"] = catalog_entry["subject"]
    result["chapterTitle"] = chapter["chapter_title"]
    result["chapterNumber"] = chapter.get("chapter_number", chapter_number)
    result["pageStart"] = chapter.get("page_start")
    result["pageEnd"] = chapter.get("page_end")
    return result
