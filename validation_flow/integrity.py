"""Did the chain hold? — the two checks only this node is positioned to make.

`checks.py` asks whether a sheet is well made and `learner.py` asks whether it
teaches. Both of those judge the sheet against ITSELF and against Node 1's
contract. Neither can ask the question the node split created:

    the material came out of a pipeline. Did the pipeline do what it said?

Two halves, and each is the far end of a hook deliberately left in an earlier
node:

  TARGET PRESERVATION  `prep_flow/contract.py` fingerprints the five fields
                       nothing downstream may move, and says in its own docstring
                       that "Node 3 recomputes the fingerprint over what came
                       back". This is where that happens. It is a hash
                       comparison, not a judgement — a mastery target that moved
                       cannot be argued about, and no amount of model confidence
                       about the adaptation being useful changes it.

  CONTEXT ADOPTION     `context_flow/shadow.py` reports §17's `contextAdoption`
                       as None with the reason "requires the generated sheet —
                       compare accepted adaptations against what Generation
                       actually wrote". The sheet exists here. This computes it.

WHY ADOPTION IS A VALIDATION CHECK AND NOT A METRIC. §18 names "Generation
ignores Node 2" as a failure mode, with "context adoption metric + generator
contract" as its guardrail — and a metric nobody blocks on is a number on a
dashboard. An accepted, gated, feasible adaptation that the sheet simply does not
contain means the contextual layer ran, cost a call, and changed nothing. That is
a defect in the pipeline, and it is reported as an advisory finding against the
topic so it lands next to the sheet it concerns.

ADVISORY, NOT BLOCKING, and the line is drawn deliberately. A missing adaptation
is a sheet that is merely un-adapted — still correct, still teachable, just not
tailored. A moved mastery target is a sheet teaching something nobody approved.
Only the second is worth refusing to ship.

WHAT THIS CHECK CAN AND CANNOT TELL YOU, because both errors are real and the
wording of every finding below depends on admitting it.

It compares the words of an adaptation against the words of the sheet, after
subtracting the vocabulary the topic was going to contain anyway — the mastery
target, the concepts, the trajectory, the anchor, the activity. What is left is
the adaptation's own contribution, and that is what the sheet is searched for.

    It reliably catches           an accepted adaptation the sheet shows no
                                  trace of. That is the failure §18 names, and
                                  it is the reason the check exists.

    It CANNOT distinguish         "the teacher was not told to do this" from
                                  "the teacher was told, in different words".
                                  An adaptation phrased as a prohibition —
                                  "pairs walk round the desk RATHER THAN moving
                                  as a circuit" — is followed by a sheet that
                                  simply describes walking round the desk and
                                  never says "circuit". Correct behaviour, and
                                  this check reports it as unconfirmed.

BOTH ERRORS WERE OBSERVED WHILE BUILDING THIS, in that order. The first version
subtracted nothing and scored the Marathi language step as adopted on the
strength of "partners", "changed" and "view" — words any sheet about views of
objects contains. Subtracting the expected vocabulary fixed that and immediately
produced the opposite error on the adaptation above.

So the finding says CONFIRMED / NOT CONFIRMED, never ADOPTED / IGNORED, and the
metric is named `contextAdoptionConfirmed`. A number that under-counts and says
so is usable — the bias is constant, so a drop across a chapter after a prompt
change still means something. A number that claims to measure adoption and
quietly under-counts would get a working generator blamed for ignoring Node 2.

Deciding whether an unconfirmed adaptation was actually dropped is a job for the
reviewer reading the sheet, and the finding is worded to send them there.
"""
import re
from typing import Optional

try:
    from prep_flow.clauses import CLAUSE_STOPWORDS, roots
    from prep_flow.contract import PRESERVE, fingerprint, verify
    from prep_flow.sections import SECTION_ORDER, section_text
except ImportError:  # pragma: no cover — the package layout in the real backend
    from ..prep_flow.clauses import CLAUSE_STOPWORDS, roots
    from ..prep_flow.contract import PRESERVE, fingerprint, verify
    from ..prep_flow.sections import SECTION_ORDER, section_text

