# Pedagogy skill set - goal coverage

Eighteen skills. Seven are new; eleven are the originals with the five learning
goals wired into them.

## The five goals

    G1  a below-grade child grasps the concept and what it is for
    G2  the child engages actively, through the world in front of them
    G3  the child reaches a passing grade on class time alone
    G4  the child can keep exploring after the bell, alone and for free
    G5  the capable child is genuinely stretched

Stated in full, with the self-critique gate, in `learning-goals/SKILL.md`.

## Coverage map

    goal  owned by                    enforced in
    G1    differentiation             experience-design (floor mark),
          language-support            activity-design (procedure floor),
                                      curriculum-reasoning (prereq repair),
                                      core-pedagogy (floor ≠ lower standard)
    G2    real-world-connection       lesson-design (Real Life duty),
          engagement                  activity-design (staging test #5),
                                      contextual-reinforcement (tier 1 needs
                                      no provenance)
    G3    assessment-alignment        lesson-design (exercise ledger, closure),
                                      grounding (printed questions),
                                      misconception-analysis (closure line),
                                      language-support (exam term)
    G4    self-study                  lesson-design (Explore seam, not
                                      load-bearing), curriculum-reasoning
                                      (home-available anchors),
                                      early-math / literacy (prompt sets)
    G5    stretch-design              differentiation (open top),
                                      activity-design (zero-cost extension),
                                      core-pedagogy (invited vs required
                                      demand)

## Wiring requirement

`learning-goals` and `core-pedagogy` should be in **every** stage's prompt.
The other fifteen load by stage as before. Every goal-carrying rule lives in a
skill some stage already loads, but the gate itself only runs if
`learning-goals` is present - a stage that loads only its own skill will
produce a locally correct artifact and miss the outcomes.

## New files

    learning-goals/         the five goals, the minute arithmetic, the gate
    differentiation/        the floor, step-scoring, the open top
    real-world-connection/  three tiers of evidence; mention vs application
    engagement/            participation floor, private response, attention arc
    assessment-alignment/   printed questions, exercise ledger, notebook line
    self-study/             the take-home question and its five conditions
    stretch-design/         invited vs required demand, the six moves

## Edited files

    core-pedagogy           + what every period owes; floor ≠ lowered standard;
                              home study cannot be assumed; invited demand
    curriculum-reasoning    + 60-second prerequisite repair; home-available
                              anchors; floor is not a weaker `gained`
    experience-design       + three marks on the trajectory (jump/floor/
                              stretch); third standard; participation read
    lesson-design           + goal duties per section; closure and take-home
                              minutes; exercise ledger; worked 22-min division
    activity-design         + staging test #5 (participation); named floor in
                              procedures; home-doable variant; device constancy
    contextual-reinforcement + the room is not a local claim; check 8 scope
    language-support        + the notebook term is the exam term; spoken
                              take-home
    misconception-analysis  + private-response probes; the closure line
    grounding               + the printed questions are part of the chapter
    early-math              + application list, take-home set, stretch set,
                              floor at *arrange*
    literacy                + oral floor, take-home set, stretch set, printed
                              question forms

## Two new fields the pipeline would benefit from

Both are content, not schema, and both work written into existing sections
today. A field each makes them checkable in code:

    floor            per topic - one demonstrable sentence, child's voice
    exercisesCovered per period - page + question numbers met in class

## What a validator can check without a model

    - a floor exists per topic, and is not a copy of `gained`
    - a stretch sentence exists and names a noun from the anchor pool
    - the take-home names no material outside the resource profile
    - a closure line exists and contains a term from the vocabulary ledger
    - exercise coverage across the chapter has no gaps (or gaps are reported)
    - station count x dwell time fits Challenge's minute share
    - the section minutes sum to ~22
    - no take-home phrasing containing "parents", "ask your", "print", "phone"

Judgment calls that still need a model or a human: whether the floor is on the
same road as the target, whether the application is an application, whether the
stretch is open-ended, whether a prompt is boilerplate.
