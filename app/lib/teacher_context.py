"""One clean teacher-scope resolver, replacing the pattern repeated across
`app/routes/teacher.py` (three separate auth-resolution paths — `require_teacher`,
`_soft_resolve_teacher`, `_strict_resolve_teacher` — none of which return
`schoolId` + `teacherId` + `classIds` together in one call). The Teacher
Agent needs all three on every turn, so this is the one place that computes
them.
"""
from .supabase_clients import create_admin_client


def get_teacher_context(teacher_id: str, ac=None) -> dict:
    """{"teacherId", "schoolId", "classIds"} — classIds is the same
    owned-classes UNION assigned-classes query `teacher.py`'s `_my_class_ids`
    already uses (kept duplicated here rather than importing that private
    helper, since this module is meant to be the one place other code reaches
    for this, not one more copy chained onto it)."""
    ac = ac or create_admin_client()

    owned = ac.table("classes").select("id, school_id").eq("teacher_id", teacher_id).execute().data or []
    assigned = ac.table("teacher_class_assignments").select("class_id").eq("teacher_id", teacher_id).execute().data or []
    assigned_ids = {a["class_id"] for a in assigned}

    class_ids = list({c["id"] for c in owned} | assigned_ids)

    school_id = owned[0]["school_id"] if owned else None
    if school_id is None and assigned_ids:
        rows = ac.table("classes").select("school_id").in_("id", list(assigned_ids)).limit(1).execute().data or []
        school_id = rows[0]["school_id"] if rows else None

    return {"teacherId": teacher_id, "schoolId": school_id, "classIds": class_ids}
