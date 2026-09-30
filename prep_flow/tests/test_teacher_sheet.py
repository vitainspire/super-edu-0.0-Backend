"""The teacher-facing layout: does the page carry what the room needs?

Every assertion here is a thing generation ALREADY produces and the old renderer
threw away — the minutes, the materials, what to watch for, the handoff. The
value of this module is entirely in not discarding them, so the tests are about
presence rather than prose.
"""
import pytest

from prep_flow.teacher_sheet import period_title, render_chapter, render_period


MATERIAL = {
    "index": 2,
    "topic": "5. Let's play with match-sticks",
    "objective": "Lay match-sticks to copy a drawn shape's outline",
    "teachingOrder": ["refresher", "concept", "realLife", "challenge",
                      "levelSet", "explore"],
    "timings": {"refresher": 3, "concept": 10, "realLife": 3,
                "challenge": 7, "levelSet": 4, "explore": 3},
    "materialsUsed": ["match-sticks", "board and chalk",
                      "textbook open to pages 17, 18"],
    "pagesCited": [17, 20],
    "previousTopicRefresher": {"previousTopic": "views",
                               "recap": [{"text": "The cup is still on the desk",
                                          "detail": "Ask: 'What moved yesterday?'"}]},
    "concept": {"points": [{"text": "One stick, one side",
                            "detail": "Lay three sticks along a triangle."}]},
    "realLife": {"points": [{"text": "Rangoli", "detail": "Floor patterns."}]},
    "challenge": {"activity": "Clap Patterns",
                  "points": [{"text": "Build the six shapes", "detail": "Page 18."}]},
    "levelSet": {"points": [{"text": "Thumbs", "detail": "Check understanding."}]},
    "explore": {"points": [{"text": "Paper boat", "detail": "Fold and count."}],
                "handoff": {"scene": "a rectangle left on the board",
                            "object": "match-sticks",
                            "question": "How many sticks would we need for a big pile?",
                            "discovery": ""}},
    "sectionWatch": {"concept": {"text": "three sticks",
                                 "detail": "Watch for a child counting corners."}},
}

CONTRACT = {
    "grade": "3", "subject": "Mathematics",
    "chapter": {"number": 2, "title": "Chapter 2",
                "arc": "see differently, then count in tens"},
    "topics": [
        {"index": 1, "academic": {"masteryTarget": "a flat picture shows one side"},
         "knowledgeChain": {"gained": "can show which face is hidden"},
         "experiencePlan": {"anchor": "cup"},
         "grounding": {"pageStart": 12, "pageEnd": 13}},
        {"index": 2, "academic": {"masteryTarget": "sticks equal sides"},
         "knowledgeChain": {"gained": "can lay sticks along an outline"},
         "experiencePlan": {"anchor": "match-stick"},
         "grounding": {"pageStart": 17, "pageEnd": 20}},
    ],
}


# ── Period titles ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heading", ["Activity-1", "Activity 2", "Do This",
                                     "Exercise-4", "7.", "NUMBERS", "IV."])
def test_a_book_heading_that_names_nothing_does_not_become_the_title(heading):
    """Textbooks print `Activity-1` and `NUMBERS` as section headings. They are
    real boundaries — sequencing is right to cut on them — and useless as titles:
    a teacher planning a week cannot tell three `Activity-1`s apart."""
    title = period_title({"topic": heading,
                          "objective": "Say how many tens and ones are in a pile"})
    assert "SAY HOW MANY TENS" in title
    assert heading.upper() not in title


def test_the_objective_is_the_title_because_it_was_written_to_be_one():
    """Generation already writes `objective` as a verb-first headline of four to
    eight words. That is a period title; nothing has to invent one."""
    assert period_title(MATERIAL) == "LAY MATCH-STICKS TO COPY A DRAWN SHAPE'S OUTLINE"


def test_a_real_book_heading_survives_when_there_is_no_objective():
    assert period_title({"topic": "Comparing numbers"}) == "COMPARING NUMBERS"


# ── The period page ──────────────────────────────────────────────────────────

def test_the_page_carries_what_the_room_needs():
    page = render_period(MATERIAL, number=2, total=3, next_number=3)

    assert "# PERIOD 2 —" in page
    assert "### Objective" in page
    # 3+10+3+7+4+3 — read off the sheet's own timings, not assumed.
    assert "**30 minutes**" in page
    assert "### Materials" in page
    assert "* match-sticks" in page
    assert "* Textbook, pages **17–20**" in page


def test_each_section_carries_a_running_clock():
    """Mid-lesson the question is never "how long is Concept", it is "am I
    behind" — which needs the wall clock, not a duration the teacher has to
    sum in their head."""
    page = render_period(MATERIAL, number=2, total=3)
    assert "1. REFRESHER" in page and "0–3" in page
    assert "2. CONCEPT" in page and "3–13" in page       # 3 + 10
    assert "6. EXPLORE" in page and "27–30" in page      # ends on the period


