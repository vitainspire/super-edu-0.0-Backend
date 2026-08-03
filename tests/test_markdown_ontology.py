"""Markdown <-> ontology parser.

The parser exists so that structure generation stops being the model's job. That
argument only holds if the parser is (a) correct on well-formed input, (b) total
on malformed input, and (c) emits exactly the shape validate_and_fix() consumes.
Those three claims are what this suite tests.
"""

import json

import pytest

from app.lib.markdown_ontology import (
    parse_markdown_ontology,
    ontology_to_markdown,
    markdown_chapter_prompt,
    VALID_SKILL_TYPES,
    VALID_EXERCISE_TYPES,
)

# ── A well-formed chapter, used across the happy-path tests ───────────────────

GOOD = """---
chapter: 3
title: Numbers Around Us
pages: 24-36
---

## Counting to Ten
---
pages: 24-27
---

Students count objects up to ten and match each count to its numeral.

### Writing the numeral 1 `writing_skill` p24
Trace the numeral, then say its name aloud.

### Recognising groups of ten `recognition_skill` p26
Circle the group that has exactly ten objects.

#### Exercises
- `writing_practice` p25 — Trace each numeral three times in the box.
- `counting_activity` p26 — Count the mangoes in the basket and write the number.

#### Sidebars
- p27 — Did you know? Finger counting is one of the oldest methods.

## Counting to Twenty
---
pages: 28-33
prerequisites: [Counting to Ten]
---

Students extend counting beyond ten, noticing the repeating pattern.

### Numbers eleven to fifteen `counting_skill` p28
Say each number aloud while pointing at the number line.

#### Exercises
- `matching_exercise` p30 — Match each numeral to the correct group of objects.
"""


@pytest.fixture
def good():
    return parse_markdown_ontology(GOOD)


# ── Happy path ────────────────────────────────────────────────────────────────

class TestWellFormedInput:
    def test_reports_no_warnings(self, good):
        assert good.warnings == []

    def test_is_ok(self, good):
        assert good.ok is True

    def test_reads_chapter_metadata(self, good):
        chapters = good.ontology["entities"]["chapters"]
        assert len(chapters) == 1
        assert chapters[0]["id"] == "C_3"
        assert chapters[0]["number"] == 3
        assert chapters[0]["title"] == "Numbers Around Us"
        assert chapters[0]["page_start"] == 24
        assert chapters[0]["page_end"] == 36

    def test_extracts_both_topics_in_order(self, good):
        topics = good.ontology["entities"]["topics"]
        assert [t["name"] for t in topics] == ["Counting to Ten", "Counting to Twenty"]
        assert [t["id"] for t in topics] == ["T_3_1", "T_3_2"]

    def test_topic_pages_and_summary(self, good):
        t = good.ontology["entities"]["topics"][0]
        assert (t["page_start"], t["page_end"]) == (24, 27)
        assert t["summary"].startswith("Students count objects up to ten")

    def test_every_topic_belongs_to_the_chapter(self, good):
        for t in good.ontology["entities"]["topics"]:
            assert t["chapter_id"] == "C_3"

    def test_subtopics_are_flat_and_carry_topic_id(self, good):
        # validate_and_fix() iterates entities["subtopics"] as a flat list
        subs = good.ontology["entities"]["subtopics"]
        assert len(subs) == 3
        assert [s["topic_id"] for s in subs] == ["T_3_1", "T_3_1", "T_3_2"]
        # and nothing nested is left behind on the topics
        for t in good.ontology["entities"]["topics"]:
            assert "subtopics" not in t

    def test_subtopic_fields(self, good):
        s = good.ontology["entities"]["subtopics"][0]
        assert s["id"] == "ST_3_1_1"
        assert s["name"] == "Writing the numeral 1"
        assert s["skill_type"] == "writing_skill"
        assert s["page_start"] == 24
        assert s["summary"] == "Trace the numeral, then say its name aloud."

    def test_exercises_carry_type_page_and_text(self, good):
        ex = good.ontology["entities"]["exercises"]
        assert len(ex) == 3
        first = ex[0]
        assert first["id"] == "E_3_1_1"
        assert first["topic_id"] == "T_3_1"
        assert first["exercise_type"] == "writing_practice"
        assert first["page"] == 25
        assert first["text"] == "Trace each numeral three times in the box."

    def test_sidebars(self, good):
        sb = good.ontology["entities"]["sidebars"]
        assert len(sb) == 1
        assert sb[0]["topic_id"] == "T_3_1"
        assert sb[0]["page"] == 27
        assert sb[0]["text"].startswith("Did you know?")

    def test_stats_match_the_entities(self, good):
        e = good.ontology["entities"]
        assert good.stats == {
            "topics": len(e["topics"]),
            "subtopics": len(e["subtopics"]),
            "exercises": len(e["exercises"]),
            "sidebars": len(e["sidebars"]),
            "prerequisite_edges": len(good.ontology["graphs"]["concept_dependencies"]),
        }

    def test_ids_are_unique_across_every_entity_type(self, good):
        e = good.ontology["entities"]
        ids = [x["id"] for key in ("chapters", "topics", "subtopics", "exercises", "sidebars")
               for x in e[key]]
        assert len(ids) == len(set(ids))


