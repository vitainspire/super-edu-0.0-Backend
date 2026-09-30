"""Guards for the per-adaptation adoption verdict.

`check_context_adoption` already decided, for every accepted adaptation, whether
the sheet showed a trace of it — and threw that answer away, keeping a rate and a
sentence. The reinforcement loop joins on it: a teacher's rating of T4's Challenge
is evidence about an adaptation only if the adaptation reached the page. These
tests guard the three verdicts and, above all, the distinction between the two
that look alike.

    python -m validation_flow.tests.test_adoption_verdicts
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from validation_flow import integrity                     # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def _sheet(**sections) -> dict:
    """A material in the shape section_text() reads."""
    base = {s: {"bullets": []} for s in
            ("refresher", "concept", "realLife", "challenge", "levelSet", "explore")}
    for name, text in sections.items():
        base[name] = {"bullets": [{"text": text, "detail": text}]}
    return base


def _plan(*adaptations) -> dict:
    return {"adaptations": list(adaptations)}


def _adaptation(ordinal: int, **overrides) -> dict:
    base = {
        "topicIndex": 4, "ordinal": ordinal, "factor": "funds_of_knowledge",
        "type": "add", "section": "realLife", "accepted": True,
        "change": "children examine the bucket rim and trace its circular outline",
    }
    base.update(overrides)
    return base


def _verdicts(plan, materials, rows=None):
    _, metrics = integrity.check_context_adoption(plan, materials, rows or {})
    return metrics.get("verdicts") or {}


# ── the three verdicts ───────────────────────────────────────────────────────

def test_an_adaptation_present_in_its_section_lands() -> None:
    plan = _plan(_adaptation(0))
    materials = {4: _sheet(realLife="children examine the bucket rim and trace "
                                    "its circular outline together")}
    v = _verdicts(plan, materials)
    check("present in its section -> landed",
          v.get((4, 0), {}).get("adopted") == "landed", f"got {v}")


def test_an_adaptation_absent_from_the_sheet_does_not_land() -> None:
    plan = _plan(_adaptation(0))
    materials = {4: _sheet(realLife="children count the windows in the classroom")}
    v = _verdicts(plan, materials)
    check("absent from the sheet -> not_landed",
          v.get((4, 0), {}).get("adopted") == "not_landed", f"got {v}")


def test_an_adaptation_with_no_sheet_does_not_land() -> None:
    v = _verdicts(_plan(_adaptation(0)), {})
    check("no sheet for the topic -> not_landed",
          v.get((4, 0), {}).get("adopted") == "not_landed", f"got {v}")


def test_unmeasurable_wording_is_unknown_not_a_failure() -> None:
    """THE DISTINCTION THIS FILE EXISTS FOR.

    An adaptation whose wording added nothing distinctive to search for cannot be
    confirmed OR denied — the sheet cannot be interrogated about it. Recording
    that as `not_landed` would blame a generator that may well have followed it,
    and would feed the loop a fabricated negative about the factor behind it.
    """
    plan = _plan(_adaptation(0, change="do it"))
    materials = {4: _sheet(realLife="children count windows")}
    v = _verdicts(plan, materials)
    outcome = v.get((4, 0), {}).get("adopted")
    check("unmeasurable wording -> unknown", outcome == "unknown", f"got {outcome!r}")
    check("unmeasurable wording is NOT not_landed", outcome != "not_landed")


def test_landing_outside_the_named_section_still_counts_as_landed() -> None:
    """Generation composes the six sections and is entitled to place an
    adaptation elsewhere. Reporting that as ignored would punish the right
    outcome — and would teach the loop to distrust a factor that worked."""
    plan = _plan(_adaptation(0, section="realLife"))
    materials = {4: _sheet(challenge="children examine the bucket rim and trace "
                                     "its circular outline")}
    v = _verdicts(plan, materials)
    entry = v.get((4, 0), {})
    check("placed elsewhere -> still landed", entry.get("adopted") == "landed",
          f"got {entry}")
    check("and the note says where", "outside the section" in (entry.get("note") or ""))


# ── identity ─────────────────────────────────────────────────────────────────

def test_verdicts_are_keyed_by_topic_and_ordinal() -> None:
    """The key must match what migration 036 stores rows under, or the write-back
    lands on the wrong proposal."""
    plan = _plan(_adaptation(0), _adaptation(1, factor="language", section="concept",
                                             change="say the Telugu word alongside "
                                                    "the English one"))
    materials = {4: _sheet(
        realLife="children examine the bucket rim and trace its circular outline",
        concept="say the Telugu word alongside the English one twice")}
    v = _verdicts(plan, materials)
    check("both adaptations get their own key", set(v) == {(4, 0), (4, 1)}, f"got {set(v)}")


def test_an_adaptation_without_an_ordinal_is_skipped_not_guessed() -> None:
    """A plan assembled outside Node 2 has no ordinals. Such an adaptation still
    counts toward the rate; it has no row to be written back to, and inventing a
    key would stamp a verdict onto whichever proposal sat at that position."""
    plan = _plan(_adaptation(0))
    plan["adaptations"][0].pop("ordinal")
    materials = {4: _sheet(realLife="children examine the bucket rim and trace "
                                    "its circular outline")}
    _, metrics = integrity.check_context_adoption(plan, materials, {})
    check("no ordinal -> no verdict written", not metrics.get("verdicts"),
          f"got {metrics.get('verdicts')}")
    check("but it still counts toward the rate",
          metrics.get("contextAdaptationsConfirmed") == 1,
          f"got {metrics.get('contextAdaptationsConfirmed')}")


def test_no_change_adaptations_are_never_given_a_verdict() -> None:
    """`no_change` is a statement that a topic needs nothing. There is nothing
    for a sheet to show a trace of, so it is not an adoption question at all."""
    plan = _plan(_adaptation(0, type="no_change", section=None))
    check("no_change produces no verdict", not _verdicts(plan, {4: _sheet()}))


def test_a_shadow_run_with_no_plan_produces_no_verdicts() -> None:
    """Node 2 is in shadow mode by default. A chapter generated without a plan
    has nothing to have ignored, and an empty verdict map is the honest answer —
    not a set of not_landed rows about adaptations nobody applied."""
    _, metrics = integrity.check_context_adoption(None, {4: _sheet()}, {})
    check("no plan -> empty verdicts", metrics.get("verdicts") == {})
    check("no plan -> rate is None, not zero",
          metrics.get("contextAdoptionConfirmed") is None)


def main() -> int:
    print("validation_flow — adoption verdicts\n")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all guards pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
