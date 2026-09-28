"""The equity and safety gate — deterministic, per adaptation, before anything ships.

§10: "use deterministic checks for target preservation, provenance, resource
availability and schema validity; use a lightweight model field only for the
residual judgement 'does any adaptation lower rigor?'".

That division is the design. Everything a rule can decide is decided by a rule,
and the model contributes exactly one boolean it has already given
(`lowersStandard`). A gate that asked a model whether a plan was safe would be
asking the same family of model that wrote the plan, and the two would agree.

EACH CHECK IS ONE OF §18's FAILURE MODES, named after it, so a rejection reads
as a diagnosis rather than as a schema complaint:

    invented_local_context   a local fact with no verified field behind it
    evidence_mismatch        a real field cited by a factor that does not read it
    contradicts_evidence     a cited number, and a different number stated for it
    unechoed_evidence        a cited field whose value the change never mentions
                             (ADVISORY — accepted, but worth a look)
    wrong_target             an adaptation that serves nothing the topic requires
    resource_contradiction   a change needing something the room does not have
    stereotyping             evidence that is a demographic label, not a fact
    inactive_factor          a factor the profile never switched on
    unsafe_disclosure        a change requiring personal disclosure or singling out
                             (a heuristic — §16's teacher review is the real one)
    teacher_overload         more changes than a period can absorb
    schema                   an entry too incomplete to act on
    lowered_rigor            the model's own residual judgement, plus the diff

REJECTION IS PER ADAPTATION, NOT PER PLAN, and that is not leniency. A plan with
three good adaptations and one invented village should ship the three: rejecting
the plan wholesale would throw away real work and — worse — would teach whoever
is reading the shadow-mode logs that Node 2 "fails a lot", when what happened is
that one of four proposals was caught. The exception is `lowered_rigor`, which
is a property of the plan taken together and takes the plan with it.

WHAT THIS GATE CANNOT DO. It cannot tell whether a verified fact is TRUE — only
that somebody with an origin recorded said it. It cannot tell whether an
adaptation will help. Both belong to the shadow-mode evaluation in §16 and to
the teachers rating the proposals, and pretending otherwise here would put a
green light in front of a claim nobody has tested.
"""
import re
from typing import Optional

from . import factors as factors_module
from . import plan as plan_module
from . import profile as profile_module

_DEMOGRAPHIC_EVIDENCE = re.compile(
    r"\b(rural|urban|village|tribal|slum|poor|poverty|low.?income|backward|"
    r"illiterate|uneducated|caste|minority|migrant|underprivileged|"
    r"disadvantaged|typical|usually|generally|most\s+students)\b", re.I)


_ASKS_OF_CHILDREN = re.compile(
    r"\b(ask|have|get|invite|call\s+on|make)\b[^.;]{0,40}?"
    r"\b(child|children|student|students|learner|learners|pupil|pupils|each\s+one)\b",
    re.I)
_PERSONAL_SUBJECT = re.compile(
    r"\b(famil(?:y|ies)|home|homes|household|parents?|father|mother|guardian|"
    r"income|salary|earn(?:s|ings)?|wage|money|job|occupation|work\s+their|"
    r"caste|religion|tribe|circumstances|situation\s+at\s+home|"
    r"why\s+(?:they|he|she)\s+(?:were|was)\s+(?:absent|away)|absence)\b", re.I)

_SINGLES_OUT = re.compile(
    r"\b(whose\s+(?:famil|parents?|father|mother|house|home)|"
    r"(?:children|students|those)\s+who\s+(?:cannot|can'?t|do\s+not\s+have|"
    r"don'?t\s+have|lack|are\s+unable)|"
    r"(?:raise|put\s+up|show\s+me)\s+(?:your|their|a|the)?\s*hands?\s+if|"
    r"stand\s+up\s+if)\b", re.I)

_MATERIAL_WORDS = re.compile(
    r"\b(projector|screen|tablet|laptop|computer|phone|device|internet|"
    r"printout|print-?out|printed|handout|photocop\w+|paper|chart|poster|"
    r"marker|chalk|board|video|slide)\b", re.I)

