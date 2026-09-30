"""Which skills each agent is told to load — the routing table, in one place.

The eighteen files under `skills/` are all MOUNTED for all three agents already
(`workspace.SKILL_SOURCES`, `permissions._library_rules`), and `SkillsMiddleware`
shows every one of their descriptions to every agent. So nothing here grants
access. What it decides is the much more consequential thing: which ones an agent
is TOLD to load, in what order, before it starts.

WHY BEING MOUNTED IS NOT ENOUGH, and this is the whole reason this module exists.
A skill the agent merely *could* load is a skill it loads when it happens to
recognise its own need — which is exactly the case where it does not, because a
stage that has never heard of the below-grade child does not notice that it has
forgotten them. `skills/README.md` states the requirement plainly:

    `learning-goals` and `core-pedagogy` should be in **every** stage's prompt.
    [...] Every goal-carrying rule lives in a skill some stage already loads, but
    the gate itself only runs if `learning-goals` is present - a stage that loads
    only its own skill will produce a locally correct artifact and miss the
    outcomes.

That is a wiring requirement written in prose in a README, which is a requirement
nothing checks. Here it is `ALWAYS`, it is rendered into all three prompts by one
function, and `deep_agents.cli doctor` fails if a stage loses it.

THE ORDER IS THE TEACHING ORDER, not alphabetical and not arbitrary. Goals and
constants first (what any period owes), then the stage's own craft skill, then
the skills that answer questions the stage will actually hit, then the subject.
An agent reads this list top to bottom and loads on demand; a list that opened
with the narrow skill would have it reasoning about section mechanics before it
knew what a section is for.

WHAT IS DELIBERATELY NOT ROUTED TO A STAGE. Node 2 is not pointed at
`assessment-alignment`, `self-study` or `stretch-design`. Those three own G3, G4
and G5 — what the period assesses, sends home and offers a fast finisher — and
all three are settled upstream by the time Node 2 runs. Node 2 adapts the ROUTE
and may not touch the destination (architecture §9), so naming them would be
inviting it into decisions the equity gate would then have to refuse. It still
gets `learning-goals`, whose check 9 ("standard intact") is the part of that file
Node 2 is actually answerable for.
"""
from __future__ import annotations

from typing import Optional

from .workspace import SKILLS_ROOT

# The three stages, named once. These are the `AgentRun` labels' suffixes and the
# keys of `BY_STAGE`; nothing else should spell them.
CURRICULUM = "curriculum"
EXPERIENCE = "experience"
CONTEXT = "context"

STAGES = (CURRICULUM, EXPERIENCE, CONTEXT)


# ── The universal two ────────────────────────────────────────────────────────
#
# `core-pedagogy` says what the room is; `learning-goals` says what a period has
# to achieve in it and carries the ten-check self-critique gate. Neither is
# optional at any stage, and the second is the one that is easy to lose: it is
# the only file that asks a stage to grade its own draft, so a stage without it
# does not fail loudly, it just quietly stops asking.
ALWAYS: tuple[str, ...] = ("learning-goals", "core-pedagogy")


# ── Required before answering, vs available on demand ────────────────────────
#
# THE DISTINCTION THE FIRST LIVE RUN FORCED. Progressive disclosure worked
# exactly as specified — every agent opened the library, and each opened three
# to five files of the seven to thirteen it was pointed at. The design says load
# on demand, so that is the system working.
#
# Then look at WHICH files went unopened: `differentiation`, `assessment-
# alignment`, `self-study`, `stretch-design`. The four that own G1, G3, G4 and
# G5. Every agent had read `learning-goals` and was therefore under instruction
# to check its draft for a floor, exam contact, a take-home and a stretch — and
# not one of them opened the skill that says what a good answer to those checks
# looks like. The gate was read; the material to answer it was not.
#
# An agent cannot recognise it needs `stretch-design` from a description, because
# the whole reason it needs it is that it has not yet thought about the fast
# finisher. Progressive disclosure works for a skill you reach for when a task
# turns out to need it. It does not work for the skill whose absence is the
# defect — that one has to be in front of you before you start.
#
# So: the goal-owning skills for a stage are REQUIRED, everything else stays on
# demand. Kept as a separate set rather than folded into ALWAYS because it is
# per stage — Node 2 must not be handed the G3/G4/G5 skills at all (see the
# module docstring).
REQUIRED_BY_STAGE: dict[str, tuple[str, ...]] = {
    # The chain is where G1's starting line and G4's home-available anchors are
    # decided. `grounding` because every claim this stage makes is a claim about
    # a printed page, and it is cheap to skip and expensive to be wrong about.
    CURRICULUM: ("curriculum-reasoning", "grounding", "differentiation"),
    # ALL FIVE GOALS ARE DECIDED IN THE PERIOD, so this stage needs a home skill
    # for each before it drafts, not after. That makes its required set nearly
    # its whole routed set, and that is the honest answer rather than a failure
    # of the design: the objection to loading everything is that most of it is
    # irrelevant, and here almost none of it is.
    EXPERIENCE: ("experience-design", "lesson-design", "differentiation",
                 "engagement", "real-world-connection", "assessment-alignment",
                 "self-study", "stretch-design"),
    # Node 2's own two: the checks it will be judged on, and the evidence tiers
    # that decide whether a local example is allowed at all. `differentiation`
    # because widening a section rather than lowering it is the rule this stage
    # breaks most easily.
    CONTEXT: ("contextual-reinforcement", "real-world-connection",
              "differentiation"),
}


