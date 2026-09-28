"""The bench, with a Run button — Node 2 actually executing behind it.

    python -m context_flow.serve
    -> http://127.0.0.1:8813

WHY A SERVER AND NOT A BIGGER PAGE. `context_bench.html` recomputes factor
activation in JavaScript so it can respond as you type, and that port is checked
against the Python by `tests/test_bench_parity.py`. Fine for a preview. It is not
fine for the actual run: the adaptation wording needs a model, the gate's
verdicts decide what a teacher would be handed, and a second implementation of
either — in a language the pipeline is not written in, checked by a test somebody
has to remember to run — is a place for the two to disagree about something that
matters.

So the run goes through the real thing. This process imports `context_flow` and
calls `run_context()`; the page posts a profile and renders what comes back. The
prompt it shows is the prompt that was sent, the gate verdicts are the gate's,
and the API key stays in this process rather than in a browser tab.

STDLIB ONLY, deliberately. `http.server` rather than FastAPI: this repo's FastAPI
and Starlette are on incompatible versions (`APIRouter()` raises), and a
diagnostic tool that cannot start because of an unrelated dependency conflict is
a diagnostic tool nobody uses. Nothing here needs a framework — two routes and a
file.

LOCAL ONLY. Bound to 127.0.0.1, and that is not a default to change casually:
the process holds an API key and will spend it on request, so it must not be
reachable from the network.
"""
import asyncio
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for candidate in (_ROOT, _ROOT.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

try:
    from dotenv import load_dotenv
    for _env in (_ROOT / ".env", _ROOT.parent / ".env"):
        if _env.exists():
            load_dotenv(_env)
            break
except ImportError:
    pass

from context_flow import activation as activation_module   # noqa: E402
from context_flow import profile as profile_module         # noqa: E402
from context_flow import reasoning as reasoning_module     # noqa: E402
from context_flow.graph import apply as apply_plan         # noqa: E402
from context_flow.graph import report as build_report, run_context  # noqa: E402

PAGE = _ROOT / "context_bench.html"
TOPICS = _ROOT / "topics.sample.json"
HOST, PORT = "127.0.0.1", 8813


def _topics() -> list:
    if not TOPICS.exists():
        return []
    return json.loads(TOPICS.read_text(encoding="utf-8"))


def _contract(rows: list) -> dict:
    """The smallest Node 1 contract that carries these topics.

    The bench is about the ROOM, not the chapter, so the contract around the
    topics is minimal — but it is a real one: `run_context` reads
    `deliveryAssumptions` to size the prompt and `topics` to judge relevance, and
    a stub missing either would make the run disagree with a real one for a
    reason that has nothing to do with the profile being tested.
    """
    return {
        "contractVersion": "1.0",
        "producedBy": "node1.textbook_representation",
        "consumedBy": "node2.contextual_reinforcement",
        "grade": "3", "subject": "Maths",
        "chapter": {"number": 2, "title": "Views of objects"},
        "deliveryAssumptions": {"durationMinutes": 30, "classSize": 40,
                                "language": "English"},
        "topics": rows,
        "preserve": ["masteryTarget", "requiredConcepts", "competencies",
                     "knowledgeChain", "prerequisites"],
        "integrity": {"topicFingerprints": {}, "chapterFingerprint": ""},
    }


def _prompt_for(profile, rows: list) -> str:
    """The prompt this profile would actually produce, assembled by reasoning.py.

    Shown whether or not a key is present: it is the most useful thing on the
    page for someone who has not paid for a call yet, and it costs nothing.
    """
    by_index = {}
    for row in rows:
        index = row.get("index")
        if index is not None:
            by_index[index] = activation_module.activate(profile, row)
    merged = {}
    for index in sorted(by_index):
        for entry in activation_module.proposable(by_index[index]):
            merged.setdefault(entry.factor.id, entry)
    entries = list(merged.values())
    if not entries:
        return ("(no factor is both active and relevant for these topics — "
                "Node 2 would make no model call at all)")
    return (f"═══ THE ROOM ═══\n"
            f"{reasoning_module._profile_block(profile, entries)}\n\n"
            f"═══ WHAT YOU MAY REASON ABOUT ═══\n"
            f"{reasoning_module._factor_block(entries)}")


def _run(raw_profile: dict, indexes: list, apply_it: bool) -> dict:
    rows = [r for r in _topics()
            if not indexes or r.get("index") in indexes]
    if not rows:
        return {"error": "no topics — is topics.sample.json present?"}

    profile = profile_module.normalise(raw_profile)
    prompt = _prompt_for(profile, rows)

    if not (os.environ.get("OPENROUTER_API_KEY") or "").strip():
        # The prompt is still worth returning: it is what a key would have sent.
        return {"noKey": True, "prompt": prompt,
                "profile": profile.as_dict(), "summary": profile.summary()}

    state = asyncio.run(run_context(
        contract=_contract(rows), context_profile=raw_profile,
        config={"shadow_mode": not apply_it}))

    record = build_report(state)
    handed = apply_plan(state)
    return {
        "prompt": prompt,
        "profile": profile.as_dict(),
        "summary": profile.summary(),
        "status": state.get("status"),
        "shadow": state.get("shadow"),
        "plan": state.get("plan") or {},
        "gate": state.get("gate") or {},
        "activation": state.get("activation") or {},
        "metrics": record.get("metrics") or {},
        "handedToGeneration": len((handed or {}).get("adaptations") or []),
        "errors": state.get("errors") or [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):          # quiet; the run prints its own
        return

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            if not PAGE.exists():
                self._send(500, b"context_bench.html is missing - run "
                                b"`python build_bench.py` first", "text/plain")
                return
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {
                "served": True,
                # The page shows a different affordance depending on this, so it
                # is reported rather than discovered by a call that fails.
                "hasKey": bool((os.environ.get("OPENROUTER_API_KEY") or "").strip()),
                "model": os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct"),
                "topics": [{"index": r.get("index"), "topic": r.get("topic")}
                           for r in _topics()],
            })
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad request body: {exc}"})
            return

        indexes = body.get("topicIndexes") or []
        apply_it = bool(body.get("apply"))
        print(f"[bench] running Node 2 over topic(s) {indexes or 'all'} "
              f"({'APPLIED' if apply_it else 'shadow'}) ...")
        try:
            self._json(200, _run(body.get("profile") or {}, indexes, apply_it))
        except Exception as exc:                       # noqa: BLE001
            # Reported to the page rather than only to this console: the person
            # is looking at a browser tab, and a run that fails silently there
            # is a run they will assume produced nothing.
            import traceback
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    if not PAGE.exists():
        raise SystemExit("context_bench.html is missing — run `python build_bench.py`")
    key = bool((os.environ.get("OPENROUTER_API_KEY") or "").strip())
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Context Bench -> {url}")
    print(f"  model:  {os.environ.get('OPENROUTER_MODEL', 'meta-llama/llama-3.3-70b-instruct')}")
    print(f"  API key: {'found — Run Node 2 will make a real call' if key else 'NOT SET'}")
    if not key:
        print("           set OPENROUTER_API_KEY (or put it in .env) to run;")
        print("           without it the page still shows the prompt and the factors.")
    print("  local only, and deliberately: this process spends the key on request.")
    print("  ctrl-c to stop")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
