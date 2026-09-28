"""A separate, additive experiment: what changes if the CHECKING and FIXING
stages of prep-material generation are genuine autonomous agents (they decide
which checks to run and how to fix what they find) instead of the fixed
Node 3 LangGraph in `validation_flow/`.

NOTHING IN `prep_flow/`, `generation/`, `validation_flow/`, or `app/lib/` is
edited or imported in a way that changes its behaviour. This package only
READS those modules' functions as tools. The live pipeline is untouched and
this can be deleted with zero effect on it.

WHAT STAYS DETERMINISTIC, ON PURPOSE. `deep_agents/registry.py` (the existing
agent framework this package reuses) already reasoned through and rejected
giving GENERATION itself autonomy: "by the time generation runs, every
judgement has been made... a constrained structured call is the right shape
for that." This experiment agrees and does not touch that boundary --
generation stays exactly the deterministic composer it already is. The
autonomy under test here is scoped to the checking/fixing side, which has a
real precedent already in this codebase: `deep_agents`'s Agent 4
(RepairInvestigator) is a genuine tool-using agent that investigates a
failed sheet before it is rewritten, rather than a fixed check. This package
generalises that pattern across all of Node 3's checks, and adds a second
agent that can actually rewrite, not just investigate.

SCOPE OF v1, STATED RATHER THAN LEFT IMPLICIT. Not every check the live
Node 3 graph runs is wrapped as a tool here -- see `tools_checks.py` for the
list. Missing: check_continuity, check_timing_plan, check_vocabulary_order,
check_experience_alignment, and full learner-simulation (`learner.py`'s
`_evaluate` needs a whole-chapter `ChapterState`, not a single topic, so it
does not fit this per-topic agent loop without more plumbing than a first
pilot earns). A real comparison against the live pipeline should read that
gap as a real limitation of this run, not a hidden one.

See `run.py` for the CLI driver and `orchestrator.py` for the actual
checker/fixer loop.
"""
