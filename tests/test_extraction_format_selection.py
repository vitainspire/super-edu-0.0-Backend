"""Per-job extraction format and model tier.

The admin picks these on the upload form, so they travel as request data all the
way to the model call. Two things must hold: the choice is actually honoured
per job (not read from a module global at import time), and a client can never
push an unsupported value through into a response_format or a model id.
"""

import json

import pytest

pytest.importorskip("fitz", reason="vision_extraction imports PyMuPDF")

from app.lib import vision_extraction as ve
from app.lib import syllabus_pdf_jobs as jobs


CHAPTER_MD = """---
chapter: 1
title: Shapes
pages: 1-4
---

## Circles
---
pages: 1-2
---

Students find round things.

### Round objects `recognition_skill` p1
Look around the room.

#### Exercises
- `matching_exercise` p2 — Match the lid to the cup.
"""

CHAPTER_JSON = json.dumps({
    "entities": {
        "chapters": [{"id": "C_1", "number": 1, "title": "Shapes",
                      "page_start": 1, "page_end": 4}],
        "topics": [{"id": "T_1_1", "name": "Circles", "summary": "s", "chapter_id": "C_1",
                    "page_start": 1, "page_end": 2, "prerequisites": [], "subtopics": []}],
        "exercises": [], "sidebars": [],
    },
    "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
})


class FakeDoc:
    def __len__(self):
        return 20

    def close(self):
        pass


# ── Resolution ────────────────────────────────────────────────────────────────

class TestResolveFormat:
    @pytest.mark.parametrize("given,expected", [
        ("markdown", "markdown"),
        ("json", "json"),
        ("MARKDOWN", "markdown"),
        ("  json  ", "json"),
    ])
    def test_accepts_supported_values(self, given, expected):
        assert ve.resolve_extraction_format(given) == expected

    @pytest.mark.parametrize("given", [None, "", "   ", "yaml", "xml", "markdown; DROP TABLE"])
    def test_falls_back_for_anything_else(self, given):
        """Falls back rather than raising — an unrecognised value should degrade to
        the safe path, not fail an admin's upload halfway through."""
        assert ve.resolve_extraction_format(given) in ve.VALID_EXTRACTION_FORMATS

    def test_default_is_markdown(self):
        assert ve.resolve_extraction_format(None) == "markdown"


class TestResolveTier:
    @pytest.mark.parametrize("given,expected", [
        ("standard", "standard"), ("simple", "simple"), ("SIMPLE", "simple"),
    ])
    def test_accepts_supported_values(self, given, expected):
        assert ve.resolve_model_tier(given) == expected

    @pytest.mark.parametrize("given", [None, "", "cheap", "gpt-4", "../../etc/passwd"])
    def test_falls_back_for_anything_else(self, given):
        assert ve.resolve_model_tier(given) in ve.VALID_MODEL_TIERS

    def test_an_unsupported_tier_can_never_become_a_model_id(self):
        """The tier is the only thing that picks a model, so a bad tier must not
        reach model_for_tier as-is."""
        resolved = ve.resolve_model_tier("anthropic/claude-opus-4")
        assert ve.model_for_tier(resolved) in (ve.OPENROUTER_MODEL, ve.OPENROUTER_MODEL_SIMPLE)


# ── The parameter actually drives the call ────────────────────────────────────

@pytest.fixture
def spy(monkeypatch):
    calls: list = []

    def fake_call(contents, max_retries=6, base_delay=5,
                  response_format="json", tier="standard"):
        calls.append({"response_format": response_format, "tier": tier})
        return CHAPTER_MD if response_format == "markdown" else CHAPTER_JSON

    monkeypatch.setattr(ve, "call_gemini", fake_call)
    monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: object())
    return calls


def extract(**kw):
    return ve.extract_chapter_vision(
        FakeDoc(), [1, 2], 1, "Shapes", "English", "1. Shapes", **kw
    )