def required_for(stage: str) -> tuple[str, ...]:
    """What this stage opens before it starts, in load order."""
    ordered = [*ALWAYS, *REQUIRED_BY_STAGE.get(stage, ())]
    seen: set[str] = set()
    return tuple(s for s in ordered if not (s in seen or seen.add(s)))


# ── Per stage ────────────────────────────────────────────────────────────────
#
# Each entry follows the skill's own `Load when` line — the frontmatter
# description is the contract, and a routing decision that contradicts it is a
# bug in one of the two. Read them with `python -m deep_agents.cli skills`.
BY_STAGE: dict[str, tuple[str, ...]] = {
    # Agent 1 reasons about the chapter: prerequisites, anchors, misconceptions,
    # the forward chain. `grounding` because every claim it makes is a claim
    # about a printed page; `assessment-alignment` for the chapter-level half of
    # that skill ("checking a chapter for exam coverage"), which is the only
    # place exercise coverage can be seen whole; `differentiation` because the
    # prerequisite repair and the floor are written here and a floor that is
    # just a weaker `gained` is the failure that skill names; `self-study`
    # because whether an anchor object is available at home is decided when the
    # anchor pool is chosen, not later.
    CURRICULUM: (
        "curriculum-reasoning",
        "grounding",
        "misconception-analysis",
        "differentiation",
        "assessment-alignment",
        "self-study",
    ),
    # Agent 2 designs the route through one period, so it meets every goal at
    # once and gets the widest list. This is the stage the README's coverage map
    # points at most often, and it is where G1 and G5 have to be resolved into
    # ONE task rather than two tracks.
    EXPERIENCE: (
        "experience-design",
        "lesson-design",
        "activity-design",
        "differentiation",
        "engagement",
        "real-world-connection",
        "assessment-alignment",
        "self-study",
        "stretch-design",
        "misconception-analysis",
    ),
    # Agent 3 produces a patch. `real-world-connection` is the one addition that
    # matters most here: its three tiers of evidence are exactly the provenance
    # question Node 2's gate enforces, and the "generic because no local profile
    # was recorded" case is Node 2's most common one.
    CONTEXT: (
        "contextual-reinforcement",
        "real-world-connection",
        "language-support",
        "differentiation",
        "engagement",
    ),
}


# ── Subject, when one fits ───────────────────────────────────────────────────
#
# Matched on a lowercased substring of the subject name, because the same
# subject reaches this system as "Maths", "Mathematics" and "MAT" depending on
# which corpus it came from. Order matters: the first hit wins.
#
# A subject that matches nothing — EVS, Environmental Studies, Science — gets NO
# subject skill, and that is correct rather than a gap. `early-math` and
# `literacy` are subject KNOWLEDGE, and pointing an EVS chapter at the
# misconceptions place value produces would be worse than pointing it at
# nothing.
_SUBJECT_SKILLS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("math", "maths", "mat", "ganit", "numeracy"), "early-math"),
    (("english", "telugu", "hindi", "urdu", "marathi", "language", "literacy",
      "reading"), "literacy"),
)


def subject_skill(subject: Optional[str]) -> Optional[str]:
    """The subject knowledge file for this subject, or None.

    None is a real answer. See the note above `_SUBJECT_SKILLS`.
    """
    text = str(subject or "").strip().lower()
    if not text:
        return None
    for needles, skill in _SUBJECT_SKILLS:
        if any(needle in text for needle in needles):
            return skill
    return None


# ── What a stage is told ─────────────────────────────────────────────────────

def for_stage(stage: str, *, subject: Optional[str] = None) -> tuple[str, ...]:
    """Every skill this stage is pointed at, in load order, deduplicated.

    Unknown stage returns just `ALWAYS` rather than raising: a caller that
    mistypes a stage name should get an agent that still knows what a period
    owes a child, not a crash — and `doctor` is what catches the typo.
    """
    ordered = [*ALWAYS, *BY_STAGE.get(stage, ())]
    fitted = subject_skill(subject)
    if fitted:
        ordered.append(fitted)

    seen: set[str] = set()
    return tuple(s for s in ordered if not (s in seen or seen.add(s)))


