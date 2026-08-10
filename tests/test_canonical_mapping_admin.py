"""Unit tests for the admin review/management side of canonical_mapping.py
(list_canonical, get_canonical_topics, rename_canonical, merge_canonical,
delete_canonical, split_canonical) — the tools app/routes/admin_canonical.py
exposes for fixing what resolve_canonical() got wrong on its own.

Uses its own generic fake Supabase client (not the name_col-keyed one in
test_canonical_mapping.py, which doesn't model junction tables or deletes).
"""

import pytest

import app.lib.canonical_mapping as cm


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

    def _match(self, row):
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "in" and row.get(col) not in val:
                return False
        return True

    def execute(self):
        if self.op == "select":
            return Res([r for r in self.table.rows.values() if self._match(r)])
        if self.op == "insert":
            for row in self.payload:
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


class TestListCanonical:
    def test_reports_usage_count_from_the_junction_table(self):
        ac = FakeClient(
            competencies=[
                {"id": "c1", "name": "Count Objects", "aliases": [], "created_at": "t"},
                {"id": "c2", "name": "Add Numbers", "aliases": [], "created_at": "t"},
            ],
            topic_competencies=[
                {"id": "l1", "topic_definition_id": "d1", "competency_id": "c1", "source_name": "Count Objects"},
                {"id": "l2", "topic_definition_id": "d2", "competency_id": "c1", "source_name": "Count Objects"},
            ],
        )
        entries = {e["id"]: e for e in cm.list_canonical(ac, "competencies")}
        assert entries["c1"]["usageCount"] == 2
        assert entries["c2"]["usageCount"] == 0

    def test_search_matches_name_or_alias_case_insensitively(self):
        ac = FakeClient(competencies=[
            {"id": "c1", "name": "Number Recognition", "aliases": ["Recognize Numerals"], "created_at": "t"},
            {"id": "c2", "name": "Add Numbers", "aliases": [], "created_at": "t"},
        ])
        by_name = cm.list_canonical(ac, "competencies", search="numerals")
        assert [e["id"] for e in by_name] == ["c1"]


class TestGetCanonicalTopics:
    def test_collapses_fanned_out_sections_to_one_row_per_topic(self):
        ac = FakeClient(
            topic_competencies=[
                {"id": "l1", "topic_definition_id": "d1", "competency_id": "c1", "source_name": "Count Objects"},
            ],
            syllabus_topics=[
                {"id": "row-a", "definition_id": "d1", "topic": "Counting to Ten", "grade": "1", "subject": "Maths", "school_id": "s1"},
                {"id": "row-b", "definition_id": "d1", "topic": "Counting to Ten", "grade": "1", "subject": "Maths", "school_id": "s1"},
            ],
        )
        topics = cm.get_canonical_topics(ac, "competencies", "c1")
        assert len(topics) == 1
        assert topics[0]["topic"] == "Counting to Ten"
        assert topics[0]["sourceName"] == "Count Objects"


class TestRenameCanonical:
    def test_renames_without_touching_aliases(self):
        ac = FakeClient(competencies=[
            {"id": "c1", "name": "Old Name", "aliases": ["Some Alias"], "created_at": "t"},
        ])
        result = cm.rename_canonical(ac, "competencies", "c1", "New Name")
        assert result["name"] == "New Name"
        assert ac.table("competencies").rows["c1"]["aliases"] == ["Some Alias"]

    def test_rejects_an_empty_name(self):
        ac = FakeClient(competencies=[{"id": "c1", "name": "X", "aliases": [], "created_at": "t"}])
        with pytest.raises(ValueError):
            cm.rename_canonical(ac, "competencies", "c1", "   ")


