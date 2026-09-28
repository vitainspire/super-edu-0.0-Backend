"""Structured answers the Checker and Fixer agents must give.

Same shape convention as `deep_agents/schemas.py`'s `RepairEvidence`: every
field is a `Field(description=...)`, because for a `response_format` schema
the description IS the instruction -- it is what the model reads, not
documentation for a human reader who happens to open this file.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SectionKey = Literal["refresher", "concept", "realLife", "challenge", "levelSet", "explore"]


class CheckFinding(BaseModel):
    section: Optional[str] = Field(
        default=None, description="Which section this is about, or null for a whole-sheet issue.")
    severity: str = Field(description="'blocking' or 'advisory'.")
    check: str = Field(default="", description="Which tool/check this came from.")
    message: str = Field(description="What is wrong, specific enough to act on.")


class CheckerVerdict(BaseModel):
    passed: bool = Field(
        description="True ONLY if there is no blocking finding left outstanding after "
                    "everything you decided to check.")
    checks_run: list[str] = Field(
        default_factory=list,
        description="The tool names you actually called, in the order you called them.")
    findings: list[CheckFinding] = Field(
        default_factory=list,
        description="Every finding from every check you ran -- blocking and advisory both.")
    reasoning: str = Field(
        description="1-2 sentences: what you checked, what you deliberately skipped and why, "
                    "and why you judged it done.")


class Bullet(BaseModel):
    text: str = Field(description="The short headline of the point.")
    detail: str = Field(default="", description="The one or two sentences under it.")


class RevisedSection(BaseModel):
    section: SectionKey = Field(description="Which section this revision replaces.")
    points: list[Bullet] = Field(description="The FULL replacement bullet list for this section.")
    activity: Optional[str] = Field(
        default=None, description="challenge ONLY -- the activity name, if you changed it.")
    image_focus: Optional[str] = Field(
        default=None, description="explore ONLY -- the board-sketch phrase, if you changed it.")


class FixerExtra(BaseModel):
    section: SectionKey = Field(description="Which section finishes with time left over.")
    text: str = Field(description="6-12 word headline for the optional stretch task.")
    detail: str = Field(default="", description="1-2 sentences, under 30 words.")


class FixerOutput(BaseModel):
    revisions: list[RevisedSection] = Field(
        default_factory=list,
        description="ONLY the sections you actually rewrote. Leave every section the checker "
                    "did not flag out of this list entirely -- do not rewrite what was not "
                    "broken, and do not include a section unchanged just to be thorough. Use "
                    "this for any structural/voice/grounding/duplication finding -- rewrite "
                    "the section itself.")
    extra: Optional[FixerExtra] = Field(
        default=None,
        description="ONLY for a check_timing finding (a section that genuinely finishes "
                    "early with real minutes unfilled) -- an optional, never-required stretch "
                    "task for that section. This is NEVER a section rewrite: it is a separate "
                    "add-on field, so do not also put this section in `revisions` for the same "
                    "reason. Leave null if no timing finding was reported.")
    investigated: list[str] = Field(
        default_factory=list,
        description="Facts you looked up in the book and used to correct something, if any.")
    notes: str = Field(description="1-2 sentences: what you changed and why.")