# ── Prerequisites resolved by name, in code ───────────────────────────────────

class TestPrerequisiteResolution:
    def test_resolves_a_prerequisite_name_to_the_topic_id(self, good):
        second = good.ontology["entities"]["topics"][1]
        assert second["prerequisites"] == ["T_3_1"]

    def test_derives_the_matching_dependency_edge(self, good):
        assert good.ontology["graphs"]["concept_dependencies"] == [
            {"from": "T_3_1", "to": "T_3_2", "type": "prerequisite_of"}
        ]

    def test_topic_without_prerequisites_gets_an_empty_list(self, good):
        assert good.ontology["entities"]["topics"][0]["prerequisites"] == []

    def test_name_matching_is_case_insensitive(self):
        md = "## Alpha\n\nFirst.\n\n## Beta\n---\nprerequisites: [ALPHA]\n---\n\nSecond.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][1]["prerequisites"] == ["T_1_1"]

    def test_drops_an_unresolvable_prerequisite_with_a_warning(self):
        md = "## Alpha\n---\nprerequisites: [Nonexistent Topic]\n---\n\nText.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][0]["prerequisites"] == []
        assert any("Nonexistent Topic" in w for w in r.warnings)

    def test_never_emits_a_plain_string_prerequisite(self):
        # Prompt rule 6 asks the model to promise this; here it's enforced.
        md = "## Alpha\n\nA.\n\n## Beta\n---\nprerequisites: [Alpha, Some Prose, Alpha]\n---\n\nB.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        pres = r.ontology["entities"]["topics"][1]["prerequisites"]
        assert pres == ["T_1_1"]  # resolved, deduplicated, prose dropped
        for p in pres:
            assert p.startswith("T_")

    def test_a_topic_cannot_be_its_own_prerequisite(self):
        md = "## Alpha\n---\nprerequisites: [Alpha]\n---\n\nA.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][0]["prerequisites"] == []

    def test_accepts_an_explicit_id_as_well_as_a_name(self):
        md = "## Alpha\n\nA.\n\n## Beta\n---\nprerequisites: [T_1_1]\n---\n\nB.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][1]["prerequisites"] == ["T_1_1"]

    def test_accepts_a_bare_comma_separated_string(self):
        md = "## Alpha\n\nA.\n\n## Beta\n---\nprerequisites: Alpha\n---\n\nB.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][1]["prerequisites"] == ["T_1_1"]


# ── Derived graphs ────────────────────────────────────────────────────────────

class TestDerivedGraphs:
    def test_chapter_structure_has_one_edge_per_topic(self, good):
        edges = good.ontology["graphs"]["chapter_structure"]
        topics = good.ontology["entities"]["topics"]
        assert len(edges) == len(topics)
        assert edges == [
            {"from": "C_3", "to": t["id"], "type": "contains"} for t in topics
        ]

    def test_exercise_mapping_has_one_edge_per_exercise(self, good):
        edges = good.ontology["graphs"]["exercise_mapping"]
        exercises = good.ontology["entities"]["exercises"]
        assert len(edges) == len(exercises)
        for edge, ex in zip(edges, exercises):
            assert edge == {"from": ex["id"], "to": ex["topic_id"], "type": "tests"}

    def test_every_graph_edge_points_at_a_real_entity(self, good):
        e = good.ontology["entities"]
        known = {x["id"] for key in ("chapters", "topics", "subtopics", "exercises", "sidebars")
                 for x in e[key]}
        for edges in good.ontology["graphs"].values():
            for edge in edges:
                assert edge["from"] in known
                assert edge["to"] in known


# ── Batch continuation ────────────────────────────────────────────────────────

