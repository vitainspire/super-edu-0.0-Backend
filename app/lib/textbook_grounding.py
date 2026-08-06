"""Grounds a prep sheet in the textbook the class actually holds. Mirrors
frontend/lib/prep/textbook-grounding.ts.

The older grounding (prep_context.py's fetch_grounding) reads the *ontology*:
chapter title, page range, exercise/sidebar text pulled out by vision
extraction — a summary, never the book's own words. This reads the ingested
chapter instead: the verbatim markdown for the matched topic, plus the
illustrations printed alongside it.

Everything here fails closed. No confident match means no textbook grounding
and the ontology grounding carries on alone — a lesson grounded in the WRONG
chapter is far worse than one grounded in none.
"""
import re
from typing import Optional

# Enough of the book to teach from, bounded so a long chapter cannot crowd out
# the rest of the prompt.
MAX_EXCERPT_CHARS = 8000
# Below this share of the topic's words matched, the two titles are about
# different things. Above a half rather than at it, on evidence: 'Group work'
# shares exactly one of its two words with '9.6 Musical instruments that work
# with the help of wind', scored 0.5, and grounded a lesson in the wrong
# chapter. Every real topic tried clears 0.67.
MIN_TITLE_OVERLAP = 0.6

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "is",
    "are", "we", "our", "you", "your", "it", "its", "this", "that", "these",
    "those", "do", "does", "let", "lets", "about", "from", "by", "at", "as",
}


def title_tokens(title: str) -> list[str]:
    """Comparable words: lowercase, punctuation and section numbers gone."""
    t = title.lower()
    # '3.6 Council for Green Revolution' -> 'council for green revolution'
    t = re.sub(r"^\s*\d+(\.\d+)*[.):]?\s*", "", t)
    # Unicode-aware equivalent of JS's /[^\p{L}\p{N}\s]/gu — Python's `re`
    # (unlike the third-party `regex` package) has no \p{} property escapes,
    # so this replaces non-letter/non-digit/non-space characters by hand.
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return [w for w in t.split() if len(w) > 1 and w not in STOPWORDS]


def title_score(topic: str, candidate: str) -> float:
    """How well a candidate heading answers to the topic being taught, 0-1.

    Scored against the topic's own words rather than the candidate's, so a
    short topic inside a long chapter heading still scores full marks — the
    question is "does this heading cover my topic", not "are these the same
    length".
    """
    wanted = title_tokens(topic)
    if not wanted:
        return 0.0
    have = set(title_tokens(candidate))
    hits = [w for w in wanted if w in have]
    if not hits:
        return 0.0
    # A single short word in common ('our', 'sun') is a coincidence, not a match.
    if not any(len(w) >= 4 for w in hits):
        return 0.0
    # A one-word topic scores 1.0 against any heading containing that word, so
    # it needs the heading to be essentially that word too.
    if len(wanted) == 1 and len(title_tokens(candidate)) > 2:
        return 0.0
    return len(hits) / len(wanted)


def anchors_in(markdown: str) -> list[str]:
    """Anchors (<img id="..." />) appearing in a stretch of markdown."""
    return re.findall(r'<img id="([^"]+)"\s*/>', markdown)


def best_match(chapters: list[dict], topic: str, subtopic: Optional[str] = None) -> Optional[dict]:
    """Best chapter (and topic within it) for what is being taught.

    Tries the topic headings first: finer-grained, so matching one gives a
    slice rather than a whole chapter. Falls back to chapter titles. Returns
    None when nothing clears the bar.
    """
    # A subtopic is the more specific thing to teach, so try it first and only
    # fall back to the parent topic when it finds nothing.
    queries = [q for q in (subtopic, topic) if q and q.strip()]
    for query in queries:
        best: Optional[dict] = None

        def consider(candidate: dict):
            nonlocal best
            if candidate["score"] < MIN_TITLE_OVERLAP:
                return
            if best is None or candidate["score"] > best["score"]:
                best = candidate

        # Chapters first, so an equal-scoring topic replaces the chapter
        # rather than the other way round.
        for chapter in chapters:
            consider({"chapter": chapter, "topic": None, "score": title_score(query, chapter["chapter_title"])})
        for chapter in chapters:
            for t in (chapter.get("topics") or []):
                # Only real curriculum sections — the pipeline marks recurring
                # activity blocks ('Group work', 'Do this') as unnumbered, and
                # they're the worst thing to match on since they recur in
                # every chapter.
                if t.get("numbered") is False:
                    continue
                consider({"chapter": chapter, "topic": t, "score": title_score(query, t["title"])})
        if best:
            return best
    return None


