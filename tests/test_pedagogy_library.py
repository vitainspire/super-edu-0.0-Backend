"""Unit tests for app.lib.pedagogy_library — the Phase C Pedagogy Library:
Stage 2's get_recommended_activities() lookup, plus the admin authoring
functions app/routes/admin_pedagogy.py exposes.

Uses its own generic fake Supabase client (same shape as
test_canonical_mapping_admin.py's, kept separate per this test suite's
existing per-file convention).
"""

import pytest

import app.lib.pedagogy_library as pl


class Res:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table, op, payload=None):
        self.table = table
        self.op = op
        self.payload = payload
        self._filters = []

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def ilike(self, col, val):
        self._filters.append(("ilike", col, val))
        return self

    def limit(self, _n):
        return self

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "in" and row.get(col) not in val:
                return False
            if kind == "ilike" and row.get(col, "").lower() != val.lower():
                return False
        return True

    def execute(self):
        if self.op == "select":
            return Res([r for r in self.table.rows.values() if self._match(r)])
        if self.op == "insert":
            for row in self.payload:
                if row["id"] in self.table.rows:
                    raise ValueError("duplicate key")
                self.table.rows[row["id"]] = dict(row)
            return Res(list(self.payload))
        if self.op == "update":
            matched = [r for r in self.table.rows.values() if self._match(r)]
            for r in matched:
                r.update(self.payload)
            return Res(matched)
        if self.op == "delete":
            matched = [rid for rid, r in self.table.rows.items() if self._match(r)]
            for rid in matched:
                del self.table.rows[rid]
            return Res([])
        raise NotImplementedError(self.op)


class FakeTable:
    def __init__(self, seed=None):
        self.rows = {r["id"]: dict(r) for r in (seed or [])}

    def select(self, _cols):
        return FakeQuery(self, "select")

    def insert(self, rows):
        return FakeQuery(self, "insert", payload=rows if isinstance(rows, list) else [rows])

    def update(self, payload):
        return FakeQuery(self, "update", payload=payload)

    def delete(self):
        return FakeQuery(self, "delete")


class FakeClient:
    def __init__(self, **seeds):
        self._tables = {name: FakeTable(rows) for name, rows in seeds.items()}

    def table(self, name):
        if name not in self._tables:
            self._tables[name] = FakeTable()
        return self._tables[name]


def _seeded_client():
    """One template ("Count Real Objects") linked to competency c1 and
    contexts x1/x2; one unrelated template linked to a different competency."""
    return FakeClient(
        activity_templates=[
            {"id": "t1", "grade_band": "1-3", "category": "Count", "name": "Count Real Objects",
             "resource_level": 0, "fln_compatible": True},
            {"id": "t2", "grade_band": "4-5", "category": "Apply", "name": "Kirana Shop Budget",
             "resource_level": 1, "fln_compatible": False},
        ],
        activity_template_competencies=[
            {"id": "l1", "activity_template_id": "t1", "competency_id": "c1"},
            {"id": "l2", "activity_template_id": "t2", "competency_id": "c2"},
        ],
        activity_template_contexts=[
            {"id": "cl1", "activity_template_id": "t1", "context_id": "x1"},
            {"id": "cl2", "activity_template_id": "t1", "context_id": "x2"},
        ],
        contexts=[
            {"id": "x1", "name": "Mangoes", "category": "NATURE"},
            {"id": "x2", "name": "Pebbles", "category": "NATURE"},
        ],
        competencies=[
            {"id": "c1", "name": "Count Objects", "aliases": []},
            {"id": "c2", "name": "Simple Budgeting", "aliases": []},
        ],
    )


