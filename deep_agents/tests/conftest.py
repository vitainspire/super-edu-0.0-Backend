"""Test fixtures for the deep agent layer.

NO NETWORK IS USED BY ANY TEST IN THIS DIRECTORY. Building an agent constructs a
`ChatOpenAI`, which requires a key to exist but does not call anything, so a
placeholder is enough and is what CI gets. A real key in the environment is left
alone, because a developer running these beside a live experiment should not have
their credentials swapped out from under them.
"""
import os

import pytest

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except ImportError:  # pragma: no cover
    pass

if not os.environ.get("OPENROUTER_API_KEY"):
    os.environ["OPENROUTER_API_KEY"] = "test-key-not-used-no-request-is-made"


@pytest.fixture(autouse=True)
def _no_accidental_calls(monkeypatch):
    """Fail loudly rather than slowly if a test ever starts making a request."""
    def _boom(*args, **kwargs):
        raise AssertionError(
            "a test in deep_agents/tests attempted a real model call; these "
            "tests assert structure and must not need a provider")

    monkeypatch.setattr("langchain_openai.ChatOpenAI._generate", _boom, raising=False)
    monkeypatch.setattr("langchain_openai.ChatOpenAI._agenerate", _boom, raising=False)
