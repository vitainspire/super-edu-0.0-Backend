"""The Markdown extraction path, wired into vision_extraction.

Covers the switch itself rather than the parser (test_markdown_ontology.py does
that): the right prompt goes out, the right system prompt goes with it, id
offsets reach the parser instead of the prompt, batches don't collide, the raw
Markdown is archived, and setting EXTRACTION_FORMAT=json still works as a
rollback.

No network: call_gemini and render_page are both replaced.
"""

import json

import pytest

fitz = pytest.importorskip("fitz", reason="vision_extraction imports PyMuPDF")

from app.lib import vision_extraction as ve


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeDoc:
    """Only __len__ is used by the code under test."""

    def __init__(self, pages: int = 40):
        self._pages = pages

    def __len__(self):
        return self._pages

    def close(self):
        pass


class FakeImage:
    """Stands in for a PIL image.

    Deliberately NOT a str: call_gemini splits its payload on isinstance(item, str)
    to decide text part vs image part, so a string stand-in would be counted as
    prompt text and hide whether pages were sent at all.
    """

    def __init__(self, page: int):
        self.page = page

    def __repr__(self):
        return f"<FakeImage p{self.page}>"


CHAPTER_MD = """---
chapter: 2
title: Shapes Around Us
pages: 12-18
---

## Circles and Squares
---
pages: 12-14
---

Students identify circles and squares in everyday objects.

### Spotting circles `recognition_skill` p12
Find round objects in the classroom.

#### Exercises
- `matching_exercise` p13 — Match each object to its shape.

## Triangles
---
pages: 15-18
prerequisites: [Circles and Squares]
---

Students count the sides of a triangle.

#### Exercises
- `counting_activity` p16 — Count the sides and write the number.
"""

SECOND_BATCH_MD = """## Rectangles
---
pages: 19-21
---

Students compare rectangles to squares.

### Long and short sides `recognition_skill` p19
Compare the sides.

#### Exercises
- `matching_exercise` p20 — Circle the rectangles.
"""


@pytest.fixture
def spy(monkeypatch):
    """Replace the network call and page rendering; record what was sent."""
    calls: list = []

    def fake_call_gemini(contents, max_retries=6, base_delay=5,
                         response_format="json", tier="standard"):
        prompt = next((c for c in contents if isinstance(c, str)), "")
        calls.append({"prompt": prompt, "response_format": response_format,
                      "tier": tier, "model": ve.model_for_tier(tier),
                      "images": sum(1 for c in contents if not isinstance(c, str))})
        return spy.responses.pop(0) if spy.responses else CHAPTER_MD

    monkeypatch.setattr(ve, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: FakeImage(p))
    monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "markdown")
    spy.calls = calls
    spy.responses = []
    return spy


def extract(doc=None, pages=None, **kw):
    return ve.extract_chapter_vision(
        doc or FakeDoc(),
        pages if pages is not None else [11, 12, 13],
        kw.pop("chap_num", 2),
        kw.pop("chap_title", "Shapes Around Us"),
        kw.pop("language", "English"),
        kw.pop("global_chapter_list", "1. Numbers\n2. Shapes"),
        **kw,
    )


# ── The request that goes out ─────────────────────────────────────────────────

class TestRequest:
    def test_asks_for_markdown_not_json(self, spy):
        extract()
        assert spy.calls[0]["response_format"] == "markdown"

    def test_system_prompt_does_not_demand_json(self, spy):
        # Telling a model "valid JSON only" while asking for Markdown is the
        # failure this switch has to avoid.
        assert "JSON" not in ve._SYSTEM_PROMPTS["markdown"]
        assert "Markdown" in ve._SYSTEM_PROMPTS["markdown"]

    def test_prompt_contains_no_id_schema(self, spy):
        extract()
        prompt = spy.calls[0]["prompt"]
        assert "ID SCHEMA" not in prompt
        assert "ST_" not in prompt

    def test_prompt_carries_context_and_chapter_list(self, spy):
        extract()
        prompt = spy.calls[0]["prompt"]
        assert "Shapes Around Us" in prompt
        assert "1. Numbers" in prompt

    def test_sends_one_image_per_page(self, spy):
        extract(pages=[11, 12, 13, 14])
        assert spy.calls[0]["images"] == 4

    def test_skips_pages_past_the_end_of_the_document(self, spy):
        extract(doc=FakeDoc(pages=13), pages=[11, 12, 13, 14])
        assert spy.calls[0]["images"] == 2  # pages 13 and 14 are out of range