_AVOIDING = re.compile(
    r"\b(no|not|without|lacking|avoid\w*|instead\s+of|rather\s+than|"
    r"in\s+place\s+of|since\s+there\s+(?:is|are)\s+no|"
    r"do(?:es)?\s*n[o']t\s+(?:have|need|require))\b[^.;]{0,28}$", re.I)


class Finding(dict):
    """One rejection, shaped so a report can group them."""

    def __init__(self, check: str, message: str, *, adaptation: Optional[dict] = None):
        super().__init__(check=check, message=message,
                         factor=(adaptation or {}).get("factor"),
                         topicIndex=(adaptation or {}).get("topicIndex"))


# ── The checks ───────────────────────────────────────────────────────────────

def _schema(a: dict) -> list[Finding]:
    out = []
    if a.get("type") == plan_module.NO_CHANGE:
        # A deliberate no-op needs a reason and nothing else. Requiring a
        # section and a purpose of it would make the honest answer harder to
        # give than the unnecessary one, which is exactly backwards.
        if not a.get("reason"):
            out.append(Finding("schema", "a no_change entry must say why nothing "
                                         "needed changing", adaptation=a))
        return out
    for field, label in (("factor", "a factor id"), ("type", "an adaptation type"),
                         ("purpose", "a purpose"), ("change", "what the teacher does"),
                         ("reason", "why")):
        if not a.get(field):
            out.append(Finding("schema", f"missing {label}", adaptation=a))
    return out


def _inactive_factor(a: dict, activations: list) -> list[Finding]:
    factor_id = a.get("factor")
    factor = factors_module.BY_ID.get(factor_id)
    if factor is None:
        return [Finding("schema", f"'{factor_id}' is not a factor", adaptation=a)]
    if factor.tier == factors_module.GATE:
        return [Finding("inactive_factor",
                        f"{factor.name} is a constraint, not an adaptation source",
                        adaptation=a)]
    entry = next((e for e in activations if e.factor.id == factor_id), None)
    if entry is None or not entry.active:
        return [Finding("inactive_factor",
                        f"{factor.name} was not active for this lesson: "
                        f"{entry.reason if entry else 'not evaluated'}",
                        adaptation=a)]
    if entry.relevant is False:
        return [Finding("inactive_factor",
                        f"{factor.name} was ruled out for this topic: {entry.hint}",
                        adaptation=a)]
    if a.get("purpose") and a["purpose"] not in factor.purposes:
        return [Finding("wrong_target",
                        f"{factor.name} produces {'/'.join(factor.purposes)} "
                        f"adaptations, not '{a['purpose']}'", adaptation=a)]
    return []


def _provenance(a: dict, profile, activations: list) -> list[Finding]:
    """The rule §4 calls hard, enforced against the profile rather than the claim.

    Three separate things go wrong here and they are reported separately because
    the fixes differ: citing a field that does not exist (the model invented the
    evidence), citing one that exists but is not verified (the model over-claimed
    `data_source`), and making a local claim with no citation at all (the model
    skipped the step).
    """
    out: list[Finding] = []
    factor = factors_module.BY_ID.get(a.get("factor"))
    if factor is None:
        return out

    entry = next((e for e in activations if e.factor.id == factor.id), None)
    # WHAT THE FACTOR OWNS, PLUS WHAT IT IS PERMITTED TO CITE. `exposes` is the
    # activation surface — the fields that switched this factor on and may be
    # shown to the model for it. Citation is a wider question: a factor can have
    # honest operational use for a field it does not own and must not interpret.
    # See `factors.SHARED_EVIDENCE`, which exists because six of nine sound
    # adaptations in one run were refused purely for citing across that line.
    allowed = set(entry.exposes) if entry else set()
    allowed |= factors_module.citable_by(factor.id)
    cited = [e for e in (a.get("evidence") or [])]

    for name in cited:
        field = profile.get(name)
        if field is None:
            out.append(Finding("invented_local_context",
                               f"cites '{name}', which nothing recorded",
                               adaptation=a))
            continue
        if name not in allowed and name not in profile_module.ROOM_FACTS:
            # NOT `invented_local_context`. The field exists and is recorded —
            # what is wrong is that THIS factor has no business reading it, which
            # is a different defect with a different fix, and filing it under
            # "invented" sends a reader hunting for a fabrication that is not
            # there. Room facts are exempt entirely: see profile.ROOM_FACTS.
            out.append(Finding("evidence_mismatch",
                               f"cites '{name}', which is recorded but neither "
                               f"belongs to the {factor.name} factor nor is shared "
                               f"with it — either the wrong factor was chosen for "
                               f"this change, or the wrong field was cited for it", adaptation=a))
            continue
        if a.get("dataSource") == profile_module.VERIFIED and not field.verified:
            # THE HARD RULE. Not a warning and not weighted by confidence:
            # `data_source` is a claim about the data and the data disagrees.
            out.append(Finding("invented_local_context",
                               f"claims verified data, but '{name}' is "
                               f"'{field.provenance}' — model confidence cannot "
                               f"make an assumed field verified", adaptation=a))

    # A LOCAL-tier factor with no citation is asserting something about a
    # community from nowhere. BASELINE factors are exempt: an adaptation
    # reasoning purely from the lesson (seven new terms in thirty minutes) is
    # legitimate and cites nothing, which is what rule 2 of the prompt allows.
    if factor.tier == factors_module.LOCAL and not cited:
        out.append(Finding("invented_local_context",
                           f"{factor.name} adaptations must cite the verified "
                           f"field they rest on", adaptation=a))

    for name in cited:
        if _DEMOGRAPHIC_EVIDENCE.search(name):
            out.append(Finding("stereotyping",
                               f"'{name}' is a population label, not a recorded "
                               f"fact about this class", adaptation=a))
    return out


