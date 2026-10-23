"""The one dispatch table that turns a confirmed Teacher Agent proposal into
a real write — mirrors app/lib/argus_confirm_dispatch.py exactly. This stays
the only path a proposed teacher-agent action can turn into an actual
effect, and it only runs because the teacher themself confirmed it.
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from .supabase_clients import create_admin_client
from .admin_queries import create_pending_leave_request
from .schemas import (
    TeacherMarkUpsertSchema, TeacherAttendanceUpsertSchema, TeacherInterventionUpsertSchema,
    TeacherTestUpsertSchema, TeacherPrepMaterialUpsertSchema,
)
from .teacher_context import get_teacher_context
from ..routes.teacher import (
    upsert_mark, upsert_attendance, answer_student_doubt, upsert_own_intervention,
    upsert_own_test, upsert_own_prep_material,
)
from ..routes.smart_lesson_routes import generate_smart_lesson


async def execute_confirmed_teacher_action(teacher_id: str, tool: str, args: dict) -> dict:
    if tool == "record_mark":
        return upsert_mark(TeacherMarkUpsertSchema(**args), teacher_id=teacher_id)
    if tool == "record_attendance":
        return upsert_attendance(TeacherAttendanceUpsertSchema(**args), teacher_id=teacher_id)
    if tool == "answer_doubt":
        return answer_student_doubt(id=args["id"], answer=args["answer"], teacher_id=teacher_id)
    if tool == "add_intervention_note":
        return upsert_own_intervention(TeacherInterventionUpsertSchema(**args), teacher_id=teacher_id)
    if tool == "request_leave":
        # Same write path post_leave itself uses (create_pending_leave_request),
        # just driven from here since post_leave's own signature needs a raw
        # Request object the agent loop doesn't have — see teacher_agent.py's
        # module docstring.
        ctx = get_teacher_context(teacher_id)
        if not ctx["schoolId"]:
            raise HTTPException(status_code=400, detail="Could not resolve this teacher's school.")
        ac = create_admin_client()
        start = date.fromisoformat(args["startDate"])
        end = date.fromisoformat(args.get("endDate") or args["startDate"])
        d = start
        while d <= end:
            create_pending_leave_request(ctx["schoolId"], teacher_id, d.isoformat(), args["reason"], args.get("note"), ac)
            d += timedelta(days=1)
        return {"ok": True, "startDate": args["startDate"], "endDate": args.get("endDate") or args["startDate"]}
    if tool == "create_test":
        return upsert_own_test(TeacherTestUpsertSchema(**args), teacher_id=teacher_id)
    if tool == "record_attendance_bulk":
        for record in args["records"]:
            upsert_attendance(TeacherAttendanceUpsertSchema(**record), teacher_id=teacher_id)
        return {"ok": True, "count": len(args["records"])}
    if tool == "record_marks_bulk":
        for record in args["records"]:
            upsert_mark(TeacherMarkUpsertSchema(**record), teacher_id=teacher_id)
        return {"ok": True, "count": len(args["records"])}
    if tool == "generate_prep_material":
        # The real, slow, costly pipeline — reused whole from smart_lesson_routes.py,
        # not re-derived. Saved the same way the frontend saves a generated lesson
        # (upsert_own_prep_material), so it's immediately visible in Prep Materials.
        import uuid
        result = await generate_smart_lesson(
            class_id=args["classId"], topic=args["topic"], subject=args["subject"],
            grade=args["grade"], teacher_id=teacher_id, subtopic=args.get("subtopic"),
        )
        upsert_own_prep_material(
            TeacherPrepMaterialUpsertSchema(
                id=str(uuid.uuid4()), classId=args["classId"], subject=args["subject"],
                grade=args["grade"], topic=args["topic"], subtopic=args.get("subtopic"),
                lesson=result["lesson"], createdAt=datetime.now(timezone.utc).isoformat(), source="personal",
            ),
            teacher_id=teacher_id,
        )
        return {"ok": True, "topic": args["topic"]}
    raise HTTPException(status_code=400, detail=f'"{tool}" is not a confirmable action.')