# ── The ontology that comes back ──────────────────────────────────────────────

class TestResult:
    def test_returns_the_canonical_entity_shape(self, spy):
        ont = extract()
        assert set(ont["entities"]) == {
            "chapters", "topics", "subtopics", "exercises", "sidebars"
        }
        assert set(ont["graphs"]) == {
            "chapter_structure", "exercise_mapping", "concept_dependencies"
        }

    def test_extracts_the_topics(self, spy):
        ont = extract()
        assert [t["name"] for t in ont["entities"]["topics"]] == [
            "Circles and Squares", "Triangles"
        ]

    def test_ids_embed_the_chapter_number(self, spy):
        ont = extract(chap_num=2)
        assert [t["id"] for t in ont["entities"]["topics"]] == ["T_2_1", "T_2_2"]
        assert ont["entities"]["chapters"][0]["id"] == "C_2"

    def test_subtopics_are_flat_with_topic_id(self, spy):
        ont = extract()
        subs = ont["entities"]["subtopics"]
        assert subs and all("topic_id" in s for s in subs)
        assert all("subtopics" not in t for t in ont["entities"]["topics"])

    def test_prerequisites_resolved_to_ids(self, spy):
        ont = extract()
        assert ont["entities"]["topics"][1]["prerequisites"] == ["T_2_1"]

    def test_result_is_json_serialisable(self, spy):
        json.dumps(extract())

    def test_raises_when_nothing_is_recoverable(self, spy):
        # Must raise so _process_chunk falls through to the simplified tier
        spy.responses = ["I could not read these pages."]
        with pytest.raises(ValueError, match="no topics"):
            extract()


# ── Offsets go to the parser, not the prompt ──────────────────────────────────

class TestOffsets:
    def test_offsets_are_not_mentioned_in_the_prompt(self, spy):
        extract(topic_start=7, exercise_start=20, subtopic_start=15)
        prompt = spy.calls[0]["prompt"]
        assert "T_2_7" not in prompt
        assert "start at 7" not in prompt.lower()

    def test_offsets_shift_the_assigned_ids(self, spy):
        ont = extract(topic_start=7)
        assert [t["id"] for t in ont["entities"]["topics"]] == ["T_2_7", "T_2_8"]

    def test_two_batches_produce_disjoint_ids(self, spy):
        first = extract()
        counts = {
            "topics": len(first["entities"]["topics"]),
            "subtopics": len(first["entities"]["subtopics"]),
            "exercises": len(first["entities"]["exercises"]),
        }
        spy.responses = [SECOND_BATCH_MD]
        second = extract(
            topic_start=counts["topics"] + 1,
            subtopic_start=counts["subtopics"] + 1,
            exercise_start=counts["exercises"] + 1,
        )

        def ids(o):
            return {x["id"] for k in ("topics", "subtopics", "exercises")
                    for x in o["entities"][k]}

        assert ids(first).isdisjoint(ids(second))


# ── Sinks ─────────────────────────────────────────────────────────────────────

class TestSinks:
    def test_markdown_sink_captures_the_raw_response(self, spy):
        sink: list = []
        extract(markdown_sink=sink)
        assert sink == [CHAPTER_MD]

    def test_warnings_sink_captures_parser_warnings(self, spy):
        spy.responses = ["## Alpha\n---\nprerequisites: [Nope]\n---\n\nText.\n"]
        sink: list = []
        extract(warnings_sink=sink)
        assert any("Nope" in w for w in sink)
        assert all(w.startswith("Chapter 2:") for w in sink)

    def test_no_warnings_on_clean_input(self, spy):
        sink: list = []
        extract(warnings_sink=sink)
        assert sink == []

    def test_sinks_are_optional(self, spy):
        extract()  # must not raise without sinks


# ── Batching ──────────────────────────────────────────────────────────────────

