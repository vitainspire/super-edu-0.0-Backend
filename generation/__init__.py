"""GENERATION — the stage between Node 2 and Node 3.

    NODE 1 contract ──┐
                      ├──> GENERATION ──> the six-section material ──> NODE 3
    NODE 2 plan ──────┘      (this package)

Not a node, a stage, and the framework draws the line: Node 1 answers what must
be mastered, Node 2 answers what the room changes about the route, and generation
answers *how those two decisions appear in the final prep material*. It decides
nothing on its own — everything it writes traces to a contract field or a plan
adaptation.

It ran inside Node 1 until the master orchestration was split, spent a while
archived in `_deferred/`, and came back here when the question "does Node 2
actually change anything" needed an answer. `compose.py` is that archived module,
unchanged in substance: imports rewired, `ChapterState` dropped, and one new
block appended to the per-topic prompt.

THAT ONE BLOCK IS THE GENERATOR CONTRACT. §18 names "Generation ignores Node 2"
as a failure mode and gives its guardrail as "context adoption metric + generator
contract". Node 3 computes the metric; `reinforcement.py` is the contract, and it
had to exist first — a metric measuring whether the generator used a plan it was
never shown measures nothing.

    compose.py        the sheet writer, and the targeted repair beside it
    reinforcement.py  how Node 2's accepted adaptations reach the prompt
    adapt.py          contract -> the state compose.py reads
    graph.py          the stage: run_generation(contract=..., context_plan=...)
    compare.py        the A/B — the same chapter with the plan and without
    cli.py            python -m generation.cli compare --help
    app_template.html the viewer the A/B fills in
    tests/            python -m generation.tests.test_generation

THE BASELINE ARM'S PROMPT IS BYTE-IDENTICAL to what this stage produced before
Node 2 existed, because `reinforcement.block()` returns an empty string when
there is no plan. That property is what makes the A/B an experiment rather than a
demonstration, and `tests/` asserts it first.
"""

from .reinforcement import accepted_for, block as reinforcement_block  # noqa: F401

__all__ = ["accepted_for", "reinforcement_block"]