class TestBatchNumbering:
    def test_offsets_continue_ids_without_colliding(self):
        first = parse_markdown_ontology(GOOD, chapter_number=3)
        counts = first.stats

        second = parse_markdown_ontology(
            GOOD,
            chapter_number=3,
            topic_start=counts["topics"] + 1,
            subtopic_start=counts["subtopics"] + 1,
            exercise_start=counts["exercises"] + 1,
        )

        first_ids = {x["id"] for key in ("topics", "subtopics", "exercises")
                     for x in first.ontology["entities"][key]}
        second_ids = {x["id"] for key in ("topics", "subtopics", "exercises")
                      for x in second.ontology["entities"][key]}
        assert first_ids.isdisjoint(second_ids)

    def test_second_batch_topics_start_at_the_offset(self):
        r = parse_markdown_ontology(GOOD, chapter_number=3, topic_start=5)
        assert [t["id"] for t in r.ontology["entities"]["topics"]] == ["T_3_5", "T_3_6"]


# ── Totality: the parser must never raise ─────────────────────────────────────

class TestNeverRaises:
    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        "\n\n\n",
        "no markdown structure at all, just a sentence",
        "---",
        "---\nchapter: not-a-number\n---",
        "## ",
        "###### deeply nested with no parent",
        "## Topic\n---\nprerequisites: [\n---\n",
        "{\"entities\": {\"topics\": []}}",           # JSON fed to the MD parser
        "```markdown\n## Topic\n\nText.\n```",
        "\x00\x01\x02 binary garbage \xff",
        "## " + "x" * 10000,
        "- orphan bullet with no section\n- another",
        "#### Exercises\n- `writing_practice` p1 — orphaned exercise section",
        "## A\n## A\n## A\n",
        "---\n---\n---\n---\n",
    ], ids=lambda s: repr(s[:36]))
    def test_returns_a_valid_structure_for_any_input(self, bad):
        r = parse_markdown_ontology(bad)
        assert isinstance(r.ontology, dict)
        e = r.ontology["entities"]
        for key in ("chapters", "topics", "subtopics", "exercises", "sidebars"):
            assert isinstance(e[key], list)
        for key in ("chapter_structure", "exercise_mapping", "concept_dependencies"):
            assert isinstance(r.ontology["graphs"][key], list)
        assert isinstance(r.warnings, list)
        # and it must be JSON-serialisable, since the job manager stores it
        json.dumps(r.ontology)

    def test_none_input_is_handled(self):
        r = parse_markdown_ontology(None)  # type: ignore[arg-type]
        assert r.ontology["entities"]["topics"] == []
        assert r.ok is False

    def test_empty_input_is_not_ok(self):
        assert parse_markdown_ontology("").ok is False


# ── Robustness on realistic model misbehaviour ────────────────────────────────

