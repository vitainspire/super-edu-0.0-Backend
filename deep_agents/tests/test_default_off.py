"""With the flag off, this package must not even be imported.

THE CLAIM `requirements-deep-agents.txt` MAKES is that nothing here is needed to
run the pipeline: leave `deep_agents` False and the three seams make the
structured `call_json` they always made. That claim is only true if the imports
are inside the branches, and an import accidentally hoisted to module scope
would break it in the one way nobody notices locally — where `deepagents` is
installed — and everyone notices in a deployment where it is not.

So this asserts the shape of the source rather than the behaviour. A behavioural
test cannot distinguish "not imported" from "imported and unused" on a machine
that has the package.
"""
import ast
import importlib
from pathlib import Path

import pytest

SEAMS = [
    ("prep_flow.agents.reasoning", "derive_curriculum_reasoning"),
    ("prep_flow.agents.experience", "derive_experience_plans"),
    ("context_flow.reasoning", "derive_context_plan"),
]


def _tree(module: str) -> ast.Module:
    path = Path(importlib.import_module(module).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("module,marker", SEAMS)
def test_the_seam_is_wired(module, marker):
    source = Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
    assert marker in source, f"{module} no longer routes to the bridge"
    assert 'get("deep_agents")' in source, f"{module} no longer checks the flag"


@pytest.mark.parametrize("module,_marker", SEAMS)
def test_the_bridge_import_is_lazy(module, _marker):
    """A module-scope `from deep_agents...` would make deepagents a hard
    dependency of the pipeline."""
    tree = _tree(module)
    for node in tree.body:  # top level only — that is the whole point
        # `ast.Import` carries only `names`; `ast.ImportFrom` also carries the
        # module the names came from, and that is where `deep_agents` would show
        # up in `from deep_agents.bridge import ...`.
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        assert not any(n.startswith("deep_agents") for n in names), \
            f"{module} imports deep_agents at module scope"


@pytest.mark.parametrize("module,marker", SEAMS)
def test_the_original_call_is_still_there(module, marker):
    """The flag selects between two implementations. If the `call_json` branch
    were ever deleted, `deep_agents: False` would silently stop meaning what
    every default in the repo says it means."""
    source = Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
    assert "call_json(" in source, f"{module} lost its non-agent path"


def test_both_default_configs_ship_the_flag_off():
    from context_flow.state import DEFAULT_CONFIG as CTX
    from prep_flow.state import DEFAULT_CONFIG as PREP

    assert PREP["deep_agents"] is False
    assert CTX["deep_agents"] is False


def test_the_workspace_defaults_to_state_not_disk():
    """A FilesystemBackend rooted anywhere useful hands a model on a shared web
    process read access to the repository and to .env."""
    from deepagents.backends import StateBackend

    from deep_agents.workspace import WORKSPACE_MOUNT, build_backend

    backend = build_backend()
    assert isinstance(backend.default, StateBackend)
    assert WORKSPACE_MOUNT not in backend.routes
    # Opt in explicitly and it becomes a real directory.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        assert WORKSPACE_MOUNT in build_backend(tmp).routes