class TestPerCallOverride:
    def test_markdown_param_asks_for_markdown(self, spy, monkeypatch):
        # Global set the other way, to prove the parameter wins
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "json")
        extract(extraction_format="markdown")
        assert spy[0]["response_format"] == "markdown"

    def test_json_param_asks_for_json(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "markdown")
        extract(extraction_format="json")
        assert spy[0]["response_format"] == "json"

    def test_omitting_the_param_uses_the_configured_default(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "json")
        extract()
        assert spy[0]["response_format"] == "json"

    def test_tier_param_reaches_the_model_call(self, spy):
        extract(extraction_format="markdown", model_tier="simple")
        assert spy[0]["tier"] == "simple"

    def test_tier_param_overrides_the_global(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "EXTRACTION_MODEL_TIER", "simple")
        extract(extraction_format="markdown", model_tier="standard")
        assert spy[0]["tier"] == "standard"

    def test_a_bogus_format_degrades_instead_of_raising(self, spy):
        ont = extract(extraction_format="yaml")
        assert spy[0]["response_format"] in ("markdown", "json")
        assert ont["entities"]["topics"]

    def test_a_bogus_tier_degrades_instead_of_raising(self, spy):
        extract(extraction_format="markdown", model_tier="nonsense")
        assert spy[0]["tier"] in ve.VALID_MODEL_TIERS

    def test_both_formats_converge_after_merge(self, spy):
        """The two paths differ at the chunk level and that is by design: the JSON
        prompt asks for subtopics NESTED inside each topic, and _merge flattens
        them into entities.subtopics. The Markdown parser emits them already flat.

        So the contract worth asserting is post-_merge equivalence — that is the
        shape validate_and_fix and the persistence layer actually consume.
        """
        def merged(fmt):
            chunk = extract(extraction_format=fmt)
            full = {
                "entities": {"chapters": [], "topics": [], "subtopics": [],
                             "exercises": [], "sidebars": []},
                "graphs": {"chapter_structure": [], "exercise_mapping": [],
                           "concept_dependencies": []},
            }
            ve._merge(full, chunk)
            return full

        md = merged("markdown")
        spy.clear()
        js = merged("json")

        assert set(md["entities"]) == set(js["entities"])
        assert md["entities"]["topics"][0]["name"] == js["entities"]["topics"][0]["name"]
        # Neither leaves nested subtopics behind on a topic after merging
        for full in (md, js):
            assert all("subtopics" not in t for t in full["entities"]["topics"])

    def test_batched_passes_the_choice_down(self, spy, monkeypatch):
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        ve.extract_chapter_batched(
            FakeDoc(), [1, 2], 1, "Shapes", "English", "list",
            extraction_format="markdown", model_tier="simple",
        )
        assert spy[0] == {"response_format": "markdown", "tier": "simple"}


# ── End to end through generate_ontology_vision ───────────────────────────────

class TestGenerateRecordsTheChoice:
    @pytest.fixture
    def pipeline(self, monkeypatch, tmp_path):
        class Shim:
            @staticmethod
            def open(_p):
                return FakeDoc()

        monkeypatch.setattr(ve, "fitz", Shim)
        monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: object())
        monkeypatch.setattr(ve, "INTER_CALL_DELAY", 0)
        monkeypatch.setattr(ve, "detect_language_vision", lambda _p: "English")
        monkeypatch.setattr(ve, "detect_chapters_vision", lambda _p: [
            {"title": "Shapes", "start_page": 0, "end_page": 3},
        ])
        monkeypatch.setattr(ve, "_infer_cross_chapter_deps", lambda _o: [])

        seen: list = []

        def fake_call(contents, max_retries=6, base_delay=5,
                      response_format="json", tier="standard"):
            seen.append({"response_format": response_format, "tier": tier})
            return CHAPTER_MD if response_format == "markdown" else CHAPTER_JSON

        monkeypatch.setattr(ve, "call_gemini", fake_call)
        return seen, tmp_path

    def test_markdown_choice_is_honoured_and_recorded(self, pipeline):
        seen, tmp_path = pipeline
        ont, _ = ve.generate_ontology_vision(
            "b.pdf", output_dir=str(tmp_path), language="English",
            extraction_format="markdown", model_tier="standard",
        )
        assert ont["extraction_format"] == "markdown"
        assert ont["extraction_model_tier"] == "standard"
        assert ont["extraction_model"] == ve.OPENROUTER_MODEL
        # the chapter call used markdown (the TOC read is stubbed out here)
        assert any(c["response_format"] == "markdown" for c in seen)

    def test_json_choice_is_honoured_and_recorded(self, pipeline):
        seen, tmp_path = pipeline
        ont, _ = ve.generate_ontology_vision(
            "b.pdf", output_dir=str(tmp_path), language="English",
            extraction_format="json",
        )
        assert ont["extraction_format"] == "json"
        assert all(c["response_format"] == "json" for c in seen)

    def test_simple_tier_is_recorded_with_its_model(self, pipeline):
        seen, tmp_path = pipeline
        ont, _ = ve.generate_ontology_vision(
            "b.pdf", output_dir=str(tmp_path), language="English",
            extraction_format="markdown", model_tier="simple",
        )
        assert ont["extraction_model_tier"] == "simple"
        assert ont["extraction_model"] == ve.OPENROUTER_MODEL_SIMPLE

    def test_a_bogus_choice_still_completes(self, pipeline):
        seen, tmp_path = pipeline
        ont, _ = ve.generate_ontology_vision(
            "b.pdf", output_dir=str(tmp_path), language="English",
            extraction_format="toml", model_tier="free",
        )
        assert ont["extraction_format"] in ve.VALID_EXTRACTION_FORMATS
        assert ont["extraction_model_tier"] in ve.VALID_MODEL_TIERS
        assert ont["entities"]["topics"]


