"""Same shape as `agent_prep_pipeline_writers_room`'s (deleted) TopicSpec --
recreated here rather than imported across packages, on purpose: each
experiment package stands alone as its own comparison arm (see
`agent_prep_pipeline/registry.py`'s note on why `invoke_structured` is
duplicated rather than shared)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TopicSpec:
    index: int
    topic: str
    subtopic: str = ""
    excerpt: str = ""
    is_first: bool = True

    mastery_target: str = ""
    concepts: list = field(default_factory=list)
    competencies: list = field(default_factory=list)
    vocabulary: list = field(default_factory=list)

    anchor: str = ""
    trajectory: str = ""
    floor: str = ""
    gap: str = ""
    gap_closer: str = ""

    page_start: object = None
    page_end: object = None

    activity_name: str = ""

    previous_topic: str = ""
    previous_explore_points: list = field(default_factory=list)