class TestMergeCanonical:
    def test_moves_links_and_aliases_and_deletes_the_merged_row(self):
        ac = FakeClient(
            competencies=[
                {"id": "keep", "name": "Number Recognition", "aliases": [], "created_at": "t"},
                {"id": "dupe", "name": "Recognize Digits", "aliases": ["Digit Recognition"], "created_at": "t"},
            ],
            topic_competencies=[
                {"id": "l1", "topic_definition_id": "d1", "competency_id": "dupe", "source_name": "Recognize Digits"},
            ],
        )
        result = cm.merge_canonical(ac, "competencies", "keep", ["dupe"])

        assert result["mergedCount"] == 1
        assert set(result["aliases"]) == {"Recognize Digits", "Digit Recognition"}
        assert "dupe" not in ac.table("competencies").rows
        link = ac.table("topic_competencies").rows["l1"]
        assert link["competency_id"] == "keep"

    def test_drops_a_duplicate_link_instead_of_keeping_both(self):
        """A topic already linked to the keeper must not end up double-linked
        after the merge — the unique (topic_definition_id, competency_id)
        constraint in the real DB would reject that anyway."""
        ac = FakeClient(
            competencies=[
                {"id": "keep", "name": "Number Recognition", "aliases": [], "created_at": "t"},
                {"id": "dupe", "name": "Recognize Digits", "aliases": [], "created_at": "t"},
            ],
            topic_competencies=[
                {"id": "l1", "topic_definition_id": "d1", "competency_id": "keep", "source_name": "Number Recognition"},
                {"id": "l2", "topic_definition_id": "d1", "competency_id": "dupe", "source_name": "Recognize Digits"},
            ],
        )
        cm.merge_canonical(ac, "competencies", "keep", ["dupe"])
        remaining = list(ac.table("topic_competencies").rows.values())
        assert len(remaining) == 1
        assert remaining[0]["id"] == "l1"

    def test_rejects_merging_nothing(self):
        ac = FakeClient(competencies=[{"id": "keep", "name": "X", "aliases": [], "created_at": "t"}])
        with pytest.raises(ValueError):
            cm.merge_canonical(ac, "competencies", "keep", [])
        with pytest.raises(ValueError):
            cm.merge_canonical(ac, "competencies", "keep", ["keep"])  # merging into itself


class TestDeleteCanonical:
    def test_refuses_to_delete_an_in_use_entry_without_force(self):
        ac = FakeClient(
            competencies=[{"id": "c1", "name": "X", "aliases": [], "created_at": "t"}],
            topic_competencies=[{"id": "l1", "topic_definition_id": "d1", "competency_id": "c1", "source_name": "X"}],
        )
        with pytest.raises(ValueError):
            cm.delete_canonical(ac, "competencies", "c1")
        assert "c1" in ac.table("competencies").rows

    def test_force_deletes_an_in_use_entry(self):
        ac = FakeClient(
            competencies=[{"id": "c1", "name": "X", "aliases": [], "created_at": "t"}],
            topic_competencies=[{"id": "l1", "topic_definition_id": "d1", "competency_id": "c1", "source_name": "X"}],
        )
        result = cm.delete_canonical(ac, "competencies", "c1", force=True)
        assert result["deletedLinks"] == 1
        assert "c1" not in ac.table("competencies").rows

    def test_deletes_an_unused_entry_without_needing_force(self):
        ac = FakeClient(competencies=[{"id": "c1", "name": "X", "aliases": [], "created_at": "t"}])
        cm.delete_canonical(ac, "competencies", "c1")
        assert "c1" not in ac.table("competencies").rows


class TestSplitCanonical:
    def test_moves_only_the_links_matching_the_given_source_name(self):
        ac = FakeClient(
            competencies=[{"id": "merged", "name": "Number Recognition", "aliases": ["Recognize Numerals"], "created_at": "t"}],
            topic_competencies=[
                {"id": "l1", "topic_definition_id": "d1", "competency_id": "merged", "source_name": "Number Recognition"},
                {"id": "l2", "topic_definition_id": "d2", "competency_id": "merged", "source_name": "Recognize Numerals"},
            ],
        )
        result = cm.split_canonical(ac, "competencies", "merged", "Recognize Numerals")

        assert result["movedLinks"] == 1
        new_id = result["newId"]
        assert ac.table("competencies").rows[new_id]["name"] == "Recognize Numerals"
        assert ac.table("topic_competencies").rows["l2"]["competency_id"] == new_id
        assert ac.table("topic_competencies").rows["l1"]["competency_id"] == "merged"
        assert "Recognize Numerals" not in ac.table("competencies").rows["merged"]["aliases"]

    def test_raises_when_no_link_has_that_source_name(self):
        ac = FakeClient(
            competencies=[{"id": "c1", "name": "X", "aliases": [], "created_at": "t"}],
            topic_competencies=[{"id": "l1", "topic_definition_id": "d1", "competency_id": "c1", "source_name": "X"}],
        )
        with pytest.raises(ValueError):
            cm.split_canonical(ac, "competencies", "c1", "Something Never Extracted")
