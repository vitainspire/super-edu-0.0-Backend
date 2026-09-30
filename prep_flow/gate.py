"""The activation gate: does a candidate adaptive state make the material worse?

CURRENTLY UNRUNNABLE, and deliberately loud about it. This gate generates the
golden chapter twice and compares the two arms, and generation left this package
with the node split — `_run_arm()` raises a RuntimeError saying exactly that
rather than comparing an empty arm against another empty arm. Everything else
here (the fixture, the pairing, the repeats, the rubric, the statistics) is
unchanged and correct; it needs one import re-pointed at wherever the generation
stage lands downstream of Node 2.

A canary deploy for prompts. Before a new state goes live, generate the same
golden chapter under the OLD state and the NEW one, score both blind, and refuse
to activate a state that scores materially worse.

Three things make the comparison mean anything:

  * **A frozen golden fixture.** Sequencing and context assembly are run once,
    ever, and stored. If they re-ran per gate the topics would differ between
    arms and the comparison would measure sequencing variance instead of the
    state.
  * **Paired by topic.** Both arms generate the same topics, so pairing removes
    topic difficulty. With four topics that difference otherwise swamps the
    effect being measured.
  * **Repeats.** Generation runs at temperature 0.6. One sheet per arm measures
    the sampler, not the state.

The gate is OFF by default and costs real money when on — roughly $0.03 per
generation run plus $0.002 per judged sheet, so the default 3 repeats over a
4-topic fixture is about $0.25 per activation. That is cheap against shipping a
regression to every teacher of a grade, and not cheap enough to run casually.

A verdict of `inconclusive` is the common one and is NOT a failure: it means the
arms are indistinguishable at this sample size, which is what "this state changes
nothing measurable" looks like.
"""
import json
import os
from pathlib import Path
from typing import Optional

from . import render
from .evaluation import RUBRIC, judge_sheet, mean_ci, paired_delta
from .llm import gather_bounded
from .sections import section_text
from .state import DEFAULT_CONFIG

# How much worse a candidate may score before it is refused. Half a rubric point
# on the paired mean, AND the interval must exclude zero — a wide-but-negative
# result is noise, not a regression, and blocking on it would make the gate
# refuse states at random.
REGRESSION_THRESHOLD = float(os.environ.get("PREP_FLOW_GATE_THRESHOLD", "0.25"))


