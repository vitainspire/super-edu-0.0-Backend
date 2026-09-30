"""Blind rubric scoring for prep sheets — the harness that makes "did v2 beat v1"
an answerable question.

The feedback loop can change the generation prompt, but until now nothing could
say whether a change helped. The only signal was the teacher's YES/SOMEWHAT/NO,
which is also the thing the optimizer maximises — so the loop was grading its own
homework, with no independent check. That is the arrangement in which a directive
like "state the worked answer" gets satisfied by stating a trivial answer, and the
metric improves while the teaching gets worse.

This scores sheets against a rubric derived from SECTION_POLICY, and it scores
them BLIND:

  * one judge call per sheet, so nothing is scored by comparison to a sibling —
    no anchoring, and no "this one must be the improved version" reasoning;
  * the judge is never told which adaptive state produced the sheet, or that
    variants exist at all;
  * sheets are shuffled deterministically before judging, so ordering cannot
    correlate with variant.

It is a proxy, not truth. A model scoring prose written by a model shares its
blind spots, and the only real evidence is a teacher in a classroom. What this
catches is the regression a subjective read misses — a section that got measurably
thinner while its directive was "working".

    python -m prep_flow.eval_cli data/generated/compare_v0_cold.md \
                             data/generated/compare_v1.md data/generated/compare_v2.md
"""
import argparse
import asyncio
from collections import Counter
import json
import re
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

from prep_flow.llm import gather_bounded  # noqa: E402

# The rubric, the judge prompt and the interval maths all live in
# prep_flow/evaluation.py, and this module imports them rather than keeping its
# own. It kept its own until now, and the two had drifted exactly as far as you
# would expect: this file scored NINE criteria to the shared rubric's ten
# (`book_order` was missing outright, so a mean here was over a different
# denominator and not comparable with the gate's at all), and its
# `concept_discovery` was a generation behind — it still penalised a sheet for
# naming the term first, which the shared version had already been corrected to
# accept where the textbook itself names first.
#
# evaluation.py's own docstring says it was factored out of this file so that
# "the regression gate scores a candidate adaptive state with exactly the same
# rubric a human runs by hand". That was true of the copy and not of the
# original; this import is what makes the sentence true.
from prep_flow.evaluation import (RUBRIC, judge_sheet,  # noqa: E402
                                  mean_ci as _mean_ci)


