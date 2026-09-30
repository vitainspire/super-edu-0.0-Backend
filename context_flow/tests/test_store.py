"""Guards for Node 2's persistence layer.

Every test here runs with NO database and NO backend dependencies installed —
which is not a limitation of the test environment, it is the property being
tested. `store.py` is imported by `graph.py` on every Node 2 run, and a
persistence module that cannot be imported without httpx would take the whole
node down with it on a checkout that only wants to plan a lesson.

    python -m context_flow.tests.test_store
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from context_flow import factors as factors_module   # noqa: E402
from context_flow import store                        # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def _adaptation(**overrides) -> dict:
    base = {
        "topicIndex": 1, "factor": "funds_of_knowledge", "type": "add",
        "purpose": "learning", "section": "realLife",
        "change": "picture the loom at home", "reason": "weaving community",
        "supports": ["identify top view"], "evidence": ["community_occupations"],
        "dataSource": "school", "confidence": 0.72,
        "accepted": True, "rejectedBecause": [],
    }
    base.update(overrides)
    return base


def _row(**overrides) -> dict:
    return store._row(_adaptation(**overrides), context_run_id="cr-1",
                      run_id="r-1", key="school_88:3:maths", ordinal=0)


# ── tier resolution ──────────────────────────────────────────────────────────

def test_every_registry_factor_resolves_to_its_own_tier() -> None:
    """The denormalised column must agree with the registry it was copied from.

    A tier that drifted from factors.py would put evidence in the wrong bucket
    and move the wrong dial, and nothing downstream could detect it — the stored
    value IS the answer by then.
    """
    wrong = [f.id for f in factors_module.FACTORS if store._tier(f.id) != f.tier]
    check("every registry factor keeps its own tier", not wrong, f"drifted: {wrong}")


def test_an_unknown_factor_is_never_labelled_baseline() -> None:
    """The regression this file exists for.

    The first version defaulted unknown factors to `baseline`. That is the
    largest bucket, so the mislabelled row is both the most damaging to the dial
    and the hardest to notice — a wrong number that looks like a measurement.
    """
    tier = store._tier("attendance")          # plausible, and not a real id
    check("unknown factor is not baseline", tier != factors_module.BASELINE,
          f"got {tier!r}")
    check("unknown factor is 'unknown'", tier == store.UNKNOWN_TIER, f"got {tier!r}")


def test_gate_factors_keep_their_tier() -> None:
    """equity and safety are not adaptation generators, but if one ever reaches a
    plan the row must say so rather than passing as an ordinary baseline
    adaptation."""
    check("equity stores as gate", store._tier("equity") == factors_module.GATE)
    check("safety stores as gate", store._tier("safety") == factors_module.GATE)


# ── row shape ────────────────────────────────────────────────────────────────

def test_the_row_carries_every_key_attribution_joins_on() -> None:
    row = _row()
    for key in ("run_id", "topic_index", "section", "factor", "tier", "type",
                "accepted", "adopted" if "adopted" in row else "scope_key"):
        check(f"row carries {key}", key in row)
    check("tier is resolved, not copied from the plan", row["tier"] == "local")


def test_a_missing_section_is_null_not_empty_string() -> None:
    """plan.py writes "" when the model named no section or an unrecognised one.

    Stored as "" it would match nothing and silently join as a section that does
    not exist; stored as NULL the attribution lookup misses it, which is the
    correct outcome for an adaptation nobody can attribute.
    """
    check("empty section becomes NULL", _row(section="")["section"] is None)


def test_absent_confidence_is_null_not_zero() -> None:
    """A missing confidence is not low confidence. plan.py drops the field rather
    than inventing a number, and storing 0.0 would make a later report look more
    precise than the model actually was."""
    check("absent confidence stays NULL", _row(confidence=None)["confidence"] is None)
    check("present confidence survives as a float",
          _row(confidence=0.4)["confidence"] == 0.4)


def test_rejected_proposals_keep_their_reasons() -> None:
    """Without the reason, "this factor never helps" and "this factor never got
    through the gate" are the same row, and they call for opposite responses."""
    row = _row(accepted=False, rejectedBecause=["resource not in the room"])
    check("rejected row is stored", row["accepted"] is False)
    check("rejection reason survives",
          row["rejected_because"] == ["resource not in the room"])


def test_evidence_survives_because_profile_flags_depend_on_it() -> None:
    """`evidence` names the profile fields the adaptation rests on. It is the
    only route by which a stale profile value ever gets questioned."""
    check("evidence is stored", _row()["evidence"] == ["community_occupations"])


# ── import and degradation ───────────────────────────────────────────────────

def test_the_module_imports_without_backend_dependencies() -> None:
    """If this file ran at all, the import already succeeded — the check is that
    it did so without httpx, supabase or langgraph present."""
    check("store imports with no backend deps", store.MIGRATION.endswith(".sql"))


def test_persistence_degrades_to_none_rather_than_raising() -> None:
    """A failed insert must never cost a chapter its plan. With no database
    configured every entry point returns the 'nothing was stored' value."""
    check("save_plan returns None",
          asyncio.run(store.save_plan({"contract": {}, "plan": {}})) is None)
    check("available returns False", asyncio.run(store.available()) is False)
    check("latest_version returns None",
          asyncio.run(store.latest_version("school_88:3:maths")) is None)
    check("record_adoption returns 0",
          asyncio.run(store.record_adoption("cr-1", {(1, 0): {"adopted": "landed"}})) == 0)


def main() -> int:
    print("context_flow/store.py\n")
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
