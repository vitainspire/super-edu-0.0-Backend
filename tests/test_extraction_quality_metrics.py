"""Metrics behind the tier A/B harness (scripts/extraction_quality_compare.py).

These decide whether a 70%-cheaper model gets adopted, so a wrong metric is worse
than no metric: it would green-light a model that transliterates Telugu.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("fitz", reason="the harness imports vision_extraction")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extraction_quality_compare import (  # noqa: E402
    script_histogram,
    dominant_script,
    names_text,
    coverage,
    type_validity,
    page_spans,
    align_topics,
    spread,
)

TELUGU = "సంఖ్యలు లెక్కించడం"
HINDI = "संख्या और गिनती"
LATIN = "Sankhyalu Lekkinchadam"


def ontology(topics=(), subtopics=(), exercises=(), sidebars=(), chapters=None):
    return {
        "entities": {
            "chapters": list(chapters if chapters is not None else
                             [{"id": "C_1", "number": 1, "title": "Ch", "page_start": 1, "page_end": 9}]),
            "topics": list(topics),
            "subtopics": list(subtopics),
            "exercises": list(exercises),
            "sidebars": list(sidebars),
        },
        "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
    }


def topic(name, ps=1, pe=2, tid="T_1_1"):
    return {"id": tid, "name": name, "summary": "s", "chapter_id": "C_1",
            "page_start": ps, "page_end": pe, "prerequisites": []}


# ── Script detection: the automatic disqualifier ───────────────────────────────

class TestScriptDetection:
    def test_identifies_telugu(self):
        assert dominant_script(TELUGU) == "Telugu"

    def test_identifies_devanagari(self):
        assert dominant_script(HINDI) == "Devanagari"

    def test_identifies_latin(self):
        assert dominant_script(LATIN) == "Latin"

    def test_distinguishes_a_transliteration_from_the_original(self):
        """The whole point: same words, and the metric must not call them equal."""
        assert dominant_script(TELUGU) != dominant_script(LATIN)

    def test_ignores_digits_and_punctuation(self):
        # Page markers and separators are script-neutral and would dilute the signal
        assert script_histogram("123 -- ,.!") == {}

    def test_ignores_whitespace(self):
        assert script_histogram("   \n\t ") == {}

    def test_a_mostly_telugu_name_with_a_latin_word_is_still_telugu(self):
        assert dominant_script(f"{TELUGU} Maths") == "Telugu"

    def test_counts_each_script_separately(self):
        hist = script_histogram(f"{TELUGU} {HINDI}")
        assert hist["Telugu"] > 0
        assert hist["Devanagari"] > 0

    def test_returns_none_for_empty_input(self):
        assert dominant_script("") is None
        assert dominant_script(None) is None

    @pytest.mark.parametrize("text,expected", [
        ("தமிழ்", "Tamil"),
        ("ಕನ್ನಡ", "Kannada"),
        ("മലയാളം", "Malayalam"),
        ("বাংলা", "Bengali"),
        ("ગુજરાતી", "Gujarati"),
    ])
    def test_covers_the_other_indian_scripts(self, text, expected):
        assert dominant_script(text) == expected


class TestNamesText:
    def test_collects_verbatim_fields_only(self):
        ont = ontology(
            topics=[topic(TELUGU)],
            subtopics=[{"id": "ST_1_1_1", "name": HINDI, "summary": "English summary",
                        "skill_type": "reading_skill", "page_start": 1, "page_end": 1,
                        "topic_id": "T_1_1"}],
        )
        text = names_text(ont)
        assert TELUGU in text
        assert HINDI in text

    def test_excludes_summaries(self):
        """Summaries are written in English by instruction. Including them would
        drown out a transliterated NAME and hide the failure."""
        ont = ontology(topics=[{**topic(TELUGU), "summary": "A long English summary " * 20}])
        assert dominant_script(names_text(ont)) == "Telugu"

    def test_handles_an_empty_ontology(self):
        assert names_text({}) == ""
        assert names_text(ontology()) != ""  # chapter title still counts


# ── Coverage ──────────────────────────────────────────────────────────────────

class TestCoverage:
    def test_counts_each_entity_type(self):
        ont = ontology(
            topics=[topic("A"), topic("B", tid="T_1_2")],
            subtopics=[{"id": "ST_1_1_1"}],
            exercises=[{"id": "E_1_1_1"}, {"id": "E_1_1_2"}, {"id": "E_1_1_3"}],
        )
        c = coverage(ont)
        assert c == {"chapters": 1, "topics": 2, "subtopics": 1, "exercises": 3, "sidebars": 0}

    def test_handles_missing_keys(self):
        assert coverage({}) == {
            "chapters": 0, "topics": 0, "subtopics": 0, "exercises": 0, "sidebars": 0
        }

    def test_handles_none_values(self):
        assert coverage({"entities": {"topics": None}})["topics"] == 0


# ── Type validity ─────────────────────────────────────────────────────────────

class TestTypeValidity:
    def test_counts_valid_types(self):
        ont = ontology(
            subtopics=[{"skill_type": "reading_skill"}, {"skill_type": "writing_skill"}],
            exercises=[{"exercise_type": "matching_exercise"}],
        )
        v = type_validity(ont)
        assert v["subtopics"] == 2
        assert v["valid_skill_types"] == 2
        assert v["exercises"] == 1
        assert v["valid_exercise_types"] == 1

    def test_tracks_generic_types_separately(self):
        """A pile of generics means the model omitted types and the parser inferred
        them — valid output, worse contract-following. That distinction is the signal."""
        ont = ontology(
            subtopics=[{"skill_type": "general_skill"}, {"skill_type": "reading_skill"}],
            exercises=[{"exercise_type": "general_activity"}],
        )
        v = type_validity(ont)
        assert v["generic_skill_types"] == 1
        assert v["generic_exercise_types"] == 1
        assert v["valid_skill_types"] == 2  # general_skill IS valid, just uninformative

    def test_does_not_count_an_invalid_type_as_valid(self):
        ont = ontology(subtopics=[{"skill_type": "made_up_skill"}])
        v = type_validity(ont)
        assert v["subtopics"] == 1
        assert v["valid_skill_types"] == 0

    def test_handles_an_empty_ontology(self):
        v = type_validity({})
        assert v["subtopics"] == 0 and v["exercises"] == 0


# ── Topic alignment ───────────────────────────────────────────────────────────

class TestAlignTopics:
    def test_matches_identical_names(self):
        a = ontology(topics=[topic("Counting to Ten")])
        b = ontology(topics=[topic("Counting to Ten")])
        r = align_topics(a, b)
        assert len(r["matched"]) == 1
        assert r["matched"][0]["identical"] is True
        assert r["identical_count"] == 1
        assert r["only_in_standard"] == [] and r["only_in_simple"] == []

    def test_matches_a_near_miss_and_flags_it_as_not_identical(self):
        """A dropped word is the interesting failure — it must align (so it lands on
        the review sheet) but not count as identical."""
        a = ontology(topics=[topic("Counting to Twenty")])
        b = ontology(topics=[topic("Counting Twenty")])
        r = align_topics(a, b)
        assert len(r["matched"]) == 1
        assert r["matched"][0]["identical"] is False
        assert 0.7 < r["matched"][0]["similarity"] < 1.0

    def test_is_case_and_whitespace_insensitive_for_identity(self):
        a = ontology(topics=[topic("Counting  to Ten")])
        b = ontology(topics=[topic("counting to ten")])
        assert align_topics(a, b)["matched"][0]["identical"] is True

    def test_reports_topics_only_the_first_run_found(self):
        a = ontology(topics=[topic("Alpha"), topic("Beta", tid="T_1_2")])
        b = ontology(topics=[topic("Alpha")])
        r = align_topics(a, b)
        assert r["only_in_standard"] == ["Beta"]
        assert r["only_in_simple"] == []

    def test_reports_topics_only_the_second_run_found(self):
        a = ontology(topics=[topic("Alpha")])
        b = ontology(topics=[topic("Alpha"), topic("Invented", tid="T_1_2")])
        r = align_topics(a, b)
        assert r["only_in_simple"] == ["Invented"]

    def test_a_transliteration_does_not_align(self):
        # Shares no characters, so it correctly lands in only_in_* rather than
        # being silently reported as a match
        a = ontology(topics=[topic(TELUGU)])
        b = ontology(topics=[topic(LATIN)])
        r = align_topics(a, b)
        assert r["matched"] == []
        assert r["only_in_standard"] == [TELUGU]
        assert r["only_in_simple"] == [LATIN]

    def test_does_not_reuse_one_topic_for_two_matches(self):
        a = ontology(topics=[topic("Counting"), topic("Counting", tid="T_1_2")])
        b = ontology(topics=[topic("Counting")])
        r = align_topics(a, b)
        assert len(r["matched"]) == 1
        assert len(r["only_in_standard"]) == 1

    def test_handles_both_sides_empty(self):
        r = align_topics(ontology(), ontology())
        assert r["matched"] == [] and r["identical_count"] == 0

    def test_threshold_is_respected(self):
        a = ontology(topics=[topic("Fractions")])
        b = ontology(topics=[topic("Photosynthesis")])
        assert align_topics(a, b)["matched"] == []


# ── Page spans ────────────────────────────────────────────────────────────────

class TestPageSpans:
    def test_maps_normalised_name_to_span(self):
        ont = ontology(topics=[topic("Counting To Ten", ps=12, pe=14)])
        assert page_spans(ont) == {"counting to ten": (12, 14)}

    def test_handles_an_empty_ontology(self):
        assert page_spans({}) == {}


# ── Spread formatting ─────────────────────────────────────────────────────────

class TestSpread:
    def test_single_value(self):
        assert spread([5]) == "5"

    def test_multiple_values_show_mean_and_range(self):
        out = spread([2, 4])
        assert "3.0" in out and "min 2" in out and "max 4" in out

    def test_empty(self):
        assert spread([]) == "—"

    def test_ignores_none(self):
        assert spread([None, 7]) == "7"
