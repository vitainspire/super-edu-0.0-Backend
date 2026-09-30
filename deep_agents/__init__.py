"""The Deep Agent layer: bounded reasoning at the three ambiguous seams.

WHERE THIS SITS. `prep_flow`, `context_flow`, `generation` and `validation_flow`
are the pipeline. This package does not replace any of them; it supplies an
alternative implementation of three specific decisions inside them, and the rest
of each node is untouched:

    prep_flow.curriculum      -> CurriculumReasoner   (agents/reasoning.py's call)
    prep_flow.lesson_design   -> LessonDesigner       (agents/experience.py's call)
    context_flow.reasoning    -> ContextReinforcer    (context_flow/reasoning.py's)

Everything around those three — sequencing, move extraction, context assembly,
activity selection, factor activation, the equity gate, generation, validation —
stays exactly as it is, because every one of them is a decision code should make
and the architecture says so.

HOW TO TURN IT ON. Nothing here runs unless asked. `registry.py` builds the
agents, `bridge.py` adapts them to the node signatures the graphs already call,
and the config flag `deep_agents` (default False) is what selects between the
two implementations. A chapter derived either way produces the same state shape
and the same contract, which is what makes the A/B in `eval/` meaningful.
"""
from .model import build_model, labelled
from .tools import RunContext
from .workspace import build_backend

__all__ = ["build_model", "labelled", "RunContext", "build_backend"]