class TestModelMisbehaviour:
    def test_strips_a_fence_wrapping_the_whole_document(self):
        r = parse_markdown_ontology("```markdown\n## Fractions\n\nHalves and quarters.\n```")
        assert [t["name"] for t in r.ontology["entities"]["topics"]] == ["Fractions"]
        assert r.ontology["entities"]["topics"][0]["summary"] == "Halves and quarters."

    def test_recovers_everything_above_a_truncation(self):
        truncated = GOOD[: GOOD.index("## Counting to Twenty") + 60]
        r = parse_markdown_ontology(truncated)
        # First topic survives intact — the value JSON cannot offer
        assert r.ontology["entities"]["topics"][0]["name"] == "Counting to Ten"
        assert len(r.ontology["entities"]["exercises"]) == 2
        assert r.ok is True

    def test_survives_an_unterminated_yaml_block(self):
        md = "## Alpha\n---\npages: 4-8\n\n## Beta\n\nSecond topic.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        names = [t["name"] for t in r.ontology["entities"]["topics"]]
        assert names == ["Alpha", "Beta"]
        # the unterminated block still yielded its page range
        assert r.ontology["entities"]["topics"][0]["page_start"] == 4

    def test_attaches_an_orphan_subtopic_to_a_synthesised_topic(self):
        r = parse_markdown_ontology("### Orphan `reading_skill` p2\nSome text.\n")
        assert r.ontology["entities"]["topics"][0]["name"] == "(untitled topic)"
        assert r.ontology["entities"]["subtopics"][0]["name"] == "Orphan"
        assert any("untitled topic" in w for w in r.warnings)

    def test_reclassifies_an_invalid_skill_type(self):
        md = "## Alpha\n\n### Reading aloud `not_a_real_skill` p3\nRead the poem aloud.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        skill = r.ontology["entities"]["subtopics"][0]["skill_type"]
        assert skill in VALID_SKILL_TYPES
        assert any("not_a_real_skill" in w for w in r.warnings)

    def test_infers_a_missing_skill_type(self):
        md = "## Alpha\n\n### Tracing letters\nTrace and copy each letter.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["subtopics"][0]["skill_type"] in VALID_SKILL_TYPES

    def test_infers_a_missing_exercise_type(self):
        md = "## Alpha\n\n#### Exercises\n- p3 — Draw and colour the mango.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        ex = r.ontology["entities"]["exercises"][0]
        assert ex["exercise_type"] in VALID_EXERCISE_TYPES
        assert ex["text"] == "Draw and colour the mango."

    @pytest.mark.parametrize("heading,expected", [
        ("#### Exercises", "exercises"),
        ("#### Activities", "exercises"),
        ("#### Sidebars", "sidebars"),
        ("#### Notes", "sidebars"),
    ])
    def test_accepts_section_label_variations(self, heading, expected):
        md = f"## Alpha\n\n{heading}\n- p3 — Something to do.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        bucket = "exercises" if expected == "exercises" else "sidebars"
        assert len(r.ontology["entities"][bucket]) == 1

    @pytest.mark.parametrize("marker", ["p24", "p. 24", "pages 24-27", "pages: 24-27", "24-27"])
    def test_accepts_page_marker_variations(self, marker):
        md = f"## Alpha\n---\npages: {marker}\n---\n\nText.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][0]["page_start"] == 24

    @pytest.mark.parametrize("bullet", ["- text", "* text", "+ text"])
    def test_accepts_bullet_marker_variations(self, bullet):
        md = f"## Alpha\n\n#### Exercises\n{bullet}\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert len(r.ontology["entities"]["exercises"]) == 1

    def test_preserves_non_latin_script_verbatim(self):
        md = "## संख्या और गिनती\n---\npages: 4-6\n---\n\nStudents learn counting.\n\n### एक लिखना `writing_skill` p4\nTrace it.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][0]["name"] == "संख्या और गिनती"
        assert r.ontology["entities"]["subtopics"][0]["name"] == "एक लिखना"

    def test_drops_empty_topic_headings(self):
        md = "## Real Topic\n\nHas content.\n\n## \n\n## Another\n\nAlso content.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert [t["name"] for t in r.ontology["entities"]["topics"]] == ["Real Topic", "Another"]

    def test_a_topic_with_no_page_inherits_from_its_subtopic(self):
        md = "## Alpha\n\nText.\n\n### Sub `reading_skill` p17\nRead it.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        assert r.ontology["entities"]["topics"][0]["page_start"] == 17

    def test_a_topic_with_no_page_falls_back_to_the_chapter(self):
        md = "---\nchapter: 2\npages: 10-20\n---\n\n## Alpha\n\nText.\n"
        r = parse_markdown_ontology(md)
        assert r.ontology["entities"]["topics"][0]["page_start"] == 10

    def test_page_end_is_never_before_page_start(self):
        md = "## Alpha\n---\npages: 30-12\n---\n\nText.\n"
        r = parse_markdown_ontology(md, chapter_number=1)
        t = r.ontology["entities"]["topics"][0]
        assert t["page_end"] >= t["page_start"]


# ── Round trip ────────────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_markdown_to_ontology_to_markdown_to_ontology_is_stable(self, good):
        once = good.ontology
        rendered = ontology_to_markdown(once)
        twice = parse_markdown_ontology(rendered).ontology
        assert twice == once

    def test_rendered_markdown_reparses_without_warnings(self, good):
        again = parse_markdown_ontology(ontology_to_markdown(good.ontology))
        assert again.warnings == []

    def test_round_trip_preserves_counts(self, good):
        again = parse_markdown_ontology(ontology_to_markdown(good.ontology))
        assert again.stats == good.stats

    def test_round_trip_preserves_prerequisites(self, good):
        again = parse_markdown_ontology(ontology_to_markdown(good.ontology))
        assert again.ontology["entities"]["topics"][1]["prerequisites"] == ["T_3_1"]

    def test_renders_nothing_for_an_empty_ontology(self):
        assert ontology_to_markdown({}) == ""
        assert ontology_to_markdown({"entities": {"chapters": []}}) == ""

    def test_can_render_a_single_chapter_from_a_multi_chapter_ontology(self, good):
        merged = {
            "entities": {
                "chapters": good.ontology["entities"]["chapters"] + [
                    {"id": "C_9", "number": 9, "title": "Other", "page_start": 90, "page_end": 99}
                ],
                "topics": good.ontology["entities"]["topics"],
                "subtopics": good.ontology["entities"]["subtopics"],
                "exercises": good.ontology["entities"]["exercises"],
                "sidebars": good.ontology["entities"]["sidebars"],
            },
            "graphs": good.ontology["graphs"],
        }
        only_three = ontology_to_markdown(merged, chapter_id="C_3")
        assert "Numbers Around Us" in only_three
        assert "Other" not in only_three


