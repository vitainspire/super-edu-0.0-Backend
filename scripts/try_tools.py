"""Calls Argus's tools directly — no LLM involved. Proves the data layer and
the matching logic. Action tools still only ever return proposals."""
import json, sys
from dotenv import load_dotenv
load_dotenv()
from app.lib.argus_agent import TOOLS
from app.lib.supabase_clients import create_admin_client

SCHOOL = sys.argv[1] if len(sys.argv) > 1 else "8a0758b2-697d-49c2-aafe-3d084b4f9179"

def show(name, args=None):
    args = args or {}
    try:
        out = TOOLS[name]["fn"](SCHOOL, args)
    except Exception as e:
        print(f"  {name}: EXCEPTION {type(e).__name__}: {e}"); return
    s = json.dumps(out, default=str)
    print(f"  {name}({args}) ->")
    print(f"    {s[:600]}")

print("--- READ TOOLS -------------------------------------------")
show("get_overview")
show("get_teachers")
show("get_pending_leave_requests")
show("get_substitutes_today")
show("get_classes")

print("\n--- ACTION TOOLS (propose only, no writes) ---------------")
ac = create_admin_client()
classes = ac.table("classes").select("id, name, grade, section").eq("school_id", SCHOOL).limit(3).execute().data or []
teachers = ac.table("teachers").select("name").eq("school_id", SCHOOL).limit(1).execute().data or []
print(f"  (sample classes: {[c.get('name') for c in classes]}, sample teacher: {[t.get('name') for t in teachers]})")

if classes:
    c = classes[0]
    ref = c.get("name") or f"{c.get('grade')}{c.get('section')}"
    show("assign_teacher_to_class", {"classRef": ref, "subject": "Maths"})
if teachers:
    show("mark_teacher_unavailable", {"teacherRef": teachers[0]["name"], "reason": "on_leave"})
show("post_announcement", {"title": "Unit test Monday", "body": "The unit test starts Monday."})
show("assign_substitute", {})
show("approve_leave", {"teacherRef": "zzz-nobody"})
