"""Model routing for the extraction pipeline.

Cheap tier for calls whose answer is a word or a short list, standard tier for
real extraction. The property that makes this safe is escalation: a cheap tier
that fails retries on the standard model, so routing can cost money but never
quality.

No network — the HTTP post is replaced throughout.
"""

import pytest

pytest.importorskip("fitz", reason="vision_extraction imports PyMuPDF")

from app.lib import vision_extraction as ve


# ── Fake transport ────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, content="ok"):
        self.status_code = status
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture
def transport(monkeypatch):
    """Record every model actually posted to, and script the replies."""
    posted: list = []

    def fake_post(url, headers=None, json=None, timeout=None):
        model = (json or {}).get("model")
        posted.append(model)
        behaviour = transport.script.get(model, ("ok", f"reply from {model}"))
        kind, payload = behaviour
        if kind == "ok":
            return FakeResponse(200, payload)
        if kind == "http":
            return FakeResponse(payload, "")
        raise RuntimeError(payload)

    monkeypatch.setattr(ve.requests, "post", fake_post)
    monkeypatch.setattr(ve, "_api_key", lambda: "test-key")
    monkeypatch.setattr(ve.time, "sleep", lambda _s: None)
    transport.posted = posted
    transport.script = {}
    return transport


SIMPLE = "google/gemini-2.5-flash-lite"
STANDARD = "google/gemini-2.5-flash"


# ── Tier resolution ──────────────────────────────────────────────────────────

class TestTierResolution:
    def test_standard_tier_uses_the_main_model(self):
        assert ve.model_for_tier("standard") == ve.OPENROUTER_MODEL

    def test_simple_tier_uses_the_cheap_model(self):
        assert ve.model_for_tier("simple") == ve.OPENROUTER_MODEL_SIMPLE

    def test_the_two_tiers_are_different_models(self):
        assert ve.model_for_tier("simple") != ve.model_for_tier("standard")

    def test_an_unknown_tier_falls_back_to_standard(self):
        # Safer to overpay than to silently route somewhere unintended
        assert ve.model_for_tier("nonsense") == ve.OPENROUTER_MODEL

    def test_the_cheap_tier_is_the_same_model_family(self):
        """Same family means same image tiling and, critically, the same
        non-Latin script handling — these are Telugu and Hindi textbooks."""
        assert ve.OPENROUTER_MODEL_SIMPLE.split("/")[0] == ve.OPENROUTER_MODEL.split("/")[0]

    def test_defaults_are_the_expected_models(self, monkeypatch):
        assert ve.OPENROUTER_MODEL == STANDARD
        assert ve.OPENROUTER_MODEL_SIMPLE == SIMPLE


# ── Which model each call gets ───────────────────────────────────────────────

class TestRouting:
    def test_default_tier_is_standard(self, transport):
        ve.call_gemini(["prompt"])
        assert transport.posted == [STANDARD]

    def test_simple_tier_posts_to_the_cheap_model(self, transport):
        ve.call_gemini(["prompt"], tier="simple")
        assert transport.posted == [SIMPLE]

    def test_standard_tier_posts_to_the_main_model(self, transport):
        ve.call_gemini(["prompt"], tier="standard")
        assert transport.posted == [STANDARD]

    def test_returns_the_model_response(self, transport):
        transport.script = {SIMPLE: ("ok", "Telugu")}
        assert ve.call_gemini(["prompt"], tier="simple") == "Telugu"


# ── Escalation: the property that makes routing safe ─────────────────────────