def _values_of(field) -> list:
    value = field.value
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _contradicts_evidence(a: dict, profile) -> list[Finding]:
    """Does the change state a different value than the field it cites?

    Numeric only, and deliberately: "class size of 30" against a recorded 52 is
    arithmetic, not interpretation. The alias is the field name with its
    underscores opened out, which is how these fields are actually referred to
    in prose — `class_size` becomes "class size", `lesson_duration` becomes
    "lesson duration".
    """
    out: list[Finding] = []
    text = f"{a.get('change') or ''} {a.get('reason') or ''}"
    for name in a.get("evidence") or []:
        field = profile.get(name)
        if field is None or isinstance(field.value, bool):
            continue
        if not isinstance(field.value, (int, float)):
            continue
        # Escape the PARTS then join — `re.escape` on a string that already
        # contains \s+ escapes the backslash and matches nothing.
        alias = r"\s+".join(re.escape(part) for part in name.split("_"))
        # The number on either side of the phrase, within a few words of it.
        for pattern in (rf"{alias}[^.;]{{0,18}}?(\d+)", rf"(\d+)[^.;]{{0,18}}?{alias}"):
            for match in re.finditer(pattern, text, re.I):
                stated = int(match.group(1))
                if stated != int(field.value):
                    out.append(Finding(
                        "contradicts_evidence",
                        f"cites '{name}' = {field.value}, but says {stated} — the "
                        f"adaptation is reasoning about a room other than the one "
                        f"recorded", adaptation=a))
                    return out          # one is enough; the citation is unsound
    return out


