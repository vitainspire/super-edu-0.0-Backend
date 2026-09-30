"""CLI for the generation stage, and for the A/B that shows whether Node 2 lands.

    # write a chapter from a Node 1 contract (and optionally a Node 2 plan)
    python -m generation.cli write --contract chapter02.contract.json \\
        --chapter chapter02.md --plan chapter02.record.json \\
        --out chapter02.materials.json

    # the A/B: the same chapter twice, with the plan and without, then the app
    python -m generation.cli compare --contract chapter02.contract.json \\
        --plan chapter02.record.json --chapter chapter02.md \\
        --app did-the-context-land.html

`compare` is the one that answers "did Node 2 change anything". It runs three
arms — baseline, adapted, and a second baseline — and writes a self-contained
HTML page you can open in a browser. The third arm is the ruler: generation runs
at temperature 0.6, so two runs of the SAME arm differ, and a change smaller
than that gap is the sampler rather than the plan. `--no-noise-arm` saves a third
of the cost and the page then says, in as many words, that it has no ruler.

COSTS REAL MONEY. Two or three full generations of a chapter. On a 30-topic
chapter at window 3 that is 20–30 model calls per arm.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_ROOT = Path(__file__).resolve().parent.parent
for candidate in (_ROOT, _ROOT.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

try:
    from dotenv import load_dotenv
    for env in (_ROOT / ".env", _ROOT.parent / ".env"):
        if env.exists():
            load_dotenv(env)
            break
except ImportError:
    pass

from generation import reinforcement, report  # noqa: E402
from generation.adapt import sources_from_chapter  # noqa: E402
from generation.compare import run_ab  # noqa: E402
from generation.graph import run_generation  # noqa: E402

_RULE = "─" * 78
_TEMPLATE = Path(__file__).resolve().parent / "app_template.html"


def _header(title: str) -> None:
    print(f"\n{_RULE}\n{title}\n{_RULE}")


def _load(path: str, what: str) -> dict:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"no {what} at {path}")
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}") from exc


def _plan_from(path: str) -> dict:
    """Accept either a bare plan or a Node 2 shadow record.

    `python -m context_flow.cli --out` writes the RECORD, which carries the
    adaptations under `proposals`. That is the file most people will have, so
    taking only the bare plan would make the common case an error.
    """
    loaded = _load(path, "context plan")
    if "adaptations" in loaded:
        return loaded
    return {
        "adaptations": loaded.get("proposals") or [],
        "preserve": (loaded.get("gate") or {}).get("preserve") or [],
        # The room's hard limits ride along. Dropping them here would make the
        # file path quietly weaker than the in-process one.
        "constraints": loaded.get("constraints") or {},
        "lowersStandard": bool((loaded.get("gate") or {}).get("lowersStandard")),
        "note": (loaded.get("plan") or {}).get("note") or "",
    }


def _chapter_text(path) -> str:
    if not path:
        return ""
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"no chapter text at {path}")
    return file.read_text(encoding="utf-8")


def _write_app(result: dict, out: str) -> None:
    if not _TEMPLATE.exists():
        raise SystemExit(f"the app template is missing at {_TEMPLATE}")
    blob = json.dumps({k: v for k, v in result.items() if k != "arms"},
                      ensure_ascii=False, separators=(",", ":"))
    # A literal </script> inside a JSON string would close the tag early.
    blob = blob.replace("</", "<\\/")
    page = _TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)
    Path(out).write_text(page, encoding="utf-8")
    print(f"\n  wrote {out} ({len(page):,} chars) — open it in a browser")


# ── write ────────────────────────────────────────────────────────────────────

async def write_cli(args) -> None:
    contract = _load(args.contract, "contract")
    if not contract.get("topics"):
        raise SystemExit(f"{args.contract} carries no topics — is it a Node 1 contract?")
    plan = _plan_from(args.plan) if args.plan else None
    chapter_text = _chapter_text(args.chapter)

    chapter = contract.get("chapter") or {}
    _header(f"GENERATION — {chapter.get('title') or 'chapter'} "
            f"(grade {contract.get('grade')} {contract.get('subject')})")
    print(f"{len(contract['topics'])} topic(s) | "
          f"textbook: {'supplied' if chapter_text else 'NOT supplied — Concept will stay to what the title implies'}")
    handover = reinforcement.summary(plan)
    print(f"context plan: {handover['adaptations']} adaptation(s) across "
          f"{handover['topics']} topic(s)" if plan else "context plan: none")

    result = await run_generation(
        contract=contract, context_plan=plan,
        sources=sources_from_chapter(contract, chapter_text) if chapter_text else None,
        chapter_text=chapter_text,
        config={"window_size": args.window})

    _header("RESULT")
    print(f"  {result['metrics']['topicsGenerated']}/{result['metrics']['topicsTotal']} "
          f"sheet(s) written in {result['metrics']['windows']} window(s)")
    for error in result.get("errors") or []:
        print(f"  ! {error}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({str(k): v for k, v in result["materials"].items()},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  wrote {args.out} — this is Node 3's input")


# ── compare ──────────────────────────────────────────────────────────────────

async def compare_cli(args) -> None:
    contract = _load(args.contract, "contract")
    if not contract.get("topics"):
        raise SystemExit(f"{args.contract} carries no topics — is it a Node 1 contract?")
    plan = _plan_from(args.plan)
    handover = reinforcement.summary(plan)
    if not handover["adaptations"]:
        raise SystemExit(
            "the context plan carries no accepted adaptations, so both arms would be "
            "identical. Nothing to compare — check that Node 2's gate accepted "
            "anything, and that you passed the plan rather than an empty record.")

    chapter_text = _chapter_text(args.chapter)
    chapter = contract.get("chapter") or {}
    arms = 2 if args.no_noise_arm else 3

    _header(f"A/B — {chapter.get('title') or 'chapter'} "
            f"(grade {contract.get('grade')} {contract.get('subject')})")
    print(f"{len(contract['topics'])} topic(s) | {handover['adaptations']} adaptation(s) "
          f"to hand over | {arms} full generations of the chapter")
    print(f"textbook: {'supplied' if chapter_text else 'NOT supplied'} | "
          f"noise arm: {'skipped — the comparison will have no ruler' if args.no_noise_arm else 'on'}")

    result = await run_ab(
        contract=contract, plan=plan,
        sources=sources_from_chapter(contract, chapter_text) if chapter_text else None,
        chapter_text=chapter_text,
        config={"window_size": args.window},
        with_noise_arm=not args.no_noise_arm)

    if result.get("error"):
        raise SystemExit(f"{result['error']}\n  " + "\n  ".join(result.get("errors") or []))

    summary = result["summary"]
    _header("DID IT LAND?")
    print(f"  handed to the generator   {summary['adaptationsHandedOver']}")
    print(f"  landed where aimed        {summary['landedWhereAimed']}")
    print(f"  landed elsewhere          {summary['landedElsewhere']}")
    print(f"  did not land              {summary['didNotLand']}")
    print(f"\n  sections changed          {summary['sectionsChanged']}/{summary['sectionsCompared']}"
          f"  ({(summary['changeRate'] or 0):.0%})")
    print(f"  topics Node 2 left alone  {summary['topicsUnchanged']}")

    for topic in result["topics"]:
        if not topic["adaptations"]:
            continue
        print(f"\n  T{topic['index']}")
        for a in topic["adaptations"]:
            mark = {"yes": "LANDED", "elsewhere": "NOT WHERE AIMED",
                    "no": "DID NOT LAND", "noise-only": "NOISE ONLY",
                    "unplaced": "NO SECTION NAMED"}.get(a["landed"], a["landed"])
            print(f"    [{mark:<16}] {a['factor']:<16} aimed at {a['section'] or '-'}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({k: v for k, v in result.items() if k != "arms"},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  wrote {args.out}")
    if args.md:
        Path(args.md).write_text(report.render(result), encoding="utf-8")
        print(f"  wrote {args.md}")
    if args.app:
        _write_app(result, args.app)


def app_cli(args) -> None:
    """Rebuild the page or the report from a stored comparison, without re-generating.

    Both outputs come from the same result file, so a run paid for once can be
    re-rendered as often as the presentation changes.
    """
    if not args.app and not args.md:
        raise SystemExit("give --app, --md, or both")
    result = _load(args.result, "comparison result")
    if args.app:
        _write_app(result, args.app)
    if args.md:
        Path(args.md).write_text(report.render(result), encoding="utf-8")
        print(f"  wrote {args.md}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    write = sub.add_parser("write", help="write a chapter's sheets from a contract")
    write.add_argument("--contract", required=True)
    write.add_argument("--plan", default=None, help="Node 2's plan or shadow record")
    write.add_argument("--chapter", default=None, help="the textbook markdown")
    write.add_argument("--window", type=int, default=3, help="topics per call (default 3)")
    write.add_argument("--out", default=None, help="write {index: sheet} here")

    compare = sub.add_parser("compare", help="the A/B: with the plan and without")
    compare.add_argument("--contract", required=True)
    compare.add_argument("--plan", required=True)
    compare.add_argument("--chapter", default=None)
    compare.add_argument("--window", type=int, default=3)
    compare.add_argument("--no-noise-arm", action="store_true",
                         help="skip the second baseline. Saves a third of the cost "
                              "and leaves the comparison with no ruler for the "
                              "sampler's own variation.")
    compare.add_argument("--out", default=None, help="write the comparison JSON here")
    compare.add_argument("--app", default=None, help="write the viewer HTML here")
    compare.add_argument("--md", default=None,
                         help="write the comparison as Markdown here — the same "
                              "findings, paste-able into a review thread")

    app = sub.add_parser("app", help="rebuild the viewer from a stored comparison")
    app.add_argument("--result", required=True)
    app.add_argument("--app", default=None, help="rebuild the viewer HTML here")
    app.add_argument("--md", default=None, help="rebuild the Markdown report here")

    args = parser.parse_args()
    if args.command == "write":
        asyncio.run(write_cli(args))
    elif args.command == "compare":
        asyncio.run(compare_cli(args))
    elif args.command == "app":
        app_cli(args)


if __name__ == "__main__":
    main()
