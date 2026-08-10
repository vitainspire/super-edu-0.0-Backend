"""End-to-end test for persist_extraction's Phase B semantic-entity writes.

Does not re-test the pre-existing chapter/topic/subtopic/exercise/sidebar fan-
out (untouched by Phase B) — just verifies that an ontology carrying
concepts/competencies/vocabulary/learning_outcomes/contexts/bloom_level/
difficulty on its topics gets those written to the new tables correctly when
run through the real persist_extraction(), against a fake Supabase client.
"""

import app.lib.syllabus_persist as sp


class FakeQuery:
    def __init__(self, table, op, payload=None, on_conflict=None):
        self.table = table
        self.op = op
        self.payload = payload
        self.on_conflict = on_conflict
        self._filters = []
        self._order = None
        self._limit = None

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _filtered_rows(self):
        rows = list(self.table.rows.values())
        for kind, col, val in self._filters:
            if kind == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif kind == "in":
                rows = [r for r in rows if r.get(col) in val]
        return rows

    def execute(self):
        if self.op == "select":
            rows = self._filtered_rows()
            if self._order:
                col, desc = self._order
                rows = sorted(rows, key=lambda r: r.get(col) or 0, reverse=desc)
            if self._limit is not None:
                rows = rows[: self._limit]
            return type("Res", (), {"data": rows})()

        if self.op == "insert":
            for row in self.payload:
                self.table.rows[row["id"]] = row
            return type("Res", (), {"data": self.payload})()

        if self.op == "upsert":
            key_cols = (self.on_conflict or "id").split(",")
            for row in self.payload:
                dup_key = tuple(row.get(c) for c in key_cols)
                existing_id = next(
                    (rid for rid, r in self.table.rows.items()
                     if tuple(r.get(c) for c in key_cols) == dup_key),
                    None,
                )
                if existing_id:
                    self.table.rows[existing_id].update(row)
                else:
                    self.table.rows[row["id"]] = row
            return type("Res", (), {"data": self.payload})()

        raise NotImplementedError(self.op)


class FakeTable:
    def __init__(self, seed=None):
        self.rows = {r["id"]: r for r in (seed or [])}

    def select(self, _cols):
        return FakeQuery(self, "select")

    def insert(self, rows):
        return FakeQuery(self, "insert", payload=rows if isinstance(rows, list) else [rows])

    def upsert(self, rows, on_conflict=None):
        return FakeQuery(self, "upsert", payload=rows if isinstance(rows, list) else [rows], on_conflict=on_conflict)


class FakeAdminClient:
    """Auto-creates a FakeTable for any name — persist_extraction touches ~14
    tables and this test only cares about the Phase B ones' contents."""

    def __init__(self, classes_seed):
        self._tables = {"classes": FakeTable(classes_seed)}

    def table(self, name):
        if name not in self._tables:
            self._tables[name] = FakeTable()
        return self._tables[name]


def _ontology_with_phase_b_topic():
    return {
        "language": "English",
        "entities": {
            "chapters": [
                {"id": "C_1", "number": 1, "title": "Numbers Around Us", "page_start": 1, "page_end": 10},
            ],
            "topics": [
                {
                    "id": "T_1_1",
                    "name": "Counting to Ten",
                    "summary": "Students count objects up to ten.",
                    "chapter_id": "C_1",
                    "page_start": 1,
                    "page_end": 4,
                    "prerequisites": [],
                    "concepts": ["Addition", "Counting"],
                    "competencies": ["Count Objects", "Add Numbers"],
                    "vocabulary": ["More", "Total"],
                    "learning_outcomes": ["Count objects", "Add quantities"],
                    "contexts": ["Fruit", "Market"],
                    "bloom_level": "remember",
                    "difficulty": "easy",
                },
            ],
            "subtopics": [],
            "exercises": [],
            "sidebars": [],
        },
        "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
    }


class TestPersistPhaseB:
    def test_writes_canonical_entities_and_links_them_to_the_topic(self):
        # Empty library: resolve_canonical creates every name fresh, no LLM call.
        ac = FakeAdminClient(classes_seed=[{"id": "class-1", "school_id": "school-1", "grade": "1"}])
        ontology = _ontology_with_phase_b_topic()

        summary = sp.persist_extraction(
            ac, school_id="school-1", grade="1", subject="Maths",
            ontology=ontology, filename="book.pdf", exclude_topic_ids=set(),
        )

        assert summary["topics"] == 1
        assert summary["concepts"] == 2
        assert summary["competencies"] == 2
        assert summary["vocabulary"] == 2
        assert summary["contexts"] == 2
        assert summary["learningOutcomes"] == 2

        assert len(ac.table("concepts").rows) == 2
        assert {r["name"] for r in ac.table("concepts").rows.values()} == {"Addition", "Counting"}

        def_id = next(iter(ac.table("syllabus_topics").rows.values()))["definition_id"]
        topic_concept_rows = list(ac.table("topic_concepts").rows.values())
        assert all(r["topic_definition_id"] == def_id for r in topic_concept_rows)
        linked_concept_ids = {r["concept_id"] for r in topic_concept_rows}
        assert linked_concept_ids == set(ac.table("concepts").rows.keys())

        outcomes = {r["text"] for r in ac.table("topic_learning_outcomes").rows.values()}
        assert outcomes == {"Count objects", "Add quantities"}

    def test_bloom_level_and_difficulty_land_on_the_syllabus_topics_row(self):
        ac = FakeAdminClient(classes_seed=[{"id": "class-1", "school_id": "school-1", "grade": "1"}])
        sp.persist_extraction(
            ac, school_id="school-1", grade="1", subject="Maths",
            ontology=_ontology_with_phase_b_topic(), filename="book.pdf", exclude_topic_ids=set(),
        )
        row = next(iter(ac.table("syllabus_topics").rows.values()))
        assert row["bloom_level"] == "remember"
        assert row["difficulty"] == "easy"

    def test_a_topic_with_no_phase_b_fields_persists_without_error(self):
        """Older extractions (or a topic the model didn't tag) must not crash the
        whole persist call — Phase B fields are additive, not required."""
        ac = FakeAdminClient(classes_seed=[{"id": "class-1", "school_id": "school-1", "grade": "1"}])
        ontology = _ontology_with_phase_b_topic()
        for key in ("concepts", "competencies", "vocabulary", "learning_outcomes", "contexts", "bloom_level", "difficulty"):
            ontology["entities"]["topics"][0].pop(key, None)

        summary = sp.persist_extraction(
            ac, school_id="school-1", grade="1", subject="Maths",
            ontology=ontology, filename="book.pdf", exclude_topic_ids=set(),
        )
        assert summary["topics"] == 1
        assert summary["concepts"] == 0
        assert summary["learningOutcomes"] == 0