class TestEscalation:
    def test_a_failing_cheap_tier_escalates_to_standard(self, transport):
        transport.script = {
            SIMPLE: ("raise", "cheap model choked"),
            STANDARD: ("ok", "good answer"),
        }
        assert ve.call_gemini(["prompt"], tier="simple", max_retries=1) == "good answer"
        assert transport.posted == [SIMPLE, STANDARD]

    def test_escalation_also_covers_http_errors(self, transport):
        transport.script = {SIMPLE: ("http", 500), STANDARD: ("ok", "recovered")}
        assert ve.call_gemini(["prompt"], tier="simple", max_retries=1) == "recovered"
        assert transport.posted == [SIMPLE, STANDARD]

    def test_standard_tier_has_nowhere_to_escalate_and_raises(self, transport):
        transport.script = {STANDARD: ("raise", "boom")}
        with pytest.raises(Exception, match="boom"):
            ve.call_gemini(["prompt"], tier="standard", max_retries=1)
        assert transport.posted == [STANDARD]

    def test_raises_when_every_tier_fails(self, transport):
        transport.script = {
            SIMPLE: ("raise", "cheap failed"),
            STANDARD: ("raise", "standard failed too"),
        }
        with pytest.raises(Exception, match="standard failed too"):
            ve.call_gemini(["prompt"], tier="simple", max_retries=1)
        assert transport.posted == [SIMPLE, STANDARD]

    def test_no_escalation_when_the_cheap_tier_succeeds(self, transport):
        transport.script = {SIMPLE: ("ok", "fine")}
        ve.call_gemini(["prompt"], tier="simple", max_retries=1)
        assert transport.posted == [SIMPLE]  # standard never touched

    def test_escalation_chain_cannot_loop(self, transport, monkeypatch):
        # A misconfigured escalation map must not spin forever
        monkeypatch.setattr(ve, "_TIER_ESCALATION", {"simple": "standard", "standard": "simple"})
        transport.script = {SIMPLE: ("raise", "a"), STANDARD: ("raise", "b")}
        with pytest.raises(Exception):
            ve.call_gemini(["prompt"], tier="simple", max_retries=1)
        assert len(transport.posted) == 2  # each tier tried once, no cycle


# ── Rate limits are retried, not escalated away ──────────────────────────────

class TestRateLimits:
    def test_a_rate_limited_tier_retries_before_escalating(self, transport):
        """429 means "wait", not "this model can't do it" — burning the escalation
        on a transient limit would push load onto the expensive model."""
        attempts = {"n": 0}

        def flaky_post(url, headers=None, json=None, timeout=None):
            transport.posted.append((json or {}).get("model"))
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("429 rate limit exceeded")
            return FakeResponse(200, "succeeded after waiting")

        transport_post = flaky_post
        import app.lib.vision_extraction as mod
        mod.requests.post = transport_post

        result = ve.call_gemini(["prompt"], tier="simple", max_retries=5, base_delay=0)
        assert result == "succeeded after waiting"
        assert transport.posted == [SIMPLE, SIMPLE, SIMPLE]  # stayed on cheap tier

    def test_exhausted_rate_limit_retries_then_escalates(self, transport):
        transport.script = {
            SIMPLE: ("raise", "429 quota exceeded"),
            STANDARD: ("ok", "escalated"),
        }
        assert ve.call_gemini(["prompt"], tier="simple", max_retries=2, base_delay=0) == "escalated"
        # two attempts on cheap, then one on standard
        assert transport.posted == [SIMPLE, SIMPLE, STANDARD]


# ── The routing decisions themselves ─────────────────────────────────────────

