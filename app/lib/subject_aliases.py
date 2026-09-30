"""Subjects that are the same course under a different name at some schools.

EVS (Environmental Studies) is taught as "Science" at primary grades in many
schools, and both spellings show up in real data for the same actual course
-- confirmed against this app's own data: one school has shared material
saved under both "science" and "Environmental Studies" for Grade 5, from two
different generation runs that each used whichever name was on hand at the
time. Without this, a teacher whose class record says "science" would never
find material saved under "Environmental Studies", and vice versa -- two
names for one subject, silently never matching.

Widen a subject match to its whole alias group wherever shared prep material
is looked up by subject; nowhere else (a teacher's own subject label, the
syllabus, an exam plan -- those stay exactly what the school entered).

TWO INDEPENDENT VOCABULARIES, NOT ONE. A school's own grade_subjects rows
hold whatever text an admin typed when setting up that grade ("mathematics",
"environmental science"); shared_prep_materials.subject holds whatever the
textbook catalog's own metadata called that subject when the book was
ingested ("Maths", "Environmental Studies"). Nothing keeps these in sync --
confirmed for real on this exact pilot school: Grade 3's admin-entered
"mathematics" never matched the catalog's "Maths", and Grade 5's
"environmental science" didn't even match the EXISTING science/EVS group
above ("environmental science" was never one of its three exact strings) --
so BOTH grades silently showed "nothing shared" despite real, already-
generated material sitting right there under a different spelling of the
same subject.
"""

SUBJECT_ALIAS_GROUPS: list[set[str]] = [
    {"science", "evs", "environmental studies", "environmental science"},
    {"maths", "math", "mathematics"},
]


def subject_matches(row_subject: str | None, query_subject: str) -> bool:
    """True if row_subject is the same subject as query_subject -- an exact
    case-insensitive match, or both belong to the same alias group above."""
    if not row_subject:
        return False
    a, b = row_subject.strip().lower(), query_subject.strip().lower()
    if a == b:
        return True
    return any(a in group and b in group for group in SUBJECT_ALIAS_GROUPS)
