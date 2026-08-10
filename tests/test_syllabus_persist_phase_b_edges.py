"""Edge cases for persist_extraction's Phase B block that test_syllabus_persist.py
doesn't cover: re-ingest behaviour, CHECK-constraint sanitising, duplicate
collapsing, and provenance.

Reuses the fakes from test_syllabus_persist rather than redefining them — that
module is the canonical description of the shape persist_extraction expects.
"""

import app.lib.syllabus_persist as sp
from test_syllabus_persist import FakeAdminClient, _ontology_with_phase_b_topic


def _client():
    return FakeAdminClient(classes_seed=[{"id": "class-1", "school_id": "school-1", "grade": "1"}])


def _run(ac, ontology):
    return sp.persist_extraction(
        ac, school_id="school-1", grade="1", subject="Maths",
        ontology=ontology, filename="book.pdf", exclude_topic_ids=set(),
    )


class TestCanonicalReuse:
    def test_a_second_extraction_reuses_entries_instead_of_duplicating(self):
        """The whole point of the canonical library: ingesting a second book
        that names the same concepts must link to the existing rows."""
        ac = _client()
        _run(ac, _ontology_with_phase_b_topic())
        assert len(ac.table("concepts").rows) == 2

        _run(ac, _ontology_with_phase_b_topic())
        assert len(ac.table("concepts").rows) == 2          # not 4
        assert len(ac.table("topic_concepts").rows) == 4    # but both topics linked


class TestScalarSanitising:
    """syllabus_topics has CHECK constraints on both columns (migration 020);
    an unsanitised value would fail the INSERT for every topic in the book."""

    def test_casing_is_normalised(self):
        ac = _client()
        ontology = _ontology_with_phase_b_topic()
        ontology["entities"]["topics"][0]["bloom_level"] = "Remember"
        _run(ac, ontology)
        assert next(iter(ac.table("syllabus_topics").rows.values()))["bloom_level"] == "remember"

    def test_a_value_outside_the_constraint_becomes_null(self):
        ac = _client()
        ontology = _ontology_with_phase_b_topic()
        ontology["entities"]["topics"][0]["difficulty"] = "intermediate"
        _run(ac, ontology)
        assert next(iter(ac.table("syllabus_topics").rows.values()))["difficulty"] is None


class TestNameCleaning:
    def test_names_differing_only_in_case_or_spacing_collapse(self):
        """The junction tables are unique on (topic_definition_id, entity_id),
        so two raw names resolving to one canonical row must not both be written."""
        ac = _client()
        ontology = _ontology_with_phase_b_topic()
        ontology["entities"]["topics"][0]["concepts"] = ["Addition", "addition", " Addition ", "Counting"]
        summary = _run(ac, ontology)
        assert len(ac.table("concepts").rows) == 2
        assert summary["concepts"] == 2

    def test_non_string_and_empty_entries_are_ignored(self):
        ac = _client()
        ontology = _ontology_with_phase_b_topic()
        ontology["entities"]["topics"][0]["concepts"] = ["Addition", "", "   ", None, 42, {"x": 1}]
        assert _run(ac, ontology)["concepts"] == 1


class TestProvenance:
    def test_every_link_records_the_raw_extracted_name(self):
        """source_name (migration 021) is what makes a bad merge undoable via
        canonical_mapping.split_canonical()."""
        ac = _client()
        _run(ac, _ontology_with_phase_b_topic())
        links = list(ac.table("topic_concepts").rows.values())
        assert all(link.get("source_name") for link in links)
        assert {link["source_name"] for link in links} == {"Addition", "Counting"}


class TestSummaryShape:
    def test_phase_b_counts_are_added_without_dropping_the_existing_keys(self):
        ac = _client()
        summary = _run(ac, _ontology_with_phase_b_topic())
        assert summary["concepts"] == 2
        assert summary["competencies"] == 2
        assert summary["vocabulary"] == 2
        assert summary["contexts"] == 2
        assert summary["learningOutcomes"] == 2
        for key in ("chapters", "topics", "subtopics", "exercises", "sidebars", "dependencies"):
            assert key in summary

    def test_vocabulary_is_keyed_on_term_not_name(self):
        """resolve_canonical maps kind -> name column; vocabulary is the one
        table that doesn't use 'name'."""
        ac = _client()
        _run(ac, _ontology_with_phase_b_topic())
        rows = list(ac.table("vocabulary").rows.values())
        assert len(rows) == 2
        assert all("term" in r for r in rows)
