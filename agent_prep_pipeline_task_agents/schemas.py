"""The Writer's structured output. `Bullet` is imported from
`agent_prep_pipeline.schemas` rather than redefined -- one bullet shape for
every agent in both packages.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from prep_flow.sections import BULLETS_PER_SECTION

from agent_prep_pipeline.schemas import Bullet  # noqa: F401 (re-exported)


class SectionWatch(BaseModel):
    section: str = Field(
        description="refresher | concept | realLife | challenge | levelSet | explore")
    text: str
    detail: str = ""


class WriterOutput(BaseModel):
    """The whole six-section sheet, written by ONE agent in one pass."""

    objective: str = Field(
        description="ONE verb-first headline, 4-8 words. Good: 'Count and compare small "
                    "groups of objects'. Bad: 'Students will be able to count objects.'")
    planning_note: str = Field(
        description="1-2 sentences, your own reasoning, written first: what this period "
                    "bridges from and what it sets up.")
    refresher_points: list[Bullet] = Field(
        default_factory=list,
        description=f"EXACTLY {BULLETS_PER_SECTION} if a PREVIOUS TOPIC'S EXPLORE was "
                    "given to you -- built from it and nothing else. Empty list ONLY if "
                    "this is the first topic of the chapter.")
    concept_points: list[Bullet] = Field(
        description=f"EXACTLY {BULLETS_PER_SECTION}, grounded in the textbook excerpt, "
                    "with (Page N) citations.")
    real_life_points: list[Bullet] = Field(description=f"EXACTLY {BULLETS_PER_SECTION}.")
    activity_name: str = Field(
        description="Challenge's activity name -- copy character-for-character from the "
                    "SELECTED ACTIVITY given, or the book's own task if the pages set one.")
    challenge_points: list[Bullet] = Field(
        description=f"EXACTLY {BULLETS_PER_SECTION}, staged PLAY -> REFLECT -> ACT.")
    level_set_points: list[Bullet] = Field(description=f"EXACTLY {BULLETS_PER_SECTION}.")
    explore_points: list[Bullet] = Field(description=f"EXACTLY {BULLETS_PER_SECTION}.")
    image_focus: str = Field(
        description="One short phrase: the single most useful thing to sketch on the "
                    "board for Explore -- must be an object your own explore_points "
                    "actually mention.")
    materials_used: list[str] = Field(
        default_factory=list, description="Every real object you actually referenced.")
    pages_cited: list[int] = Field(default_factory=list)
    watch: list[SectionWatch] = Field(default_factory=list)
    minutes: dict[str, int] = Field(
        description="All six keys: refresher, concept, realLife, challenge, levelSet, "
                    "explore. refresher may be 0 if this is the first topic.")
