"""Unit tests for app.lib.canonical_mapping.resolve_canonical.

Uses a fake Supabase client rather than a real DB — this module's contract is
"given what's already in the library and what the model returns for an LLM
match call, does it resolve/create the right rows", which a fake table backed
by a plain dict can express exactly, with no network or fixtures involved.
"""

import pytest

import app.lib.canonical_mapping as cm


class FakeQuery:
    def __init__(self, table, op, **kwargs):
        self.table = table
        self.op = op
        self.kwargs = kwargs
        self._filters = []

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def ilike(self, col, val):
        self._filters.append(("ilike", col, val))
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.op == "select":
            rows = list(self.table.rows.values())
            for kind, col, val in self._filters:
                if kind == "ilike":
                    rows = [r for r in rows if r.get(col, "").lower() == val.lower()]
                elif kind == "eq":
                    rows = [r for r in rows if r.get(col) == val]
            return type("Res", (), {"data": rows})()
        if self.op == "insert":
            row = self.kwargs["row"]
            key = row[self.table.name_col].strip().lower()
            if key in self.table.by_name_lower:
                raise ValueError("duplicate key value violates unique constraint")
            self.table.rows[row["id"]] = row
            self.table.by_name_lower[key] = row
            return type("Res", (), {"data": [row]})()
        if self.op == "update":
            for kind, col, val in self._filters:
                if kind == "eq" and col == "id":
                    self.table.rows[val].update(self.kwargs["row"])
            return type("Res", (), {"data": []})()
        raise NotImplementedError(self.op)


class FakeTable:
    def __init__(self, name, name_col, seed=None):
        self.name = name
        self.name_col = name_col
        self.rows = {}
        self.by_name_lower = {}
        for row in seed or []:
            self.rows[row["id"]] = row
            self.by_name_lower[row[name_col].strip().lower()] = row

    def select(self, _cols):
        return FakeQuery(self, "select")

    def insert(self, row):
        return FakeQuery(self, "insert", row=row)

    def update(self, row):
        return FakeQuery(self, "update", row=row)


class FakeClient:
    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name):
        return self._tables[name]


def _client(seed_competencies=None):
    return FakeClient({
        "competencies": FakeTable("competencies", "name", seed_competencies),
        "concepts": FakeTable("concepts", "name"),
        "vocabulary": FakeTable("vocabulary", "term"),
        "contexts": FakeTable("contexts", "name"),
    })


class TestExactAndAliasMatch:
    def test_exact_case_insensitive_match_needs_no_llm_call(self, monkeypatch):
        ac = _client(seed_competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": []},
        ])
        monkeypatch.setattr(cm, "_llm_match", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not call the LLM for an exact match")
        ))
        result = cm.resolve_canonical(ac, "competencies", ["number recognition"])
        assert result == {"number recognition": "c1"}

    def test_known_alias_resolves_without_an_llm_call(self, monkeypatch):
        ac = _client(seed_competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": ["Recognize Numerals"]},
        ])
        monkeypatch.setattr(cm, "_llm_match", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not call the LLM for a known alias")
        ))
        result = cm.resolve_canonical(ac, "competencies", ["Recognize Numerals"])
        assert result == {"Recognize Numerals": "c1"}

    def test_duplicate_input_names_collapse_to_one_resolution(self):
        ac = _client(seed_competencies=[{"id": "c1", "name": "Count Objects", "aliases": []}])
        result = cm.resolve_canonical(ac, "competencies", ["Count Objects", "count objects"])
        assert result == {"Count Objects": "c1"}


class TestEmptyLibrary:
    def test_first_ever_extraction_creates_everything_without_calling_the_llm(self, monkeypatch):
        ac = _client()
        monkeypatch.setattr(cm, "_llm_match", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("an empty library has nothing to match against")
        ))
        result = cm.resolve_canonical(ac, "competencies", ["Count Objects", "Add Numbers"])
        assert set(result) == {"Count Objects", "Add Numbers"}
        assert len(ac.table("competencies").rows) == 2


class TestLLMMatch:
    def test_a_confident_match_reuses_the_existing_row_and_records_an_alias(self, monkeypatch):
        ac = _client(seed_competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": []},
        ])
        monkeypatch.setattr(cm, "_llm_match", lambda ac_, kind, name_col, unmatched, existing: {
            "Recognize Numerals": "c1",
        })
        result = cm.resolve_canonical(ac, "competencies", ["Recognize Numerals"])
        assert result == {"Recognize Numerals": "c1"}

    def test_the_real_llm_match_only_trusts_names_actually_in_the_library(self, monkeypatch):
        """A model that hallucinates a matched_existing value not in the passed-in
        library must not resolve to it — this is the actual _llm_match function,
        not a stub, so it exercises the guard directly."""
        pytest.importorskip("fitz")
        ac = _client(seed_competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": []},
        ])

        def fake_call_gemini(contents, tier="standard"):
            import json
            return json.dumps({"resolutions": [
                {"new_name": "Photosynthesis", "matched_existing": "Something Not In The Library"},
            ]})

        import app.lib.vision_extraction as ve
        monkeypatch.setattr(ve, "call_gemini", fake_call_gemini)

        existing = ac.table("competencies").select("id, name, aliases").execute().data
        resolved = cm._llm_match(ac, "competencies", "name", ["Photosynthesis"], existing)
        assert resolved == {}

    def test_a_genuine_match_via_the_real_llm_match_records_the_alias(self, monkeypatch):
        pytest.importorskip("fitz")
        ac = _client(seed_competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": []},
        ])

        def fake_call_gemini(contents, tier="standard"):
            import json
            return json.dumps({"resolutions": [
                {"new_name": "Recognize Numerals", "matched_existing": "Number Recognition"},
            ]})

        import app.lib.vision_extraction as ve
        monkeypatch.setattr(ve, "call_gemini", fake_call_gemini)

        existing = ac.table("competencies").select("id, name, aliases").execute().data
        resolved = cm._llm_match(ac, "competencies", "name", ["Recognize Numerals"], existing)
        assert resolved == {"Recognize Numerals": "c1"}
        assert ac.table("competencies").rows["c1"]["aliases"] == ["Recognize Numerals"]

    def test_llm_failure_leaves_everything_unmatched_rather_than_raising(self, monkeypatch):
        pytest.importorskip("fitz")
        ac = _client(seed_competencies=[{"id": "c1", "name": "Number Recognition", "aliases": []}])

        import app.lib.vision_extraction as ve

        def boom(*a, **k):
            raise RuntimeError("openrouter is down")

        monkeypatch.setattr(ve, "call_gemini", boom)
        existing = ac.table("competencies").select("id, name, aliases").execute().data
        resolved = cm._llm_match(ac, "competencies", "name", ["Something New"], existing)
        assert resolved == {}


class TestUnknownKind:
    def test_raises_on_an_unsupported_kind(self):
        ac = _client()
        try:
            cm.resolve_canonical(ac, "not_a_real_kind", ["x"])
            assert False, "expected ValueError"
        except ValueError:
            pass
