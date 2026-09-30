"""Has the material got better or worse since last time?

The activation gate (gate.py) answers a narrow version of this — "is THIS
candidate adaptive state worse than the one in force" — by generating both arms
right now, so the two differ by the state and nothing else. That is the right
design for gating an activation and the wrong one for tracking quality over
time, because the thing you want to compare against is not a state you can
re-run. It is the pipeline as it stood three weeks ago: different prompts,
possibly a different model, whatever the code was then.

You cannot regenerate that. So this stores SCORES rather than states, and
compares today's scored run against a stored one.

    snapshot   generate the golden chapter, score it blind, write the result
    compare    paired against a stored snapshot, per criterion
    baseline   the snapshot comparisons are made against, chosen deliberately

WHAT MAKES A COMPARISON VALID, AND WHY THAT IS THE HARD PART.

Storing numbers and subtracting them later is easy, and is how an eval harness
starts lying. Two scores are comparable only if the same rubric judged the same
material, and both of those change:

  * **The rubric changes.** RUBRIC is derived from SECTION_POLICY, so editing the
    generation contract edits the rubric with it. Every stored score was produced
    under the old wording, and subtracting across that edit measures the edit.
  * **The fixture changes.** Rebuilding the golden chapter re-sequences it, and
    new topics score differently for reasons that have nothing to do with
    quality.

Both are fingerprinted into every snapshot, and a comparison across a change in
either returns `incomparable` rather than a number. That verdict is the point of
this module: a harness that silently reports "+0.4, improved" after a rubric edit
is worse than no harness, because someone will believe it.

WHAT IS DELIBERATELY NOT HERE. No automatic action. This reports, and from the
CLI exits non-zero — it does not roll anything back, retune a prompt, or write an
adaptive state. An instrument that also moves the thing it measures cannot be
trusted to have measured it.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .evaluation import RUBRIC, mean_ci, methodology_fingerprint, paired_delta

SCHEMA = 1

# How far the mean may move before it is called a change rather than noise. The
# same default the activation gate uses, and for the same reason: half a rubric
# point per topic is about the smallest difference a human reading both sets
# would also call real.
THRESHOLD = float(os.environ.get("PREP_FLOW_REGRESSION_THRESHOLD",
                                 os.environ.get("PREP_FLOW_GATE_THRESHOLD", "0.25")))

EVAL_ROOT = Path(os.environ.get("PREP_FLOW_EVAL_DIR", "eval"))


# ── Fingerprints ─────────────────────────────────────────────────────────────

def _digest(value) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def rubric_fingerprint() -> str:
    """Changes whenever anything that decides what a score MEANS changes.

    Delegated to `evaluation.methodology_fingerprint`, which covers the rubric
    text AND the judge prompt. It covered only the rubric until the textbook
    excerpt was added to the prompt — a change that moved `concept_fidelity` from
    "does it sound confident and cite pages" to "is the fact on the page" while
    touching no criterion text, and would therefore have been invisible here.

    Over the full text, not just the keys, for the same reason: rewording "5 =
    the class re-enters the exact moment they left" moves every seam score ever
    produced, and a fingerprint over key names alone would call that comparable.
    """
    return methodology_fingerprint()


def fixture_fingerprint(fixture: dict) -> str:
    """Changes whenever the golden chapter is rebuilt into different topics.

    Over the topic spine and the generation config — the two things that decide
    what gets written. NOT over `reasoning`, which is cached and re-derived on
    its own schedule, and would churn the fingerprint without changing a word of
    the material.
    """
    return _digest({
        "topics": [{"index": t.get("index"), "topic": t.get("topic"),
                    "pageStart": t.get("page_start"), "pageEnd": t.get("page_end")}
                   for t in (fixture.get("topics") or [])],
        "config": fixture.get("config") or {},
        "grade": fixture.get("grade"), "subject": fixture.get("subject"),
        "chapterTitle": fixture.get("chapterTitle"),
    })


def _family_factor(tests: int, alpha: float = 0.05) -> float:
    """How much wider one interval must be when `tests` of them are read at once.

    The stored `ci95` is a 95% interval — right for reading one criterion, wrong
    for scanning ten and reacting to whichever moved most. This returns the ratio
    between the two-sided z at alpha/k and the z at alpha the interval was built
    with, so the existing number can be reused rather than recomputed from the
    raw scores.

    At ten criteria that is about 1.43x. Measured against a real pure-noise pair:
    `materials_realistic` moved 0.67 against a 0.61 spread and was flagged; the
    widened bar is 0.87 and correctly says nothing happened.
    """
    if tests < 2:
        return 1.0
    from statistics import NormalDist
    normal = NormalDist()
    return normal.inv_cdf(1 - alpha / (2 * tests)) / normal.inv_cdf(1 - alpha / 2)


def _environment(adaptive: dict, repeats: int, config: dict) -> dict:
    """What produced these numbers, beyond the code itself.

    Recorded because every one of these is a legitimate cause of a score moving,
    and a delta with no explanation attached invites the wrong one. The model
    especially: changing it moves the generator and the judge at the same time,
    in the same direction, and the harness cannot tell those apart.
    """
    try:
        # Through deps rather than by name: `ai` is `app.lib.ai` in a real
        # backend and deps is the only module that has to know which.
        from .deps import ai_module
        MODEL, FALLBACK_MODEL = ai_module.MODEL, ai_module.FALLBACK_MODEL
    except Exception:                                    # pragma: no cover
        MODEL = FALLBACK_MODEL = None
    return {
        "model": MODEL,
        "fallbackModel": FALLBACK_MODEL,
        "adaptiveVersion": (adaptive or {}).get("_version"),
        "adaptiveDirectives": sorted((adaptive or {}).get("sectionDirectives") or {}),
        "repeats": repeats,
        # Four of these five keys left DEFAULT_CONFIG with generation and the
        # repair loop, so a snapshot taken now would record them as null. The
        # list is kept exactly as it was rather than trimmed: it is a
        # FINGERPRINT, and every stored snapshot was fingerprinted against these
        # five names. Dropping one would make every snapshot on disk
        # incomparable with every snapshot taken after — which is the one thing
        # this function exists to prevent. (Moot until the generation stage has
        # a home again: `gate._run_arm` cannot produce an arm without it.)
        "config": {key: (config or {}).get(key) for key in
                   ("window_size", "target_topics", "include_visuals",
                    "max_repair_rounds", "simulate_learner")},
    }


# ── Taking a snapshot ────────────────────────────────────────────────────────

async def run_snapshot(fixture: dict, *, adaptive: dict = None, repeats: int = 3,
                       label: str = None, scope_key: str = None,
                       fixture_path: str = None) -> dict:
    """Generate the golden chapter `repeats` times, score every sheet, summarise.

    Generation and judging are the gate's own, imported rather than
    reimplemented. Two scorers that drifted apart would make every stored
    snapshot incomparable with every future one — the exact failure this module
    exists to prevent, arriving through the back door.
    """
    from .gate import _run_arm, _score_arm
    from .llm import gather_bounded

    adaptive = adaptive or {}
    sheets: list[dict] = []
    failures: list[str] = []

    runs = await gather_bounded(
        [_run_arm(fixture, adaptive, f"r{i}") for i in range(1, repeats + 1)],
        limit=3)
    for i, run in enumerate(runs, start=1):
        if isinstance(run, BaseException):
            # Recorded, not raised. A snapshot built from four of five repeats is
            # still a usable measurement; one that vanished because a single
            # request timed out is not.
            failures.append(f"repeat {i}: {run}")
            continue
        sheets += run

    if not sheets:
        raise RuntimeError("no sheets were generated: "
                           + "; ".join(failures or ["unknown"]))

    scored = await _score_arm(sheets, fixture, label or "snapshot")
    if not scored:
        raise RuntimeError("no sheets could be scored")

    means = [s["mean"] for s in scored]
    mean, sd, ci = mean_ci(means)

    per_criterion = {}
    for key in RUBRIC:
        values = [s["scores"][key] for s in scored if key in (s.get("scores") or {})]
        if values:
            c_mean, c_sd, c_ci = mean_ci(values)
            per_criterion[key] = {"mean": round(c_mean, 3), "sd": round(c_sd, 3),
                                  "ci95": round(c_ci, 3), "n": len(values)}

    per_topic: dict[str, dict] = {}
    for sheet in scored:
        entry = per_topic.setdefault(str(sheet["index"]),
                                     {"topic": sheet.get("topic"), "means": []})
        entry["means"].append(sheet["mean"])
    for entry in per_topic.values():
        entry["mean"] = round(sum(entry["means"]) / len(entry["means"]), 3)
        entry["n"] = len(entry.pop("means"))

    stamp = datetime.now(timezone.utc)
    return {
        "schema": SCHEMA,
        "id": stamp.strftime("%Y%m%dT%H%M%SZ"),
        "label": label,
        "createdAt": stamp.isoformat(),
        "scopeKey": scope_key,
        "fixture": {
            "path": str(fixture_path) if fixture_path else None,
            "fingerprint": fixture_fingerprint(fixture),
            "chapterTitle": fixture.get("chapterTitle"),
            "grade": fixture.get("grade"), "subject": fixture.get("subject"),
            "topics": len(fixture.get("topics") or []),
        },
        "rubric": {"fingerprint": rubric_fingerprint(), "criteria": sorted(RUBRIC)},
        "environment": _environment(adaptive, repeats, fixture.get("config") or {}),
        "summary": {
            "mean": round(mean, 3), "sd": round(sd, 3), "ci95": round(ci, 3),
            "sheets": len(scored),
            "wouldUseAsIs": round(
                sum(1 for s in scored if s.get("wouldUseAsIs")) / len(scored), 3),
        },
        "perCriterion": per_criterion,
        "perTopic": per_topic,
        # Every judged sheet, so a delta can be argued with rather than believed —
        # INCLUDING the judge's one-line reason per criterion.
        #
        # Those reasons were left out of the first version on the theory that
        # `weakest` covered it and prose would bloat the file. Both halves were
        # wrong. Chasing why `book_order` sat at 2.88 meant paying for a separate
        # judging run purely to recover them, and when they arrived they were the
        # only thing in the exercise that actually explained a score: "ends with a
        # 'shape hunt' which is a closed activity, not an open question" is a
        # fixable defect, and 3.00 is not. The bloat is about 1KB per sheet.
        "sheets": [{"index": s["index"], "topic": s.get("topic"), "arm": s.get("arm"),
                    "mean": s["mean"], "scores": s["scores"],
                    "reasons": s.get("reasons") or {},
                    "weakest": s.get("weakest"), "wouldUseAsIs": s.get("wouldUseAsIs")}
                   for s in scored],
        "failures": failures,
    }


# ── Comparing ────────────────────────────────────────────────────────────────

def comparable(baseline: dict, candidate: dict) -> list[str]:
    """Everything that makes these two sets of numbers not subtractable."""
    problems = []
    if baseline.get("schema") != candidate.get("schema"):
        problems.append(
            f"snapshot schema differs ({baseline.get('schema')} vs "
            f"{candidate.get('schema')})")
    b_rubric = (baseline.get("rubric") or {}).get("fingerprint")
    c_rubric = (candidate.get("rubric") or {}).get("fingerprint")
    if b_rubric != c_rubric:
        problems.append(
            f"the rubric changed since the baseline ({b_rubric} -> {c_rubric}) — "
            f"every stored score was produced under different wording, so a delta "
            f"would measure the edit rather than the material")
    b_fix = (baseline.get("fixture") or {}).get("fingerprint")
    c_fix = (candidate.get("fixture") or {}).get("fingerprint")
    if b_fix != c_fix:
        problems.append(
            f"the golden fixture changed ({b_fix} -> {c_fix}) — the two runs did "
            f"not write about the same topics")
    return problems


def compare(baseline: dict, candidate: dict, threshold: float = None) -> dict:
    """Paired verdict, per criterion, or a refusal to give one.

    Paired on topic index via `paired_delta`, because both snapshots wrote the
    same golden topics and with a handful of them topic difficulty otherwise
    swamps the effect. A verdict needs the change to be BOTH past the threshold
    and separated from zero — requiring only one produces a harness that either
    never fires or fires at random, and both are worse than none.
    """
    bar = THRESHOLD if threshold is None else float(threshold)

    blocked = comparable(baseline, candidate)
    if blocked:
        return {
            "verdict": "incomparable", "regressed": False,
            "reason": "; ".join(blocked), "problems": blocked,
            "baselineId": baseline.get("id"), "candidateId": candidate.get("id"),
        }

    delta, delta_ci, pairs = paired_delta(baseline.get("sheets") or [],
                                          candidate.get("sheets") or [])

    # A criterion is only "moved" when it clears the threshold AND its own noise,
    # widened for the fact that ten of them are being tested at once.
    #
    # The threshold alone is calibrated for the paired mean, which averages over
    # every sheet in both snapshots. A single criterion has far less behind it,
    # and the judge scores in whole numbers 1-5 — so at eight sheets, one sheet
    # moving one point shifts that criterion's mean by 0.125, and two sheets
    # clear a 0.25 bar without anything having changed. Measured, not assumed: a
    # snapshot taken twice with NOTHING altered flagged four criteria improved
    # and one regressed, `seam` swinging +0.50 on noise alone.
    #
    # Using each snapshot's stored interval fixed that — and then a 3-repeat pair
    # with nothing changed still flagged `materials_realistic` at -0.67 against a
    # combined spread of 0.61. That is not a bug in the interval; it is what
    # testing ten criteria at 95% each means. The chance of at least one false
    # flag per comparison is roughly 40%, and a harness that cries wolf twice a
    # week is one nobody reads.
    #
    # So the separation required is widened by the number of criteria under test,
    # Bonferroni-style: the same interval, re-derived at alpha/k instead of alpha.
    # A real regression repeats across sheets, has a narrow interval, and clears
    # the wider bar anyway.
    family_factor = _family_factor(len(RUBRIC))
    per_criterion = {}
    for key in RUBRIC:
        base = (baseline.get("perCriterion") or {}).get(key) or {}
        cand = (candidate.get("perCriterion") or {}).get(key) or {}
        if base.get("mean") is None or cand.get("mean") is None:
            continue
        # NOT named `delta`: that is the paired figure computed above, and the
        # whole verdict is decided from it further down. Shadowing it here left
        # the function reporting the last criterion's movement as the overall
        # result — a run that had already printed "stable +0.12" reported
        # "improvement +0.38" on the same two files.
        criterion_delta = round(cand["mean"] - base["mean"], 3)
        spread = round((((base.get("ci95") or 0.0) ** 2
                         + (cand.get("ci95") or 0.0) ** 2) ** 0.5)
                       * family_factor, 3)
        per_criterion[key] = {
            "baseline": base["mean"], "candidate": cand["mean"],
            "delta": criterion_delta, "spread": spread,
            "separated": abs(criterion_delta) > spread,
        }
    moved = {k: v for k, v in per_criterion.items()
             if abs(v["delta"]) >= bar and v["separated"]}
    regressed = sorted((k for k, v in moved.items() if v["delta"] < 0),
                       key=lambda k: per_criterion[k]["delta"])
    improved = sorted((k for k, v in moved.items() if v["delta"] > 0),
                      key=lambda k: -per_criterion[k]["delta"])

    # `pairs >= 2` rather than a positive-interval test. A change that repeats
    # identically on every topic has zero variance, so the interval is zero — and
    # a test requiring ci > 0 would score the most confident evidence available
    # as unmeasurable. One pair genuinely cannot separate anything; that is what
    # the guard should say. (The same reasoning, and the same bug, as gate.py.)
    separated = pairs >= 2 and abs(delta) > delta_ci

    if delta < -bar and separated:
        verdict, regression = "regression", True
        reason = (f"scores {abs(delta):.2f} lower per topic (±{delta_ci:.2f}, "
                  f"{pairs} paired topics) — past the {bar} threshold and "
                  f"separated from zero")
        if regressed:
            reason += f". Worst: {', '.join(regressed[:3])}"
    elif delta > bar and separated:
        verdict, regression = "improvement", False
        reason = (f"scores {delta:+.2f} higher per topic (±{delta_ci:.2f}, "
                  f"{pairs} paired topics)")
        if improved:
            reason += f". Best: {', '.join(improved[:3])}"
    else:
        verdict, regression = "stable", False
        reason = (f"paired difference {delta:+.2f} ± {delta_ci:.2f} over {pairs} "
                  f"topics — inside the noise, no evidence either way")

    # A criterion can regress hard while the overall mean barely moves: one of
    # ten dropping a full point shifts the mean by 0.1, which is inside the
    # threshold by design. "The average held" is the wrong conclusion to draw
    # from that, so a criterion that cleared the widened bar fails the check even
    # when the overall verdict is `stable`. It has to repeat across sheets to get
    # that far, which is what makes it worth failing on.
    if regressed and not regression:
        regression = True
        reason += (f". But {', '.join(regressed[:3])} regressed on its own — "
                   f"consistently enough to clear the noise, while the "
                   f"ten-criterion mean absorbed it")

    return {
        "verdict": verdict, "regressed": regression, "reason": reason,
        "delta": round(delta, 3), "ci95": round(delta_ci, 3), "pairs": pairs,
        "threshold": bar,
        "means": {"baseline": (baseline.get("summary") or {}).get("mean"),
                  "candidate": (candidate.get("summary") or {}).get("mean")},
        "perCriterion": per_criterion,
        # Named even when the overall verdict is `stable`. One criterion can fall
        # hard while another rises and the mean barely moves, and "the average
        # held" is exactly the wrong thing to conclude from that.
        "regressedCriteria": regressed, "improvedCriteria": improved,
        "baselineId": baseline.get("id"), "candidateId": candidate.get("id"),
        "environmentChanged": _environment_diff(baseline, candidate),
    }


def _environment_diff(baseline: dict, candidate: dict) -> dict:
    """What else moved between the two runs.

    Not a blocker — a model or adaptive-state change is a legitimate thing to
    measure the effect of, and refusing to compare across one would make the
    harness useless exactly when it is most needed. But it must be ON THE REPORT,
    because "the score dropped" and "the score dropped and we changed the model"
    are different findings and only one of them is about the prompt.
    """
    before = baseline.get("environment") or {}
    after = candidate.get("environment") or {}
    changed = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed[key] = {"baseline": before.get(key), "candidate": after.get(key)}
    return changed


# ── Storage ──────────────────────────────────────────────────────────────────

def _slug(scope_key: Optional[str]) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in (scope_key or "default"))


def snapshot_dir(scope_key: str = None) -> Path:
    return EVAL_ROOT / _slug(scope_key)


def save_snapshot(snapshot: dict, scope_key: str = None) -> Path:
    directory = snapshot_dir(scope_key)
    directory.mkdir(parents=True, exist_ok=True)
    label = f"_{_slug(snapshot['label'])}" if snapshot.get("label") else ""
    path = directory / f"{snapshot['id']}{label}.json"
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path


def list_snapshots(scope_key: str = None) -> list[Path]:
    directory = snapshot_dir(scope_key)
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.json") if p.name != "BASELINE.json")


def load_snapshot(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _baseline_pointer(scope_key: str = None) -> Path:
    return snapshot_dir(scope_key) / "BASELINE.json"


def set_baseline(path, scope_key: str = None) -> Path:
    """Point the baseline at a snapshot.

    A pointer rather than a copy: two files that are supposed to be the same
    snapshot will eventually not be, and then the harness has two answers to
    "what are we comparing against".
    """
    pointer = _baseline_pointer(scope_key)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"snapshot": Path(path).name}, indent=2),
                       encoding="utf-8")
    return pointer


def baseline_path(scope_key: str = None) -> Optional[Path]:
    """The chosen baseline, or the most recent snapshot when none was chosen.

    Falling back to "most recent" makes the harness useful from the second run
    onwards with no setup, and a drifting baseline is the right default for
    catching a sudden break. Pinning one with `set_baseline` is what you do to
    catch slow erosion instead: against a drifting baseline, material that loses
    0.05 a week forever never once regresses.
    """
    pointer = _baseline_pointer(scope_key)
    if pointer.exists():
        try:
            name = json.loads(pointer.read_text(encoding="utf-8")).get("snapshot")
            candidate = snapshot_dir(scope_key) / str(name)
            if candidate.exists():
                return candidate
            print(f"[prep_flow:regression] BASELINE.json points at {name}, which is "
                  f"missing — falling back to the most recent snapshot")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[prep_flow:regression] unreadable BASELINE.json ({exc}) — "
                  f"falling back to the most recent snapshot")
    snapshots = list_snapshots(scope_key)
    return snapshots[-1] if snapshots else None
