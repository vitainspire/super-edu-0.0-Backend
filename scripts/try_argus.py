"""Manual harness for the Argus agent loop — no server, no auth token, no frontend.

Calls run_argus() directly against real Supabase data and a real LLM, and
prints both the answer and the trace of which tools it chose. Read tools
really execute; action tools can only ever return a proposal, so this script
is incapable of writing anything (it never calls /argus/confirm).

Run from the backend project root with the venv's python:
    venv/Scripts/python.exe try_argus.py                 # auto-pick a school
    venv/Scripts/python.exe try_argus.py <schoolId>       # a specific school
    venv/Scripts/python.exe try_argus.py <schoolId> "your own question"
"""
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from app.lib.supabase_clients import create_admin_client  # noqa: E402
from app.lib.argus_agent import run_argus, TOOLS  # noqa: E402

# Chosen to exercise different paths: a single read, a question that needs
# more than one tool, one that should find nothing, and one that must stop at
# a proposal instead of acting.
DEFAULT_QUESTIONS = [
    "what needs my attention today?",
    "how many classes still have no teacher, and which teachers do I have?",
    "is anyone off today and is all their cover sorted?",
    "are there any leave requests waiting on me?",
    "post an announcement telling teachers the unit test starts Monday",
]


def pick_school(argv: list) -> str:
    if len(argv) > 1:
        return argv[1]
    ac = create_admin_client()
    rows = ac.table("schools").select("id, name").limit(10).execute().data or []
    if not rows:
        sys.exit("No schools in this database — pass a schoolId explicitly.")
    print("Schools found:")
    for r in rows:
        print(f'  {r["id"]}  {r.get("name")}')
    print(f'\nUsing the first one: {rows[0].get("name")}\n')
    return rows[0]["id"]


async def main() -> None:
    school_id = pick_school(sys.argv)
    questions = [sys.argv[2]] if len(sys.argv) > 2 else DEFAULT_QUESTIONS

    reads = [n for n, s in TOOLS.items() if s.get("side_effect", "read") == "read"]
    actions = [n for n, s in TOOLS.items() if s.get("side_effect") == "action"]
    print(f"Argus has {len(reads)} read tools and {len(actions)} action tools.")
    print(f"school_id = {school_id}\n")

    for question in questions:
        print("=" * 78)
        print(f"ASK: {question}")
        result = await run_argus(school_id, question, [], "cli-test")

        print(f'\nstatus : {result.get("status")}')
        trace = result.get("trace") or []
        if trace:
            print("tools  :")
            for t in trace:
                mark = "ok" if t.get("ok") else "FAILED"
                print(f'         - {t.get("tool")}({t.get("args")}) [{mark}]')
        else:
            print("tools  : (none called)")

        if result.get("status") == "pending_confirmation":
            print(f'\nPROPOSED (nothing written): {result.get("label")}')
            print(f'payload : {result.get("proposal")}')
            print("-> a human would have to POST this to /argus/confirm for it to happen.")
        else:
            print(f'\nANSWER : {result.get("response")}')
        print()


if __name__ == "__main__":
    asyncio.run(main())
