"""Does context_bench.html agree with the Python it was ported from?

    python -m context_flow.tests.test_bench_parity

The bench re-implements factor activation, the provenance drop rules and the
prompt assembly in JavaScript so the page can recompute them as you type. That
duplication is the point of the tool and also its risk.

A bench that quietly disagrees with the pipeline is worse than no bench: it would
teach somebody the wrong rule and they would only find out in production. So the
same profiles go through both, and the activation verdicts must match exactly.

The JS runs under Node when it is on PATH; otherwise this reports that it could
not run rather than passing silently — a parity check that skips quietly is a
parity check that is not being done.
"""
import json
import pathlib
import shutil
import subprocess
import tempfile
import sys

# The repo root: this file is context_flow/tests/<name>.py.
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from context_flow import activation, profile as profile_module  # noqa: E402

CASES = {
    "rural-assumed": {
        "class_size": {"value": 52, "provenance": "verified", "origin": "school_setup"},
        "lesson_duration": {"value": 30, "provenance": "verified", "origin": "school_setup"},
        "medium_of_instruction": {"value": "English", "provenance": "verified", "origin": "school_setup"},
        "home_languages": {"value": "Marathi", "provenance": "verified", "origin": "school_setup"},
        "materials_available": {"value": "cup, chalk, string", "provenance": "verified", "origin": "school_setup"},
        "has_board": {"value": True, "provenance": "verified", "origin": "school_setup"},
        "has_paper": {"value": False, "provenance": "verified", "origin": "school_setup"},
        "electricity": {"value": False, "provenance": "verified", "origin": "school_setup"},
        "devices_available": {"value": 0, "provenance": "verified", "origin": "school_setup"},
        "community_occupations": {"value": "fishing", "provenance": "assumed", "origin": "system"},
    },
    "community-verified": {
        "class_size": {"value": 52, "provenance": "verified", "origin": "school_setup"},
        "medium_of_instruction": {"value": "English", "provenance": "verified", "origin": "school_setup"},
        "home_languages": {"value": "Marathi", "provenance": "verified", "origin": "school_setup"},
        "community_occupations": {"value": "fishing", "provenance": "verified", "origin": "school_setup"},
        "local_markets": {"value": "the Tuesday vegetable market", "provenance": "verified", "origin": "school_setup"},
        "household_skills": {"value": "net mending", "provenance": "verified", "origin": "school_setup"},
    },
    "attendance-recalled": {
        "class_size": {"value": 45, "provenance": "verified", "origin": "school_setup"},
        "attendance_history": {"value": "patchy", "provenance": "verified", "origin": "teacher"},
    },
    "attendance-measured": {
        "class_size": {"value": 45, "provenance": "verified", "origin": "school_setup"},
        "attendance_history": {"value": "patchy", "provenance": "verified", "origin": "measured"},
    },
    "empty": {},
}

topics = json.loads((ROOT / "topics.sample.json").read_text(encoding="utf-8"))

# ── Python side ─────────────────────────────────────────────────────────────
expected = {}
for name, raw in CASES.items():
    prof = profile_module.normalise(raw)
    for ti, row in enumerate(topics):
        entries = activation.activate(prof, row)
        expected[f"{name}|{ti}"] = {
            "kept": sorted(prof.fields),
            "dropped": sorted(d["field"] for d in prof.dropped),
            "proposable": sorted(e.factor.id for e in activation.proposable(entries)),
            "off": sorted(e.factor.id for e in entries if not e.active),
            "notHere": sorted(e.factor.id for e in entries
                              if e.active and e.relevant is False),
        }

# ── JS side ─────────────────────────────────────────────────────────────────
node = shutil.which("node")
if not node:
    print("SKIPPED — node is not on PATH, so the browser port could not be executed.")
    print("The Python side is verified by context_flow/tests/; the port is not.")
    sys.exit(0)

page = (ROOT / "context_bench.html").read_text(encoding="utf-8")
js = page[page.index("const REG    ="):page.index("/* ── render ")]

harness = js + """
const CASES = %s, TOPICS_IN = %s;
const out = {};
for (const [name, raw] of Object.entries(CASES)) {
  const state = {};
  for (const [k, cell] of Object.entries(raw))
    state[k] = {v: cell.value, p: cell.provenance, o: cell.origin};
  const p = normalise(state);
  TOPICS_IN.forEach((row, ti) => {
    const acts = activate(p, row);
    out[name + "|" + ti] = {
      kept: Object.keys(p.fields).sort(),
      dropped: p.dropped.map(d => d.field).sort(),
      proposable: proposable(acts).map(e => e.f.id).sort(),
      off: acts.filter(e => !e.active).map(e => e.f.id).sort(),
      notHere: acts.filter(e => e.active && e.relevant === false).map(e => e.f.id).sort(),
    };
  });
}
console.log(JSON.stringify(out));
""" % (json.dumps(CASES), json.dumps(topics))

# The port reads TOPICS from the page; the harness passes its own in.
harness = harness.replace("const TOPICS = ", "const TOPICS_UNUSED = ")

# Windows caps a command line at ~32k and the harness carries the registry, so
# it goes to a file rather than to -e.
script = pathlib.Path(tempfile.gettempdir()) / "_bench_parity_harness.mjs"
script.write_text(harness, encoding="utf-8")
proc = subprocess.run([node, str(script)], capture_output=True, text=True)
if proc.returncode != 0:
    print("the ported JS did not run:\n", proc.stderr[:1500])
    sys.exit(1)

actual = json.loads(proc.stdout)

# ── compare ─────────────────────────────────────────────────────────────────
bad = 0
for key in sorted(expected):
    e, a = expected[key], actual.get(key)
    if a is None:
        print(f"  MISSING  {key}"); bad += 1; continue
    for field in ("kept", "dropped", "proposable", "off", "notHere"):
        if e[field] != a[field]:
            bad += 1
            print(f"  MISMATCH {key}.{field}")
            print(f"     python: {e[field]}")
            print(f"     js    : {a[field]}")
    if all(e[f] == a[f] for f in ("kept", "dropped", "proposable", "off", "notHere")):
        print(f"  match    {key:<28} {len(e['proposable'])} proposable, "
              f"{len(e['off'])} off, {len(e['dropped'])} dropped")

print()
print("PARITY OK" if not bad else f"{bad} MISMATCH(ES) — the bench would lie")
sys.exit(1 if bad else 0)
