"""One place that knows where the existing modules live.

These files were written for the real backend tree (`app/lib/ai.py`,
`app/routes/...`) and use relative imports to prove it. Rather than sprinkle
try/except around every import site — which is how two layouts silently drift
apart — every borrowed symbol is resolved exactly once, here, and the rest of
this package imports it from `.deps`.

THIS CHECKOUT NOW HAS A REAL `app/lib/`, which is the layout `_load` below
already preferred: it tries `app.lib.<name>` first and only then the flat name.
The consequence is that `_install_flat_shim` returns immediately — it bails when
`app/lib` is a directory — so the fake package it used to register at import
time no longer exists. That removes a real hazard rather than just tidying one:
the shim made `ai` importable under TWO names, and a module reached by the wrong
one got separate globals, a separate circuit breaker and a separate contextvar
(see the note on `ai_module` below, and `prep_flow/llm.py`).

The shim is kept because the flat layout is still legal — drop `app/` and
everything here resolves the old way — and because deleting the fallback would
make this file assume a tree it is the only defence against.

Nothing in this package imports the old modules directly. If that rule holds,
moving this package into the real tree (where it lives at `app/lib/prep_flow/`
— see `app/main.py`) is a change to this file only.
"""
import importlib
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

# A flat checkout puts ai.py / prep_material_generator.py in the repo root,
# which is this file's grandparent.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load(name: str) -> ModuleType:
    """Import `name` from wherever it happens to live in this deployment.

    Tries the packaged path first (`app.lib.ai`) so a real backend never picks
    up a stale flat copy sitting next to it, then the flat one.
    """
    last: Optional[Exception] = None
    for candidate in (f"app.lib.{name}", f"app.routes.{name}", name):
        try:
            return importlib.import_module(candidate)
        except ImportError as exc:
            last = exc
    raise ImportError(
        f"prep_flow could not import '{name}' as app.lib.{name} or {name}. "
        f"Checked sys.path[0]={sys.path[0]!r}. Last error: {last}"
    )


# ── The flat checkout's own problem ──────────────────────────────────────────
# prep_material_generator.py opens with `from .ai import call_ai`, which cannot
# resolve when the file is a top-level module. Registering a one-module package
# that aliases the root makes the relative import legal without editing the
# original file, which is the point — this package must not require changes to
# the pilot it sits beside.
def _install_flat_shim() -> None:
    if "app.lib" in sys.modules or (_ROOT / "app" / "lib").is_dir():
        return
    if not (_ROOT / "ai.py").exists():
        return

    app_pkg = sys.modules.setdefault("app", ModuleType("app"))
    app_pkg.__path__ = []  # namespace-ish: enough for importlib to walk into it
    lib_pkg = ModuleType("app.lib")
    lib_pkg.__path__ = [str(_ROOT)]
    sys.modules["app.lib"] = lib_pkg
    app_pkg.lib = lib_pkg


_install_flat_shim()

_ai = _load("ai")
_generator = _load("prep_material_generator")
_pedagogy = _load("pedagogy_library")
_canonical = _load("canonical_mapping")
_grounding = _load("textbook_grounding")
_prep_context = _load("prep_context")
_fragments = _load("prompt_fragments")
_subject_prompts = _load("subject_prompts")
_supabase = _load("supabase_clients")

# ── Re-exports. The full set of things this package borrows. ─────────────────
# The MODULE, not just the function. `_load` resolves ai.py as `app.lib.ai`
# whenever the flat shim above is installed, which makes it a genuinely
# different module object from a plain `import ai` — separate globals, separate
# contextvars, separate circuit-breaker counters. Anything that has to share
# module-level state with the call_ai below (prep_flow/llm.py setting the call
# label ai.py reads back) must reach it through here, or it will set a variable
# in one copy and read it in the other.
ai_module = _ai
call_ai = _ai.call_ai

extract_knowledge_from_text = _generator.extract_knowledge_from_text
select_activity_and_context = _generator.select_activity_and_context
grade_band_for = _generator.grade_band_for
validate_material = _generator.validate_material
validate_visuals = _generator.validate_visuals
render_prep_material_markdown = _generator.render_prep_material_markdown
parse_json_response = _generator._parse_json_response
strip_fences = _generator._strip_fences
as_section_dict = _generator._as_section_dict
section_bullets = _generator._section_bullets
VALID_BLOOM_LEVELS = _generator.VALID_BLOOM_LEVELS
VALID_DIFFICULTIES = _generator.VALID_DIFFICULTIES

get_recommended_activities = _pedagogy.get_recommended_activities
resolve_canonical = _canonical.resolve_canonical

fetch_textbook_grounding = _grounding.fetch_textbook_grounding
textbook_prompt_block = _grounding.textbook_prompt_block
best_match = _grounding.best_match
title_score = _grounding.title_score
title_tokens = _grounding.title_tokens

gather_class_context = _prep_context.gather_class_context
fetch_feedback_context = _prep_context.fetch_feedback_context
personalization_tier_line = _prep_context.personalization_tier_line

engagement_level = _fragments.engagement_level
engagement_level_guidance = _fragments.engagement_level_guidance

# The MODULE, like `ai_module` above, because the caller wants two attributes off
# it (`resolve_subject_module` and the SubjectModule dataclass's fields) rather
# than one function. `deep_agents/tools/pedagogy.py` is the only reader.
subject_prompts_module = _subject_prompts
resolve_subject_module = _subject_prompts.resolve_subject_module

create_admin_client = _supabase.create_admin_client


# ── Environment ──────────────────────────────────────────────────────────────

def database_url() -> Optional[str]:
    """Supabase's DIRECT Postgres connection string, for the LangGraph
    checkpointer only.

    Everything this package stores goes through the Supabase client
    (`create_admin_client`) like the rest of the backend. This is the one
    exception: `AsyncPostgresSaver` speaks the Postgres wire protocol, not
    PostgREST, so checkpointing needs the real connection string — found in the
    Supabase dashboard under Settings -> Database (either the direct connection
    or the pooler; both work).

    Absent, the graph falls back to in-memory checkpoints: runs still complete
    and still persist, they just cannot be resumed. Several names are accepted
    because Supabase projects surface it under different ones depending on how
    they were provisioned, and making the operator guess which one this code
    wants is a pointless failure.
    """
    for key in ("PREP_FLOW_DATABASE_URL", "SUPABASE_DB_URL", "DATABASE_URL", "POSTGRES_URL"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
