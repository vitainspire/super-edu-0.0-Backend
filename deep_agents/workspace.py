"""The agents' filesystem: what is mounted where, and which mount is authoritative.

    /skills/      the pedagogy library, on disk, READ ONLY
    /memory/      AGENTS.md — how this system works, on disk, READ ONLY
    /workspace/   this run's working artifacts, WRITABLE

THE RULE THIS MODULE EXISTS TO ENFORCE (architecture §19): *the agent workspace
is not the source of truth*. Supabase is. A file under `/workspace/` is a
scratch artifact of one run's reasoning — useful to read back when a chapter
comes out wrong, worthless as a record — and nothing downstream may read it.
What crosses out of an agent is its structured output, which the calling node
puts in graph state and `persist_node` writes to a row.

THREE PERSISTENCE CONCEPTS, kept apart on purpose (§20):

    LangGraph checkpoints  where am I, what completed, what runs next
    /workspace/            what the reasoning produced on the way
    Supabase               what is permanently true

WHY THE DEFAULT IS `StateBackend` AND NOT A DIRECTORY. `/workspace/` living in
graph state means it is checkpointed with everything else, so a chapter that
dies in `curriculum` resumes with its half-written artifacts intact and without
a directory to garbage-collect. It also means this layer is safe under FastAPI,
where a `FilesystemBackend` rooted anywhere useful would hand a model running on
a shared web process read access to the repository and to `.env` — which is the
exact hazard `deepagents` warns about on `FilesystemBackend`. Pass `run_dir` to
opt into on-disk artifacts for a local debugging run; production leaves it None.

`/skills/` and `/memory/` ARE real directories, because they are checked-in
source that a reviewer edits in an editor and a diff shows. They are mounted
read-only through `permissions.py` rather than by the backend, since
`FilesystemBackend` has no read-only mode of its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import BackendProtocol

_HERE = Path(__file__).resolve().parent

SKILLS_ROOT = _HERE / "skills"
MEMORY_ROOT = _HERE / "memory"

# Mount points, as the agent sees them. POSIX, always — `deepagents` routes on
# PurePosixPath and a Windows separator here silently matches nothing.
SKILLS_MOUNT = "/skills/"
MEMORY_MOUNT = "/memory/"
WORKSPACE_MOUNT = "/workspace/"

# What `create_deep_agent(skills=...)` is given. One source today; the tuple
# shape is what a second (a school's own overrides, say) would slot into.
SKILL_SOURCES = [SKILLS_MOUNT]
MEMORY_SOURCES = [MEMORY_MOUNT + "AGENTS.md"]


def build_backend(run_dir: Optional[str | Path] = None) -> BackendProtocol:
    """Mount the three trees.

    `run_dir`, when given, moves `/workspace/` onto real disk under that
    directory — for a local run whose artifacts somebody wants to open. It is
    created if absent. Leave it None anywhere a request is being served.
    """
    routes: dict[str, BackendProtocol] = {
        SKILLS_MOUNT: FilesystemBackend(root_dir=SKILLS_ROOT),
        MEMORY_MOUNT: FilesystemBackend(root_dir=MEMORY_ROOT),
    }
    if run_dir is not None:
        path = Path(run_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        routes[WORKSPACE_MOUNT] = FilesystemBackend(root_dir=path)

    return CompositeBackend(default=StateBackend(), routes=routes)


# ── Where each stage writes ──────────────────────────────────────────────────
#
# Named here rather than spelled into each prompt so that a path appears once.
# The agents are told these in their system prompts; `permissions.py` is what
# actually holds them to it.

def curriculum_artifact(chapter_key: str) -> str:
    return f"{WORKSPACE_MOUNT}curriculum/{chapter_key}.json"


def experience_artifact(chapter_key: str) -> str:
    return f"{WORKSPACE_MOUNT}lesson_design/{chapter_key}.json"


def reinforcement_artifact(run_key: str) -> str:
    return f"{WORKSPACE_MOUNT}reinforcement/{run_key}.json"


def notes_dir(chapter_key: str) -> str:
    """Scratch. The one place an agent may write freely — deliberately not the
    same directory as the artifacts, so "what did it conclude" and "what was it
    thinking" do not have to be told apart by filename."""
    return f"{WORKSPACE_MOUNT}notes/{chapter_key}/"