# ── The job record ────────────────────────────────────────────────────────────

class TestJobRecord:
    @pytest.fixture(autouse=True)
    def no_thread(self, monkeypatch):
        """start_extraction spawns a daemon thread; stub it so these tests only
        assert the job record it creates, without running an extraction."""
        class NoThread:
            def __init__(self, *a, **kw):
                self.args = kw.get("args")

            def start(self):
                pass

        monkeypatch.setattr(jobs.threading, "Thread", NoThread)

    def test_resolved_settings_are_on_the_job_immediately(self):
        job_id = jobs.start_extraction("x", "b.pdf", "auto",
                                       extraction_format="json", model_tier="simple")
        job = jobs.get_job(job_id)
        assert job["extractionFormat"] == "json"
        assert job["modelTier"] == "simple"
        assert job["model"] == ve.OPENROUTER_MODEL_SIMPLE
        assert job["status"] == "pending"

    def test_settings_are_visible_before_extraction_finishes(self):
        """The poller shows them while the job is still running, so they must be
        set at creation rather than written at the end."""
        job_id = jobs.start_extraction("x", "b.pdf", "auto", extraction_format="markdown")
        job = jobs.get_job(job_id)
        assert job["progress"] == 0 and job["topics"] is None
        assert job["extractionFormat"] == "markdown"

    def test_defaults_apply_when_nothing_is_chosen(self):
        job_id = jobs.start_extraction("x", "b.pdf", "auto")
        job = jobs.get_job(job_id)
        assert job["extractionFormat"] == ve.resolve_extraction_format(None)
        assert job["modelTier"] == ve.resolve_model_tier(None)

    def test_a_bogus_choice_is_sanitised_on_the_record(self):
        job_id = jobs.start_extraction("x", "b.pdf", "auto",
                                       extraction_format="../etc", model_tier="opus")
        job = jobs.get_job(job_id)
        assert job["extractionFormat"] in ve.VALID_EXTRACTION_FORMATS
        assert job["modelTier"] in ve.VALID_MODEL_TIERS

    def test_warnings_start_empty(self):
        job_id = jobs.start_extraction("x", "b.pdf", "auto")
        assert jobs.get_job(job_id)["warnings"] == []

    def test_the_resolved_settings_are_passed_to_the_worker(self, monkeypatch):
        captured: dict = {}

        class Capture:
            def __init__(self, *a, **kw):
                captured["args"] = kw.get("args")

            def start(self):
                pass

        monkeypatch.setattr(jobs.threading, "Thread", Capture)
        jobs.start_extraction("x", "b.pdf", "auto",
                              extraction_format="json", model_tier="simple")
        # (job_id, pdf, filename, language, fmt, tier)
        assert captured["args"][4] == "json"
        assert captured["args"][5] == "simple"


# ── The options endpoint the UI renders from ──────────────────────────────────

class TestOptionsEndpoint:
    @pytest.fixture
    def options(self):
        from app.routes.admin_syllabus_pdf import extract_syllabus_pdf_options
        return extract_syllabus_pdf_options("school-1", admin={"school_id": "school-1"})

    def test_lists_every_supported_format_and_tier(self, options):
        assert {f["value"] for f in options["formats"]} == set(ve.VALID_EXTRACTION_FORMATS)
        assert {t["value"] for t in options["tiers"]} == set(ve.VALID_MODEL_TIERS)

    def test_defaults_match_what_the_pipeline_would_choose(self, options):
        assert options["defaultFormat"] == ve.resolve_extraction_format(None)
        assert options["defaultTier"] == ve.resolve_model_tier(None)

    def test_every_choice_has_a_label_and_description(self, options):
        for choice in options["formats"] + options["tiers"]:
            assert choice["label"] and choice["description"]

    def test_tiers_name_the_actual_model(self, options):
        by_value = {t["value"]: t for t in options["tiers"]}
        assert by_value["standard"]["model"] == ve.OPENROUTER_MODEL
        assert by_value["simple"]["model"] == ve.OPENROUTER_MODEL_SIMPLE

    def test_the_cheap_tier_carries_a_quality_caveat(self, options):
        """It saves ~70% but can transliterate non-Latin scripts. An admin picking
        it from a dropdown has to be told that, not just the price."""
        simple = next(t for t in options["tiers"] if t["value"] == "simple")
        assert "transliterat" in simple["description"].lower()

    def test_is_json_serialisable(self, options):
        json.dumps(options)