class TestCallSiteRouting:
    """Which pipeline calls are routed cheap. These assertions encode the
    blast-radius reasoning, so flipping one is a deliberate, visible change."""

    @pytest.fixture
    def route_spy(self, monkeypatch):
        seen: list = []

        def fake_call(contents, max_retries=6, base_delay=5,
                      response_format="json", tier="standard"):
            seen.append(tier)
            return spy_reply["value"]

        spy_reply = {"value": "{}"}
        monkeypatch.setattr(ve, "call_gemini", fake_call)
        monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: object())
        monkeypatch.setattr(ve.time, "sleep", lambda _s: None)
        fake_call.seen = seen
        fake_call.reply = spy_reply
        return fake_call

    def test_language_detection_is_cheap(self, route_spy, monkeypatch):
        class Shim:
            @staticmethod
            def open(_p):
                class D:
                    def __len__(self): return 4
                    def close(self): pass
                return D()

        monkeypatch.setattr(ve, "fitz", Shim)
        route_spy.reply["value"] = '{"language": "Telugu"}'
        ve.detect_language_vision("book.pdf")
        assert route_spy.seen == ["simple"]

    def test_cross_chapter_dependency_inference_is_cheap(self, route_spy):
        # Needs >= 4 topics or _infer_cross_chapter_deps returns without calling out
        route_spy.reply["value"] = '{"dependencies": []}'
        ve._infer_cross_chapter_deps({
            "entities": {
                "chapters": [
                    {"id": "C_1", "number": 1, "title": "A"},
                    {"id": "C_2", "number": 2, "title": "B"},
                ],
                "topics": [
                    {"id": f"T_{c}_{i}", "name": f"Topic {c}.{i}",
                     "chapter_id": f"C_{c}", "summary": "summary text"}
                    for c in (1, 2) for i in (1, 2)
                ],
            },
            "graphs": {"concept_dependencies": []},
        })
        assert route_spy.seen == ["simple"]

    def test_cross_dep_inference_is_skipped_entirely_on_small_books(self, route_spy):
        """Fewer than 4 topics and it never calls out — no model, cheap or otherwise."""
        ve._infer_cross_chapter_deps({
            "entities": {
                "chapters": [{"id": "C_1", "number": 1, "title": "A"}],
                "topics": [{"id": "T_1_1", "name": "Alpha", "chapter_id": "C_1", "summary": "s"}],
            },
            "graphs": {"concept_dependencies": []},
        })
        assert route_spy.seen == []

    def test_chapter_extraction_follows_the_configured_tier(self, monkeypatch):
        seen: list = []

        def fake_call(contents, max_retries=6, base_delay=5,
                      response_format="json", tier="standard"):
            seen.append(tier)
            return "## Alpha\n---\npages: 1-2\n---\n\nText.\n"

        monkeypatch.setattr(ve, "call_gemini", fake_call)
        monkeypatch.setattr(ve, "render_page", lambda doc, p, dpi=ve.PAGE_DPI: object())
        monkeypatch.setattr(ve, "EXTRACTION_FORMAT", "markdown")

        class D:
            def __len__(self): return 20

        monkeypatch.setattr(ve, "EXTRACTION_MODEL_TIER", "standard")
        ve.extract_chapter_vision(D(), [1, 2], 1, "A", "English", "list")
        assert seen == ["standard"]

        seen.clear()
        monkeypatch.setattr(ve, "EXTRACTION_MODEL_TIER", "simple")
        ve.extract_chapter_vision(D(), [1, 2], 1, "A", "English", "list")
        assert seen == ["simple"], "EXTRACTION_MODEL_TIER must let you A/B the cost driver"

    def test_chapter_extraction_defaults_to_standard(self):
        """The call whose quality decides whether the extraction is usable at all
        must not default to the cheap model."""
        assert ve.EXTRACTION_MODEL_TIER == "standard"


# ── Cost relationship ────────────────────────────────────────────────────────

def test_the_cheap_tier_is_actually_cheaper():
    """Guards against a rename or config change quietly pointing the 'simple'
    tier at something that costs the same or more.

    Skipped without network — this asserts against live OpenRouter pricing rather
    than numbers hardcoded here, which would rot.
    """
    requests = pytest.importorskip("requests")
    try:
        data = requests.get("https://openrouter.ai/api/v1/models", timeout=20).json()["data"]
    except Exception as exc:
        pytest.skip(f"OpenRouter model list unreachable: {exc}")

    prices = {
        m["id"]: (
            float((m.get("pricing") or {}).get("prompt") or 0),
            float((m.get("pricing") or {}).get("completion") or 0),
        )
        for m in data
    }
    simple = prices.get(ve.OPENROUTER_MODEL_SIMPLE)
    standard = prices.get(ve.OPENROUTER_MODEL)
    if not simple or not standard:
        pytest.skip("configured model not present in the OpenRouter catalogue")

    assert simple[0] < standard[0], "cheap tier input price is not cheaper"
    assert simple[1] < standard[1], "cheap tier output price is not cheaper"
