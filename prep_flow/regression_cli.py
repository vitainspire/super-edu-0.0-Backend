"""The regression loop: run it on a schedule, or before you ship a prompt change.

    python -m prep_flow.regression_cli run                    # snapshot + compare
    python -m prep_flow.regression_cli run --set-baseline     # ...and pin it
    python -m prep_flow.regression_cli list                   # the history
    python -m prep_flow.regression_cli compare A.json B.json  # no generation, free
    python -m prep_flow.regression_cli baseline <snapshot>    # pin one deliberately

`run` EXITS NON-ZERO ON A REGRESSION, so CI or a cron wrapper can fail on it
without parsing anything. `incomparable` also exits non-zero, deliberately: a
comparison that could not be made is not a passing check, and the failure mode
worth guarding against is a rubric edit quietly turning the harness into a
random number generator that nobody reads.

WHAT IT COSTS. `run` generates the golden chapter `--repeats` times and judges
every sheet — roughly $0.03 per generation plus $0.002 per sheet, so the default
3 repeats over a 4-topic fixture is about $0.12. Cheap enough to run nightly,
not cheap enough to run in a loop while you edit a prompt. `compare` re-reads two
stored snapshots and costs nothing.

WHAT IT DOES NOT DO. It never edits a prompt, activates a state, or rolls
anything back. It prints what moved and which criterion moved it.
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

from prep_flow import db, gate, regression  # noqa: E402

_RULE = "─" * 78

_MARK = {"regression": "REGRESSION", "improvement": "IMPROVED",
         "stable": "stable", "incomparable": "INCOMPARABLE"}


def _header(text: str) -> None:
    print(f"\n{_RULE}\n{text}\n{_RULE}")


def render_snapshot(snapshot: dict) -> None:
    summary = snapshot["summary"]
    fixture = snapshot["fixture"]
    print(f"  snapshot {snapshot['id']}"
          + (f"  ({snapshot['label']})" if snapshot.get("label") else ""))
    print(f"  {fixture.get('chapterTitle')} — grade {fixture.get('grade')} "
          f"{fixture.get('subject')}, {fixture.get('topics')} topics, "
          f"{snapshot['environment']['repeats']} repeats")
    print(f"  mean {summary['mean']:.2f} ±{summary['ci95']:.2f} "
          f"over {summary['sheets']} sheets   "
          f"would-use-as-is {summary['wouldUseAsIs']:.0%}")
    print(f"  rubric {snapshot['rubric']['fingerprint']}   "
          f"fixture {fixture['fingerprint']}   "
          f"model {snapshot['environment'].get('model')}")
    if snapshot.get("failures"):
        print(f"  {len(snapshot['failures'])} repeat(s) failed and were excluded:")
        for problem in snapshot["failures"][:3]:
            print(f"    - {problem}")
    print()
    for key, stats in snapshot["perCriterion"].items():
        bar = "█" * int(round(stats["mean"] * 4))
        print(f"    {key:<22}{stats['mean']:.2f} ±{stats['ci95']:.2f}  {bar}")


def render_comparison(result: dict) -> None:
    verdict = result["verdict"]
    print(f"  {_MARK[verdict]}: {result['reason']}")
    if verdict == "incomparable":
        # Named one per line rather than folded into the reason: each of these
        # has a different fix, and a wall of semicolons hides which one applies.
        for problem in result.get("problems") or []:
            print(f"    - {problem}")
        return

    means = result["means"]
    print(f"  baseline {result['baselineId']}  mean {means['baseline']:.2f}")
    print(f"  candidate {result['candidateId']}  mean {means['candidate']:.2f}")
    print(f"\n  {'criterion':<22}{'base':>6}{'now':>7}{'delta':>8}")
    for key, stats in sorted(result["perCriterion"].items(),
                             key=lambda item: item[1]["delta"]):
        if key in result["regressedCriteria"]:
            flag = "  <- REGRESSED"
        elif key in result["improvedCriteria"]:
            flag = "  <- improved"
        elif abs(stats["delta"]) >= result["threshold"]:
            # Moved past the bar but inside its own interval. Shown rather than
            # hidden: this is the line someone will otherwise read as a finding.
            flag = f"     (within noise, ±{stats['spread']:.2f})"
        else:
            flag = ""
        print(f"    {key:<22}{stats['baseline']:>6.2f}{stats['candidate']:>7.2f}"
              f"{stats['delta']:>+8.2f}{flag}")

    changed = result.get("environmentChanged") or {}
    if changed:
        # Printed whatever the verdict, because it is the first thing that makes
        # a delta mean something other than "the prompt got worse".
        print("\n  NOTE — something other than the code changed between these runs:")
        for key, pair in changed.items():
            print(f"    {key}: {pair['baseline']!r} -> {pair['candidate']!r}")


async def cmd_run(args) -> int:
    scope = args.scope
    path = Path(args.fixture) if args.fixture else gate.golden_path(scope)
    try:
        fixture = gate.load_golden(path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot run: {exc}")
        # The default path is golden/default.json, and a fixture built for one
        # cohort sits under its own name — so "not found" is most often "you have
        # one, it is just not called that". Say so rather than sending someone to
        # rebuild a fixture they already have.
        available = sorted(path.parent.glob("*.json")) if path.parent.exists() else []
        if available:
            print(f"\n  fixtures that DO exist in {path.parent}:")
            for found in available:
                print(f"    --fixture {found}")
        return 2

    adaptive = {}
    if args.adaptive and scope and db.configured():
        adaptive = await db.fetch_active_adaptive_state(scope) or {}
        print(f"  using adaptive state v{adaptive.get('_version')} for {scope}")
    elif args.adaptive and not db.configured():
        # Said out loud. A snapshot taken with no adaptive state, compared later
        # against one taken with a v7 state in force, is a real difference the
        # report would otherwise attribute to the prompt.
        print("  no Supabase configured — snapshotting with NO adaptive state")

    _header(f"SNAPSHOT — {fixture.get('chapterTitle')}, {args.repeats} repeats")
    snapshot = await regression.run_snapshot(
        fixture, adaptive=adaptive, repeats=args.repeats, label=args.label,
        scope_key=scope, fixture_path=str(path))
    saved = regression.save_snapshot(snapshot, scope)
    render_snapshot(snapshot)
    print(f"\n  wrote {saved}")

    baseline_file = Path(args.baseline) if args.baseline else \
        regression.baseline_path(scope)
    # The snapshot just written is in the directory now, so "most recent" would
    # find itself. Compare against the most recent OTHER one.
    if baseline_file and Path(baseline_file).resolve() == saved.resolve():
        others = [p for p in regression.list_snapshots(scope)
                  if p.resolve() != saved.resolve()]
        baseline_file = others[-1] if others else None

    exit_code = 0
    if not baseline_file:
        _header("NO BASELINE YET")
        print("  This is the first snapshot for this scope — nothing to compare\n"
              "  against. Run again after a change, or pin this one now:\n"
              f"    python -m prep_flow.regression_cli baseline {saved.name}"
              + (f" --scope {scope}" if scope else ""))
    else:
        _header("COMPARISON")
        result = regression.compare(regression.load_snapshot(baseline_file),
                                    snapshot, args.threshold)
        render_comparison(result)
        if result["verdict"] == "incomparable":
            # A pinned baseline goes stale the moment the fixture or the rubric
            # changes, and "incomparable" alone leaves someone staring at a
            # correct refusal with no idea what to do about it. Naming the newest
            # snapshot that WOULD compare turns it into an instruction.
            usable = [p for p in regression.list_snapshots(scope)
                      if p.resolve() != saved.resolve()
                      and not regression.comparable(
                          regression.load_snapshot(p), snapshot)]
            if usable:
                print(f"\n  The most recent snapshot this one CAN be compared "
                      f"against is\n    {usable[-1].name}\n"
                      f"  Compare against it directly, or pin it:\n"
                      f"    python -m prep_flow.regression_cli baseline "
                      f"{usable[-1].name}"
                      + (f" --scope {scope}" if scope else ""))
            else:
                print("\n  No stored snapshot is comparable with this one — this "
                      "is the first\n  under the current fixture and rubric. Pin "
                      "it as the new baseline:\n"
                      f"    python -m prep_flow.regression_cli baseline {saved.name}"
                      + (f" --scope {scope}" if scope else ""))
        # `incomparable` fails too — see the module docstring. A check that could
        # not be made is not a check that passed.
        if result["regressed"] or result["verdict"] == "incomparable":
            exit_code = 1

    if args.set_baseline:
        regression.set_baseline(saved, scope)
        print(f"\n  baseline pinned to {saved.name}")
    if args.out:
        Path(args.out).write_text(json.dumps(snapshot, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"  raw snapshot also written to {args.out}")
    return exit_code


def cmd_compare(args) -> int:
    baseline = regression.load_snapshot(args.baseline)
    candidate = regression.load_snapshot(args.candidate)
    _header("COMPARISON")
    result = regression.compare(baseline, candidate, args.threshold)
    render_comparison(result)
    if args.json:
        print("\n" + json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if (result["regressed"] or result["verdict"] == "incomparable") else 0


def cmd_why(args) -> int:
    """Every judge reason for one criterion, across a snapshot's sheets.

    The question a low score actually raises is "low because of what", and until
    the reasons were stored the only way to answer it was to pay for another
    judging run. This is that answer, for free, from a snapshot you already have.
    """
    snapshot = regression.load_snapshot(args.snapshot)
    criterion = args.criterion
    sheets = [s for s in snapshot.get("sheets") or [] if criterion in (s.get("scores") or {})]
    if not sheets:
        print(f"no sheet in {Path(args.snapshot).name} carries '{criterion}'")
        known = sorted((snapshot.get("perCriterion") or {}))
        print("  criteria in this snapshot: " + ", ".join(known))
        return 2
    if not any(s.get("reasons") for s in sheets):
        print(f"{Path(args.snapshot).name} stores no judge reasons — it was taken\n"
              "before they were persisted. Take a new snapshot to get them.")
        return 2

    stats = (snapshot.get("perCriterion") or {}).get(criterion) or {}
    _header(f"{criterion} — {stats.get('mean', '?')} ±{stats.get('ci95', '?')} "
            f"in {snapshot['id']}")
    # Worst first: the point of reading reasons is to find the fixable defect,
    # and the sheets that scored lowest are where it is stated most plainly.
    for sheet in sorted(sheets, key=lambda s: s["scores"][criterion]):
        reason = (sheet.get("reasons") or {}).get(criterion) or "(no reason given)"
        print(f"\n  [{sheet['scores'][criterion]}] T{sheet['index']} {sheet.get('arm', '')}")
        print(f"      {reason}")
    return 0


def cmd_list(args) -> int:
    snapshots = regression.list_snapshots(args.scope)
    if not snapshots:
        print(f"no snapshots yet in {regression.snapshot_dir(args.scope)}")
        return 0
    pinned = regression.baseline_path(args.scope)
    _header(f"SNAPSHOTS — {regression.snapshot_dir(args.scope)}")
    print(f"  {'id':<26}{'mean':>6}{'sheets':>8}  rubric        model")
    for path in snapshots:
        try:
            snap = regression.load_snapshot(path)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  {path.name:<26}  unreadable: {exc}")
            continue
        mark = " *" if pinned and path.resolve() == Path(pinned).resolve() else "  "
        print(f"{mark}{path.stem:<26}{snap['summary']['mean']:>6.2f}"
              f"{snap['summary']['sheets']:>8}  "
              f"{snap['rubric']['fingerprint']}  "
              f"{(snap['environment'].get('model') or '?')[:28]}")
    print("\n  * = the pinned baseline"
          if pinned else "\n  (no baseline pinned — comparisons drift to the previous run)")
    return 0


def cmd_baseline(args) -> int:
    path = Path(args.snapshot)
    if not path.exists():
        path = regression.snapshot_dir(args.scope) / args.snapshot
    if not path.exists():
        print(f"no such snapshot: {args.snapshot}")
        return 2
    regression.set_baseline(path, args.scope)
    print(f"baseline for {args.scope or 'default'} pinned to {path.name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", default=None,
                        help="cohort key, as db.scope_key() spells it — snapshots "
                             "are kept per scope")
    parser.add_argument("--threshold", type=float, default=None,
                        help=f"points per topic before a move counts "
                             f"(default {regression.THRESHOLD})")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="snapshot the golden chapter and compare")
    run.add_argument("--repeats", type=int, default=3,
                     help="generations of the fixture; 1 measures the sampler, "
                          "not the pipeline")
    run.add_argument("--fixture", default=None, help="path to a golden fixture")
    run.add_argument("--label", default=None,
                     help="a name for this snapshot, e.g. 'after-seam-rewrite'")
    run.add_argument("--baseline", default=None,
                     help="compare against this snapshot instead of the pinned one")
    run.add_argument("--set-baseline", action="store_true",
                     help="pin this snapshot as the baseline afterwards")
    run.add_argument("--adaptive", action="store_true",
                     help="generate under the cohort's active adaptive state "
                          "(needs --scope and Supabase)")
    run.add_argument("--out", default=None, help="also write the snapshot here")

    comp = sub.add_parser("compare", help="compare two stored snapshots — free")
    comp.add_argument("baseline")
    comp.add_argument("candidate")
    comp.add_argument("--json", action="store_true", help="print the raw verdict")

    why = sub.add_parser("why", help="the judge's reasons for one criterion — free")
    why.add_argument("snapshot", help="path to a stored snapshot")
    why.add_argument("criterion", help="e.g. seam, book_order, explore_hook")

    sub.add_parser("list", help="every snapshot for this scope")

    base = sub.add_parser("baseline", help="pin the snapshot to compare against")
    base.add_argument("snapshot", help="a snapshot filename, or a path")

    args = parser.parse_args()
    if args.command == "run":
        sys.exit(asyncio.run(cmd_run(args)))
    elif args.command == "compare":
        sys.exit(cmd_compare(args))
    elif args.command == "why":
        sys.exit(cmd_why(args))
    elif args.command == "list":
        sys.exit(cmd_list(args))
    else:
        sys.exit(cmd_baseline(args))


if __name__ == "__main__":
    main()
