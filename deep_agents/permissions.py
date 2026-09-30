"""Who may write where — architecture §6, as rules the harness enforces.

`deepagents` evaluates these in declaration order and the FIRST MATCH WINS, with
"no rule matched" meaning allow. Every set below therefore ends in a catch-all
deny on write: an agent that invents a path nobody anticipated must be refused,
not permitted, and relying on a default of allow to be safe is how a curriculum
agent ends up writing over `/workspace/reinforcement/`.

WHAT THESE ARE ACTUALLY PROTECTING. Not the files — the files are scratch
(`workspace.py`). What they protect is the ORDER OF AUTHORITY the pipeline is
built on:

    curriculum decides what must be learned      -> only curriculum writes it
    lesson design decides the route to it        -> only lesson design writes it
    Node 2 decides what the room changes         -> only Node 2 writes it, and
                                                    it may not touch either of
                                                    the two above

That last clause is architecture §9 stated as a filesystem rule. Node 2 is not
allowed to redefine the academic contract, and the cheapest place to make that
true is here, where a `write_file` at the wrong path fails rather than succeeds
and gets caught two stages later by the equity gate.

`/skills/` and `/memory/` are checked-in source — the pedagogy library and the
system's own operating rules. An agent proposing to edit them is proposing to
change how every future chapter is reasoned about, which is §11's example of a
thing a human approves. `mode="interrupt"` is that approval; `deny` is what a
background run gets instead, because an interrupt nobody is present to answer is
a hung batch job rather than a safe one.
"""
from __future__ import annotations

from deepagents import FilesystemPermission

from .workspace import MEMORY_MOUNT, SKILLS_MOUNT, WORKSPACE_MOUNT

READ = ["read"]
WRITE = ["write"]
BOTH = ["read", "write"]

_CURRICULUM_OUT = f"{WORKSPACE_MOUNT}curriculum/**"
_LESSON_OUT = f"{WORKSPACE_MOUNT}lesson_design/**"
_REINFORCEMENT_OUT = f"{WORKSPACE_MOUNT}reinforcement/**"
_NOTES = f"{WORKSPACE_MOUNT}notes/**"


def _library_rules(*, hitl: bool) -> list[FilesystemPermission]:
    """Reading the library is always fine; writing it is the §11 decision."""
    mode = "interrupt" if hitl else "deny"
    return [
        FilesystemPermission(operations=READ, paths=[f"{SKILLS_MOUNT}**",
                                                     f"{MEMORY_MOUNT}**"], mode="allow"),
        FilesystemPermission(operations=WRITE, paths=[f"{SKILLS_MOUNT}**",
                                                      f"{MEMORY_MOUNT}**"], mode=mode),
    ]


def _deny_the_rest() -> FilesystemPermission:
    return FilesystemPermission(operations=WRITE, paths=["/**"], mode="deny")


def curriculum_permissions(*, hitl: bool = False) -> list[FilesystemPermission]:
    """Reads the whole workspace, writes only its own reasoning and its notes."""
    return [
        *_library_rules(hitl=hitl),
        FilesystemPermission(operations=READ, paths=[f"{WORKSPACE_MOUNT}**"], mode="allow"),
        FilesystemPermission(operations=WRITE, paths=[_CURRICULUM_OUT, _NOTES], mode="allow"),
        _deny_the_rest(),
    ]


def lesson_design_permissions(*, hitl: bool = False) -> list[FilesystemPermission]:
    """Reads curriculum's output — that is the whole point of the stage — and
    may not amend it. A designer that could rewrite the prerequisites it was
    handed would make the two stages one stage with a longer prompt."""
    return [
        *_library_rules(hitl=hitl),
        FilesystemPermission(operations=READ, paths=[f"{WORKSPACE_MOUNT}**"], mode="allow"),
        FilesystemPermission(operations=WRITE, paths=[_LESSON_OUT, _NOTES], mode="allow"),
        _deny_the_rest(),
    ]


def context_permissions(*, hitl: bool = False) -> list[FilesystemPermission]:
    """Node 2. Reads everything academic, writes only a patch.

    The explicit deny on the two upstream directories is redundant against the
    catch-all below it and is kept anyway: it is the one rule in this file whose
    absence would be a design error rather than an oversight, so it should be
    visible in the list rather than implied by ordering.
    """
    return [
        *_library_rules(hitl=hitl),
        FilesystemPermission(operations=WRITE,
                             paths=[_CURRICULUM_OUT, _LESSON_OUT], mode="deny"),
        FilesystemPermission(operations=READ, paths=[f"{WORKSPACE_MOUNT}**"], mode="allow"),
        FilesystemPermission(operations=WRITE,
                             paths=[_REINFORCEMENT_OUT, _NOTES], mode="allow"),
        _deny_the_rest(),
    ]


def repair_permissions(*, hitl: bool = False) -> list[FilesystemPermission]:
    """READ-ONLY, and that is the whole design of this agent.

    It exists to look things up in the book and report what it found; it never
    writes the sheet, never amends the contract, and never touches the shared
    library. A repair that could edit the reasoning it was judged against would
    be able to make its own failure disappear.
    """
    return [
        FilesystemPermission(operations=READ, paths=[f"{WORKSPACE_MOUNT}**"], mode="allow"),
        _deny_the_rest(),
    ]