async def fetch_textbook_grounding(
    admin, school_id: Optional[str], grade: str, subject: str, topic: str, subtopic: Optional[str] = None,
) -> Optional[dict]:
    if not school_id or not grade or not subject:
        return None

    try:
        books = (
            admin.table("textbook_books").select("id")
            .eq("school_id", school_id).eq("grade", str(grade)).ilike("subject", subject)
            .execute()
        ).data or []
        if not books:
            return None

        # Published only. An unreviewed chapter may be mis-split, and a lesson
        # built on the wrong pages is exactly what the review step exists to stop.
        chapters = (
            admin.table("textbook_chapters")
            .select("id, chapter_number, chapter_title, page_start, page_end, content_markdown, topics")
            .in_("book_uuid", [b["id"] for b in books])
            .eq("published", True)
            .order("chapter_number")
            .execute()
        ).data or []
        if not chapters:
            return None

        match = best_match(chapters, topic, subtopic)
        if not match:
            return None

        chapter, matched = match["chapter"], match["topic"]
        slice_ = (
            chapter["content_markdown"][matched["start"]:matched["end"]]
            if matched else chapter["content_markdown"]
        )
        excerpt = slice_[:MAX_EXCERPT_CHARS]

        # Illustrations printed inside the slice, in the order the book prints
        # them. The topic index records them, but the anchors in the text are
        # the ground truth and cost nothing to read.
        wanted = set((matched or {}).get("images") or anchors_in(excerpt))
        image_rows = (
            admin.table("textbook_images")
            .select("id, image_id, caption, source_page, decorative")
            .eq("chapter_uuid", chapter["id"])
            .order("order_index")
            .execute()
        ).data or []

        images = [
            {"id": r["id"], "imageId": r["image_id"], "caption": r.get("caption"), "sourcePage": r["source_page"]}
            for r in image_rows
            # Borders and page furniture are never worth putting on a prep sheet.
            if not r.get("decorative") and (not wanted or r["image_id"] in wanted)
        ]

        return {
            "chapterTitle": chapter["chapter_title"],
            "chapterNumber": chapter["chapter_number"],
            "pageStart": chapter["page_start"],
            "pageEnd": chapter["page_end"],
            "matchedTopic": (matched or {}).get("title"),
            "excerpt": excerpt,
            "truncated": len(slice_) > len(excerpt),
            "images": images,
        }
    except Exception:
        return None


def textbook_prompt_block(g: dict) -> str:
    """The textbook block of the prompt. Says plainly which parts are fixed
    (the book's content) and which are the model's to invent (the teaching)."""
    where = (
        f'"{g["matchedTopic"]}" in Chapter {g["chapterNumber"]}: "{g["chapterTitle"]}"'
        if g.get("matchedTopic")
        else f'Chapter {g["chapterNumber"]}: "{g["chapterTitle"]}"'
    )

    if g["images"]:
        listing = "\n".join(f'- {i["imageId"]} (p{i["sourcePage"]}): {i.get("caption") or "no caption"}' for i in g["images"])
        catalogue = (
            "\nIllustrations printed on these pages. These are the REAL pictures from the\n"
            "book — prefer them over asking for a drawing, and reference them by id in\n"
            f'"textbookImages":\n{listing}'
        )
    else:
        catalogue = "\nThis stretch of the book has no usable illustrations, so any picture will have to be drawn."

    truncated_note = "\n[…continues]" if g["truncated"] else ""

    return f"""THE TEXTBOOK PAGES THIS LESSON MUST TEACH — {where}, pages {g["pageStart"]}-{g["pageEnd"]}.

This is the book in the children's hands, transcribed verbatim. Page markers
appear as <!-- page N -->; <img id="..." /> marks where a picture sits.

--- BEGIN TEXTBOOK ---
{g["excerpt"]}{truncated_note}
--- END TEXTBOOK ---
{catalogue}

HOW TO USE IT — this is the difference between a good prep sheet and a wrong one:
- The CONCEPT bullets teach what these pages teach. Same definitions, same
  facts, same worked examples, same numbers, same technical vocabulary. If the
  book says a forest should cover one-third of the earth, the lesson says
  one-third — you do not round it, improve it, or substitute a fact you know
  better.
- Never introduce a fact, term or example these pages do not contain. If
  something feels missing, that is the book's decision, not an error to correct.
- Reuse the book's own exercises and activities where they fit, word for word.
- Cite pages as (Page N) using the markers, so the teacher can point at the book.
- YOUR CREATIVITY GOES INTO THE TEACHING, not the content. Explore's real-life
  scenario, the Challenge activity, the analogies, the local framing, how it is
  pitched at this class's interests — all yours, and the lesson lives or dies on
  them. Invent the lesson; do not invent the syllabus."""
