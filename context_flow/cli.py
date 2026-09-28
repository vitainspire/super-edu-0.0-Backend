"""CLI for Node 2 — contextual reinforcement, run against a Node 1 contract.

Node 1 writes a contract; this reads one and says what the room should change
about the route to it. Nothing is persisted and nothing is handed to Generation:
shadow mode is on unless you turn it off, which is what §16 asks for.

    # what a school's context data actually supports, before spending anything
    python -m context_flow.cli factors --profile school.json

    # the run: a contract in, a Context Reinforcement Plan out
    python -m context_flow.cli run --contract chapter02.contract.json \\
        --profile school.json --out chapter02.plan.json

    # the same, allowed to hand its plan to Generation
    python -m context_flow.cli run --contract chapter02.contract.json \\
        --profile school.json --apply

THE PROFILE FILE is a flat JSON object of context fields. A bare value is
treated as `assumed`; state provenance explicitly to get anything more:

    {
      "class_size":            {"value": 52, "provenance": "verified", "origin": "admin"},
      "home_languages":        {"value": ["Marathi"], "provenance": "verified", "origin": "teacher"},
      "has_paper":             {"value": false, "provenance": "verified", "origin": "school_setup"},
      "materials_available":   {"value": ["cup", "chalk", "string"], "provenance": "verified", "origin": "teacher"},
      "local_markets":         {"value": ["the Tuesday vegetable market"], "provenance": "verified", "origin": "teacher"}
    }

`factors` tells you which fields are worth collecting: it prints every factor,
whether this profile switches it on, and — for the ones it does not — exactly
what would.
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

from context_flow import activation, factors, profile as profile_module  # noqa: E402
from context_flow import shadow  # noqa: E402
from context_flow.graph import apply as apply_plan  # noqa: E402
from context_flow.graph import report as build_report, run_context  # noqa: E402

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


# ── factors ──────────────────────────────────────────────────────────────────

def cmd_factors(args) -> None:
    """What this school's data supports, and what it would take to support more.

    Run before spending anything. A school that has recorded no verified
    community data will get no local adaptations however good the model is, and
    seeing that in a table beats inferring it from a thin plan.
    """
    raw = _load(args.profile, "profile") if args.profile else {}
    prof = profile_module.normalise(raw)

    _header("CONTEXT PROFILE")
    summary = prof.summary()
    print(f"  {summary['fields']} field(s) usable — {summary['verified']} verified, "
          f"{summary['assumed']} assumed, {summary['stale']} stale")
    for name, field in sorted(prof.fields.items()):
        stale = "  [STALE]" if field.stale else ""
        print(f"    {name:<26} = {json.dumps(field.value, ensure_ascii=False)[:36]:<38} "
              f"({field.provenance}, {field.origin}){stale}")
    if prof.dropped:
        print(f"\n  {len(prof.dropped)} dropped:")
        for entry in prof.dropped:
            print(f"    {entry['field']:<26}   {entry['why']}")

    _header("FACTORS — what this profile switches on")
    # Activated against an empty topic: this is the §5 question (does the factor
    # have evidence) without the §6 one (is it relevant HERE), which needs a
    # lesson. The command is for judging a school's data, not a lesson.
    entries = activation.activate(prof, {})
    for entry in entries:
        factor = entry.factor
        mark = "ON " if entry.active else "off"
        print(f"  [{mark}] {factor.number:>2}. {factor.name:<38} ({factor.tier})")
        if entry.active and entry.exposes:
            print(f"          reads: {', '.join(entry.exposes)}")
        elif not entry.active:
            print(f"          {entry.reason}")
            missing = [f for f in factor.evidence if not prof.has(f)]
            if missing:
                need = ("verified" if factor.tier == factors.LOCAL
                        else "measured" if factor.tier == factors.LONGITUDINAL
                        else "recorded")
                print(f"          would need {need}: {', '.join(missing)}")

    active = [e for e in entries if e.active and e.factor.tier != factors.GATE]
    local_on = [e for e in active if e.factor.tier == factors.LOCAL]
    print(f"\n  {len(active)}/{len(factors.ADAPTABLE)} adaptable factor(s) active.")
    if not local_on:
        print("  No local factor is active: this school will get feasibility, "
              "language and\n  cognitive support, and no community context at "
              "all. That is correct — local\n  context without verified input is "
              "a guess about a village.")


# ── run ──────────────────────────────────────────────────────────────────────

async def run_cli(args) -> None:
    contract = _load(args.contract, "contract")
    if not contract.get("topics"):
        raise SystemExit(f"{args.contract} carries no topics — is it a Node 1 contract?")
    raw_profile = _load(args.profile, "profile") if args.profile else {}

    chapter = contract.get("chapter") or {}
    _header(f"NODE 2 — {chapter.get('title') or 'chapter'} "
            f"(grade {contract.get('grade')} {contract.get('subject')})")
    print(f"{len(contract['topics'])} topic(s) from Node 1 | "
          f"contract v{contract.get('contractVersion')} | "
          f"generationReady={(contract.get('readiness') or {}).get('generationReady')}")
    print(f"mode: {'APPLIED — the plan may reach Generation' if args.apply else 'SHADOW — the plan is logged, not applied'}")

    state = await run_context(
        contract=contract, context_profile=raw_profile,
        school_id=args.school, class_id=args.class_id,
        config={"shadow_mode": not args.apply, "window_size": args.window},
    )

    prof = state.get("profile") or {}
    _header("PROFILE — what survived normalisation")
    for name, field in sorted((prof.get("fields") or {}).items()):
        print(f"  {name:<26} = {json.dumps(field['value'], ensure_ascii=False)[:34]:<36} "
              f"({field['provenance']}, {field['origin']})")
    for entry in prof.get("dropped") or []:
        print(f"  DROPPED {entry['field']:<24}   {entry['why']}")

    _header("ACTIVATION — which factors may speak")
    for index, summary in sorted((state.get("activation") or {}).items()):
        print(f"  T{index}: {', '.join(summary['proposable']) or '(nothing proposable)'}")
        for factor_id, why in sorted(summary.get("ruledOutForTopic", {}).items()):
            print(f"       n/a {factor_id}: {why[:88]}")
    inactive = next(iter((state.get("activation") or {}).values()), {}).get("inactive", {})
    for factor_id, why in sorted(inactive.items()):
        print(f"  off  {factor_id:<24} {why}")

    _header("THE PLAN — every proposal, and what the gate did with it")
    rows = {r.get("index"): r for r in contract["topics"]}
    for a in (state.get("plan") or {}).get("adaptations") or []:
        mark = "ACCEPT" if a.get("accepted") else "REJECT"
        index = a.get("topicIndex")
        print(f"\n  [{mark}] T{index} {a.get('factor')} / {a.get('type')} "
              f"-> {a.get('section') or 'unplaced'}  ({a.get('purpose')}, "
              f"conf {a.get('confidence')})")
        print(f"         topic:  {(rows.get(index) or {}).get('topic')}")
        print(f"         change: {a.get('change')}")
        print(f"         why:    {a.get('reason')}")
        print(f"         serves: {', '.join(a.get('supports') or []) or '(nothing named)'}")
        print(f"         from:   {', '.join(a.get('evidence') or []) or '(the lesson itself)'} "
              f"[{a.get('dataSource') or '?'}]")
        for why in a.get("rejectedBecause") or []:
            print(f"         -> REJECTED: {why}")

    gate_report = state.get("gate") or {}
    _header("RESULT")
    print(shadow.summary_line(state))
    if gate_report.get("byCheck"):
        for check, count in sorted(gate_report["byCheck"].items()):
            print(f"  {check:<26} {count}")
    if state.get("errors"):
        print("\n  errors:")
        for error in state["errors"]:
            print(f"    - {error}")

    print("\n  metrics (§17):")
    metrics = build_report(state)["metrics"]
    pending = metrics.get("_pending") or {}
    for key, value in metrics.items():
        if key == "_pending":
            continue
        if value is not None:
            shown = value
        elif key in pending:
            # Genuinely unmeasurable from a Node 2 run — it needs the generated
            # sheet, a later node, or a teacher.
            shown = f"— {pending[key]}"
        else:
            # Measurable, but nothing to measure: a rate over zero cases. Said
            # differently from the line above on purpose. "We cannot compute
            # this yet" and "there was nothing of this kind in the plan" are
            # different facts, and a dashboard showing both as a dash would let
            # a node that quietly stopped producing local adaptations read as
            # pending.
            shown = "n/a (no cases in this plan)"
        print(f"    {key:<28} {shown}")

    handed = apply_plan(state)
    print(f"\n  Generation would receive: "
          f"{len(handed['adaptations']) if handed else 0} adaptation(s)"
          + ("" if handed else "  (nothing — shadow mode or a refused plan)"))

    if args.out:
        Path(args.out).write_text(
            json.dumps(build_report(state), indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"\n  wrote the shadow record to {args.out} — the proposals carry an "
              f"empty `review` block for a teacher to fill in (§16)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser(
        "factors", help="which factors this school's data switches on, and what "
                        "would switch on the rest")
    listing.add_argument("--profile", help="a context profile JSON file")

    run = sub.add_parser("run", help="run Node 2 over a Node 1 contract")
    run.add_argument("--contract", required=True,
                     help="a contract written by prep_flow/cli.py --out")
    run.add_argument("--profile", help="a context profile JSON file")
    run.add_argument("--school", default=None)
    run.add_argument("--class-id", default=None)
    run.add_argument("--window", type=int, default=3,
                     help="topics per reasoning call (default 3)")
    run.add_argument("--apply", action="store_true",
                     help="leave shadow mode — the accepted plan may reach "
                          "Generation. §16 asks for a teacher-rated golden set "
                          "before this is used in production.")
    run.add_argument("--out", default=None,
                     help="write the shadow record here as JSON")

    args = parser.parse_args()
    if args.command == "factors":
        cmd_factors(args)
    elif args.command == "run":
        asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
