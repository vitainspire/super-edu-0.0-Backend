"""Check the agent layer without spending a token.

    python -m deep_agents.cli doctor
    python -m deep_agents.cli skills
    python -m deep_agents.cli tools [--agent curriculum|design|context]

WHY A DOCTOR COMMAND. Almost everything that goes wrong with this layer goes
wrong silently. A skill whose frontmatter will not parse is skipped with a log
line nobody reads, and the agent then works from general knowledge instead of
the pedagogy library - producing output that is plausible, wrong in a way that
takes a chapter to notice, and identical in shape to correct output. Same for a
permission rule that matches nothing, and for a schema field renamed out from
under a normaliser.

So `doctor` asserts the things whose absence is invisible.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def _check(label: str, ok: bool, detail: str = "", *, fatal: bool = True) -> bool:
    mark = OK if ok else (BAD if fatal else WARN)
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok or not fatal


def doctor() -> int:
    import os

    from . import permissions as perms
    from . import schemas
    from .workspace import MEMORY_ROOT, SKILLS_ROOT

    good = True
    print("VitaInspire deep agent layer\n")

    # ── The library ──────────────────────────────────────────────────────────
    print("Skills and memory")
    good &= _check("skills directory exists", SKILLS_ROOT.is_dir(), str(SKILLS_ROOT))
    good &= _check("memory/AGENTS.md exists", (MEMORY_ROOT / "AGENTS.md").is_file())

    import yaml

    for skill_dir in sorted(p for p in SKILLS_ROOT.glob("*") if p.is_dir()):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            good &= _check(f"  {skill_dir.name}", False, "no SKILL.md")
            continue
        text = md.read_text(encoding="utf-8")
        # The same parse `SkillsMiddleware` does. A skill that fails it is
        # skipped at runtime with a log line and no other symptom.
        if not text.startswith("---"):
            good &= _check(f"  {skill_dir.name}", False, "no YAML frontmatter")
            continue
        try:
            front = yaml.safe_load(text.split("---", 2)[1])
        except Exception as exc:
            good &= _check(f"  {skill_dir.name}", False, f"unparseable frontmatter: {exc}")
            continue
        name = (front or {}).get("name", "")
        description = (front or {}).get("description", "")
        if not name or not description:
            good &= _check(f"  {skill_dir.name}", False, "missing name or description")
        elif len(description) > 1024:
            good &= _check(f"  {skill_dir.name}", False,
                           f"description is {len(description)} chars (max 1024)")
        else:
            _check(f"  {skill_dir.name}", True, f"{len(text)} chars")

    # ── The wiring requirement ───────────────────────────────────────────────
    #
    # Every skill is mounted for every agent, so none of the checks above can
    # tell you whether a stage is actually POINTED at the ones it needs. That is
    # the failure `skills/README.md` warns about — "a stage that loads only its
    # own skill will produce a locally correct artifact and miss the outcomes" —
    # and it is invisible in output, in logs and in every check above.
    print("\nSkill routing (deep_agents/skillset.py)")
    from . import skillset

    good &= _check("every routed skill exists on disk", not skillset.missing(),
                   ", ".join(skillset.missing()) or f"{len(skillset.routed())} routed")
    _check("every skill on disk is routed to a stage", not skillset.unrouted(),
           ", ".join(skillset.unrouted()) or f"{len(skillset.on_disk())} skills",
           # A warning, not a failure. An unrouted skill is still offered to
           # every agent by the middleware and may still be loaded on the
           # agent's own initiative — it is reported because a file somebody
           # wrote and nobody is pointed at is more often an oversight than a
           # deliberate extra.
           fatal=False)

    for stage in skillset.STAGES:
        routed_here = skillset.for_stage(stage)
        # THE ONE NON-NEGOTIABLE. `learning-goals` carries the ten-check gate,
        # and it is the only file that asks a stage to grade its own draft — so
        # a stage that loses it does not fail loudly, it just stops asking.
        good &= _check(f"  {stage} loads the universal two",
                       set(skillset.ALWAYS) <= set(routed_here),
                       f"{len(routed_here)} skills, "
                       f"{skillset.ALWAYS[0]} first" if routed_here else "")

    # THE GATE MUST NOT POINT AT A DOOR THAT IS NOT THERE. Each of
    # `learning-goals`' checks names the skill that answers it, and a stage
    # sent to a skill it was never routed will reasonably skip the check.
    for stage in skillset.STAGES:
        routed = set(skillset.for_stage(stage))
        text = skillset.gate_instruction(stage)
        stray = [sk for _, sk in skillset.GATE_CHECK_SKILLS
                 if f"`{sk}`" in text and sk not in routed]
        good &= _check(f"  {stage} gate names only skills it has",
                       not stray, ", ".join(stray) or
                       f"{len(routed)} routed")
        must = skillset.required_for(stage)
        good &= _check(f"  {stage} required set is routed",
                       set(must) <= routed,
                       f"{len(must)} opened before starting")

    for stage, subject, expected in (("curriculum", "Mathematics", "early-math"),
                                     ("experience", "Telugu", "literacy"),
                                     ("context", "EVS", None)):
        fitted = skillset.subject_skill(subject)
        _check(f"  subject routing: {subject}", fitted == expected,
               fitted or "no subject skill (correct for EVS/Science)", fatal=False)

    # THE PROMPTS ACTUALLY CARRY IT. Everything above tests the table; this
    # tests that the table reaches a prompt. A routing table nothing renders is
    # the same bug as a policy that never reaches a model — see
    # reinforcement/liveness.py, which exists for exactly this reason.
    from .registry import _CONTEXT_PROMPT, _CURRICULUM_PROMPT, _EXPERIENCE_PROMPT

    for label, template in (("curriculum", _CURRICULUM_PROMPT),
                            ("experience", _EXPERIENCE_PROMPT),
                            ("context", _CONTEXT_PROMPT)):
        good &= _check(f"  {label} prompt renders the skill list and the gate",
                       "{skills}" in template and "{gate}" in template)

    # ── The contract with the pipeline ───────────────────────────────────────
    print("\nSchema parity with the existing normalisers")
    import typing

    from context_flow import plan as plan_module

    def _literals(field):
        return set(typing.get_args(schemas.Adaptation.model_fields[field].annotation))

    good &= _check("adaptation sections match plan.SECTIONS",
                   _literals("section") == set(plan_module.SECTIONS))
    good &= _check("adaptation types match plan.TYPES",
                   _literals("type") == set(plan_module.TYPES))
    good &= _check("adaptation purposes match plan.PURPOSES",
                   _literals("purpose") == set(plan_module.PURPOSES))
    good &= _check("lowers_standard defaults to stopping a plan",
                   schemas.ContextReinforcement().lowers_standard is True)

    chain_fields = set(schemas.ChainEntry.model_fields)
    good &= _check("chain entry carries the seam fields",
                   {"gained", "bridgesTo", "assumes"} <= chain_fields,
                   ", ".join(sorted(chain_fields)))

    # ── Permissions ──────────────────────────────────────────────────────────
    print("\nThe order of authority")
    from wcmatch import glob as wcglob

    def denies(rules, path) -> bool:
        for rule in rules:
            if "write" not in rule.operations:
                continue
            if any(wcglob.globmatch(path, p, flags=wcglob.GLOBSTAR)
                   for p in rule.paths):
                return rule.mode == "deny"
        return False

    good &= _check("Node 2 cannot write the curriculum artifact",
                   denies(perms.context_permissions(), "/workspace/curriculum/x.json"))
    good &= _check("lesson design cannot write the curriculum artifact",
                   denies(perms.lesson_design_permissions(), "/workspace/curriculum/x.json"))
    good &= _check("an unanticipated path is refused, not allowed",
                   denies(perms.curriculum_permissions(), "/etc/passwd"))

    # ── The provider ─────────────────────────────────────────────────────────
    print("\nModel")
    from .model import fallback_model_name, primary_model_name

    has_key = bool(os.environ.get("OPENROUTER_API_KEY"))
    good &= _check("OPENROUTER_API_KEY is set", has_key,
                   "same key ai.py uses")
    _check("primary model", True, primary_model_name())
    _check("fallback model", True, fallback_model_name())

    # ── The seams ────────────────────────────────────────────────────────────
    print("\nThe three seams the flag routes through")
    for module, marker in (("prep_flow.agents.reasoning", "derive_curriculum_reasoning"),
                           ("prep_flow.agents.experience", "derive_experience_plans"),
                           ("context_flow.reasoning", "derive_context_plan")):
        import importlib
        source = Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
        good &= _check(f"  {module}", marker in source and 'get("deep_agents")' in source,
                       "wired" if marker in source else "NOT wired")

    print("\n" + ("All checks passed." if good else
                  "Some checks FAILED — the layer is not safe to switch on."))
    return 0 if good else 1


def show_skills(routing: bool = False) -> int:
    """What the agent sees before it loads anything — the progressive-disclosure
    first pass. If a description here does not make it obvious when to load the
    skill, the agent will not load it.

    `--routing` answers the other half of the question: every skill is MOUNTED
    for every agent, so this listing does not tell you which ones a stage is
    actually told to load. That is `skillset.py`, and it is the thing that
    decides whether a stage remembers the below-grade child.
    """
    import yaml

    from . import skillset
    from .workspace import SKILLS_ROOT

    if routing:
        for stage in skillset.STAGES:
            names = skillset.for_stage(stage)
            print(f"\n{stage}  ({len(names)} routed, before the subject skill)")
            print(skillset.prompt_block(stage))
        print("\nsubject skills, chosen from the subject name")
        for subject in ("Mathematics", "Maths", "English", "Telugu",
                        "Environmental Studies"):
            print(f"  {subject:<24} {skillset.subject_skill(subject) or '(none)'}")
        if skillset.unrouted():
            print(f"\nnot routed to any stage: {', '.join(skillset.unrouted())}")
        return 0

    routed_by = {}
    for stage in skillset.STAGES:
        for name in skillset.for_stage(stage):
            routed_by.setdefault(name, []).append(stage)
    for _, skill in skillset._SUBJECT_SKILLS:
        routed_by.setdefault(skill, []).append("by subject")

    for skill_dir in sorted(p for p in SKILLS_ROOT.glob("*") if p.is_dir()):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        text = md.read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1]) or {}
        name = front.get("name", skill_dir.name)
        stages = routed_by.get(name) or routed_by.get(skill_dir.name)
        print(f"\n{name}"
              + (f"   [{', '.join(stages)}]" if stages else "   [not routed]"))
        print(f"  {front.get('description', '(no description)')}")
    return 0


def show_tools(which: str) -> int:
    from .tools import RunContext, book, knowledge, pedagogy, room

    ctx = RunContext(grade="3", subject="Mathematics", chapter_markdown="<!-- page 1 -->\n")
    sets = {
        "curriculum": [*book.build(ctx), *knowledge.build(ctx, allow_writes=True),
                       *pedagogy.build(ctx)],
        "design": [*pedagogy.build(ctx), *knowledge.build(ctx)],
        "context": room.build(ctx),
    }
    for name in ([which] if which != "all" else sets):
        print(f"\n{name}:")
        for tool in sets[name]:
            first_line = (tool.description or "").strip().split("\n")[0]
            print(f"  {tool.name:36} {first_line[:80]}")
    return 0


def main(argv: list[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="deep_agents",
                                     description="Inspect the deep agent layer.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check everything that fails silently")
    skills_cmd = sub.add_parser(
        "skills", help="what the agent sees before loading a skill")
    skills_cmd.add_argument(
        "--routing", action="store_true",
        help="which skills each stage is TOLD to load, in order")
    tools_cmd = sub.add_parser("tools", help="the toolset each agent is given")
    tools_cmd.add_argument("--agent", default="all",
                           choices=["all", "curriculum", "design", "context"])

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return doctor()
    if args.command == "skills":
        return show_skills(routing=args.routing)
    return show_tools(args.agent)


if __name__ == "__main__":
    sys.exit(main())
