"""CLI for Node 3 — validation, run against a contract and the material it produced.

    # judge a chapter
    python -m validation_flow.cli judge --contract chapter02.contract.json \\
        --materials chapter02.materials.json --chapter chapter02.md \\
        --plan chapter02.plan.json --out chapter02.verdict.json

    # cheap pass: structure and integrity only, no learner gate
    python -m validation_flow.cli judge --contract c.json --materials m.json \\
        --no-simulate

    # what a stored verdict says needs rewriting
    python -m validation_flow.cli brief --verdict chapter02.verdict.json

THE INPUTS, and what is lost without each of the optional ones:

    --contract   REQUIRED. Node 1's, as written by `prep_flow/cli.py --out`.
    --materials  REQUIRED. A JSON object of {topicIndex: sheet}.
    --chapter    the textbook markdown. WITHOUT IT the two grounding checks
                 cannot run — they report an advisory saying so rather than
                 passing quietly, and `coverage.groundingChecked` on the verdict
                 records that a sheet was judged without the book.
    --plan       Node 2's Context Reinforcement Plan. WITHOUT IT the adoption
                 check is silent: a chapter generated with no context plan has
                 not ignored one.
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

# The repo root, which is now this file's GRANDparent: every CLI moved into the
# package it drives. Both it and its parent go on the path — the second is what
# lets a flat prep-material checkout and the real backend tree both import
# `ai`, `canonical_mapping` and the other modules that live beside the package.
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

from validation_flow import verdict as verdict_module  # noqa: E402
from validation_flow.graph import repair_brief, run_validation, shippable  # noqa: E402

_RULE = "─" * 78


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


def _print_verdict(document: dict) -> None:
    counts = document.get("counts") or {}
    coverage = document.get("coverage") or {}

    _header(f"VERDICT — {document.get('verdict', '?').upper()}")
    print(f"  {counts.get('ship', 0)} ship | {counts.get('needs_review', 0)} need review | "
          f"{counts.get('refuse', 0)} refused   of {counts.get('topics', 0)} topic(s), "
          f"{counts.get('withMaterial', 0)} with material")
    print(f"  grounding checked: {coverage.get('groundingChecked')}   "
          f"learner gate ran: {coverage.get('learnerGateRan')}   "
          f"context plan: {coverage.get('contextPlanChecked')}")
    if not coverage.get("groundingChecked"):
        print("  ! no textbook text was supplied, so nothing checked whether the "
              "sheets stayed\n    inside the book. Pass --chapter to check it.")
    if not coverage.get("learnerGateRan"):
        print("  ! the learner gate did not run, so nothing checked whether the "
              "sheets teach.\n    No topic can read `ship` on this run.")

    for row in document.get("topics") or []:
        mark = {"ship": "SHIP  ", "needs_review": "REVIEW", "refuse": "REFUSE"}.get(
            row.get("verdict"), "?     ")
        print(f"\n  [{mark}] T{row['index']} {str(row.get('topic'))[:52]}"
              f"   ({row['blocking']} blocking, {row['advisory']} advisory)")
        learner = row.get("learner")
        if learner:
            gates = ", ".join(learner.get("failedGates") or [])
            print(f"           learner {learner.get('weighted')} "
                  f"{'PASS' if learner.get('passed') else 'FAIL'}"
                  + (f" — failed: {gates}" if gates else ""))
            for diagnosis in learner.get("diagnosis") or []:
                print(f"             {diagnosis.get('dimension')}: "
                      f"{str(diagnosis.get('failure'))[:66]}")
        for finding in row.get("findings") or []:
            severity = "BLOCKING" if finding.get("severity") == "blocking" else "advisory"
            print(f"           [{severity:<8}] {str(finding.get('check')):<20} "
                  f"{str(finding.get('section') or 'sheet'):<10} "
                  f"{str(finding.get('message'))[:60]}")
        if row.get("repairSections"):
            print(f"           rewrite: {', '.join(row['repairSections'])}")

    chapter_findings = document.get("chapterFindings") or []
    if chapter_findings:
        _header("CHAPTER-LEVEL")
        for finding in chapter_findings:
            severity = "BLOCKING" if finding.get("severity") == "blocking" else "advisory"
            print(f"  [{severity:<8}] {str(finding.get('check')):<20} "
                  f"{str(finding.get('message'))[:74]}")

    _header("METRICS")
    for key, value in sorted((document.get("metrics") or {}).items()):
        print(f"  {key:<32} {value if value is not None else 'n/a'}")


async def judge_cli(args) -> None:
    contract = _load(args.contract, "contract")
    if not contract.get("topics"):
        raise SystemExit(f"{args.contract} carries no topics — is it a Node 1 contract?")

    raw_materials = _load(args.materials, "materials")
    # Tolerate both {index: sheet} and the CLI's own {"materials": {...}} wrapper,
    # because a caller that saved a whole run and a caller that saved just the
    # sheets both reach for the same flag.
    materials = raw_materials.get("materials") if "materials" in raw_materials else raw_materials
    materials = {int(k): v for k, v in (materials or {}).items()}
    if not materials:
        raise SystemExit(f"{args.materials} holds no sheets")

    plan = None
    if args.plan:
        loaded = _load(args.plan, "plan")
        # A Node 2 shadow RECORD carries the plan under `proposals`; a plan
        # written directly carries `adaptations`. Accept either — the record is
        # what `context_flow/cli.py --out` writes, so it is the file most people
        # will have to hand.
        plan = loaded if "adaptations" in loaded else {
            "adaptations": loaded.get("proposals") or [],
            "preserve": (loaded.get("gate") or {}).get("preserve") or [],
            "lowersStandard": bool((loaded.get("gate") or {}).get("lowersStandard")),
        }

    chapter_text = ""
    if args.chapter:
        file = Path(args.chapter)
        if not file.exists():
            raise SystemExit(f"no chapter text at {args.chapter}")
        chapter_text = file.read_text(encoding="utf-8")

    chapter = contract.get("chapter") or {}
    _header(f"NODE 3 — {chapter.get('title') or 'chapter'} "
            f"(grade {contract.get('grade')} {contract.get('subject')})")
    print(f"{len(contract['topics'])} topic(s) in the contract | "
          f"{len(materials)} sheet(s) to judge")
    print(f"textbook: {'supplied' if chapter_text else 'NOT supplied — grounding unchecked'} | "
          f"context plan: {'supplied' if plan else 'none'} | "
          f"learner gate: {'off' if args.no_simulate else 'on'}")

    state = await run_validation(
        contract=contract, materials=materials, plan=plan,
        chapter_text=chapter_text,
        config={"simulate_learner": not args.no_simulate,
                "learner_pass_threshold": args.learner_threshold},
    )

    document = state["verdict"]
    _print_verdict(document)

    if state.get("errors"):
        _header("ERRORS")
        for error in state["errors"]:
            print(f"  - {error}")

    _header("RESULT")
    print(verdict_module.summary_line(document))
    publishable = shippable(state)
    print(f"  publishable topics: {publishable or '(none)'}")
    brief = repair_brief(state)
    if brief["topics"]:
        print(f"  needs rewriting:    "
              f"{', '.join('T' + str(t['index']) for t in brief['topics'])}")

    if args.out:
        Path(args.out).write_text(json.dumps(document, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\n  wrote the verdict to {args.out}")
    if args.brief_out:
        Path(args.brief_out).write_text(json.dumps(brief, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
        print(f"  wrote the repair brief to {args.brief_out} — this is what "
              f"Generation needs to rewrite from")


def brief_cli(args) -> None:
    """What a stored verdict says needs rewriting, without re-judging anything."""
    document = _load(args.verdict, "verdict")
    brief = repair_brief({"verdict": document})
    _header("REPAIR BRIEF")
    if not brief["topics"]:
        print("  nothing needs rewriting.")
        return
    for topic in brief["topics"]:
        sections = ", ".join(topic["sections"]) or "the whole sheet"
        print(f"\n  T{topic['index']} — rewrite {sections}")
        for why in topic["why"]:
            print(f"      because: {why[:88]}")
        for diagnosis in topic["learnerDiagnosis"]:
            print(f"      learner ({diagnosis.get('dimension')}): "
                  f"{str(diagnosis.get('failure'))[:76]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    judge = sub.add_parser("judge", help="judge a chapter's generated material")
    judge.add_argument("--contract", required=True, help="Node 1's contract, as JSON")
    judge.add_argument("--materials", required=True,
                       help="{topicIndex: sheet} as JSON")
    judge.add_argument("--chapter", default=None,
                       help="the textbook markdown — without it the grounding "
                            "checks cannot run")
    judge.add_argument("--plan", default=None,
                       help="Node 2's plan or shadow record — without it the "
                            "adoption check is silent")
    judge.add_argument("--no-simulate", action="store_true",
                       help="skip the learner gate (~1 call per sheet). No topic "
                            "can read `ship` on a run that skipped it.")
    judge.add_argument("--learner-threshold", type=float, default=0.75,
                       help="weighted score a sheet must reach (default 0.75); "
                            "the hard gates apply regardless")
    judge.add_argument("--out", default=None, help="write the verdict here")
    judge.add_argument("--brief-out", default=None,
                       help="write the repair brief here")

    brief = sub.add_parser("brief", help="what a stored verdict says to rewrite")
    brief.add_argument("--verdict", required=True)

    args = parser.parse_args()
    if args.command == "judge":
        asyncio.run(judge_cli(args))
    elif args.command == "brief":
        brief_cli(args)


if __name__ == "__main__":
    main()
