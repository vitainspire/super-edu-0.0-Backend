"""Runs a real Argus sweep. Writes nothing but Argus's own findings memory."""
import asyncio, json, sys
from dotenv import load_dotenv
load_dotenv()
from app.lib.argus_sweep import detect, run_sweep

SCHOOL = sys.argv[1] if len(sys.argv) > 1 else "8a0758b2-697d-49c2-aafe-3d084b4f9179"

print("=== DETECT (deterministic, no LLM, no writes) ===")
for f in detect(SCHOOL):
    act = f.get("suggestedAction")
    print(f'  [{f["severity"]:9}] {f["findingKey"]}')
    print(f'              {f["title"]}')
    if act:
        print(f'              FIX READY -> {act["label"]}')

async def main():
    print("\n=== FULL SWEEP (memory + briefing) ===")
    out = await run_sweep(SCHOOL, ip="cli-test")
    print("counts   :", json.dumps(out["counts"]))
    print("briefing :", out["briefing"])
    print("actionable:", len(out["actionable"]))
    print("\n=== SECOND SWEEP (suppression check) ===")
    out2 = await run_sweep(SCHOOL, ip="cli-test")
    print("counts   :", json.dumps(out2["counts"]))
    print("briefing :", out2["briefing"])

asyncio.run(main())