# How much of an adaptation's `change` has to turn up in the section it named
# before it counts as adopted. Three content roots, which on the observed
# adaptations is one verb and two nouns — enough that a sheet mentioning the
# right object for an unrelated reason does not count, and loose enough that a
# paraphrase does.
_ADOPTION_ROOTS = 3

# Words that appear in almost every adaptation and carry no evidence of adoption.
# Stripped ON TOP of the shared clause stopwords, because an adaptation is
# written as an instruction and its imperatives ("use", "give", "ask", "add")
# are the vocabulary of the genre rather than of the change.
_INSTRUCTION_WORDS = frozenset("""
use using used give giving given ask asking asked add adding added show showing
shown make making made have having has let allow provide provided instead
rather before after first then next during while when where each every
""".split())


def _content(text: str) -> set[str]:
    return {r for r in roots(text or "")
            if r not in _INSTRUCTION_WORDS and r not in CLAUSE_STOPWORDS}


# ── 1. Did the academic destination move? ────────────────────────────────────

def check_target_preservation(contract: dict, materials: dict) -> list[dict]:
    """Recompute the contract's own fingerprints over what came back.

    Two different things are checked and they fail differently:

      * the CONTRACT against itself. Only meaningful when a downstream node
        returned a contract it had edited — which nothing currently does, and
        which is exactly why the check must exist before something does. A node
        that quietly rewrote a mastery target and shipped is the failure this
        catches, and it can only be caught by whoever holds the original.
      * each SHEET against the target it was written for. A sheet whose `_meta`
        records a mastery target different from the contract's has been written
        from something else.

    `contract.verify()` is called rather than reimplemented, and that is the
    whole point of it living beside the code that built the fingerprints: the two
    can never hash a different set of fields.
    """
    findings: list[dict] = []

    returned = {"integrity": contract.get("integrity") or {},
                "topics": contract.get("topics") or []}
    verdict = verify(contract, returned)
    for index in verdict["changed"]:
        findings.append({
            "index": int(index) if str(index).isdigit() else None,
            "section": None, "severity": "blocking", "check": "target_preservation",
            "message": (f"T{index}'s academic destination no longer matches the "
                        f"contract it was derived under. One of {', '.join(PRESERVE)} "
                        f"was changed downstream of Node 1."),
        })
    for index in verdict["missing"]:
        findings.append({
            "index": int(index) if str(index).isdigit() else None,
            "section": None, "severity": "blocking", "check": "target_preservation",
            "message": (f"T{index} was in the contract Node 1 produced and is not in "
                        f"the one that came back — a topic cannot be dropped between "
                        f"nodes."),
        })

    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}
    for index, material in sorted((materials or {}).items()):
        meta = (material or {}).get("_meta") or {}
        written_for = (meta.get("masteryTarget") or "").strip()
        if not written_for:
            # Absent, not wrong. Generation is not yet re-homed and nothing
            # stamps this field; when it does, this check starts biting. Silent
            # rather than advisory, because an advisory on every topic of every
            # run would be noise that teaches people to skim the report.
            continue
        expected = ((rows.get(int(index)) or {}).get("academic") or {}).get("masteryTarget") or ""
        if fingerprint(written_for) != fingerprint(expected.strip()):
            findings.append({
                "index": int(index), "section": None, "severity": "blocking",
                "check": "target_preservation",
                "message": (f"the sheet records it was written for \"{written_for[:80]}\", "
                            f"but the contract's target for this topic is "
                            f"\"{expected[:80]}\"."),
            })
    return findings


# ── 2. Did Generation use what Node 2 accepted? ──────────────────────────────

def _expected_vocabulary(row: dict) -> set:
    """The roots this topic's sheet would contain with no adaptation at all.

    Everything Node 1 already planned: the target, the concepts, the thinking
    path, the object it chose and the activity. Subtracted from an adaptation's
    words before the sheet is searched, so adoption is measured on what the
    adaptation ADDED rather than on the lesson it was attached to.
    """
    if not row:
        return set()
    academic = row.get("academic") or {}
    experience = row.get("experiencePlan") or {}
    chain = row.get("knowledgeChain") or {}
    activity = row.get("selectedActivity") or {}
    parts = [
        academic.get("masteryTarget") or "",
        chain.get("gained") or "", chain.get("bridgesTo") or "",
        experience.get("anchor") or "", experience.get("studentAction") or "",
        experience.get("conceptualJump") or "", experience.get("inference") or "",
        activity.get("name") or "",
        " ".join(academic.get("requiredConcepts") or []),
        " ".join(academic.get("vocabulary") or []),
        " ".join(experience.get("trajectory") or []),
        " ".join(row.get("misconceptions") or []),
    ]
    return _content(" ".join(parts))