def test_the_section_order_is_the_material_s_own():
    """A period that teaches Real Life before Concept does so because the book's
    own moves said to. A renderer that imposed the canonical six would undo it."""
    reordered = {**MATERIAL,
                 "teachingOrder": ["refresher", "realLife", "challenge",
                                   "concept", "levelSet", "explore"]}
    page = render_period(reordered, number=3, total=3)
    assert page.index("REAL LIFE") < page.index("CONCEPT")


def test_the_watch_fors_are_hoisted_into_one_block():
    """CHANGED BY DESIGN. Six "Teacher watches for" paragraphs per period is six
    blocks of caution between the teacher and the next thing they have to do,
    and they stop being read. The content is hoisted to one WATCH FOR block
    before the lesson; inline, a one-line pointer is left behind."""
    page = render_period(MATERIAL, number=2, total=3)

    assert "### Watch for" in page
    assert "counting corners" in page
    # Hoisted ABOVE the first section, not buried under the one it concerns.
    assert page.index("### Watch for") < page.index("1. REFRESHER")
    # And repeated as a pointer, not as the paragraph again.
    assert "⚠ **Check**" in page
    assert page.count("counting corners") == 1


def test_the_named_activity_appears_on_the_challenge():
    page = render_period(MATERIAL, number=2, total=3)
    assert "**Activity: Clap Patterns**" in page


def test_the_handoff_becomes_a_bridge_into_the_next_period():
    """The seam is the whole reason forty sheets are a chapter. It exists in the
    data as `explore.handoff` and was never on the page."""
    page = render_period(MATERIAL, number=2, total=3, next_number=3)
    assert "### End the lesson with" in page
    assert "**Bridge into Period 3:**" in page
    assert "How many sticks would we need" in page


def test_the_last_period_has_no_bridge():
    page = render_period(MATERIAL, number=3, total=3, next_number=None)
    assert "Bridge into Period" not in page


def test_the_teacher_s_own_words_are_lifted_onto_their_own_line():
    page = render_period(MATERIAL, number=2, total=3)
    assert "> “What moved yesterday?”" in page


# ── The three levels ─────────────────────────────────────────────────────────

def test_quick_is_what_you_hold_while_teaching():
    """Actions and the words you say. No rationale, no detail paragraphs."""
    from prep_flow.teacher_sheet import QUICK
    page = render_period(MATERIAL, number=2, total=3, next_number=3, level=QUICK)

    assert "### Materials" in page
    assert "- One stick, one side" in page, "the action lead survives"
    assert "> “What moved yesterday?”" in page, "so do the words said aloud"
    # The night-before content does not.
    assert "Lay three sticks along a triangle." not in page
    assert "### Watch for" not in page
    assert "### Objective" not in page, "the title already says it"


def test_full_is_what_you_read_the_night_before():
    from prep_flow.teacher_sheet import FULL
    page = render_period(MATERIAL, number=2, total=3, next_number=3, level=FULL)
    assert "### Objective" in page
    assert "Lay three sticks along a triangle." in page
    assert "### Watch for" in page
    assert "### Design trace" not in page, "provenance is not a teacher's problem"


def test_trace_adds_where_each_decision_came_from():
    from prep_flow.teacher_sheet import TRACE
    spec = CONTRACT["topics"][1]
    page = render_period(MATERIAL, number=2, total=3, spec=spec, level=TRACE)
    assert "### Design trace" in page
    assert "sticks equal sides" in page, "the mastery target"
    assert "can lay sticks along an outline" in page, "what this period gains"


def test_quick_is_dramatically_smaller_than_full():
    """The point of the level is compression, and the thing compressed is the
    DETAIL paragraph behind each action.

    Measured on realistic material rather than the small fixture above: real
    details run to a paragraph each, and on the captured Class 3 chapter the
    whole document goes 31.7 KB -> 5.9 KB. A one-sentence fixture would make
    this test pass or fail on the size of the shared chapter footer instead.
    """
    from prep_flow.teacher_sheet import FULL, QUICK, render_period

    paragraph = (
        "Open the textbook to page 17. Point at the three pictures: triangle "
        "using 3 sticks, square using 4 sticks, rectangle using 6 sticks "
        "(Page 17). Ask the children to count aloud how many sticks Suresh used "
        "for each. Wait. The numbers must come from them, not from you. Write "
        "the three numbers on the board: 3, 4, 6.")
    realistic = {**MATERIAL,
                 "concept": {"points": [{"text": "One stick, one side",
                                         "detail": paragraph}] * 3}}

    quick = render_period(realistic, number=2, total=3, level=QUICK)
    full = render_period(realistic, number=2, total=3, level=FULL)
    assert len(quick) < len(full) * 0.5, (len(quick), len(full))
    # And the compression is the detail, not the actions.
    assert quick.count("One stick, one side") == 3
    assert paragraph not in quick