def prompt_block(stage: str, *, subject: Optional[str] = None) -> str:
    """The lines injected into the agent's system prompt.

    RENDERED, NOT LISTED. "Load `differentiation`" tells an agent a filename;
    saying what the file is for is what makes it load the thing at the moment it
    needs it rather than all of them up front and none of them again. The one
    sentence per skill is deliberately shorter than the file's own description —
    the middleware already shows the full descriptions, and repeating them here
    would spend the prompt budget the skills themselves need.
    """
    names = for_stage(stage, subject=subject)
    # Padded to the longest name in THIS stage's list, so the reasons line up as
    # a column. A ragged right edge here reads as three unrelated lists, which is
    # the same argument `reinforcement.reason.payload` makes about its dials.
    # `  - ` + backticks around the name is 6 characters of furniture.
    column = max((len(n) for n in names), default=0) + 8
    must = set(required_for(stage))
    # A MARK, NOT A SECOND LIST. Splitting these into two blocks reads as two
    # unrelated sets of skills; one list in load order with the required ones
    # marked keeps the reading order and the loading order the same thing.
    lines = [
        ("  * " if name in must else "  - ") + f"`{name}`".ljust(column - 4)
        + _wrap(_WHY.get(name, ""), indent=column)
        for name in names]
    legend = (f"    * = open these {len(must)} before you start. "
              f"- = open when the work reaches it.")
    return "\n".join([*lines, "", legend, "", _HOW_TO_LOAD])


# THE PATH, NOT JUST THE NAME, and this was learned from a real run. The first
# version of this block named skills the way a person would — `differentiation`
# — and left the model to map that to a file using the index `SkillsMiddleware`
# puts elsewhere in the prompt. On a Maths chapter through llama-3.3-70b the
# three agents made fifteen tool calls between them and not one was `read_file`:
# every skill was mounted, listed, described and routed, and none was opened.
#
# A name is a reference the model has to resolve. A path is a call it can make.
# The cost of spelling it out is four lines of prompt; the cost of not doing it
# was the entire pedagogy library going unread while every check reported green.
_HOW_TO_LOAD = (
    "    To load one, call `read_file` on its path — the name IS the directory:\n"
    "        read_file(file_path=\"/skills/<name>/SKILL.md\", limit=1000)\n"
    "    e.g. read_file(file_path=\"/skills/learning-goals/SKILL.md\", limit=1000)\n"
    "    `limit=1000` matters: the default of 100 lines truncates most of these\n"
    "    files mid-section, and a half-read skill is worse than an unread one."
)


def _wrap(reason: str, *, indent: int, width: int = 88) -> str:
    """The reason text, hard-wrapped and hanging-indented under its own column.

    Only the REASON is wrapped — the caller has already laid out the bullet and
    the name, and handing those to a word-splitter is how the leading indent
    disappears. Done at all because this text is pasted into a system prompt
    hand-wrapped at 88 columns, and one 200-character line in the middle of it
    is the sort of thing that makes an instruction look like data.
    """
    lines, current = [], ""
    for word in (reason or "").split():
        candidate = f"{current} {word}" if current else word
        if indent + len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return ("\n" + " " * indent).join(lines)


# One line each, from the skill's own frontmatter, cut to the reason THIS
# pipeline loads it. Kept beside the routing table so adding a skill to a stage
# and explaining why it is there are the same edit.
_WHY: dict[str, str] = {
    "learning-goals": "the five outcomes, and the ten-check gate you run on your "
                      "own draft before answering. Load FIRST.",
    "core-pedagogy": "what this room actually is, and demand vs content.",
    "curriculum-reasoning": "the rules, the chain-slip failure and the bounds.",
    "grounding": "load before asserting anything about what the page contains.",
    "misconception-analysis": "load before writing a misconception or a probe.",
    "differentiation": "the floor for the child furthest behind, and the open top.",
    "assessment-alignment": "the printed questions, the exercise ledger, the "
                            "notebook line.",
    "self-study": "the take-home question and its five conditions.",
    "experience-design": "the cognitive trajectory and its three marks.",
    "lesson-design": "the six sections, the seam, and the minute budget.",
    "activity-design": "load before naming anything the class does.",
    "engagement": "the participation floor and the attention arc.",
    "real-world-connection": "the three tiers of evidence; mention vs application.",
    "stretch-design": "what the child who finishes at minute five is given.",
    "contextual-reinforcement": "every check the equity gate will run on you. "
                                "Load FIRST of the stage skills.",
    "language-support": "load when home language and medium of instruction differ.",
    "early-math": "subject knowledge for primary mathematics.",
    "literacy": "subject knowledge for primary language.",
}


