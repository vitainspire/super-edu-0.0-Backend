"""A third experiment: agents divided by TASK across the whole sheet, not by
SECTION (that was `agent_prep_pipeline_writers_room/`, replaced -- it tested
the wrong split).

TWO STAGES, per topic:

    WRITER               writes all six sections itself, in one autonomous
                          pass -- REPLACES generation/compose.py entirely
    CHECKER / FIXER       REUSED, unchanged, from `agent_prep_pipeline/` --
                          critiques and loops until satisfied

Timing verification and duplication removal are NOT separate agents, even
though that was the first design tried here. They are already tools the
Checker may choose to call (`check_timing`, `check_duplication` in
`agent_prep_pipeline/tools_checks.py`), and the Fixer already knows how to
act on either kind of finding: a section rewrite for duplication, the
`extra` field for a timing suggestion (see
`agent_prep_pipeline/schemas.py`'s `FixerOutput`, extended for this). Adding
separate Timing/Duplication agents on top of that would have been a second,
competing way to do a job the Checker/Fixer pair already does -- so this
package now only has ONE new piece: the Writer.

STATED PLAINLY: THE WRITER STAGE IS THE ONE THING THIS TESTS THAT I ARGUED
AGAINST. Both my own reasoning earlier this session and this codebase's own
`deep_agents/registry.py` docstring say generation should stay
deterministic -- every defect found this session was a compliance problem
(the model had the right information and didn't reliably follow a narrow
rule), not an information-shortage problem more agent autonomy would fix.
This package exists to test that belief for real rather than just assert it
again. If the Writer's output turns out worse or no better than
`generation/compose.py`'s deterministic one, that is a real answer, not a
failure of this pilot.

REUSE, NOT DUPLICATION. `agent_prep_pipeline.orchestrator.run_checker_fixer_loop`
is imported and called directly -- holding the checking/fixing stage
identical to `agent_prep_pipeline/run.py`'s own is what makes a three-way
comparison (fixed pipeline vs. this vs. `generation/compose.py` alone)
isolate exactly one variable: who writes the sections.

SCOPE GAPS, STATED. `figureRefs` left empty, `teachingOrder` kept canonical.
The Writer processes topics strictly in order (Refresher needs the previous
topic's actual Explore output), so this cannot batch topics the way
`generation/compose.py`'s one-call-per-window can.
"""