def test_the_materials_list_names_the_textbook_once():
    """The model lists the textbook among its materials ("textbook open to pages
    17, 18") AND the sheet carries a checked `pagesCited`. Printing both inside
    Materials gives a teacher two textbook lines that disagree about the range.

    The `*Textbook section: …*` line below Materials is a different thing — the
    book's own heading, which is what the sheet is grounded in — and is expected.
    """
    page = render_period(MATERIAL, number=2, total=3)
    bullets = [line for line in
               page.split("### Materials")[1].split("---")[0].splitlines()
               if line.startswith("* ")]
    textbook_bullets = [b for b in bullets if "textbook" in b.lower()]
    assert len(textbook_bullets) == 1, textbook_bullets
    assert "pages **17–20**" in textbook_bullets[0]
    assert not any("open to pages" in b for b in bullets)
    # The grounding line is separate and still present.
    assert "*Textbook section: 5. Let's play with match-sticks*" in page


def test_a_section_with_no_content_is_not_given_a_heading():
    thin = {**MATERIAL, "levelSet": {"points": []}}
    page = render_period(thin, number=2, total=3)
    assert "LEVEL SET" not in page


# ── The chapter document ─────────────────────────────────────────────────────

def test_the_chapter_opens_with_the_arc():
    doc = render_chapter(CONTRACT, {1: MATERIAL, 2: MATERIAL})
    assert "# CHAPTER 2 — TEACHER PREP" in doc
    assert "**Grade:** 3" in doc
    assert "**Chapter arc:**" in doc
    assert "see differently, then count in tens" in doc


def test_a_placeholder_chapter_name_is_not_repeated():
    """Ingests frequently supply "Chapter 2" as the title, which the H1 says."""
    doc = render_chapter(CONTRACT, {1: MATERIAL})
    assert "**Chapter:** Chapter 2" not in doc


def test_the_chapter_ends_with_the_three_things_only_chapter_scale_gives():
    doc = render_chapter(CONTRACT, {1: MATERIAL, 2: MATERIAL})

    assert "# TEACHER QUICK REFERENCE" in doc
    assert "| Period | Core idea | Main object | Textbook |" in doc
    assert "pp. 12–13" in doc and "pp. 17–20" in doc

    assert "### The progression" in doc
    assert "**cup**  →  **match-stick**" in doc

    assert "# WHAT THE TEACHER SHOULD REMEMBER" in doc
    assert "a flat picture shows one side" in doc
    assert "sticks equal sides" in doc


def test_the_progression_is_omitted_when_every_period_shares_an_anchor():
    """An arrow chain from cup to cup to cup is noise dressed as structure."""
    same = {**CONTRACT, "topics": [
        {**CONTRACT["topics"][0]},
        {**CONTRACT["topics"][1], "experiencePlan": {"anchor": "cup"}}]}
    doc = render_chapter(same, {1: MATERIAL, 2: MATERIAL})
    assert "### The progression" not in doc


def test_periods_are_numbered_from_one_regardless_of_topic_index():
    doc = render_chapter(CONTRACT, {1: MATERIAL, 2: MATERIAL})
    assert "# PERIOD 1 —" in doc and "# PERIOD 2 —" in doc


# ── Plain text ───────────────────────────────────────────────────────────────

def test_the_plain_text_copy_keeps_the_content_and_drops_the_markup():
    """A teacher printing from a phone or pasting into a WhatsApp group has no
    markdown renderer, so the same document has to survive without one."""
    from prep_flow.teacher_sheet import render_chapter_text

    text = render_chapter_text(CONTRACT, {1: MATERIAL, 2: MATERIAL})

    assert "**" not in text, "no bold markers"
    assert "\n# " not in text and not text.startswith("# "), "no hash headings"
    assert "\n> " not in text, "no markdown blockquotes"

    # The content itself is all still there.
    assert "CHAPTER 2 — TEACHER PREP" in text
    assert "PERIOD 1 —" in text
    assert "MATERIALS" in text and "match-sticks" in text
    assert "Bridge into Period 2" in text
    assert "TEACHER QUICK REFERENCE" in text
    # The teacher's spoken line survives, indented rather than quoted.
    assert "What moved yesterday?" in text


def test_headings_become_rules_a_teacher_can_scan():
    from prep_flow.teacher_sheet import render_chapter_text
    text = render_chapter_text(CONTRACT, {1: MATERIAL})
    assert "=" * 40 in text, "period headings are ruled"
    assert "--- 0–3  1. REFRESHER" in text, "sections keep their clock and number"


def test_the_table_is_left_alone():
    """Pipes line up in a monospaced file and nothing better fits in plain text."""
    from prep_flow.teacher_sheet import render_chapter_text
    text = render_chapter_text(CONTRACT, {1: MATERIAL, 2: MATERIAL})
    assert "| Period | Core idea | Main object | Textbook |" in text