class TestBatched:
    def test_a_small_chapter_makes_one_call(self, spy):
        ve.extract_chapter_batched(
            FakeDoc(), [11, 12, 13], 2, "Shapes", "English", "list",
        )
        assert len(spy.calls) == 1

    def test_a_large_chapter_is_split_and_merged(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        pages = list(range(11, 11 + ve.PAGE_BATCH_SIZE * 2))
        spy.responses = [CHAPTER_MD, SECOND_BATCH_MD]

        merged = ve.extract_chapter_batched(
            FakeDoc(), pages, 2, "Shapes", "English", "list",
        )

        assert len(spy.calls) == 2
        names = [t["name"] for t in merged["entities"]["topics"]]
        assert names == ["Circles and Squares", "Triangles", "Rectangles"]

    def test_batched_ids_do_not_collide(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        pages = list(range(11, 11 + ve.PAGE_BATCH_SIZE * 2))
        spy.responses = [CHAPTER_MD, SECOND_BATCH_MD]

        merged = ve.extract_chapter_batched(
            FakeDoc(), pages, 2, "Shapes", "English", "list",
        )
        e = merged["entities"]
        ids = [x["id"] for k in ("topics", "subtopics", "exercises") for x in e[k]]
        assert len(ids) == len(set(ids))

    def test_a_failed_batch_does_not_lose_the_others(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        pages = list(range(11, 11 + ve.PAGE_BATCH_SIZE * 2))
        spy.responses = [CHAPTER_MD, "unreadable"]  # second batch yields nothing

        merged = ve.extract_chapter_batched(
            FakeDoc(), pages, 2, "Shapes", "English", "list",
        )
        assert [t["name"] for t in merged["entities"]["topics"]] == [
            "Circles and Squares", "Triangles"
        ]

    def test_second_batch_is_told_what_was_already_extracted(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        pages = list(range(11, 11 + ve.PAGE_BATCH_SIZE * 2))
        spy.responses = [CHAPTER_MD, SECOND_BATCH_MD]

        ve.extract_chapter_batched(FakeDoc(), pages, 2, "Shapes", "English", "list")

        assert "ALREADY EXTRACTED" not in spy.calls[0]["prompt"]
        assert "ALREADY EXTRACTED" in spy.calls[1]["prompt"]
        assert "Circles and Squares" in spy.calls[1]["prompt"]


# ── Markdown archive ──────────────────────────────────────────────────────────

class TestArchive:
    def test_writes_one_file_per_chapter(self, tmp_path):
        ve._persist_chapter_markdown(tmp_path, 3, "Shapes", [CHAPTER_MD])
        written = tmp_path / "markdown" / "chapter_03.md"
        assert written.exists()
        body = written.read_text(encoding="utf-8")
        assert "Circles and Squares" in body
        assert "chapter 3: Shapes" in body

    def test_concatenates_batches_into_one_file(self, tmp_path):
        ve._persist_chapter_markdown(tmp_path, 1, "Shapes", [CHAPTER_MD, SECOND_BATCH_MD])
        body = (tmp_path / "markdown" / "chapter_01.md").read_text(encoding="utf-8")
        assert "Triangles" in body and "Rectangles" in body

    def test_archived_markdown_is_reparseable(self, tmp_path):
        """The whole point: a later schema change reads these files instead of the PDF."""
        from app.lib.markdown_ontology import parse_markdown_ontology

        ve._persist_chapter_markdown(tmp_path, 2, "Shapes", [CHAPTER_MD])
        body = (tmp_path / "markdown" / "chapter_02.md").read_text(encoding="utf-8")
        again = parse_markdown_ontology(body, chapter_number=2)
        assert [t["name"] for t in again.ontology["entities"]["topics"]] == [
            "Circles and Squares", "Triangles"
        ]

    def test_writes_nothing_when_there_is_nothing(self, tmp_path):
        ve._persist_chapter_markdown(tmp_path, 1, "Shapes", [])
        assert not (tmp_path / "markdown").exists()

    def test_is_best_effort_and_never_raises(self, tmp_path):
        # A file where the directory should go — must not lose a good extraction
        clash = tmp_path / "markdown"
        clash.write_text("not a directory", encoding="utf-8")
        ve._persist_chapter_markdown(tmp_path, 1, "Shapes", [CHAPTER_MD])


class TestArchiveSurvivesCleanup:
    """syllabus_pdf_jobs rmtree's the work dir when extraction ends, so the archive
    has to leave on the ontology or it is destroyed immediately."""

    def test_collects_every_chapter_keyed_by_number(self, tmp_path):
        ve._persist_chapter_markdown(tmp_path, 1, "One", [CHAPTER_MD])
        ve._persist_chapter_markdown(tmp_path, 12, "Twelve", [SECOND_BATCH_MD])

        collected = ve._collect_chapter_markdown(tmp_path)
        assert set(collected) == {"1", "12"}
        assert "Circles and Squares" in collected["1"]
        assert "Rectangles" in collected["12"]

    def test_collected_markdown_is_json_serialisable(self, tmp_path):
        ve._persist_chapter_markdown(tmp_path, 1, "One", [CHAPTER_MD])
        json.dumps(ve._collect_chapter_markdown(tmp_path))

    def test_collected_markdown_is_reparseable_after_the_work_dir_is_gone(self, tmp_path):
        import shutil

        from app.lib.markdown_ontology import parse_markdown_ontology

        ve._persist_chapter_markdown(tmp_path, 2, "Shapes", [CHAPTER_MD])
        collected = ve._collect_chapter_markdown(tmp_path)
        shutil.rmtree(tmp_path)  # what the job does in its finally block

        again = parse_markdown_ontology(collected["2"], chapter_number=2)
        assert [t["name"] for t in again.ontology["entities"]["topics"]] == [
            "Circles and Squares", "Triangles"
        ]

    def test_returns_empty_when_no_archive_exists(self, tmp_path):
        assert ve._collect_chapter_markdown(tmp_path) == {}

    def test_ignores_unparseable_filenames(self, tmp_path):
        md_dir = tmp_path / "markdown"
        md_dir.mkdir()
        (md_dir / "chapter_notanumber.md").write_text("junk", encoding="utf-8")
        (md_dir / "chapter_04.md").write_text(CHAPTER_MD, encoding="utf-8")
        assert set(ve._collect_chapter_markdown(tmp_path)) == {"4"}

    def test_never_raises_on_a_hostile_directory(self, tmp_path):
        (tmp_path / "markdown").write_text("not a directory", encoding="utf-8")
        assert ve._collect_chapter_markdown(tmp_path) == {}


# ── Rollback ──────────────────────────────────────────────────────────────────

class TestJsonRollback:
    def test_json_format_still_uses_the_json_path(self, monkeypatch):
        calls: list = []

        def fake_call(contents, max_retries=6, base_delay=5,
                      response_format="json", tier="standard"):
            calls.append(response_format)
            return json.dumps({
                "entities": {
                    "chapters": [{"id": "C_2", "number": 2, "title": "Shapes",
                                  "page_start": 12, "page_end": 18}],
                    "topics": [{"id": "T_2_1", "name": "Circles", "summary": "s",
                                "chapter_id": "C_2", "page_start": 12, "page_end": 14,
                                "prerequisites": [], "subtopics": []}],
                    "exercises": [], "sidebars": [],
                },
                "graphs": {"chapter_structure": [], "exercise_mapping": [],
                           "concept_dependencies": []},
            })

        monkeypatch.setattr(ve, "call_gemini", fake_call)
        monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: FakeImage(p))
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "json")

        ont = extract()
        assert calls == ["json"]
        assert ont["entities"]["topics"][0]["name"] == "Circles"

    def test_the_json_prompt_is_still_intact(self):
        prompt = ve._chapter_prompt(2, "English", "ctx", "list")
        assert "strict JSON" in prompt
        assert "ID SCHEMA" in prompt


# ── The retry ladder ──────────────────────────────────────────────────────────

class TestFallbackTier:
    def test_markdown_simplified_prompt_asks_for_markdown(self):
        prompt = ve._simplified_markdown_prompt(2, "Telugu", "ctx")
        assert "Markdown" in prompt
        assert "JSON" not in prompt
        assert "Telugu" in prompt

    def test_markdown_simplified_prompt_parses(self):
        from app.lib.markdown_ontology import parse_markdown_ontology

        prompt = ve._simplified_markdown_prompt(2, "English", "ctx")
        example = prompt[prompt.index("## <topic name>"):]
        # The template itself must be something the parser accepts
        assert parse_markdown_ontology(example, chapter_number=2).ok


# ── Merge compatibility ───────────────────────────────────────────────────────

def test_merge_accepts_the_parsers_output(spy):
    """_merge is shared with the JSON path and must take the parser's shape as-is."""
    chunk = extract()
    full = {
        "entities": {"chapters": [], "topics": [], "subtopics": [], "exercises": [], "sidebars": []},
        "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
    }
    ve._merge(full, chunk)

    assert len(full["entities"]["topics"]) == 2
    assert len(full["entities"]["subtopics"]) == len(chunk["entities"]["subtopics"])
    assert all(s.get("topic_id") for s in full["entities"]["subtopics"])


def test_merge_is_idempotent_on_the_same_chunk(spy):
    chunk = extract()
    full = {
        "entities": {"chapters": [], "topics": [], "subtopics": [], "exercises": [], "sidebars": []},
        "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
    }
    ve._merge(full, chunk)
    ve._merge(full, json.loads(json.dumps(chunk)))
    assert len(full["entities"]["topics"]) == 2  # deduplicated by id


# ── End to end through generate_ontology_vision ───────────────────────────────

class TestEndToEnd:
    """Drives the real entry point with every external call replaced, so the
    switch is verified through the actual code path the admin flow uses."""

    @pytest.fixture
    def pipeline(self, monkeypatch, tmp_path):
        class Shim:
            @staticmethod
            def open(_path):
                return FakeDoc(pages=24)

        monkeypatch.setattr(ve, "fitz", Shim)
        monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: FakeImage(p))
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "markdown")
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        monkeypatch.setattr(ve, "detect_language_vision", lambda _p: "English")
        monkeypatch.setattr(ve, "detect_chapters_vision", lambda _p: [
            {"title": "Shapes Around Us", "start_page": 11, "end_page": 17},
        ])
        monkeypatch.setattr(ve, "_infer_cross_chapter_deps", lambda _o: [])

        sent: list = []

        def fake_call(contents, max_retries=6, base_delay=5,
                      response_format="json", tier="standard"):
            sent.append(response_format)
            return CHAPTER_MD

        monkeypatch.setattr(ve, "call_gemini", fake_call)
        return sent, tmp_path

    def test_produces_a_validated_ontology(self, pipeline):
        sent, tmp_path = pipeline
        ontology, job_dir = ve.generate_ontology_vision(
            "book.pdf", output_dir=str(tmp_path), language="auto",
        )
        assert [t["name"] for t in ontology["entities"]["topics"]] == [
            "Circles and Squares", "Triangles"
        ]
        # validate_and_fix ran: chapters get a status and renumbered position
        assert ontology["entities"]["chapters"][0]["status"] in (
            "verified", "partial", "unverified"
        )

    def test_only_ever_asked_for_markdown(self, pipeline):
        sent, tmp_path = pipeline
        ve.generate_ontology_vision("book.pdf", output_dir=str(tmp_path), language="auto")
        assert set(sent) == {"markdown"}

    def test_records_the_format_it_used(self, pipeline):
        sent, tmp_path = pipeline
        ontology, _ = ve.generate_ontology_vision(
            "book.pdf", output_dir=str(tmp_path), language="auto",
        )
        assert ontology["extraction_format"] == "markdown"

    def test_carries_the_markdown_archive_on_the_ontology(self, pipeline):
        sent, tmp_path = pipeline
        ontology, _ = ve.generate_ontology_vision(
            "book.pdf", output_dir=str(tmp_path), language="auto",
        )
        archive = ontology["chapter_markdown"]
        assert "Circles and Squares" in archive["1"]

    def test_the_result_feeds_the_syllabus_review_panel(self, pipeline):
        from app.lib.syllabus_pdf_jobs import _map_ontology_to_topics

        sent, tmp_path = pipeline
        ontology, _ = ve.generate_ontology_vision(
            "book.pdf", output_dir=str(tmp_path), language="auto",
        )
        rows = _map_ontology_to_topics(ontology)
        assert [r["topic"] for r in rows] == ["Circles and Squares", "Triangles"]
        assert rows[0]["subTopics"] == ["Spotting circles"]

    def test_writes_ontology_json(self, pipeline):
        sent, tmp_path = pipeline
        _, job_dir = ve.generate_ontology_vision(
            "book.pdf", output_dir=str(tmp_path), language="auto",
        )
        saved = json.loads((job_dir / "ontology.json").read_text(encoding="utf-8"))
        assert saved["extraction_format"] == "markdown"