def check_context_adoption(plan, materials: dict,
                           rows: dict = None) -> tuple[list[dict], dict]:
    """§17's `contextAdoption`, and §18's "Generation ignores Node 2" guardrail.

    Returns (findings, metrics). Both, because the number is what a run reports
    and the findings are what a person acts on — and a rate with no way to see
    WHICH adaptation went missing is a number nobody can do anything about.

    `metrics["verdicts"]` carries the same judgement a third way: one entry per
    adaptation, keyed by (topicIndex, ordinal), for writing back to the stored
    row. The per-adaptation answer was always computed here and only ever
    survived as a count and a sentence; the reinforcement loop needs it as data,
    because "did this adaptation land" is what decides whether a teacher's rating
    of that section is evidence about it or about nothing.

    THE THREE VERDICTS ARE NOT TWO. `unknown` is not a soft `not_landed`: it is
    an adaptation whose own wording added nothing distinctive to search for, so
    the sheet cannot be interrogated about it either way. Attribution drops
    `unknown` and `not_landed` alike — but only one of them is a finding, and
    collapsing them would turn a measurement gap into an accusation.

    Silent when there is no plan. Node 2 is in shadow mode by default, and a
    chapter generated without a context plan has nothing to have ignored — an
    adoption rate of 0% there would be an accusation about a run that never
    received the thing it is accused of dropping.

    `rows` is the contract's topics by index, used to subtract the vocabulary
    the sheet was going to contain anyway. Optional: without it the check still
    runs, less precisely, which is better than not running.
    """
    accepted = [a for a in ((plan or {}).get("adaptations") or [])
                if a.get("accepted") is not False
                and a.get("type") != "no_change"]
    if not plan or not accepted:
        return [], {"contextAdoptionConfirmed": None,
                    "contextAdaptationsCheckable": 0,
                    "contextAdaptationsConfirmed": 0,
                    "verdicts": {}}

    rows = rows or {}
    findings: list[dict] = []
    verdicts: dict = {}
    found = 0
    measurable = 0

    def verdict(a: dict, outcome: str, note: str = "") -> None:
        """Record the per-adaptation answer, if the adaptation can be addressed.

        An adaptation with no ordinal came from a plan assembled outside Node 2
        — a fixture, or a replay of a plan logged before ordinals existed. It
        still counts toward the rate; it just has no row to be written back to,
        and inventing a key for it would write a verdict onto whichever proposal
        happened to sit at that position.
        """
        ordinal = a.get("ordinal")
        index = a.get("topicIndex")
        if ordinal is None or index is None:
            return
        verdicts[(int(index), int(ordinal))] = {"adopted": outcome, "note": note}

    for a in accepted:
        index = a.get("topicIndex")
        material = (materials or {}).get(int(index)) if index is not None else None
        if material is None:
            findings.append({
                "index": index, "section": a.get("section"), "severity": "advisory",
                "check": "context_adoption",
                "message": (f"the {a.get('factor')} adaptation was accepted for T{index}, "
                            f"which has no sheet — it could not be used."),
            })
            verdict(a, "not_landed", "the topic has no sheet")
            continue

        wanted = _content(a.get("change")) - _expected_vocabulary(
            rows.get(int(index)) if index is not None else None)
        if len(wanted) < _ADOPTION_ROOTS:
            # Nothing distinctive to look for: the adaptation asked, in its own
            # words, for something the lesson already said it would do. Not
            # adoption and not neglect — excluded from the rate rather than
            # counted either way, because counting it would make the number a
            # measure of how novel the adaptations happened to be worded.
            verdict(a, "unknown", "wording added nothing distinctive to search for")
            continue

        measurable += 1
        # Against the NAMED SECTION first, and the whole sheet as a fallback. An
        # adaptation that landed in a different section than it asked for is
        # adopted — Generation composing the six sections is entitled to place
        # it — and reporting that as ignored would punish the right outcome.
        section = a.get("section") if a.get("section") in SECTION_ORDER else None
        target = section_text(material, section) if section else ""
        whole = " ".join(section_text(material, s) for s in SECTION_ORDER)

        in_section = len(wanted & _content(target)) >= _ADOPTION_ROOTS if target else False
        in_sheet = len(wanted & _content(whole)) >= _ADOPTION_ROOTS

        if in_section or in_sheet:
            found += 1
            verdict(a, "landed",
                    "" if in_section else "confirmed outside the section it named")
            if section and not in_section and in_sheet:
                findings.append({
                    "index": index, "section": section, "severity": "advisory",
                    "check": "context_adoption",
                    "message": (f"the {a.get('factor')} adaptation asked for "
                                f"{section} and was confirmed elsewhere on the "
                                f"sheet — used, but not where it was aimed."),
                })
            continue

        findings.append({
            "index": index, "section": a.get("section"), "severity": "advisory",
            "check": "context_adoption",
            "message": (f"could not confirm the {a.get('factor')} adaptation in the "
                        f"sheet: \"{(a.get('change') or '')[:110]}\" — read the "
                        f"{a.get('section') or 'sheet'} and check whether it was "
                        f"followed in different words."),
        })
        verdict(a, "not_landed", "no trace found in the sheet")

    return findings, {
        # `Confirmed`, not `contextAdoption`. The name is the disclaimer: this
        # under-counts by construction, and a caller that treats it as an
        # adoption rate will blame a working generator. See the module docstring.
        "contextAdoptionConfirmed": round(found / measurable, 3) if measurable else None,
        "contextAdaptationsCheckable": measurable,
        "contextAdaptationsConfirmed": found,
        # Separately from the two above, because the gap between `accepted` and
        # `measurable` is adaptations whose wording added nothing to search for —
        # a fact about the plan's phrasing, not about Generation.
        "contextAdaptationsAccepted": len(accepted),
        # Keyed by (topicIndex, ordinal) — the identity migration 036 stores rows
        # under. Not a count and not a sentence: the loop needs to join on this.
        "verdicts": verdicts,
    }