def split_sheets(path: Path) -> list[dict]:
    """One rendered chapter document -> its per-topic sheets."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"^## T(\d+) · (.+?)$", text, flags=re.M)
    sheets = []
    # re.split yields [preamble, idx, title, body, idx, title, body, …]
    for i in range(1, len(parts) - 2, 3):
        index, title, body = int(parts[i]), parts[i + 1].strip(), parts[i + 2]
        body = re.split(r"^---\s*$", body, flags=re.M)[0]
        # Repeats of one variant are grouped by stripping a trailing _r<n>, so
        # five runs of v1 average together instead of appearing as five variants.
        variant = re.sub(r"_r\d+$", "", path.stem)
        sheets.append({"index": index, "topic": title, "sheet": body.strip(),
                       "variant": variant, "run": path.stem, "source": str(path)})
    return sorted(sheets, key=lambda s: s["index"])


def _previous_explore(sheets: list[dict], index: int) -> str:
    """The prior sheet's Explore, so the seam criterion can actually be judged.

    Without it a judge can only ask "does this Refresher look like a recap", which
    is the exact confusion the seam rule exists to prevent.
    """
    prior = next((s for s in sheets if s["index"] == index - 1), None)
    if not prior:
        # None rather than a sentence: judge_sheet writes the "this is the first
        # sheet" framing itself, and two modules describing the same situation in
        # their own words is how the copies drifted in the first place.
        return None
    match = re.search(r"^#### Explore\b(.*?)(?=^####|\Z)", prior["sheet"], re.M | re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else None


async def judge(sheet: dict, sheets: list[dict], grade: str, subject: str) -> dict:
    """One sheet, scored by the shared judge.

    This reads RENDERED CHAPTER MARKDOWN, so two of the ten criteria have no
    evidence available and judge_sheet abstains at 3 for both: `book_order` needs
    the textbook's own explanatory order, and `concept_fidelity` needs the pages
    themselves. Neither survives being rendered to Markdown.

    That is a real limit of comparing files rather than runs — the fixture
    `prep_flow/regression_cli.py` scores from carries both — and the report says so
    rather than letting two abstentions read as a weak result.
    """
    result = await judge_sheet(
        sheet["sheet"][:9000],
        topic=sheet["topic"], grade=grade, subject=subject,
        previous_explore=_previous_explore(sheets, sheet["index"]),
        label=f"{sheet['variant']}:T{sheet['index']}")
    return {**sheet, **result}


def report(results: list[dict]) -> None:
    variants = sorted({r["variant"] for r in results})
    print(f"\n{'='*78}\nBLIND RUBRIC SCORES — {len(results)} sheets, "
          f"{len(variants)} variant(s)\n{'='*78}")
    # Said up front, because two criteria pinned at 3.00 read as a mediocre
    # result and are actually an abstention.
    print("  NOTE: concept_fidelity and book_order abstain at 3 here — rendered\n"
          "  Markdown carries neither the textbook pages nor the book's own\n"
          "  explanatory order. prep_flow/regression_cli.py scores from the fixture,\n"
          "  which carries both, and judges them properly.")

    width = max(len(v) for v in variants) + 2
    print(f"\n{'criterion':26}" + "".join(f"{v:>{width}}" for v in variants))
    print("-" * (26 + width * len(variants)))
    for key in RUBRIC:
        row = f"{key:26}"
        for v in variants:
            vals = [r["scores"][key] for r in results if r["variant"] == v]
            row += f"{sum(vals)/len(vals):>{width}.2f}" if vals else f"{'-':>{width}}"
        print(row)
    print("-" * (26 + width * len(variants)))
    row = f"{'MEAN':26}"
    for v in variants:
        vals = [r["mean"] for r in results if r["variant"] == v]
        row += f"{sum(vals)/len(vals):>{width}.2f}"
    print(row)
    row = f"{'would use as-is':26}"
    for v in variants:
        vals = [r["wouldUseAsIs"] for r in results if r["variant"] == v]
        row += f"{sum(vals)}/{len(vals):<{width-4}}"
    print(row)

    print(f"\n{'weakest criterion, counted':26}")
    for v in variants:
        counted = Counter(str(r.get("weakest") or "?")[:30]
                          for r in results if r["variant"] == v)
        print(f"  {v:24} " + ", ".join(f"{k} x{n}" for k, n in counted.most_common(3)))

    print(f"\n{'mean ± 95% CI':26}")
    stats = {}
    for v in variants:
        vals = [r["mean"] for r in results if r["variant"] == v]
        mean, sd, ci = _mean_ci(vals)
        stats[v] = (mean, sd, ci, len(vals))
        print(f"  {v:24} {mean:.3f} ± {ci:.3f}   (sd {sd:.3f}, n={len(vals)})")

    # Paired by topic. Every variant generates the SAME topics, so pairing removes
    # topic difficulty from the comparison — without it a variant that happened to
    # draw an easier topic looks better than it is.
    baseline = variants[0]
    if len(variants) > 1:
        print(f"\n{'paired by topic vs ' + baseline:26}")
        topics = sorted({r["index"] for r in results})
        for v in variants[1:]:
            deltas = []
            for t in topics:
                a = [r["mean"] for r in results if r["variant"] == baseline and r["index"] == t]
                b = [r["mean"] for r in results if r["variant"] == v and r["index"] == t]
                if a and b:
                    deltas.append(sum(b) / len(b) - sum(a) / len(a))
            if deltas:
                mean, sd, ci = _mean_ci(deltas)
                verdict = ("no detectable difference" if abs(mean) <= ci or ci == 0
                           else ("BETTER" if mean > 0 else "WORSE"))
                print(f"  {v:24} {mean:+.3f} ± {ci:.3f} per topic   -> {verdict}")

    print(f"\n{'='*78}")
    per_variant = min(s[3] for s in stats.values())
    means = [s[0] for s in stats.values()]
    spread = max(means) - min(means)
    widest_ci = max(s[2] for s in stats.values())
    print(f"spread between best and worst variant: {spread:.3f} points")
    print(f"widest 95% CI: ±{widest_ci:.3f}")

    if spread <= widest_ci:
        print("\nTHE VARIANTS ARE NOT DISTINGUISHABLE. The spread is inside the noise,")
        print("so this run is evidence of NO EFFECT at this sample size — not evidence")
        print("that the smaller number is worse.")
    if per_variant < 15:
        print(f"\n{per_variant} sheets per variant is thin. Generation runs at temperature")
        print("0.6; ~20 per variant is where a 0.2-point effect becomes visible.")
    print("\nA model scoring prose written by a model shares its blind spots. This")
    print("catches regressions a subjective read misses; it does not establish that")
    print("a sheet teaches. The only real evidence is a teacher in a classroom.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="rendered chapter Markdown files")
    parser.add_argument("--grade", default="3")
    parser.add_argument("--subject", default="Maths")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default=None, help="write the raw scores as JSON")
    args = parser.parse_args()

    by_variant = {}
    for name in args.files:
        path = Path(name)
        if not path.exists():
            parser.error(f"no such file: {path}")
        sheets = split_sheets(path)
        if not sheets:
            parser.error(f"{path} contains no '## T<n> · <title>' sheets")
        by_variant[path.stem] = sheets
        print(f"{path.name}: {len(sheets)} sheet(s) -> variant {sheets[0]['variant']}")

    jobs = [(s, sheets) for sheets in by_variant.values() for s in sheets]
    # Deterministic shuffle by a content hash, so judging order cannot correlate
    # with variant, and re-running scores the same sheets in the same order.
    jobs.sort(key=lambda j: hash((j[0]["index"], j[0]["variant"])) % 9973)

    print(f"\njudging {len(jobs)} sheets blind (one call each, order shuffled)…")
    results = await gather_bounded(
        [judge(s, sheets, args.grade, args.subject) for s, sheets in jobs],
        limit=args.concurrency)

    ok = [r for r in results if not isinstance(r, BaseException)]
    for r in results:
        if isinstance(r, BaseException):
            print(f"  a sheet could not be judged: {r}")
    if not ok:
        print("nothing was judged successfully")
        return

    report(ok)
    if args.out:
        Path(args.out).write_text(json.dumps(ok, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