def _unechoed_evidence(a: dict, profile) -> list[Finding]:
    """A cited field whose value appears nowhere in what the adaptation says.

    The Marathi/Telugu case. The citation looks like grounding and is not: the
    model reached for a real field name to justify a claim it got from
    somewhere else — here, from the chapter's Telugu-region place names.

    ADVISORY. A legitimate adaptation can cite `class_size` and say "with a
    large class" without quoting the number, so this asks a reviewer to look
    rather than refusing on its own. Booleans are exempt — `has_paper = False`
    has no value to echo.

    ECHOED MEANS REFERRED TO, NOT QUOTED. This first asked for the field's whole
    value to appear verbatim in the text, which is right for `home_languages =
    "Marathi"` and useless for anything longer. A real school profile records
    prose — "The class understands basic concepts but prerequisite mastery is
    inconsistent" — and no adaptation will ever contain that sentence, so every
    citation of it warned. Measured on one chapter: 23 of 26 citations flagged,
    including `literacy_level = "Below grade level"` against a change that used
    all three words but not in that order. A check that fires on seven of every
    eight entries is one a reviewer learns to scroll past, and the real instance
    then goes past with it.

    So the test is now overlap of content words, at a quarter of the recorded
    value. On the same chapter that flags 5 of 26, and all five are the defect
    this check is for — including `home_languages = "Telugu, Hindi, Urdu"` cited
    by a change that never names a language.
    """
    out: list[Finding] = []
    text = (f"{a.get('change') or ''} {a.get('reason') or ''}").lower()
    for name in a.get("evidence") or []:
        field = profile.get(name)
        if field is None or isinstance(field.value, bool):
            continue
        values = [str(v).strip().lower() for v in _values_of(field) if str(v).strip()]
        if not values:
            continue
        if any(v in text for v in values):
            continue                                  # quoted outright
        if _echoes(values, text):
            continue                                  # referred to in its own words
        shown = ", ".join(str(v) for v in _values_of(field))[:60]
        out.append(Finding(
            "unechoed_evidence",
            f"cites '{name}' ({shown}) but never mentions it — check the change "
            f"is about the recorded value and not something the model inferred "
            f"elsewhere", adaptation=a))
    return out


def _wrong_target(a: dict, row: dict) -> list[Finding]:
    """§18: "Require every adaptation to name the supported mastery target."

    Matched loosely — root-word overlap against the target and the concept list
    — because the model is asked to name what it supports in its own words and
    an exact-string requirement would only teach it to copy the target verbatim
    into every entry, which measures nothing.
    """
    if a.get("type") == plan_module.NO_CHANGE:
        return []
    supports = a.get("supports") or []
    if not supports:
        return [Finding("wrong_target",
                        "names nothing it helps the class reach", adaptation=a)]
    if row is None:
        return []

    academic = row.get("academic") or {}
    target_words = _words(academic.get("masteryTarget"))
    for concept in academic.get("requiredConcepts") or []:
        target_words |= _words(concept)
    target_words |= _words((row.get("knowledgeChain") or {}).get("gained"))
    if not target_words:
        return []

    # Also allowed: naming the target by its role rather than its words. A plan
    # that says it supports "mastery_target" is using the framework's own
    # vocabulary from §7's example, and rejecting that would reject the spec.
    for claim in supports:
        if claim.strip().lower().replace(" ", "_") in (
                "mastery_target", "masterytarget", "required_concepts", "access"):
            return []
        if _words(claim) & target_words:
            return []
    return [Finding("wrong_target",
                    f"supports {supports} — none of which is this topic's target "
                    f"or a required concept", adaptation=a)]


def _resource_contradiction(a: dict, profile) -> list[Finding]:
    """§10: "Required materials and technology must actually be available."

    Only runs when the room's materials were actually recorded. A school that
    has told us nothing gets no feasibility verdict — and crucially not a PASS,
    which is why `materials_recorded` exists: an empty `available` set would
    otherwise reject every substitution, and a check that fires on every entry
    at a school with no setup data is a check somebody switches off.

    A MATERIAL BEING AVOIDED IS NOT A MATERIAL BEING REQUIRED. "Since there is
    no paper, draw on the board" names paper in order to route around it, and
    refusing that is refusing the adaptation for doing what the constraint
    asked. `_AVOIDING` looks at the short run-up to each named material and
    skips the ones that are being ruled out rather than called for.

    Still a heuristic over prose, and it will be wrong in both directions on a
    sentence convoluted enough. It is worth having because the failure it
    catches — an activity that cannot be run in the room it was written for —
    reaches a teacher mid-lesson, and the cost of a false positive is one
    advisory a reviewer can overrule.
    """
    if a.get("type") == plan_module.NO_CHANGE:
        return []
    change = a.get("change") or ""
    if not profile_module.materials_recorded(profile):
        return []

    named = set()
    for match in _MATERIAL_WORDS.finditer(change):
        run_up = change[max(0, match.start() - 40):match.start()]
        if _AVOIDING.search(run_up):
            continue                      # named in order to be avoided
        named.add(match.group(0).lower())
    if not named:
        return []

    have = profile_module.available_materials(profile)
    # `board` and `chalk` travel together, as do the digital words: a room with
    # a projector recorded and no "screen" field has a screen.
    aliases = {"chalk": "board", "screen": "projector", "slide": "projector",
               "video": "projector", "printout": "paper", "print-out": "paper",
               "printed": "paper", "handout": "paper", "photocopy": "paper",
               "photocopies": "paper", "chart": "paper", "poster": "paper",
               "marker": "board", "tablet": "device", "laptop": "device",
               "computer": "device", "phone": "device"}
    missing = []
    for word in sorted(named):
        canonical = aliases.get(word, word)
        if canonical not in have and word not in have:
            missing.append(word)
    if missing:
        return [Finding("resource_contradiction",
                        f"needs {', '.join(missing)}, which this room does not "
                        f"have recorded (it has: {', '.join(sorted(have)) or 'nothing'})",
                        adaptation=a)]
    return []


