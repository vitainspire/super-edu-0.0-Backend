"""NODE 3 of the master orchestration: validation.

The last node in the chain, and the only one that judges rather than derives.

    NODE 1  the academic contract          — what must be mastered
      |
      v
    NODE 2  the Context Reinforcement Plan — what the room changes about the route
      |
      v
    GENERATION  the six-section prep material
      |
      v
    NODE 3  the verdict                                        <- this package
            is it well made, does it teach, and did the
            pipeline do what it said it would

THREE QUESTIONS, THREE PASSES, and they are genuinely different questions:

    checks.py     IS IT WELL MADE — structure, textbook grounding, continuity
                  between periods, grade fit, timing, whether it used the
                  activity it was given. Mostly deterministic, plus one
                  chapter-level consistency call.
    learner.py    DOES IT TEACH — a simulated child is put through the sheet and
                  scored on four dimensions, two of them hard gates. Roughly one
                  call per sheet.
    integrity.py  DID THE PIPELINE DO WHAT IT SAID — the two checks only this
                  node is positioned to make, and both are the far end of a hook
                  left deliberately in an earlier node.

WHERE THE FIRST TWO CAME FROM. `checks.py` and `learner.py` are
`prep_flow/agents/validation.py` and `simulation.py`, which ran inside Node 1
until the master orchestration was split into nodes and were archived at
`_deferred/` waiting for this package. They are revived UNCHANGED IN SUBSTANCE —
imports rewired, `ChapterState` dropped, nothing else. Every check, every
threshold and every prompt in them was earned by a defect that shipped, and
`adapter.py` exists precisely so that none of it had to be rewritten to fit a new
input shape.

WHAT integrity.py ADDS, and why it could not have been written before now:

  * **Target preservation.** `prep_flow/contract.py` fingerprints the five fields
    nothing downstream may move, and its docstring says "Node 3 recomputes the
    fingerprint over what came back." This is that. A mastery target that moved
    is a hash mismatch, not a judgement.
  * **Context adoption.** `context_flow/shadow.py` reports §17's
    `contextAdoption` as None with the reason "requires the generated sheet". The
    sheet exists here. An accepted, gated, feasible adaptation that the material
    simply does not contain means the contextual layer ran, cost a call, and
    changed nothing — which the framework names as a failure mode and asks to be
    measured.

TWO KINDS OF BAD NEWS, KEPT APART. `needs_review` is a weaker version of the
right lesson; `refuse` is a different lesson. Only the second is a sheet teaching
something nobody approved, and collapsing them would put "the Refresher is thin"
in the same bucket people learn to skim.

THIS NODE DOES NOT REPAIR. The old in-package pipeline ran validation → repair →
validation with three round counters and a revision-selection pass. All of that
belonged to generation. Here the node names the sections that need rewriting
(`repair_brief`) and stops — which makes it stateless and idempotent, and makes a
verdict reproducible months later from a logged contract and a logged sheet.

Everything Node 3 needs is in this folder:

    graph.run_validation(contract=..., materials=..., plan=...)   the node
    graph.shippable(state)     the topics that may be published, by index
    graph.repair_brief(state)  which topics, which sections, and why
    cli                        python -m validation_flow.cli --help
    tests/                     python -m validation_flow.tests.test_validation_flow
                               python -m validation_flow.tests.test_validation_checks
"""

from .verdict import NEEDS_REVIEW, REFUSE, SHIP, VERDICT_VERSION  # noqa: F401

__all__ = ["SHIP", "NEEDS_REVIEW", "REFUSE", "VERDICT_VERSION"]