# The instruction that turns `learning-goals` from a file into a step. Named here
# rather than written into three prompts, because the whole point of the gate is
# that no stage is exempt and three copies is three chances to drift.
# Pre-wrapped at the three-space hanging indent every numbered step in those
# prompts uses, because it is substituted INTO such a step.
# Each of `learning-goals`' checks, and the skill that says what a good answer
# to it looks like. Data rather than prose so the gate can be rendered PER STAGE
# — Node 2 is deliberately not routed three of these, and a gate that sent it to
# `stretch-design` would be pointing at a door that is not there. The agent's
# reasonable response to that is to skip the check.
GATE_CHECK_SKILLS: tuple[tuple[str, str], ...] = (
    ("floor named", "differentiation"),
    ("application present", "real-world-connection"),
    ("participation counted", "engagement"),
    ("exam contact made", "assessment-alignment"),
    ("notebook line left", "assessment-alignment"),
    ("take-home free", "self-study"),
    ("ceiling open", "stretch-design"),
    ("standard intact", "contextual-reinforcement"),
)


def gate_instruction(stage: str, *, subject: Optional[str] = None) -> str:
    """The self-critique step, naming only the skills THIS stage actually has.

    THE POINTER IS THE POINT. The first live run had every agent read
    `learning-goals` — so every agent knew it had to check its draft for a
    floor, a take-home and a stretch — and not one of them opened the skill
    that says what those look like. Being told to check something you have no
    material to check against produces a sentence, not a check.

    Checks whose skill this stage does not carry are listed as settled
    elsewhere rather than dropped in silence: an agent that sees eight checks
    where the file it just read described ten will go looking for the other
    two, and the honest answer is that they are not its call.
    """
    routed = set(for_stage(stage, subject=subject))
    mine = [(check, skill) for check, skill in GATE_CHECK_SKILLS if skill in routed]
    theirs = sorted({check for check, skill in GATE_CHECK_SKILLS
                     if skill not in routed})

    width = max((len(c) for c, _ in mine), default=0) + 2
    lines = [
        "Run the ten-check gate at the end of `learning-goals` on your own draft.",
        "   Each check has a skill that says what a good answer to it looks like.",
        "   If you cannot answer one, open that skill and then answer it:",
        "",
    ]
    lines += [f"       {check.ljust(width)}`{skill}`" for check, skill in mine]
    if theirs:
        lines += ["", "   " + _wrap(
            ", ".join(theirs) + " are settled before you run and are not yours "
            "to change. Read them as constraints you must not break, not as "
            "work to do.", indent=3)]
    lines += [
        "",
        "   Where a check genuinely cannot be met for this topic, say which one",
        "   and why - a declared gap is actionable downstream, and a decorative",
        "   sentence that passes the check on paper is not. Apply the",
        "   anti-boilerplate test to every closer you write: if it would work",
        "   verbatim for a different topic, it has passed the check without",
        "   meeting the goal.",
    ]
    return "\n".join(lines)


# Kept for callers that want the stage-independent wording. The three prompts
# use `gate_instruction(stage)`; this is what `doctor` prints.
GATE_INSTRUCTION = gate_instruction(EXPERIENCE)


# ── What `doctor` checks ─────────────────────────────────────────────────────

def on_disk() -> tuple[str, ...]:
    """Every skill directory that actually exists, sorted."""
    if not SKILLS_ROOT.is_dir():
        return ()
    return tuple(sorted(p.name for p in SKILLS_ROOT.glob("*")
                        if p.is_dir() and (p / "SKILL.md").is_file()))


def routed() -> tuple[str, ...]:
    """Every skill named by `ALWAYS`, by any stage, or by the subject table."""
    names = {*ALWAYS}
    for stage_skills in BY_STAGE.values():
        names.update(stage_skills)
    names.update(skill for _, skill in _SUBJECT_SKILLS)
    return tuple(sorted(names))


def missing() -> tuple[str, ...]:
    """Named in the routing table, absent from disk.

    A prompt that tells an agent to load a file that is not there spends a tool
    call on an error and then carries on without it — silently, and looking
    exactly like a run that chose not to load it.
    """
    return tuple(s for s in routed() if s not in on_disk())


def unrouted() -> tuple[str, ...]:
    """On disk, named by no stage.

    Not an error — `SkillsMiddleware` still offers it and an agent may still load
    it on its own initiative. It is reported because a skill somebody wrote and
    nobody is pointed at is far more likely to be a wiring oversight than a
    deliberate optional extra.
    """
    return tuple(s for s in on_disk() if s not in routed())