def _unsafe_disclosure(a: dict) -> list[Finding]:
    """Sentence by sentence, so the two halves have to be about the same request.

    Scanning the whole `change` at once would flag "Ask pairs to compare the two
    cups. The teacher writes the family of shapes on the board." — two innocent
    sentences whose words happen to co-occur.
    """
    change = a.get("change") or ""
    for sentence in re.split(r"[.;\n]", change):
        if _SINGLES_OUT.search(sentence):
            return [Finding("unsafe_disclosure",
                            "singles out the children who lack something, or asks "
                            "them to identify themselves", adaptation=a)]
        if _ASKS_OF_CHILDREN.search(sentence) and _PERSONAL_SUBJECT.search(sentence):
            return [Finding("unsafe_disclosure",
                            "asks children to reveal personal or household "
                            "circumstances", adaptation=a)]
    return []


# A citation counts as echoed when this much of the recorded value's content
# reappears in the adaptation. Calibrated on a real chapter rather than chosen:
# at 0.5 twelve of twenty-six citations flag and most are honest paraphrase; at
# 0.25 five flag and every one is a citation the change never actually uses.
_ECHO_RATIO = 0.25


def _echoes(values: list[str], text: str) -> bool:
    """Does the text refer to any of these recorded values in its own words?"""
    said = _words(text)
    for value in values:
        content = _words(value)
        if not content:
            # Nothing but stopwords or short tokens — a number, or "none".
            # Verbatim containment already had its chance above.
            continue
        if len(content & said) / len(content) >= _ECHO_RATIO:
            return True
    return False


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", str(text or "").lower())
            if w not in _STOP}


_STOP = frozenset("""
this that they them their there when what which with from into over under about
will would could should must have been being does doing than then them these
those your yours student students learner learners teacher teachers class
children child able understand understanding know knowing help helps helping
""".split())


# ── The entry point ──────────────────────────────────────────────────────────