def enabled() -> bool:
    return (os.environ.get("PREP_FLOW_ACTIVATION_GATE", "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def golden_path(scope_key: str = None) -> Path:
    """Where the frozen fixture lives. One per cohort, or a shared default."""
    root = Path(os.environ.get("PREP_FLOW_GOLDEN_DIR", "golden"))
    if scope_key:
        safe = "".join(ch if ch.isalnum() else "_" for ch in scope_key)
        candidate = root / f"{safe}.json"
        if candidate.exists():
            return candidate
    return root / "default.json"


async def build_golden(*, chapter_markdown: str, chapter_title: str, grade: str,
                       subject: str, chapter_number: int = None,
                       page_start: int = None, page_end: int = None,
                       chapter_figures: list = None,
                       topics: int = 4, out: Path = None) -> dict:
    """Freeze a fixture: sequence and assemble ONCE, then store the result.

    Run deliberately and rarely. Everything after this reuses the stored topics,
    so a gate run costs only generation and judging — and, more importantly, two
    gate runs a month apart compare the same material.
    """
    from .agents.context_assembly import context_assembly_node
    from .agents.move_extraction import move_extraction_node
    from .agents.reasoning import reasoning_node
    from .agents.sequencing import sequencing_node

    config = {**DEFAULT_CONFIG, "target_topics": topics, "min_topics": topics,
              "max_topics": topics, "window_size": min(3, topics)}
    state = {
        "run_id": None, "school_id": None, "class_id": None,
        "grade": str(grade), "subject": subject,
        "chapter_title": chapter_title, "chapter_number": chapter_number,
        "chapter_markdown": chapter_markdown,
        "page_start": page_start, "page_end": page_end,
        "chapter_figures": chapter_figures or [],
        "teacher_settings": {"duration": 30, "classSize": 40, "resourceLevel": 0,
                             "language": "English", "learningObjective": "new lesson",
                             "teachingStyle": "interactive"},
        "config": config, "status": "running", "materials": {}, "plans": {},
        "selections": {}, "issues": {}, "metrics": {}, "errors": [],
    }
    state.update(await sequencing_node(state) or {})
    if not state.get("topics"):
        raise ValueError("could not sequence the golden chapter: "
                         + "; ".join(state.get("errors") or ["unknown"]))
    # Between sequencing and context assembly, exactly as the graph orders them.
    # Missed when move extraction was added to the pipeline, and the omission is
    # not visible in a generated sheet — it shows up as the `book_order`
    # criterion scoring a flat 3 on every sheet forever, because judge_sheet is
    # told to score 3 when a topic carries no moves. A criterion pinned to a
    # constant does not just measure nothing: it dilutes every real movement in
    # the other nine by a tenth.
    state.update(await move_extraction_node(state) or {})
    state.update(await context_assembly_node(state) or {})
    # Frozen into the fixture with the topics, not re-derived per arm. Reasoning
    # depends on the textbook alone, so both arms of a gate run must see the same
    # chain — otherwise the two generations differ by something other than the
    # adaptive state, which is the only variable the gate exists to measure.
    state.update(await reasoning_node(state) or {})

    fixture = {
        "grade": str(grade), "subject": subject,
        "chapterTitle": chapter_title, "chapterNumber": chapter_number,
        "chapterArc": state.get("sequencing_note"),
        "engagementGuidance": state.get("engagement_guidance"),
        "config": config,
        "teacherSettings": state["teacher_settings"],
        "topics": state["topics"],
        "reasoning": state.get("reasoning") or {},
        # Recorded so a fixture can say whether its topics carry the book's own
        # explanatory order. A fixture frozen before move extraction existed has
        # `movesExtracted: 0`, which is the difference between "this chapter has
        # no printed activities" and "this fixture is too old to know".
        "movesExtracted": sum(1 for t in state["topics"] if t.get("moves")),
    }
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
    return fixture


def _rekey_chain(reasoning: dict) -> dict:
    """JSON turns the chain's integer topic indices into strings; put them back.

    A silently string-keyed chain does not raise anywhere — every lookup simply
    misses, and the arm generates without the knowledge it was supposed to have.
    """
    chain = {}
    for raw, entry in (reasoning.get("chain") or {}).items():
        try:
            chain[int(raw)] = entry
        except (TypeError, ValueError):
            continue
    return {**reasoning, "chain": chain} if chain else reasoning


def load_golden(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"no golden fixture at {path}. Build one with:\n"
            f"  python -m prep_flow.cli golden --folder <textbook> --chapter 2 "
            f"--grade 3 --subject Maths --out {path}")
    return json.loads(path.read_text(encoding="utf-8"))


async def _run_arm(fixture: dict, adaptive: dict, label: str) -> list[dict]:
    """One generation of the golden chapter under one adaptive state.

    UNAVAILABLE SINCE THE NODE SPLIT, and it fails here rather than anywhere
    later on purpose. This gate's whole method is to generate the same golden
    chapter twice and score both arms blind — and generation left this package
    when the master orchestration was split into nodes (`_deferred/README.md`).
    Without it there is nothing to compare, and a gate that silently compared
    nothing would activate every candidate state, which is worse than a gate
    that is off.

    The rest of this module is intact and correct: the fixture, the pairing, the
    repeats, the rubric and the paired-delta statistics are all unchanged, and
    the arm below is the only part that needs a generator. Re-point this import
    at wherever the generation stage lands downstream of Node 2 and the gate
    works again.
    """
    from .agents.activity_selection import activity_selection_node
    from .agents.planning import planning_node
    try:
        from .agents.generation import generation_node
    except ImportError as exc:
        raise RuntimeError(
            "the activation gate needs the generation stage, which is no longer "
            "part of this package. Node 1 (prep_flow) ends at the academic "
            "contract; generation runs downstream of Node 2 and its module is "
            "archived at _deferred/generation.py. Point _run_arm() at the "
            "generation stage's new home, or leave PREP_FLOW_ACTIVATION_GATE "
            "off until it has one."
        ) from exc

    state = {
        "run_id": None, "school_id": None, "class_id": None,
        "grade": fixture["grade"], "subject": fixture["subject"],
        "chapter_title": fixture["chapterTitle"],
        "chapter_number": fixture.get("chapterNumber"),
        "chapter_arc": fixture.get("chapterArc"),
        "engagement_guidance": fixture.get("engagementGuidance"),
        "teacher_settings": fixture["teacherSettings"],
        "config": fixture["config"], "topics": fixture["topics"],
        # `or {}` rather than required: fixtures frozen before the reasoning node
        # existed are still valid gate inputs, and everything downstream treats
        # absent reasoning as "not available" rather than as an error.
        "reasoning": _rekey_chain(fixture.get("reasoning") or {}),
        "adaptive_state": adaptive, "cursor": 0, "repair_round": 0,
        "materials": {}, "plans": {}, "selections": {}, "issues": {},
        "metrics": {}, "errors": [],
    }

    def merge(delta):
        for key, value in (delta or {}).items():
            if key in ("materials", "plans", "selections") and isinstance(value, dict):
                state[key] = {**state[key], **value}
            elif value is not None and key not in ("errors", "metrics"):
                state[key] = value

    merge(await planning_node(state))
    merge(await activity_selection_node(state))
    while int(state.get("cursor") or 0) < len(state["topics"]):
        merge(await generation_node(state))

    by_index = {t["index"]: t for t in fixture["topics"]}
    return [{"index": i, "topic": by_index.get(i, {}).get("topic", ""),
             "material": m, "arm": label}
            for i, m in sorted(state["materials"].items())]


async def _score_arm(sheets: list[dict], fixture: dict, label: str) -> list[dict]:
    by_index = {s["index"]: s for s in sheets}
    # The textbook pages each topic was written from, carried on the frozen
    # fixture. Without them `concept_fidelity` is judged by a reader who has never
    # seen the book, and what it can actually check is whether the sheet sounds
    # confident and carries (Page N) markers — which a fluent invention also does.
    excerpts = {t["index"]: t.get("excerpt") for t in fixture.get("topics") or []}
    jobs = []
    for sheet in sheets:
        previous = by_index.get(sheet["index"] - 1)
        jobs.append(judge_sheet(
            render.render_topic(sheet["material"],
                                {"index": sheet["index"], "topic": sheet["topic"]},
                                show_handoff=False),
            topic=sheet["topic"], grade=fixture["grade"], subject=fixture["subject"],
            previous_explore=(section_text(previous["material"], "explore")
                              if previous else None),
            book_moves=(sheet["material"] or {}).get("bookMoves"),
            excerpt=excerpts.get(sheet["index"]),
            label=f"{label}:T{sheet['index']}"))
    scored = await gather_bounded(jobs, limit=4)
    # Judging failures used to be dropped in silence, which turned "the judge
    # errored on every sheet" into the same empty list as "there were no sheets"
    # — and the caller could only say "no sheets could be scored", which names
    # the symptom and hides the cause. Reported once with a count, so a transient
    # single failure stays quiet and a systematic one is impossible to miss.
    failed = [r for r in scored if isinstance(r, BaseException)]
    if failed:
        print(f"[prep_flow:gate] {len(failed)}/{len(scored)} sheet(s) could not be "
              f"judged in arm '{label}': {failed[0]}")
    return [{**sheet, **result} for sheet, result in zip(sheets, scored)
            if not isinstance(result, BaseException)]


async def run_gate(previous_state: dict, candidate_state: dict, *,
                   fixture: dict, repeats: int = 3) -> dict:
    """Generate and score both arms; return a verdict.

    `pass` — the candidate is not materially worse, activate it.
    `regression` — measurably worse; do not activate.
    `inconclusive` — indistinguishable at this sample size. Treated as a pass by
      callers, because refusing every state the gate cannot separate would mean
      refusing almost all of them: no-detectable-difference is the normal result.
    """
    arms = {"baseline": previous_state or {}, "candidate": candidate_state or {}}
    generated: dict[str, list[dict]] = {"baseline": [], "candidate": []}

    for arm, adaptive in arms.items():
        runs = await gather_bounded(
            [_run_arm(fixture, adaptive, f"{arm}_r{i}") for i in range(1, repeats + 1)],
            limit=3)
        for run in runs:
            if isinstance(run, BaseException):
                print(f"[prep_flow:gate] a {arm} run failed: {run}")
                continue
            generated[arm] += run

    scored: dict[str, list[dict]] = {}
    for arm, sheets in generated.items():
        scored[arm] = await _score_arm(sheets, fixture, arm) if sheets else []

    if not scored["baseline"] or not scored["candidate"]:
        return {"verdict": "inconclusive", "activate": True,
                "reason": "one arm produced no scorable sheets; the gate cannot "
                          "judge and must not block on its own failure",
                "arms": {a: len(s) for a, s in scored.items()}}

    summary = {}
    for arm, sheets in scored.items():
        mean, sd, ci = mean_ci([s["mean"] for s in sheets])
        summary[arm] = {"mean": round(mean, 3), "sd": round(sd, 3),
                        "ci95": round(ci, 3), "sheets": len(sheets)}

    delta, delta_ci, pairs = paired_delta(scored["baseline"], scored["candidate"])
    per_criterion = {}
    for key in RUBRIC:
        base = [s["scores"][key] for s in scored["baseline"]]
        cand = [s["scores"][key] for s in scored["candidate"]]
        per_criterion[key] = {
            "baseline": round(sum(base) / len(base), 2),
            "candidate": round(sum(cand) / len(cand), 2),
            "delta": round(sum(cand) / len(cand) - sum(base) / len(base), 2),
        }

    # A regression must be BOTH big enough to matter and separated from zero.
    # Requiring only one of those produces a gate that either never fires or
    # fires at random, and both are worse than no gate.
    #
    # The pairs>=2 guard replaces an earlier `delta_ci > 0`, which was exactly
    # backwards: a regression that repeats identically on every topic has zero
    # variance, so ci is 0, and the old condition scored the most confident
    # evidence available as unmeasurable. One pair genuinely cannot separate
    # anything, so that is what the guard should say.
    separated = pairs >= 2 and abs(delta) > delta_ci
    if delta < -REGRESSION_THRESHOLD and separated:
        verdict, activate = "regression", False
        reason = (f"the candidate scores {abs(delta):.2f} points lower per topic "
                  f"(±{delta_ci:.2f}, {pairs} paired topics) — past the "
                  f"{REGRESSION_THRESHOLD} threshold and separated from zero")
    elif separated and delta > REGRESSION_THRESHOLD:
        verdict, activate = "pass", True
        reason = (f"the candidate scores {delta:+.2f} points higher per topic "
                  f"(±{delta_ci:.2f}, {pairs} paired topics)")
    else:
        verdict, activate = "inconclusive", True
        reason = (f"paired difference {delta:+.2f} ± {delta_ci:.2f} over {pairs} "
                  f"topics — inside the noise, so no evidence either way. "
                  f"Activating: the gate blocks regressions, it does not require "
                  f"proof of improvement.")

    return {
        "verdict": verdict, "activate": activate, "reason": reason,
        "pairedDelta": round(delta, 3), "pairedCi95": round(delta_ci, 3),
        "pairs": pairs, "repeats": repeats,
        "summary": summary, "perCriterion": per_criterion,
        "threshold": REGRESSION_THRESHOLD,
    }


async def gate_candidate(previous_state: dict, candidate_state: dict, *,
                         scope_key: str = None, repeats: int = None) -> Optional[dict]:
    """Gate a candidate, or return None when the gate is off or unusable.

    None means "no opinion" and callers activate as normal. The gate is an extra
    safety net, and a safety net that blocks activation when it is merely
    misconfigured would be worse than not having one.
    """
    if not enabled():
        return None
    try:
        fixture = load_golden(golden_path(scope_key))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"[prep_flow:gate] enabled but unusable, activating without it: {exc}")
        return None

    repeats = repeats or int(os.environ.get("PREP_FLOW_GATE_REPEATS", "3"))
    try:
        return await run_gate(previous_state, candidate_state,
                              fixture=fixture, repeats=repeats)
    except Exception as exc:
        print(f"[prep_flow:gate] failed, activating without it: {exc}")
        return None