class TestGetRecommendedActivities:
    def test_returns_matching_template_with_its_full_context_list(self):
        ac = _seeded_client()
        result = pl.get_recommended_activities(ac, ["c1"])
        assert len(result) == 1
        assert result[0]["name"] == "Count Real Objects"
        assert result[0]["matchedCompetencyIds"] == ["c1"]
        assert {c["name"] for c in result[0]["contexts"]} == {"Mangoes", "Pebbles"}

    def test_empty_competency_list_returns_nothing(self):
        ac = _seeded_client()
        assert pl.get_recommended_activities(ac, []) == []

    def test_no_matching_competency_returns_nothing(self):
        ac = _seeded_client()
        assert pl.get_recommended_activities(ac, ["not-a-real-id"]) == []

    def test_grade_band_filters_out_a_matching_but_wrong_band_template(self):
        ac = _seeded_client()
        result = pl.get_recommended_activities(ac, ["c1", "c2"], grade_band="4-5")
        assert [r["name"] for r in result] == ["Kirana Shop Budget"]

    def test_resource_level_filters_out_templates_above_the_given_level(self):
        ac = _seeded_client()
        result = pl.get_recommended_activities(ac, ["c1", "c2"], resource_level=0)
        assert [r["name"] for r in result] == ["Count Real Objects"]


class TestCreateActivityTemplate:
    def test_rejects_an_invalid_grade_band(self):
        ac = FakeClient()
        with pytest.raises(ValueError):
            pl.create_activity_template(ac, "K-12", "Count", "Count Things")

    def test_rejects_missing_category_or_name(self):
        ac = FakeClient()
        with pytest.raises(ValueError):
            pl.create_activity_template(ac, "1-3", "", "Count Things")
        with pytest.raises(ValueError):
            pl.create_activity_template(ac, "1-3", "Count", "  ")

    def test_creates_with_sensible_defaults(self):
        ac = FakeClient()
        row = pl.create_activity_template(ac, "1-3", "Count", "Count Things")
        assert row["resource_level"] == 0
        assert row["fln_compatible"] is True
        assert ac.table("activity_templates").rows[row["id"]]["name"] == "Count Things"


class TestUpdateAndDeleteTemplate:
    def test_update_applies_only_known_fields(self):
        ac = _seeded_client()
        updated = pl.update_activity_template(ac, "t1", description="New desc", not_a_real_field="ignored")
        assert updated["description"] == "New desc"
        assert "not_a_real_field" not in ac.table("activity_templates").rows["t1"]

    def test_update_with_nothing_raises(self):
        ac = _seeded_client()
        with pytest.raises(ValueError):
            pl.update_activity_template(ac, "t1")

    def test_delete_removes_the_row(self):
        ac = _seeded_client()
        pl.delete_activity_template(ac, "t1")
        assert "t1" not in ac.table("activity_templates").rows


class TestLinking:
    def test_linking_the_same_pair_twice_does_not_raise(self):
        ac = _seeded_client()
        pl.link_competency(ac, "t1", "c1")  # already linked in the seed
        pl.link_competency(ac, "t1", "c1")  # again — must not raise

    def test_unlink_removes_only_the_matching_link(self):
        ac = _seeded_client()
        pl.unlink_context(ac, "t1", "x1")
        remaining = {r["context_id"] for r in ac.table("activity_template_contexts").rows.values()}
        assert remaining == {"x2"}


class TestGetOrCreate:
    def test_get_or_create_context_reuses_an_exact_case_insensitive_match(self):
        ac = _seeded_client()
        found_id = pl.get_or_create_context(ac, "mangoes")
        assert found_id == "x1"
        assert len(ac.table("contexts").rows) == 2  # nothing new created

    def test_get_or_create_context_creates_when_nothing_matches(self):
        ac = _seeded_client()
        new_id = pl.get_or_create_context(ac, "Kirana Shop", category="MARKET")
        assert new_id not in ("x1", "x2")
        assert ac.table("contexts").rows[new_id]["category"] == "MARKET"

    def test_get_or_create_competency_reuses_an_exact_match(self):
        ac = _seeded_client()
        found_id = pl.get_or_create_competency(ac, "count objects")
        assert found_id == "c1"
        assert len(ac.table("competencies").rows) == 2