def run(plan: dict, *, contract_rows: dict, profile,
        activations_by_index: dict[int, list], policy: dict = None) -> dict:
    """Mark every adaptation accepted or rejected, and settle the plan's verdict.

    Mutates `plan` in place (setting `accepted` / `rejectedBecause`) and returns
    the report. Both, deliberately: the plan travels on to Generation carrying
    its own verdicts so nothing downstream has to re-derive them, and the report
    is what shadow mode logs and what a reviewer reads.

    `policy` is the reinforcement policy in force for this cohort, or None. It
    influences ONE step — the per-topic cap, where it re-orders the survivors and
    may lower the limit. Every check above that point is deliberately blind to
    it: a cohort's ratings may change which of several sound adaptations survives
    a cap, and may never make an unsound one pass.
    """
    findings: list[Finding] = []
    adaptations = plan.get("adaptations") or []

    for a in adaptations:
        index = a.get("topicIndex")
        row = contract_rows.get(index)
        activations = activations_by_index.get(index) or []

        mine: list[Finding] = []
        mine += _schema(a)
        if not mine:
            mine += _inactive_factor(a, activations)
            mine += _provenance(a, profile, activations)
            # AFTER provenance, because both of these presuppose the cited field
            # exists and is readable — there is nothing to contradict otherwise.
            mine += _contradicts_evidence(a, profile)
            mine += _wrong_target(a, row)
            mine += _resource_contradiction(a, profile)
            mine += _unsafe_disclosure(a)

        if row is None and index is not None:
            mine.append(Finding("schema",
                                f"names topic T{index}, which is not in this window",
                                adaptation=a))

        a["accepted"] = not mine
        a["rejectedBecause"] = [f["message"] for f in mine]
        findings += mine

        # ADVISORY, and therefore gathered after the accept decision: an
        # unechoed citation is a reason to look, not a reason to refuse. It
        # rides on the adaptation as `warnings` so whatever renders the plan can
        # show it beside a change that was accepted.
        warnings = _unechoed_evidence(a, profile)
        a["warnings"] = [f["message"] for f in warnings]
        findings += warnings

    # ── Per-topic cap (§2 minimal intervention, §18 teacher overload) ────────
    #
    # Applied AFTER the per-adaptation checks so the cap drops surplus from among
    # the ones that actually passed. Dropping first would let a rejected entry
    # consume a slot a good one needed.
    # THE REINFORCEMENT POLICY ENTERS HERE, and only here. It does two things:
    # it re-orders the survivors within each topic so the cap drops the ones this
    # cohort's teachers found least useful, and it may lower the cap itself. It
    # cannot accept anything the checks above rejected, cannot raise the cap, and
    # cannot reach the equity or safety verdicts — those are settled before this
    # point on rules that do not consult it.
    #
    # With no policy `rank` is confidence order, which is what the plan was
    # already sorted into, and `cap` is the unchanged default. The behaviour is
    # then identical to before this loop existed.
    cap = plan_module.MAX_ADAPTATIONS_PER_TOPIC
    order = list(adaptations)
    if policy:
        from reinforcement import render as render_module
        cap = render_module.budget(policy, plan_module.MAX_ADAPTATIONS_PER_TOPIC)
        by_topic: dict[int, list] = {}
        for a in adaptations:
            by_topic.setdefault(a.get("topicIndex"), []).append(a)
        order = [a for index in sorted(by_topic, key=lambda i: (i is None, i))
                 for a in render_module.rank(by_topic[index], policy)]

    kept_by_topic: dict[int, int] = {}
    for a in order:
        if not a.get("accepted") or a.get("type") == plan_module.NO_CHANGE:
            continue
        index = a.get("topicIndex")
        kept_by_topic[index] = kept_by_topic.get(index, 0) + 1
        if kept_by_topic[index] > cap:
            a["accepted"] = False
            message = (f"beyond the {cap}-change limit for one period — kept the "
                       f"changes this cohort's feedback favours"
                       if policy else
                       f"beyond the {cap}-change limit for one period — kept the "
                       f"higher-confidence changes")
            a["rejectedBecause"] = a.get("rejectedBecause") or []
            a["rejectedBecause"].append(message)
            findings.append(Finding("teacher_overload", message, adaptation=a))

    # ── The plan-level judgement (§10's "residual") ─────────────────────────
    #
    # The one thing no rule decides, and it takes the plan with it rather than
    # one entry: "does any adaptation lower rigor" is a question about the set.
    lowered = bool(plan.get("lowersStandard"))
    if lowered:
        for a in adaptations:
            a["accepted"] = False
            a["rejectedBecause"] = (a.get("rejectedBecause") or []) + [
                "the plan as a whole was judged to lower the standard"]
        findings.append(Finding(
            "lowered_rigor",
            "the model judged that this plan would leave the class knowing less "
            "than the mastery target requires — the whole plan is refused"))

    accepted = [a for a in adaptations if a.get("accepted")]
    by_check: dict[str, int] = {}
    for f in findings:
        by_check[f["check"]] = by_check.get(f["check"], 0) + 1
    warned = sum(1 for a in adaptations if a.get("warnings"))

    return {
        "passed": not lowered,
        "proposed": len(adaptations),
        "accepted": len(accepted),
        "rejected": len(adaptations) - len(accepted),
        # Accepted, but with something a reviewer should look at. Counted apart
        # from `rejected` because the two ask for different actions.
        "warned": warned,
        "byCheck": by_check,
        "findings": list(findings),
        # Named separately because it is the one a reviewer should read first:
        # everything else is a proposal that was caught, and this is the model
        # telling you the whole approach was wrong.
        "lowersStandard": lowered,
    }
