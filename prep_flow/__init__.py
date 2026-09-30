"""NODE 1 of the master orchestration: textbook representation and transformation.

This package reads one chapter of a textbook and derives, without writing a
single line of the lesson, everything that is academically true about it: the
order it teaches in, what each topic requires a child to master, what the
printed page supplies towards that and what it does not, the cognitive path to
it, the period around that path, and the activity that carries it.

It ends in a CONTRACT (`contract.py`) — a structured document stating the fixed
academic destination and everything derived on the way to it. That contract is
the handoff to Node 2 (contextual reinforcement), which decides what conditions
around the learner should change the ROUTE to that destination, and never the
destination.

    NODE 1  what must be mastered, and the best learning experience for it
      ↓     ── the academic contract ──
    NODE 2  what conditions around this learner change the route to it
      ↓     ── the context reinforcement plan ──
    GENERATION   how both of those appear in the final prep material
      ↓
    NODE 3  validation: is the result well made, and does it teach

The ordering requirement inside a chapter is what makes Node 1 a graph rather
than a loop. The six sections of a prep material are not independent, and one of
them — Refresher — is defined entirely by the PREVIOUS topic's Explore. So the
chapter has a spine, and Node 1 fixes it:

    T1.explore ──> T2.refresher      T2.explore ──> T3.refresher   ...

Everything in here exists to serve that spine while keeping 30–40 topics
consistent with each other, and to state the result in one document somebody
else can act on.

Everything Node 1 needs is in this folder — the agents, the graph, the contract,
the HTTP layer and the guards:

    graph.build_chapter_graph()             the node
    graph.build_feedback_graph()            telemetry -> patterns -> adaptive state
    contract.build_chapter_contract(state)  the handoff Node 2 consumes
    routes.router                           the same, over HTTP
    cli / topic_cli / eval_cli / regression_cli
                                            python -m prep_flow.cli --help
    tests/                                  python -m prep_flow.tests.test_discovered_failures
"""

from .sections import SECTION_ORDER, SECTION_POLICY, SECTION_LABELS  # noqa: F401

__all__ = ["SECTION_ORDER", "SECTION_POLICY", "SECTION_LABELS"]
