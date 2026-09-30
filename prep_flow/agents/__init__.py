"""The six agents of Node 1, plus the three of the feedback loop.

Each module exports one async `*_node(state) -> dict` function. Nodes return
only the keys they changed — the reducers in state.py merge them.

Node 1 (textbook representation and transformation), in the order they run:

    sequencing        the chapter's ordered spine, cut on the book's own boundaries
    move_extraction   the order the BOOK explains each topic in
    context_assembly  concepts, competencies, vocabulary, canonical ids, activities
    reasoning         the knowledge chain, and the mastery audit beside it
    experience        the cognitive path, and the object that carries it
    planning          the minutes, the emphasis, and the seam to the next period
    activity_selection  one Challenge activity per topic, varied across the chapter

`stages.py` runs them as two graph nodes; `sequencing` is its own.

THREE AGENTS ARE NOT HERE ANY MORE. `generation.py`, `validation.py` and
`simulation.py` moved to `_deferred/` when the master orchestration was split
into nodes: generation belongs downstream of Node 2, and validation and the
learner gate belong to Node 3. `_deferred/README.md` says which is which.
"""