# ── Compatibility with the existing validation stage ──────────────────────────

class TestValidateAndFixCompatibility:
    """The parser's output must be a drop-in for what the JSON path produces."""

    def test_output_survives_validate_and_fix(self, good):
        pytest.importorskip("fitz", reason="validate_and_fix lives in a PyMuPDF-importing module")
        from app.lib.vision_extraction import validate_and_fix

        fixed = validate_and_fix(json.loads(json.dumps(good.ontology)))
        assert len(fixed["entities"]["topics"]) == 2
        assert fixed["entities"]["chapters"][0]["status"] in ("verified", "partial", "unverified")
        # chapter numbers are reassigned from sorted position
        assert fixed["entities"]["chapters"][0]["number"] == 1

    def test_output_maps_to_the_syllabus_review_shape(self, good):
        from app.lib.syllabus_pdf_jobs import _map_ontology_to_topics

        rows = _map_ontology_to_topics(good.ontology)
        assert [r["topic"] for r in rows] == ["Counting to Ten", "Counting to Twenty"]
        assert rows[0]["subTopics"] == ["Writing the numeral 1", "Recognising groups of ten"]
        assert rows[0]["description"].startswith("Students count objects")
        assert rows[0]["weekNumber"] == 3
        assert rows[0]["exerciseCount"] == 2
        assert rows[0]["sidebarCount"] == 1

    def test_entity_keys_match_the_json_prompt_contract(self, good):
        e = good.ontology["entities"]
        assert set(e) == {"chapters", "topics", "subtopics", "exercises", "sidebars"}
        assert set(good.ontology["graphs"]) == {
            "chapter_structure", "exercise_mapping", "concept_dependencies"
        }
        assert set(e["topics"][0]) == {
            "id", "name", "summary", "chapter_id", "page_start", "page_end", "prerequisites"
        }
        assert set(e["subtopics"][0]) == {
            "id", "name", "summary", "skill_type", "page_start", "page_end", "topic_id"
        }
        assert set(e["exercises"][0]) == {"id", "text", "topic_id", "page", "exercise_type"}
        assert set(e["sidebars"][0]) == {"id", "text", "topic_id", "page"}
        assert set(e["chapters"][0]) == {"id", "number", "title", "page_start", "page_end"}


# ── The prompt ────────────────────────────────────────────────────────────────

class TestPrompt:
    def test_mentions_no_id_schema(self):
        prompt = markdown_chapter_prompt(3, "Telugu", "ctx", "chapters")
        # ids are this module's job; the model must not be asked to invent them
        assert "T_3_1" not in prompt
        assert "ST_" not in prompt
        assert "ID SCHEMA" not in prompt

    def test_keeps_every_extraction_rule(self):
        prompt = markdown_chapter_prompt(3, "Telugu", "ctx", "chapters")
        for rule in ("original script", "page numbers", "Exercises", "Sidebars",
                     "prerequisites", "skill_type", "exercise_type"):
            assert rule in prompt

    def test_lists_only_valid_type_vocabularies(self):
        prompt = markdown_chapter_prompt(1, "English", "ctx", "chapters")
        for skill in VALID_SKILL_TYPES:
            assert skill in prompt
        for etype in VALID_EXERCISE_TYPES:
            assert etype in prompt

    def test_is_materially_shorter_than_the_json_prompt(self):
        pytest.importorskip("fitz")
        from app.lib.vision_extraction import _chapter_prompt

        md = markdown_chapter_prompt(3, "Telugu", "ctx", "chapters")
        js = _chapter_prompt(3, "Telugu", "ctx", "chapters")
        assert len(md) < len(js)

    def test_includes_the_prior_topics_section_only_when_given(self):
        assert "ALREADY EXTRACTED" not in markdown_chapter_prompt(1, "English", "c", "l")
        assert "ALREADY EXTRACTED" in markdown_chapter_prompt(1, "English", "c", "l", "T1: Alpha")

    def test_its_own_example_shape_parses(self):
        """The template in the prompt must actually be parseable — otherwise the
        model is being shown something the parser rejects."""
        prompt = markdown_chapter_prompt(3, "English", "ctx", "chapters")
        example = prompt[prompt.index("---\nchapter:"):]
        r = parse_markdown_ontology(example)
        assert r.ok is True
