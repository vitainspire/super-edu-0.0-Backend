"""NODE 2 of the master orchestration: local-context & learning-environment reinforcement.

Node 1 (`prep_flow`) answers *what should the learner master, and what learning
experience best supports that target*. This package answers the next question:
*what conditions around this learner and this classroom should change the ROUTE
to that target* — and never the target itself.

    NODE 1  the academic contract
      |
      v
    NODE 2  ── the Context Reinforcement Plan ──         <- this package
      |
      v
    GENERATION  writes the sheet, from both
      |
      v
    NODE 3  validation

THE CORE INVARIANT, and everything here exists to keep it:

    Node 2 adapts the route to mastery, not the mastery target itself.

WHAT IT ACTUALLY IS. One deterministic context processor and one structured
reasoning call per lesson window — not a second multi-agent system. Four steps:

    profile.py     normalise the room's data, and record how trustworthy each
                   field is. Unknown stays unknown.
    activation.py  decide IN CODE which of the twenty factors have evidence to
                   stand on, and which are relevant to this particular topic.
                   The model does not get to decide what is available.
    reasoning.py   one call, seeing only the active factors and only the fields
                   they may read, returning a compact patch.
    gate.py        every proposed adaptation checked against the room, the fixed
                   target, and each of the failure modes the framework names.

THREE THINGS THAT ARE EASY TO GET WRONG AND ARE DECIDED HERE RATHER THAN IN A
PROMPT:

  * **An empty field is not an invitation.** A model handed a blank
    `community_practices` will fill it with a plausible village. So community,
    cultural and funds-of-knowledge factors are switched OFF unless verified
    input exists, and the fields never reach the prompt at all.
  * **Confidence is not provenance.** A model can be certain about a guess.
    `data_source` describes the DATA and only its origin can change it; the gate
    checks the claim against the profile rather than believing it.
  * **Zero adaptations is a real answer.** Adapting to look responsive is a
    named failure mode, and the correct output for a lesson that already fits
    its room is a statement that it does.

SHADOW MODE IS ON BY DEFAULT. The plan is produced, gated and logged, and
`graph.apply()` returns None — Generation is not given it. Turning that off is a
decision to be made after the evaluation §16 describes: teachers rating whether
the proposals are relevant, feasible, authentic and useful, and a 20–30 topic
golden set compared with and without. `shadow.py` is what records the evidence
for that decision.

Everything Node 2 needs is in this folder:

    graph.run_context(contract=..., context_profile=...)  the node
    graph.apply(state)     the plan Generation may use, or None
    graph.report(state)    the shadow record and §17's metrics
    cli                    python -m context_flow.cli --help
    tests/                 python -m context_flow.tests.test_context_flow
"""

from .factors import FACTORS, BY_ID as FACTORS_BY_ID  # noqa: F401
from .plan import MAX_ADAPTATIONS_PER_TOPIC, TYPES as ADAPTATION_TYPES  # noqa: F401
from .profile import ASSUMED, UNKNOWN, VERIFIED  # noqa: F401

__all__ = [
    "FACTORS", "FACTORS_BY_ID",
    "ADAPTATION_TYPES", "MAX_ADAPTATIONS_PER_TOPIC",
    "VERIFIED", "ASSUMED", "UNKNOWN",
]