# ── 3. Was the plan even allowed to reach Generation? ────────────────────────

def check_plan_provenance(plan: Optional[dict]) -> list[dict]:
    """A plan that reached Generation while refused or in shadow mode.

    Node 2 gates this itself — `context_flow.graph.apply()` returns None unless
    the gate passed AND the run was not in shadow mode. This is the same question
    asked from the other side, by the node that can see what was actually used,
    because a guard nobody checks from outside is a guard that survives being
    accidentally removed.

    Blocking. A refused plan reaching material means the equity/safety gate was
    bypassed, and that is not a sheet to ship pending review.
    """
    if not plan:
        return []
    findings = []
    if plan.get("lowersStandard"):
        findings.append({
            "index": None, "section": None, "severity": "blocking",
            "check": "plan_provenance",
            "message": ("the context plan used for this material was judged to lower "
                        "the academic standard, and should not have reached "
                        "Generation at all."),
        })
    refused = [a for a in (plan.get("adaptations") or []) if a.get("accepted") is False]
    if refused:
        findings.append({
            "index": None, "section": None, "severity": "advisory",
            "check": "plan_provenance",
            "message": (f"{len(refused)} adaptation(s) in this plan were refused by "
                        f"the equity/safety gate; only the accepted ones should "
                        f"appear in the material."),
        })
    return findings


def run(contract: dict, *, materials: dict,
        plan: Optional[dict] = None) -> tuple[list[dict], dict]:
    """All three, in one call. Returns (findings, metrics)."""
    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}
    findings = check_target_preservation(contract, materials)
    findings += check_plan_provenance(plan)
    adoption, metrics = check_context_adoption(plan, materials, rows)
    findings += adoption

    metrics["targetPreserved"] = not any(
        f["check"] == "target_preservation" for f in findings)
    return findings, metrics
