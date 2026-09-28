"""The control plane around the agent loop - architecture section 10.

`deepagents` already supplies most of the middleware stack the architecture
names: filesystem, skills, memory, subagents, summarization, prompt caching,
human-in-the-loop. This module adds the two things it cannot know about, both of
which exist to keep an agent loop legible to the rest of THIS pipeline.

WHY PROVENANCE NEEDED ITS OWN MIDDLEWARE. `prep_flow/trace.py` answers "which
stage read what" by reading a contextvar that `prep_flow/llm.py` sets around each
`call_json`. One label, one completion, one row in the trace. An agent does not
work that way: one invocation is N model calls with tool turns between them, and
attributing the whole loop to a single label would report "curriculum reasoning
made 1 call" for a stage that made nine.

So `ProvenanceMiddleware` counts what actually happened - model calls, tool
calls by name, tokens where the provider reports them - and hands the caller a
record it can fold into the same trace. The trace's vocabulary does not change;
what changes is that an agent's row is now honest about its own shape.

WHY A STEP BUDGET. `model.py` gives up `ai.py`'s circuit breaker, which was the
thing that stopped a failing provider being asked forever. `StepBudget` is the
replacement and it is deliberately cruder: a hard ceiling on model calls per
invocation, after which the agent is told to answer with what it has. An agent
that has read fourteen pages and called nine tools is not going to be rescued by
a fifteenth; it is looping, and the architecture's rule is that a bounded partial
answer routes to persist rather than an unbounded retry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware


@dataclass
class AgentRun:
    """What one agent invocation actually did. Folded into the run trace."""

    label: str
    model_calls: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    # WHICH SKILLS THIS AGENT ACTUALLY OPENED, in the order it opened them.
    # Counted separately from `tool_calls` because `read_file x4` answers a
    # different question from the one this layer is built around: the whole
    # design is progressive disclosure, so "did it load the pedagogy library"
    # is the property worth reporting and a tool tally cannot express it.
    #
    # It is also the only way to see the failure that has no other symptom.
    # A model that never takes a second turn loads nothing, reasons from
    # general knowledge, and returns output shaped exactly like output that
    # used the library - observed on llama-3.3-70b, where all three agents
    # made fifteen tool calls between them and opened zero skills.
    skills_loaded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "modelCalls": self.model_calls,
            "toolCalls": dict(sorted(self.tool_calls.items())),
            "toolCallTotal": sum(self.tool_calls.values()),
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "elapsedMs": int(((self.finished_at or time.time()) - self.started_at) * 1000),
            # True when the step budget cut the loop short. A reader of the trace
            # needs to tell "answered in four calls" from "was stopped at twelve",
            # because only the second one casts doubt on the answer.
            "truncated": self.truncated,
            "skillsLoaded": list(self.skills_loaded),
            "errors": self.errors[:5],
        }


class ProvenanceMiddleware(AgentMiddleware):
    """Count the loop, so an agent's row in the trace describes an agent.

    Reads nothing and changes nothing. Every hook passes the request straight
    through; the only side effect is on the `AgentRun` the caller holds.
    """

    name = "VitaInspireProvenance"

    def __init__(self, record: AgentRun) -> None:
        super().__init__()
        self.record = record

    def wrap_model_call(self, request, handler):
        self.record.model_calls += 1
        response = handler(request)
        self._count_tokens(response)
        return response

    async def awrap_model_call(self, request, handler):
        self.record.model_calls += 1
        response = await handler(request)
        self._count_tokens(response)
        return response

    def wrap_tool_call(self, request, handler):
        self._count_tool(request)
        return handler(request)

    async def awrap_tool_call(self, request, handler):
        self._count_tool(request)
        return await handler(request)

    def after_agent(self, state, runtime):
        self.record.finished_at = time.time()
        return None

    async def aafter_agent(self, state, runtime):
        self.record.finished_at = time.time()
        return None

    # ── internals ────────────────────────────────────────────────────────────

    def _count_tool(self, request: Any) -> None:
        name = "(unknown)"
        call = getattr(request, "tool_call", None)
        if isinstance(call, dict):
            name = call.get("name") or name
        else:
            call, name = None, getattr(request, "name", None) or name
        self.record.tool_calls[name] = self.record.tool_calls.get(name, 0) + 1
        if name == "read_file":
            self._note_skill(call)

    def _note_skill(self, call: Any) -> None:
        """Record a skill by NAME when a read_file is one.

        Matched on the path shape rather than on a mount constant so that a
        run whose skills are mounted somewhere else still reports them, and
        so an ordinary read of a workspace artifact is not miscounted as a
        skill load.
        """
        path = str(((call or {}).get("args") or {}).get("file_path") or "")
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if len(parts) >= 2 and parts[-1].upper() == "SKILL.MD":
            skill = parts[-2]
            if skill not in self.record.skills_loaded:
                self.record.skills_loaded.append(skill)

    def _count_tokens(self, response: Any) -> None:
        """Best effort. Not every provider reports usage, and a missing count
        must not cost the run - a trace with zero tokens and nine calls is still
        a useful trace."""
        try:
            messages = getattr(response, "result", None) or []
            for message in messages:
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                self.record.prompt_tokens += int(usage.get("input_tokens") or 0)
                self.record.completion_tokens += int(usage.get("output_tokens") or 0)
        except Exception:  # pragma: no cover - accounting must never raise
            pass


class StepBudget(AgentMiddleware):
    """A hard ceiling on model calls per invocation.

    When the ceiling is reached the agent is told, in a system message, to answer
    with what it already has. That is deliberately different from raising: a
    curriculum agent that has read most of a chapter has a usable partial answer,
    and the pipeline's rule is that a partial answer with a note routes to
    persist while an exception routes nowhere.
    """

    name = "VitaInspireStepBudget"

    def __init__(self, record: AgentRun, *, max_model_calls: int = 14) -> None:
        super().__init__()
        self.record = record
        self.max_model_calls = max_model_calls

    def _exhausted(self) -> bool:
        return self.record.model_calls >= self.max_model_calls

    def _notice(self) -> dict:
        self.record.truncated = True
        return {
            "messages": [{
                "role": "system",
                "content": (
                    f"You have used your budget of {self.max_model_calls} model "
                    "steps for this task. Stop calling tools and produce your "
                    "final structured answer now, using what you already have. "
                    "Where something is genuinely underdetermined, leave the "
                    "field empty rather than guessing - an empty field is a "
                    "usable signal downstream and a guess is not."),
            }]
        }

    def before_model(self, state, runtime):
        return self._notice() if self._exhausted() else None

    async def abefore_model(self, state, runtime):
        return self._notice() if self._exhausted() else None


def build_middleware(record: AgentRun, *, max_model_calls: int = 14
                     ) -> list[AgentMiddleware]:
    """The stack every VitaInspire agent gets, in order.

    Provenance first so it counts the step the budget is about to refuse -
    a refused step is a step that happened and a trace that hides it under-reports
    exactly the runs a reviewer is looking for.
    """
    return [ProvenanceMiddleware(record), StepBudget(record, max_model_calls=max_model_calls)]


# `Callable` is imported for the hook signatures above and is referenced only in
# annotations; naming it here keeps linters from stripping the import.
_HANDLER = Callable[[Any], Any]
